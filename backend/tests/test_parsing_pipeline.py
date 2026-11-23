"""T10: parse runs, checkpoints, coverage, active publish, forced OCR, old refs."""
from __future__ import annotations

import dataclasses
import uuid

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas
from sqlalchemy import select

from legal_workbench import models, parsing, worker
from legal_workbench.config import load_settings
from legal_workbench.db import get_engine, get_session_factory
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.parsing import pipeline
from legal_workbench.storage import Storage

from . import factories

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


def make_pdf(path, pages: int, font_size: int = 12, scanned_pages: set[int] | None = None) -> bytes:
    scanned_pages = scanned_pages or set()
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for page in range(1, pages + 1):
        if page in scanned_pages:
            from PIL import Image, ImageDraw, ImageFont
            from reportlab.lib.utils import ImageReader
            image = Image.new("RGB", (1240, 500), "white")
            draw = ImageDraw.Draw(image)
            draw.text((40, 60), f"扫描页 第{page}页 合同金额100.10元",
                      font=ImageFont.truetype("/System/Library/Fonts/Hiragino Sans GB.ttc", 56),
                      fill="black")
            pdf.drawImage(ImageReader(image), 0, 0, width=A4[0], height=A4[1] * 0.5)
        else:
            pdf.setFont("STSong-Light", font_size)
            pdf.drawString(40, 700, f"第{page}页 TEST-ONLY 合同正文 金额100.10元")
            pdf.drawString(40, 660, "甲方与乙方约定于2024年1月1日履行合同义务。")
        pdf.showPage()
    pdf.save()
    return path.read_bytes()


def build(db, store, content: bytes, name: str) -> dict:
    return factories.build_material_with_storage(db, store, content=content, name=name)


def run_parse(db, store, built, pages=None):
    run_id, job_id = parsing.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"],
        force_ocr_pages=pages,
    )
    status = pipeline.process_parse_job(get_session_factory(), store, job_id)
    return run_id, job_id, status


def units_of(db, run_id):
    return db.execute(select(models.ParseUnit).where(models.ParseUnit.parse_run_id == run_id)
                      .order_by(models.ParseUnit.unit_key)).scalars().all()


def segments_of(db, run_id):
    return db.execute(select(models.Segment).where(models.Segment.parse_run_id == run_id)
                      .order_by(models.Segment.id)).scalars().all()


def test_txt_run_complete_and_active(db, store, tmp_path):
    built = build(db, store, "第一段内容\n\n第二段内容".encode(), "a.txt")
    run_id, job_id, status = run_parse(db, store, built)
    assert status == "complete"
    units = units_of(db, run_id)
    assert len(units) == 2 and all(u.status == "extracted" for u in units)
    segments = segments_of(db, run_id)
    assert len(segments) == 2
    units_by_id = {u.id: u.unit_key for u in units}
    texts = {units_by_id[s.parse_unit_id]: s.normalized_text for s in segments}
    assert texts == {"text:0": "第一段内容", "text:1": "第二段内容"}
    assert all(s.offset_map for s in segments)
    version = db.get(models.FileVersion, built["file_version_id"])
    assert version.active_parse_run_id == run_id
    job = db.get(models.Job, job_id)
    assert job.status == "succeeded" and job.completed_units == job.total_units == 2


def test_interrupted_pdf_resumes_without_redoing_pages(db, store, tmp_path):
    path = tmp_path / "five.pdf"
    content = make_pdf(path, 5)
    built = build(db, store, content, "five.pdf")
    run_id, job_id = parsing.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    calls = {"n": 0}

    def fail_third(stage):
        if stage.startswith("unit:"):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("simulated kill at page 3")

    with pytest.raises(RuntimeError):
        pipeline.process_parse_job(get_session_factory(), store, job_id, fault_inject=fail_third)
    assert db.get(models.ParseRun, run_id).status == "running"
    first_segments = {s.parse_unit_id: s.id for s in segments_of(db, run_id)}
    assert len(first_segments) == 3
    status = pipeline.process_parse_job(get_session_factory(), store, job_id)
    assert status == "complete"
    units = units_of(db, run_id)
    assert len(units) == 5
    segments = segments_of(db, run_id)
    assert len(segments) == 5
    for segment in segments:
        if segment.parse_unit_id in first_segments:
            assert segment.id == first_segments[segment.parse_unit_id]


def test_failed_later_run_does_not_replace_valid_version(db, store, tmp_path, monkeypatch):
    path = tmp_path / "a.pdf"
    built = build(db, store, make_pdf(path, 2), "a.pdf")
    run_id, _job, status = run_parse(db, store, built)
    assert status == "complete"
    version = db.get(models.FileVersion, built["file_version_id"])
    assert version.active_parse_run_id == run_id

    def boom(path, settings, force):
        raise WorkbenchError(ErrorCode.PARSE_FAILED, "simulated failure")

    monkeypatch.setattr(pipeline, "_iter_units", boom)
    second_run, second_job = parsing.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    assert pipeline.process_parse_job(get_session_factory(), store, second_job) == "failed"
    db.expire_all()
    assert db.get(models.FileVersion, built["file_version_id"]).active_parse_run_id == run_id
    assert db.get(models.ParseRun, second_run).status == "failed"


def test_older_run_finishing_late_does_not_regress(db, store, tmp_path):
    path = tmp_path / "two.pdf"
    built = build(db, store, make_pdf(path, 2), "two.pdf")
    run1, job1 = parsing.create_parse_run(db, case_id=built["case_id"],
                                          file_version_id=built["file_version_id"])
    run2, job2 = parsing.create_parse_run(db, case_id=built["case_id"],
                                          file_version_id=built["file_version_id"])
    assert pipeline.process_parse_job(get_session_factory(), store, job2) == "complete"
    db.expire_all()
    assert db.get(models.FileVersion, built["file_version_id"]).active_parse_run_id == run2
    assert pipeline.process_parse_job(get_session_factory(), store, job1) == "complete"
    db.expire_all()
    assert db.get(models.FileVersion, built["file_version_id"]).active_parse_run_id == run2


def test_forced_ocr_copies_other_pages_and_keeps_old_refs(db, store, tmp_path):
    path = tmp_path / "forced.pdf"
    built = build(db, store, make_pdf(path, 2, font_size=24), "forced.pdf")
    run1, _job1, status = run_parse(db, store, built)
    assert status == "complete"
    units1 = {u.id: u.unit_key for u in units_of(db, run1)}
    old_segments = {units1[s.parse_unit_id]: s for s in segments_of(db, run1)}
    old_page2 = old_segments["pdf:2"]
    old_text = old_page2.text
    run2, job2 = parsing.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"],
        force_ocr_pages=[1])
    assert pipeline.process_parse_job(get_session_factory(), store, job2) == "complete"
    units = {u.unit_key: u for u in units_of(db, run2)}
    assert units["pdf:1"].method == "ocr" and units["pdf:1"].status == "ocr_done"
    assert units["pdf:2"].method == "native"
    new_page2 = db.execute(select(models.Segment).where(
        models.Segment.parse_run_id == run2,
        models.Segment.parse_unit_id == units["pdf:2"].id)).scalar_one()
    assert new_page2.text == old_text and new_page2.id != old_page2.id
    old = db.get(models.Segment, old_page2.id)
    assert old.text == old_text
    db.expire_all()
    assert db.get(models.FileVersion, built["file_version_id"]).active_parse_run_id == run2


def test_first_partial_run_is_searchable_with_coverage(db, store, tmp_path, monkeypatch):
    path = tmp_path / "scanned.pdf"
    content = make_pdf(path, 3, scanned_pages={2})
    built = build(db, store, content, "scanned.pdf")
    settings = dataclasses.replace(load_settings(), max_image_pixels=1000)
    monkeypatch.setattr(pipeline, "load_settings", lambda: settings)
    run_id, _job, status = run_parse(db, store, built)
    assert status == "partial"
    db.expire_all()
    assert db.get(models.FileVersion, built["file_version_id"]).active_parse_run_id == run_id
    coverage = db.get(models.ParseRun, run_id).coverage
    assert coverage["failed"] == 1 and coverage["complete"] == 2
    failed = [u for u in units_of(db, run_id) if u.status == "failed"]
    assert failed and failed[0].error.startswith("max_image_pixels")


def test_missing_models_marks_run_failed(db, store, tmp_path, monkeypatch):
    path = tmp_path / "scan.pdf"
    built = build(db, store, make_pdf(path, 1, scanned_pages={1}), "scan.pdf")
    settings = dataclasses.replace(load_settings(), ocr_model_dir=tmp_path / "missing")
    monkeypatch.setattr(pipeline, "load_settings", lambda: settings)
    run_id, job_id, status = run_parse(db, store, built)
    assert status == "failed"
    run = db.get(models.ParseRun, run_id)
    assert run.status == "failed"
    assert "dependency_missing" in db.get(models.Job, job_id).error


def test_unsupported_format_run_and_no_active(db, store):
    built = build(db, store, b"unknown bytes", "a.xyz")
    run_id, job_id, status = run_parse(db, store, built)
    assert status == "failed"
    assert db.get(models.ParseRun, run_id).status == "unsupported"
    db.expire_all()
    assert db.get(models.FileVersion, built["file_version_id"]).active_parse_run_id is None


def test_force_pages_rejected_for_non_pdf(db, store):
    built = build(db, store, b"text", "a.txt")
    with pytest.raises(WorkbenchError) as exc:
        parsing.create_parse_run(db, case_id=built["case_id"],
                                 file_version_id=built["file_version_id"],
                                 force_ocr_pages=[1])
    assert exc.value.code == ErrorCode.INVALID_INPUT


def test_worker_processes_parse_job(db, store, tmp_path):
    built = build(db, store, make_pdf(tmp_path / "w.pdf", 2), "w.pdf")
    run_id, job_id = parsing.create_parse_run(db, case_id=built["case_id"],
                                              file_version_id=built["file_version_id"])
    assert worker.run_worker(get_engine(), store, once=True) == 1
    db.expire_all()
    assert db.get(models.ParseRun, run_id).status == "complete"
    assert db.get(models.Job, job_id).status == "succeeded"
