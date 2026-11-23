"""Coverage reporting must describe real outcomes, not hide them as "pending".

A parse run that ends unsupported or failed never becomes the active run, so a
naive implementation reports such a material as still waiting forever.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from legal_workbench import coverage, materials, models, parsing, search
from legal_workbench.config import load_settings
from legal_workbench.db import get_session_factory
from legal_workbench.parsing import pipeline
from legal_workbench.schemas import SearchMode
from legal_workbench.storage import Storage

from . import factories


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


def _built_with(db, store, *, name: str, content: bytes):
    built = factories.build_material_with_storage(db, store, content=content, name=name)
    run_id, job_id = parsing.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job_id)
    db.expire_all()
    return built, run_id


def test_unsupported_parse_reports_unsupported_not_pending(db, store):
    built, run_id = _built_with(db, store, name="坏文件.docx", content=b"PK not a docx")
    run = db.get(models.ParseRun, run_id)
    assert run.status == "unsupported"
    version = db.get(models.FileVersion, built["file_version_id"])
    assert version.active_parse_run_id is None

    reported = materials._coverage_for(db, version)
    assert reported.unsupported == 1
    assert reported.pending == 0

    hits = search.search_case(db, case_id=built["case_id"], query="合同",
                              mode=SearchMode.LITERAL)
    assert hits.coverage.unsupported == 1
    assert hits.coverage.pending == 0


def test_never_parsed_material_still_reports_pending(db, store):
    built = factories.build_material_with_storage(
        db, store, content="TEST-ONLY 尚未解析".encode(), name="待解析.txt")
    version = db.get(models.FileVersion, built["file_version_id"])
    reported = coverage.coverage_for_version(db, version)
    assert reported.pending == 1
    assert reported.complete == 0


def test_failed_run_is_reported_as_failed(db, store):
    built, run_id = _built_with(db, store, name="合同.txt", content="TEST-ONLY 正文".encode())
    run = db.get(models.ParseRun, run_id)
    run.status = "failed"
    unit = db.execute(select(models.ParseUnit).where(
        models.ParseUnit.parse_run_id == run_id)).scalars().first()
    unit.status = "failed"
    run.coverage = {"complete": 0, "partial": 0, "failed": 1, "pending": 0,
                    "unsupported": 0, "total": 1}
    db.commit()
    version = db.get(models.FileVersion, built["file_version_id"])
    reported = coverage.coverage_for_version(db, version)
    assert reported.failed == 1
    assert reported.pending == 0
