"""T11: literal/identifier/amount/date search, normalization and exact values.

The unreceived original 22 search tests remain pending; these are separately
named TEST-ONLY tests.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from legal_workbench import materials, models, parsing, search, worker
from legal_workbench.db import get_engine, get_session_factory
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.parsing import pipeline
from legal_workbench.schemas import SearchMode, SearchScope
from legal_workbench.storage import Storage

from . import factories


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


def make_text(db, store, content: str, name: str = "合同.txt") -> dict:
    built = factories.build_material_with_storage(db, store, content=content.encode(),
                                                  name=name)
    run_id, job_id = parsing.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"])
    assert pipeline.process_parse_job(get_session_factory(), store, job_id) in ("complete", "partial")
    return built


def do_search(db, built, query, mode=SearchMode.LITERAL, scope=SearchScope.ORIGINALS,
              currency=None, offset=0, limit=20):
    return search.search_case(db, case_id=built["case_id"], query=query, mode=mode,
                              scope=scope, currency=currency, offset=offset, limit=limit)


def test_literal_two_char_word_and_context_offsets(db, store):
    built = make_text(db, store, "本合同由甲方与乙方签订。\n\n其他条款。")
    result = do_search(db, built, "合同")
    assert result.total == 1 and result.mode_used == SearchMode.LITERAL
    hit = result.hits[0]
    assert hit.context[hit.match_start:hit.match_end] == "合同"
    assert hit.source is not None and hit.source.material_id == built["material_id"]
    assert hit.source.text == "本合同由甲方与乙方签订。"


def test_literal_and_semantics_and_short_word(db, store):
    built = make_text(db, store, "合同金额为100元。\n\n合同另行约定。\n\n仅有金额。")
    both = do_search(db, built, "合同 金额")
    assert both.total == 1
    short = do_search(db, built, "合")
    assert short.total == 2


def test_literal_wildcards_are_escaped(db, store):
    built = make_text(db, store, "折扣100%完成。\n\n编号A_B。\n\n普通文本。")
    percent = do_search(db, built, "%")
    assert percent.total == 1 and "%" in percent.hits[0].context
    underscore = do_search(db, built, "_")
    assert underscore.total == 1 and "A_B" in underscore.hits[0].context


def test_literal_fullwidth_normalization(db, store):
    built = make_text(db, store, "全角ＣＮＹ字符与（括号）。")
    result = do_search(db, built, "CNY")
    assert result.total == 1
    hit = result.hits[0]
    assert hit.context[hit.match_start:hit.match_end] == "ＣＮＹ"
    punctuation = do_search(db, built, "(括号)")
    assert punctuation.total == 1


def test_filename_exact_hit_ranks_first(db, store):
    first = make_text(db, store, "合同与文件名无关的正文。", "合同.txt")
    third = factories.build_material_with_storage(db, store, case_id=first["case_id"],
                                                  content="另一份正文提到合同。".encode(),
                                                  name="说明2.txt")
    run_id, job_id = parsing.create_parse_run(
        db, case_id=third["case_id"], file_version_id=third["file_version_id"])
    pipeline.process_parse_job(get_session_factory(), store, job_id)
    result = do_search(db, first, "合同")
    assert result.total == 2
    assert result.hits[0].source.material_id == first["material_id"]


def test_identifier_boundaries(db, store):
    built = make_text(db, store, "编号 HT-001 与 HT-0012，另有 HT-001-2。")
    one = do_search(db, built, "HT-001", mode=SearchMode.IDENTIFIER)
    assert one.total == 1
    assert one.hits[0].context[one.hits[0].match_start:one.hits[0].match_end] == "HT-001"
    two = do_search(db, built, "HT-0012", mode=SearchMode.IDENTIFIER)
    assert two.total == 1


def test_amount_exact_values_and_currency(db, store):
    built = make_text(db, store, "金额100元；另一笔1000元；美元100 USD；1万元；10000元。")
    hundred = do_search(db, built, "100", mode=SearchMode.AMOUNT, currency="CNY")
    assert hundred.total == 1
    assert hundred.hits[0].amount == Decimal("100.000000")
    ten_thousand = do_search(db, built, "1万元", mode=SearchMode.AMOUNT, currency="CNY")
    assert ten_thousand.total == 2
    usd = do_search(db, built, "100", mode=SearchMode.AMOUNT, currency="USD")
    assert usd.total == 1 and usd.hits[0].currency == "USD"
    assert do_search(db, built, "1000", mode=SearchMode.AMOUNT, currency="CNY").total == 1


def test_amount_candidates_come_from_exact_values(db, store):
    built = make_text(db, store, "金额100元。")
    db.execute(delete(models.ExactValue))
    db.commit()
    assert do_search(db, built, "100", mode=SearchMode.AMOUNT, currency="CNY").total == 0


def test_amount_requires_currency_and_valid_number(db, store):
    built = make_text(db, store, "金额100元。")
    with pytest.raises(WorkbenchError):
        do_search(db, built, "100", mode=SearchMode.AMOUNT)
    with pytest.raises(WorkbenchError):
        do_search(db, built, "一百", mode=SearchMode.AMOUNT, currency="CNY")


def test_date_requires_complete_valid_date(db, store):
    built = make_text(db, store, "签订于2024年1月1日；无效2024-02-30；残缺2024-01。")
    hit = do_search(db, built, "2024-01-01", mode=SearchMode.DATE)
    assert hit.total == 1
    assert hit.hits[0].context[hit.hits[0].match_start:hit.hits[0].match_end] == "2024年1月1日"
    for bad in ("2024-01", "2024-02-30", "01/02/2024"):
        with pytest.raises(WorkbenchError) as exc:
            do_search(db, built, bad, mode=SearchMode.DATE)
        assert exc.value.code == ErrorCode.INVALID_INPUT


def test_pagination_and_stable_order(db, store):
    built = make_text(db, store, "合同一。\n\n合同二。\n\n合同三。")
    first = do_search(db, built, "合同", limit=1, offset=0)
    second = do_search(db, built, "合同", limit=1, offset=1)
    assert first.total == 3 and len(first.hits) == 1 and len(second.hits) == 1
    assert first.hits[0].source.source_ref != second.hits[0].source.source_ref


def test_read_source_context_and_case_isolation(db, store):
    built = make_text(db, store, "第一段。\n\n第二段包含合同。\n\n第三段。")
    hit = do_search(db, built, "合同").hits[0]
    output = search.read_source(db, case_id=built["case_id"],
                                source_ref=hit.source.source_ref, context_chars=10)
    assert output.text == "第二段包含合同。"
    assert output.context_before == "第一段。"
    assert output.context_after == "第三段。"
    other_case = factories.create_case(db, "其他案件")
    with pytest.raises(WorkbenchError) as exc:
        search.read_source(db, case_id=other_case, source_ref=hit.source.source_ref)
    assert exc.value.code == ErrorCode.CASE_MISMATCH


def test_working_scope_only_registered_parsed_versions(db, store):
    built = make_text(db, store, "原始条款。")
    working_id, relative = materials.create_working_copy(
        db, store, case_id=built["case_id"], file_version_id=built["file_version_id"],
        display_name="编辑稿.txt")
    store.managed_path(relative).write_bytes("工作副本新增条款。".encode())
    materials.register_file_version(db, store, case_id=built["case_id"],
                                    working_copy_id=working_id, expected_revision=0)
    assert worker.run_worker(get_engine(), store, once=True) == 1
    original_scope = do_search(db, built, "新增条款", scope=SearchScope.ORIGINALS)
    assert original_scope.total == 0
    working_scope = do_search(db, built, "新增条款", scope=SearchScope.WORKING)
    assert working_scope.total == 1
    assert working_scope.hits[0].source.file_version_id != built["file_version_id"]


def test_index_status_counts(db, store):
    built = make_text(db, store, "合同正文。")
    status = search.get_index_status(db, case_id=built["case_id"])
    assert status.complete == 1 and status.pending == 0
    factories.build_material_with_storage(db, store, case_id=built["case_id"],
                                          content=b"not parsed", name="b.txt")
    status = search.get_index_status(db, case_id=built["case_id"])
    assert status.pending == 1


def test_search_unknown_case_and_empty_query(db, store):
    built = make_text(db, store, "合同。")
    with pytest.raises(WorkbenchError):
        search.search_case(db, case_id=uuid.uuid4(), query="合同", mode=SearchMode.LITERAL)
    with pytest.raises(WorkbenchError):
        do_search(db, built, "   ")
