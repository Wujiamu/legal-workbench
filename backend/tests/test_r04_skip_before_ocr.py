"""R04: skipping already-parsed pages must happen before render/OCR.

The iterator used to parse every page and hand the unit back, after which the
caller discarded it if the unit key was already done.  Database rows were not
duplicated, but the OCR work was — so a resumed long scan redid finished pages,
and a missing model could abort a recovery that should have succeeded.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from sqlalchemy import select

from legal_workbench import models, parsing
from legal_workbench.db import get_session_factory
from legal_workbench.parsing import ocr as ocr_module
from legal_workbench.parsing import pipeline
from legal_workbench.storage import Storage

from . import factories

FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


@pytest.fixture
def ocr_calls(monkeypatch):
    """Count real OCR invocations without replacing the engine itself."""
    counter = {"n": 0}
    original = ocr_module.OcrEngine.recognize

    def counting(self, image, timeout=None):
        counter["n"] += 1
        return original(self, image, timeout=timeout)

    monkeypatch.setattr(ocr_module.OcrEngine, "recognize", counting)
    return counter


def scanned_pdf(path, pages: int, first_page_height: int = 400) -> bytes:
    """A PDF whose pages carry only an image, so every page needs OCR.

    Page 1 can be given a distinct height so a test can make the OCR engine fail
    for that page specifically.
    """
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for page in range(1, pages + 1):
        height = first_page_height if page == 1 else 400
        image = Image.new("RGB", (1240, height), "white")
        draw = ImageDraw.Draw(image)
        draw.text((40, 60), f"TEST-ONLY 第{page}页 合同金额100元",
                  font=ImageFont.truetype(FONT, 54), fill="black")
        _ = height
        pdf.drawImage(ImageReader(image), 0, 0, width=A4[0], height=A4[1] * 0.4)
        pdf.showPage()
    pdf.save()
    return path.read_bytes()


def build(db, store, content: bytes, name: str) -> dict:
    return factories.build_material_with_storage(db, store, content=content, name=name)


def test_resume_does_not_re_ocr_finished_pages(store, tmp_path, ocr_calls):
    content = scanned_pdf(tmp_path / "scan.pdf", 4)
    db = get_session_factory()()
    try:
        built = build(db, store, content, "scan.pdf")
        run_id, job_id = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()

    def kill_after_two(stage):
        if stage == "unit:2":
            raise RuntimeError("simulated kill after page 2")

    with pytest.raises(RuntimeError):
        pipeline.process_parse_job(get_session_factory(), store, job_id,
                                   fault_inject=kill_after_two)
    first_pass = ocr_calls["n"]
    assert first_pass >= 2

    # Recovery must not re-OCR pages 1 and 2.
    ocr_calls["n"] = 0
    pipeline.process_parse_job(get_session_factory(), store, job_id)
    second_pass = ocr_calls["n"]

    db = get_session_factory()()
    try:
        run = db.get(models.ParseRun, run_id)
        units = db.execute(select(models.ParseUnit)
                           .where(models.ParseUnit.parse_run_id == run_id)).scalars().all()
        assert len(units) == 4, [u.unit_key for u in units]
        assert run.status in ("complete", "partial")
    finally:
        db.close()

    # Pages 3 and 4 remain; re-OCR-ing 1 and 2 would make this at least 4.
    assert second_pass == 2, f"恢复时重新识别了已完成页：{second_pass} 次"


def test_forced_ocr_page_skips_other_pages(store, tmp_path, ocr_calls):
    """Forcing page 2 must not re-OCR page 1, and must still cover the file."""
    content = scanned_pdf(tmp_path / "two.pdf", 2)
    db = get_session_factory()()
    try:
        built = build(db, store, content, "two.pdf")
        first_run, first_job = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()
    pipeline.process_parse_job(get_session_factory(), store, first_job)
    assert ocr_calls["n"] >= 2

    ocr_calls["n"] = 0
    db = get_session_factory()()
    try:
        second_run, second_job = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"],
            force_ocr_pages=[2])
    finally:
        db.close()
    pipeline.process_parse_job(get_session_factory(), store, second_job)

    db = get_session_factory()()
    try:
        units = db.execute(select(models.ParseUnit)
                           .where(models.ParseUnit.parse_run_id == second_run)
                           ).scalars().all()
        assert len(units) == 2, "新版本必须覆盖整份文件"
        keys = {u.unit_key for u in units}
        assert keys == {"pdf:1", "pdf:2"}
        # Page 2 was re-OCR'd; page 1 came from the base run.
        assert ocr_calls["n"] == 1, f"非强制页不应调用 OCR：{ocr_calls['n']} 次"
    finally:
        db.close()


def test_skipped_page_failure_does_not_block_recovery(store, tmp_path, monkeypatch):
    """A model that is unavailable for an already-done page must not abort."""
    content = scanned_pdf(tmp_path / "three.pdf", 3)
    db = get_session_factory()()
    try:
        built = build(db, store, content, "three.pdf")
        run_id, job_id = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()

    def kill_after_one(stage):
        if stage == "unit:1":
            raise RuntimeError("simulated kill after page 1")

    with pytest.raises(RuntimeError):
        pipeline.process_parse_job(get_session_factory(), store, job_id,
                                   fault_inject=kill_after_one)

    # From here on OCR is unavailable.  Pages 2 and 3 genuinely need it, so the
    # run may fail — but it must fail on page 2, not while re-reading page 1.
    def unavailable(self, image, timeout=None):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(ocr_module.OcrEngine, "recognize", unavailable)
    status = pipeline.process_parse_job(get_session_factory(), store, job_id)
    # The pages that genuinely need OCR fail; that is a partial outcome, not a
    # crash, and page 1 must not be disturbed by it.
    assert status in ("partial", "failed"), status

    db = get_session_factory()()
    try:
        units = db.execute(select(models.ParseUnit)
                           .where(models.ParseUnit.parse_run_id == run_id)).scalars().all()
        done = {u.unit_key: u.status for u in units}
        # Page 1 survived the failed recovery untouched.
        assert done.get("pdf:1") in ("ocr_done", "no_text_detected")
        # Pages that truly needed OCR are reported as failures, not silently ok.
        assert any(status == "failed" for key, status in done.items() if key != "pdf:1")
    finally:
        db.close()


def test_failure_triggered_only_by_skipped_page_does_not_block(store, tmp_path, monkeypatch):
    """R04 验收第 3 条：只在「被跳过页」上会触发的异常，不得影响恢复。

    Page 1 already has a result, so it is skipped before rendering.  An engine
    that would blow up on page 1 must never be asked, and pages 2-3 must finish.
    """
    # Page 1 renders at a distinct height so the engine can target it.
    content = scanned_pdf(tmp_path / "skip1.pdf", 3, first_page_height=300)
    db = get_session_factory()()
    try:
        built = build(db, store, content, "skip1.pdf")
        run_id, job_id = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()

    def kill_after_one(stage):
        if stage == "unit:1":
            raise RuntimeError("simulated kill after page 1")

    with pytest.raises(RuntimeError):
        pipeline.process_parse_job(get_session_factory(), store, job_id,
                                   fault_inject=kill_after_one)

    calls = {"n": 0}
    original = ocr_module.OcrEngine.recognize

    def fail_first_page_only(self, image, timeout=None):
        calls["n"] += 1
        # Page 1 is the only 300px-tall page.  If it is skipped correctly this
        # branch is never reached during recovery.
        if image.height == 300:
            raise RuntimeError("page 1 would fail here")
        return original(self, image, timeout=timeout)

    monkeypatch.setattr(ocr_module.OcrEngine, "recognize", fail_first_page_only)
    status = pipeline.process_parse_job(get_session_factory(), store, job_id)

    # Pages 2 and 3 are what remain; the first OCR call in recovery is page 2,
    # so the "page 1 fails" engine must not be triggered at all.
    assert status in ("complete", "partial"), status
    db = get_session_factory()()
    try:
        units = db.execute(select(models.ParseUnit)
                           .where(models.ParseUnit.parse_run_id == run_id)).scalars().all()
        assert len(units) == 3, [u.unit_key for u in units]
        assert all(u.status != "failed" for u in units), [(u.unit_key, u.status) for u in units]
    finally:
        db.close()


def blank_first_page_pdf(path) -> bytes:
    """Two scanned pages; the first carries no text at all."""
    pdf = canvas.Canvas(str(path), pagesize=A4)
    blank = Image.new("RGB", (1240, 400), "white")
    pdf.drawImage(ImageReader(blank), 0, 0, width=A4[0], height=A4[1] * 0.4)
    pdf.showPage()
    image = Image.new("RGB", (1240, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.text((40, 60), "TEST-ONLY 第2页 合同金额200元",
              font=ImageFont.truetype(FONT, 54), fill="black")
    pdf.drawImage(ImageReader(image), 0, 0, width=A4[0], height=A4[1] * 0.4)
    pdf.showPage()
    pdf.save()
    return path.read_bytes()


def test_forced_page_keeps_pages_without_reusable_text(store, tmp_path):
    """Forcing OCR on one page must not delete the pages that have no text.

    The reuse helper returned None for a base unit with no Segment, so the blank
    page vanished from the new run.  The run was then reported complete with a
    total of 1 and the job showed 1/2 — a silently short document.
    """
    content = blank_first_page_pdf(tmp_path / "blank.pdf")
    db = get_session_factory()()
    try:
        built = factories.build_material_with_storage(
            db, store, content=content, name="空白首页.pdf")
        first_run, first_job = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()
    pipeline.process_parse_job(get_session_factory(), store, first_job)

    db = get_session_factory()()
    try:
        units = db.execute(select(models.ParseUnit)
                           .where(models.ParseUnit.parse_run_id == first_run)).scalars().all()
        by_key = {unit.unit_key: unit.status for unit in units}
        assert by_key.get("pdf:1") == "no_text_detected", by_key
        assert by_key.get("pdf:2") in ("ocr_done", "extracted"), by_key
    finally:
        db.close()

    db = get_session_factory()()
    try:
        second_run, second_job = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"],
            force_ocr_pages=[2])
    finally:
        db.close()
    status = pipeline.process_parse_job(get_session_factory(), store, second_job)

    db = get_session_factory()()
    try:
        units = db.execute(select(models.ParseUnit)
                           .where(models.ParseUnit.parse_run_id == second_run)).scalars().all()
        keys = sorted(unit.unit_key for unit in units)
        assert keys == ["pdf:1", "pdf:2"], keys
        by_key = {unit.unit_key: unit.status for unit in units}
        assert by_key["pdf:1"] == "no_text_detected", by_key
        assert by_key["pdf:2"] in ("ocr_done", "extracted"), by_key
        job = db.get(models.Job, second_job)
        assert job.total_units == 2 and job.completed_units == 2, (job.total_units,
                                                                 job.completed_units)
        assert status in ("complete", "partial"), status
        # The active run covers both pages, not just the re-OCR'd one.
        version = db.get(models.FileVersion, built["file_version_id"])
        assert version.active_parse_run_id == second_run
    finally:
        db.close()
