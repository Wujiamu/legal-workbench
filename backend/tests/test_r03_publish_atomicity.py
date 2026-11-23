"""R03: a crash between the terminal status and the active-run switch.

``_finish`` committed the terminal status, then ``_publish_active`` ran as a
separate commit.  Dying in between left a succeeded job whose output was never
searchable, and re-entry returned early on the terminal status so nothing
repaired it.
"""
from __future__ import annotations

import io
import uuid

import pytest
from docx import Document
from sqlalchemy import select

from legal_workbench import models, parsing
from legal_workbench.db import get_session_factory
from legal_workbench.parsing import pipeline
from legal_workbench.storage import Storage

from . import factories


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


def docx_bytes(body: str) -> bytes:
    document = Document()
    document.add_paragraph(body)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def build(db, store, content: bytes, name: str) -> dict:
    return factories.build_material_with_storage(db, store, content=content, name=name)


def test_crash_after_terminal_status_still_publishes(store):
    """Simulate dying exactly between the two former commits."""
    db = get_session_factory()()
    try:
        built = build(db, store, docx_bytes("TEST-ONLY 正文"), "a.docx")
        run_id, job_id = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()

    # Run normally, then rewind the publish to mimic the old crash window.
    pipeline.process_parse_job(get_session_factory(), store, job_id)
    db = get_session_factory()()
    try:
        version = db.get(models.FileVersion, built["file_version_id"])
        assert version.active_parse_run_id == run_id
        version.active_parse_run_id = None
        db.commit()
    finally:
        db.close()

    # Recovery: the job is already terminal, but publishing must still happen.
    pipeline.process_parse_job(get_session_factory(), store, job_id)

    db = get_session_factory()()
    try:
        version = db.get(models.FileVersion, built["file_version_id"])
        run = db.get(models.ParseRun, run_id)
        job = db.get(models.Job, job_id)
        assert run.status == "complete"
        assert job.status == "succeeded"
        assert version.active_parse_run_id == run_id, "终态任务必须补做发布"
    finally:
        db.close()


def test_crash_before_units_do_not_publish(store):
    """A run that never finished must not become the active version."""
    db = get_session_factory()()
    try:
        built = build(db, store, docx_bytes("TEST-ONLY 正文"), "b.docx")
        run_id, job_id = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()

    with pytest.raises(RuntimeError):
        pipeline.process_parse_job(get_session_factory(), store, job_id,
                                   fault_inject=lambda stage: (_ for _ in ()).throw(
                                       RuntimeError("kill before finish")))

    db = get_session_factory()()
    try:
        version = db.get(models.FileVersion, built["file_version_id"])
        assert version.active_parse_run_id is None
        assert db.get(models.ParseRun, run_id).status == "running"
    finally:
        db.close()

    # Recovery completes and publishes in one step.
    pipeline.process_parse_job(get_session_factory(), store, job_id)
    db = get_session_factory()()
    try:
        version = db.get(models.FileVersion, built["file_version_id"])
        assert version.active_parse_run_id == run_id
    finally:
        db.close()


def test_newer_run_wins_over_late_finishing_older_run(store):
    """An older run finishing late must not overwrite a newer active one."""
    db = get_session_factory()()
    try:
        built = build(db, store, docx_bytes("TEST-ONLY 正文"), "c.docx")
        first_run, first_job = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
        second_run, second_job = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()

    pipeline.process_parse_job(get_session_factory(), store, second_job)
    pipeline.process_parse_job(get_session_factory(), store, first_job)

    db = get_session_factory()()
    try:
        version = db.get(models.FileVersion, built["file_version_id"])
        assert version.active_parse_run_id == second_run, "旧任务晚完成不得覆盖新版本"
    finally:
        db.close()


def test_partial_run_does_not_replace_valid_active_run(store, tmp_path):
    """A partial reparse must keep the existing complete run active."""
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active["A1"] = "TEST-ONLY 正文"
    buffer = io.BytesIO()
    workbook.save(buffer)
    content = buffer.getvalue()

    db = get_session_factory()()
    try:
        built = build(db, store, content, "d.xlsx")
        good_run, good_job = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()
    pipeline.process_parse_job(get_session_factory(), store, good_job)

    # Corrupt the managed bytes so the reparse cannot read them.
    db = get_session_factory()()
    try:
        version = db.get(models.FileVersion, built["file_version_id"])
        stored = db.get(models.StoredObject, version.stored_object_id)
        store.managed_path(stored.relative_path).write_bytes(b"not an xlsx")
        bad_run, bad_job = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()
    pipeline.process_parse_job(get_session_factory(), store, bad_job)

    db = get_session_factory()()
    try:
        version = db.get(models.FileVersion, built["file_version_id"])
        assert version.active_parse_run_id == good_run, "失败/部分解析不得替换有效版本"
    finally:
        db.close()


def test_old_source_ref_still_reads_after_reparse(store):
    """Historical references keep resolving to their own run."""
    from legal_workbench import search

    db = get_session_factory()()
    try:
        built = build(db, store, docx_bytes("TEST-ONLY 第一版正文"), "e.docx")
        first_run, first_job = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()
    pipeline.process_parse_job(get_session_factory(), store, first_job)

    db = get_session_factory()()
    try:
        segment = db.execute(select(models.Segment).where(
            models.Segment.parse_run_id == first_run)).scalars().first()
        source_ref = segment.id
    finally:
        db.close()

    db = get_session_factory()()
    try:
        second_run, second_job = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()
    pipeline.process_parse_job(get_session_factory(), store, second_job)

    db = get_session_factory()()
    try:
        version = db.get(models.FileVersion, built["file_version_id"])
        assert version.active_parse_run_id == second_run
        resolved = search.read_source(db, case_id=built["case_id"], source_ref=source_ref)
        assert resolved.parse_run_id == first_run, "旧 source_ref 必须仍指向原解析版本"
        assert "第一版正文" in resolved.text
    finally:
        db.close()


@pytest.mark.parametrize("boundary", ["units_done", "before_finish", "after_finish"])
def test_recovery_at_each_boundary_publishes_and_is_searchable(store, boundary):
    """R03 验收第 1、2 条：三个边界各注入退出，恢复后能检索到新版本。"""
    from legal_workbench import search
    from legal_workbench.schemas import SearchMode, SearchScope

    db = get_session_factory()()
    try:
        built = build(db, store, docx_bytes("TEST-ONLY 边界正文 关键词丁丁丁"), "f.docx")
        run_id, job_id = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()

    def kill_at(stage):
        if stage == boundary:
            raise RuntimeError(f"simulated crash at {boundary}")

    crashed = False
    try:
        pipeline.process_parse_job(get_session_factory(), store, job_id, fault_inject=kill_at)
    except RuntimeError:
        crashed = True
    assert crashed, f"{boundary} 未触发"

    db = get_session_factory()()
    try:
        run = db.get(models.ParseRun, run_id)
        version = db.get(models.FileVersion, built["file_version_id"])
        if boundary == "after_finish":
            # The commit already happened: the run must be published as-is.
            assert run.status in ("complete", "partial")
            assert version.active_parse_run_id == run_id
        else:
            # Nothing was committed yet: not published, still resumable.
            assert version.active_parse_run_id is None, boundary
    finally:
        db.close()

    # Recovery in a fresh session.
    pipeline.process_parse_job(get_session_factory(), store, job_id)

    db = get_session_factory()()
    try:
        version = db.get(models.FileVersion, built["file_version_id"])
        assert version.active_parse_run_id == run_id, boundary
        hits = search.search_case(
            db, case_id=built["case_id"], query="丁丁丁", mode=SearchMode.LITERAL,
            scope=SearchScope.ORIGINALS)
        assert hits.total == 1, (boundary, hits.total)
    finally:
        db.close()


def test_reparse_recovery_hits_new_version(store):
    """R03 验收第 2 条后半：重新解析恢复后必须命中新版本的内容。"""
    from legal_workbench import search
    from legal_workbench.schemas import SearchMode, SearchScope

    db = get_session_factory()()
    try:
        built = build(db, store, docx_bytes("TEST-ONLY 第一版 甲甲甲"), "g.docx")
        first_run, first_job = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()
    pipeline.process_parse_job(get_session_factory(), store, first_job)

    # Change the managed bytes so the reparse sees different content.
    db = get_session_factory()()
    try:
        version = db.get(models.FileVersion, built["file_version_id"])
        stored = db.get(models.StoredObject, version.stored_object_id)
        store.managed_path(stored.relative_path).write_bytes(
            docx_bytes("TEST-ONLY 第二版 乙乙乙"))
        # The stored hash no longer matches; refresh it so verify passes.
        from legal_workbench.storage import hash_file
        new_hash, new_size = hash_file(store.managed_path(stored.relative_path))
        stored.sha256, stored.size = new_hash, new_size
        db.commit()
        second_run, second_job = parsing.create_parse_run(
            db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    finally:
        db.close()

    def kill_before_finish(stage):
        if stage == "before_finish":
            raise RuntimeError("crash before publish")

    with pytest.raises(RuntimeError):
        pipeline.process_parse_job(get_session_factory(), store, second_job,
                                   fault_inject=kill_before_finish)

    pipeline.process_parse_job(get_session_factory(), store, second_job)

    db = get_session_factory()()
    try:
        version = db.get(models.FileVersion, built["file_version_id"])
        assert version.active_parse_run_id == second_run
        new_hits = search.search_case(
            db, case_id=built["case_id"], query="乙乙乙", mode=SearchMode.LITERAL,
            scope=SearchScope.ORIGINALS)
        old_hits = search.search_case(
            db, case_id=built["case_id"], query="甲甲甲", mode=SearchMode.LITERAL,
            scope=SearchScope.ORIGINALS)
        assert new_hits.total == 1, new_hits.total
        assert old_hits.total == 0, "恢复后不应再命中旧版本内容"
    finally:
        db.close()
