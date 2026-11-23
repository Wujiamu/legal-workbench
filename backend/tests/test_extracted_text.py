"""T16: extracted-text endpoint used by the material workspace."""
from __future__ import annotations

import pytest

from legal_workbench import parsing, search
from legal_workbench.db import get_session_factory
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.parsing import pipeline
from legal_workbench.schemas import ParseUnitStatus
from legal_workbench.storage import Storage

from . import factories


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


def test_extracted_text_returns_segments_in_document_order(db, store):
    built = factories.build_material_with_storage(
        db, store, content="第一段内容。\n\n第二段内容。\n\n第三段内容。".encode(), name="a.txt")
    run_id, job_id = parsing.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job_id)
    output = search.extracted_text(db, case_id=built["case_id"],
                                   file_version_id=built["file_version_id"])
    assert output.parse_run_id == run_id
    assert [segment.text for segment in output.segments] == [
        "第一段内容。", "第二段内容。", "第三段内容。"]
    assert all(segment.status == ParseUnitStatus.EXTRACTED for segment in output.segments)
    assert all(segment.locator for segment in output.segments)
    assert output.coverage.complete == 3


def test_extracted_text_empty_before_parsing(db, store):
    built = factories.build_material_with_storage(db, store, content=b"TEST-ONLY", name="b.txt")
    output = search.extracted_text(db, case_id=built["case_id"],
                                   file_version_id=built["file_version_id"])
    assert output.parse_run_id is None and output.segments == []
    assert output.coverage.pending == 1


def test_extracted_text_rejects_cross_case(db, store):
    built = factories.build_material_with_storage(db, store, content=b"TEST-ONLY", name="c.txt")
    other = factories.create_case(db, "其他案件")
    with pytest.raises(WorkbenchError) as exc:
        search.extracted_text(db, case_id=other, file_version_id=built["file_version_id"])
    assert exc.value.code == ErrorCode.CASE_MISMATCH


def test_extracted_text_unknown_version(db):
    import uuid

    with pytest.raises(WorkbenchError) as exc:
        search.extracted_text(db, case_id=uuid.uuid4(), file_version_id=uuid.uuid4())
    assert exc.value.code == ErrorCode.NOT_FOUND
