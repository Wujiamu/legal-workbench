"""T17: search offsets are code points, analysis hits carry version identity."""
from __future__ import annotations

import pytest

from legal_workbench import analysis, parsing, search
from legal_workbench.db import get_session_factory
from legal_workbench.parsing import pipeline
from legal_workbench.schemas import SearchMode, SearchScope, StatementKind
from legal_workbench.storage import Storage

from . import factories


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


@pytest.fixture
def parsed(db, store):
    built = factories.build_material_with_storage(
        db, store, content="甲方支付100元。\n\n合同约定乙方交付。".encode(), name="合同.txt")
    run_id, job_id = parsing.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job_id)
    return built


def test_match_offsets_are_code_points_not_bytes(db, store, parsed):
    """A CJK hit must slice correctly with Python string indexing (code points)."""
    result = search.search_case(db, case_id=parsed["case_id"], query="合同",
                                mode=SearchMode.LITERAL)
    hit = result.hits[0]
    assert hit.context[hit.match_start:hit.match_end] == "合同"
    # Byte offsets would land past the end for multi-byte characters.
    assert hit.match_end <= len(hit.context)


def test_analysis_hit_exposes_analysis_and_version_ids(db, store, parsed):
    segment = search.search_case(db, case_id=parsed["case_id"], query="合同",
                                 mode=SearchMode.LITERAL).hits[0].source
    saved = analysis.save_analysis(
        db, store, case_id=parsed["case_id"], title="虚构分析",
        content="分析结论：合同有效。", statement_kind=StatementKind.AGENT_ANALYSIS,
        source_refs=[segment.source_ref])
    result = search.search_case(db, case_id=parsed["case_id"], query="结论",
                                mode=SearchMode.LITERAL, scope=SearchScope.ANALYSIS)
    hit = result.hits[0]
    assert hit.source_type == SearchScope.ANALYSIS
    assert hit.source is None
    assert hit.analysis_id == saved.analysis_id
    assert hit.analysis_version_id == saved.version_id
    assert hit.context[hit.match_start:hit.match_end] == "结论"


def test_analysis_scope_ignores_superseded_versions(db, store, parsed):
    first = analysis.save_analysis(
        db, store, case_id=parsed["case_id"], title="虚构分析", content="旧结论。",
        statement_kind=StatementKind.AGENT_ANALYSIS, source_refs=[])
    analysis.save_analysis(
        db, store, case_id=parsed["case_id"], title="虚构分析", content="新结论。",
        statement_kind=StatementKind.AGENT_ANALYSIS, source_refs=[],
        analysis_id=first.analysis_id, expected_revision=0)
    assert search.search_case(db, case_id=parsed["case_id"], query="旧结论",
                              mode=SearchMode.LITERAL,
                              scope=SearchScope.ANALYSIS).total == 0
    assert search.search_case(db, case_id=parsed["case_id"], query="新结论",
                              mode=SearchMode.LITERAL,
                              scope=SearchScope.ANALYSIS).total == 1


def test_search_does_not_require_csrf_relevant_state(db, store, parsed):
    """Search is read-only: repeated calls are stable and leave no rows behind."""
    from sqlalchemy import select

    from legal_workbench import models

    before = len(db.execute(select(models.Segment)).scalars().all())
    for _ in range(3):
        assert search.search_case(db, case_id=parsed["case_id"], query="甲方",
                                  mode=SearchMode.LITERAL).total == 1
    after = len(db.execute(select(models.Segment)).scalars().all())
    assert before == after
