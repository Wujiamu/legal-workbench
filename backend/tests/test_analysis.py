"""T12: immutable analysis versions, source validation, review and analysis scope."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from legal_workbench import analysis, models, parsing, search
from legal_workbench.db import get_session_factory
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.parsing import pipeline
from legal_workbench.schemas import ReviewStatus, SearchMode, SearchScope, StatementKind
from legal_workbench.storage import Storage

from . import factories


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


@pytest.fixture
def parsed(db, store):
    built = factories.build_material_with_storage(
        db, store, content="合同约定甲方支付100元。".encode(), name="合同.txt")
    run_id, job_id = parsing.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job_id)
    segment = db.execute(select(models.Segment)).scalars().first()
    return {**built, "source_ref": segment.id}


def save(db, store, parsed, content="分析：合同有效。", refs=None, **kwargs):
    return analysis.save_analysis(
        db, store, case_id=parsed["case_id"], title=kwargs.pop("title", "虚构分析"),
        content=content, statement_kind=kwargs.pop("statement_kind", StatementKind.AGENT_ANALYSIS),
        source_refs=refs if refs is not None else [parsed["source_ref"]], **kwargs,
    )


def test_save_creates_immutable_file_and_version(db, store, parsed):
    output = save(db, store, parsed)
    assert output.revision == 0
    version = db.get(models.AnalysisVersion, output.version_id)
    assert version.content == "分析：合同有效。"
    assert version.source_refs == [str(parsed["source_ref"])]
    assert version.review_status == "pending"
    path = store.managed_path(version.relative_path)
    assert path.read_bytes() == "分析：合同有效。".encode()
    assert version.relative_path == f"cases/{parsed['case_id']}/analysis/{output.version_id}/content.md"
    summary = analysis.get_analysis(db, case_id=parsed["case_id"],
                                    analysis_id=output.analysis_id)
    assert summary.revision == 0 and summary.current_version_id == output.version_id


def test_edit_creates_new_version_and_keeps_old(db, store, parsed):
    first = save(db, store, parsed, content="第一版正文")
    second = save(db, store, parsed, content="第二版正文", analysis_id=first.analysis_id,
                  expected_revision=0)
    assert second.revision == 1 and second.version_id != first.version_id
    old = analysis.get_analysis_version(db, case_id=parsed["case_id"],
                                        analysis_id=first.analysis_id,
                                        version_id=first.version_id)
    new = analysis.get_analysis_version(db, case_id=parsed["case_id"],
                                        analysis_id=first.analysis_id,
                                        version_id=second.version_id)
    assert old.content == "第一版正文" and new.content == "第二版正文"
    versions = analysis.list_analysis_versions(db, case_id=parsed["case_id"],
                                               analysis_id=first.analysis_id)
    assert versions.total == 2
    listed = analysis.list_analyses(db, case_id=parsed["case_id"])
    assert listed.total == 1 and listed.items[0].current_version_id == second.version_id


def test_stale_revision_conflicts(db, store, parsed):
    first = save(db, store, parsed)
    save(db, store, parsed, content="新版本", analysis_id=first.analysis_id,
         expected_revision=0)
    with pytest.raises(WorkbenchError) as exc:
        save(db, store, parsed, content="过期版本", analysis_id=first.analysis_id,
             expected_revision=0)
    assert exc.value.code == ErrorCode.CONFLICT


def test_cross_case_and_missing_source_refs_rejected(db, store, parsed):
    other_case = factories.create_case(db, "其他案件")
    other_built = factories.build_material_with_storage(db, store, case_id=other_case,
                                                        content=b"other", name="o.txt")
    run_id, job_id = parsing.create_parse_run(
        db, case_id=other_case, file_version_id=other_built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job_id)
    other_segment = db.execute(select(models.Segment).where(
        models.Segment.case_id == other_case)).scalars().first()
    with pytest.raises(WorkbenchError) as exc:
        save(db, store, parsed, refs=[other_segment.id])
    assert exc.value.code == ErrorCode.CASE_MISMATCH
    with pytest.raises(WorkbenchError) as exc:
        save(db, store, parsed, refs=[uuid.uuid4()])
    assert exc.value.code == ErrorCode.NOT_FOUND


def test_review_changes_status_not_content(db, store, parsed):
    first = save(db, store, parsed, content="待审核正文")
    result = analysis.review_analysis(
        db, case_id=parsed["case_id"], analysis_id=first.analysis_id,
        version_id=first.version_id, expected_revision=0, review_status=ReviewStatus.APPROVED)
    assert result.revision == 1 and result.review_status == ReviewStatus.APPROVED
    version = db.get(models.AnalysisVersion, first.version_id)
    assert version.content == "待审核正文" and version.review_status == "approved"
    with pytest.raises(WorkbenchError) as exc:
        analysis.review_analysis(
            db, case_id=parsed["case_id"], analysis_id=first.analysis_id,
            version_id=first.version_id, expected_revision=0,
            review_status=ReviewStatus.REJECTED)
    assert exc.value.code == ErrorCode.CONFLICT


def test_analysis_scope_search_uses_current_version(db, store, parsed):
    first = save(db, store, parsed, content="分析结论：合同有效。")
    hit = search.search_case(db, case_id=parsed["case_id"], query="分析结论",
                             mode=SearchMode.LITERAL, scope=SearchScope.ANALYSIS)
    assert hit.total == 1
    row = hit.hits[0]
    assert row.source is None and row.analysis_id == first.analysis_id
    assert row.analysis_version_id == first.version_id
    assert row.context[row.match_start:row.match_end] == "分析结论"
    save(db, store, parsed, content="新版分析：合同无效。", analysis_id=first.analysis_id,
         expected_revision=0)
    old = search.search_case(db, case_id=parsed["case_id"], query="分析结论",
                             mode=SearchMode.LITERAL, scope=SearchScope.ANALYSIS)
    assert old.total == 0
    new = search.search_case(db, case_id=parsed["case_id"], query="合同无效",
                             mode=SearchMode.LITERAL, scope=SearchScope.ANALYSIS)
    assert new.total == 1


def test_markdown_content_is_stored_not_executed(db, store, parsed):
    payload = "<script>alert(1)</script>\n\n# 标题"
    output = save(db, store, parsed, content=payload)
    version = db.get(models.AnalysisVersion, output.version_id)
    assert version.content == payload
    assert store.managed_path(version.relative_path).read_text(encoding="utf-8") == payload
