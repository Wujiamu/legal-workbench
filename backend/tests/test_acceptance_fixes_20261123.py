"""Regression tests for all 9 fixes identified by 2026-11-23 TEST-ONLY E2E acceptance."""
from __future__ import annotations

import dataclasses
from decimal import Decimal
import io
import json
import os
import subprocess
import sys
import uuid

import docx
import openpyxl
import pytest

from legal_workbench import (
    analysis, cases, chart_export as export, claims, coverage,
    evidence_catalog, graph, models, parsing, search, source_notes, table_graph_import as tgi,
)
from legal_workbench.config import load_settings
from legal_workbench.db import get_session_factory
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.parsing import pipeline
from legal_workbench.schemas import (
    AddClaimGroupInput, AddClaimItemInput, AddEvidenceBindingInput,
    AddEvidenceGroupInput, AddEvidenceItemInput, ArchiveCaseInput,
    ClaimCategory, CreateAnnotationInput, CreateGraphInput,
    ReviewAnnotationInput, ReviewStatus, SearchMode, SearchScope,
    StatementKind,
)
from legal_workbench.storage import Storage

from . import factories

BACKEND = __import__("pathlib").Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path) -> Storage:
    return Storage(root=tmp_path / "data")


def _xlsx_bytes(sheets: dict[str, list[list[object]]]) -> bytes:
    wb = openpyxl.Workbook()
    first = True
    for title, rows in sheets.items():
        ws = wb.active if first else wb.create_sheet(title=title)
        ws.title = title
        first = False
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def _docx_bytes() -> bytes:
    document = docx.Document()
    document.add_paragraph("第一条 本金约定人民币 8,500,000.00 元。")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "违约金条款"
    table.cell(0, 1).text = "637,500.00 元"
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _run_parse(db, store: Storage, *, name: str, content: bytes) -> tuple[dict, str]:
    built = factories.build_material_with_storage(db, store, content=content, name=name)
    run_id, job_id = parsing.create_parse_run(
        db, case_id=built["case_id"], file_version_id=built["file_version_id"]
    )
    status = pipeline.process_parse_job(get_session_factory(), store, job_id)
    db.expire_all()
    built["parse_run_id"] = run_id
    return built, status


def test_archive_blocks_annotations_and_analysis_create_and_review(db, store):
    built, status = _run_parse(db, store, name="合同.txt", content="TEST-ONLY 合同原文".encode())
    assert status == "complete"
    case_id = built["case_id"]
    ver_id = built["file_version_id"]
    seg = db.query(models.Segment).filter(models.Segment.case_id == case_id).first()

    # 归档前：Agent 标注与分析可正常创建
    pending_ann = source_notes.create_annotation(
        db,
        CreateAnnotationInput(case_id=case_id, file_version_id=ver_id, kind="interpretation", body="待复核标注"),
        author="agent",
        storage=store,
    )
    saved_ana = analysis.save_analysis(
        db, store, case_id=case_id, title="待审核分析", content="正文",
        statement_kind=StatementKind.AGENT_ANALYSIS, source_refs=[seg.id],
    )

    case_row = db.get(models.Case, case_id)
    archived = cases.archive_case(db, ArchiveCaseInput(case_id=case_id, expected_revision=case_row.revision))

    # 归档后：新建（agent/human）与复核/审核一律拒绝
    for author in ("agent", "human"):
        with pytest.raises(WorkbenchError) as exc:
            source_notes.create_annotation(
                db,
                CreateAnnotationInput(case_id=case_id, file_version_id=ver_id, kind="interpretation", body="越权写入"),
                author=author,
                storage=store,
            )
        assert exc.value.code == ErrorCode.INVALID_INPUT
        assert "归档" in exc.value.message

    with pytest.raises(WorkbenchError) as exc:
        source_notes.review_annotation(
            db,
            ReviewAnnotationInput(case_id=case_id, annotation_id=pending_ann.annotation_id, reviewed_text="不应通过"),
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert "归档" in exc.value.message

    with pytest.raises(WorkbenchError) as exc:
        analysis.save_analysis(
            db, store, case_id=case_id, title="归档新增分析", content="禁止",
            statement_kind=StatementKind.AGENT_ANALYSIS, source_refs=[seg.id],
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT

    with pytest.raises(WorkbenchError) as exc:
        analysis.save_analysis(
            db, store, case_id=case_id, title="归档更新分析", content="禁止更新",
            statement_kind=StatementKind.AGENT_ANALYSIS, source_refs=[seg.id],
            analysis_id=saved_ana.analysis_id, expected_revision=saved_ana.revision,
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT

    with pytest.raises(WorkbenchError) as exc:
        analysis.review_analysis(
            db, case_id=case_id, analysis_id=saved_ana.analysis_id,
            version_id=saved_ana.version_id, expected_revision=saved_ana.revision,
            review_status=ReviewStatus.APPROVED,
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT

    # 取消归档后恢复正常创建与复核
    cases.unarchive_case(db, ArchiveCaseInput(case_id=case_id, expected_revision=archived.revision))
    ok_ann = source_notes.create_annotation(
        db,
        CreateAnnotationInput(case_id=case_id, file_version_id=ver_id, kind="interpretation", body="解档后新增"),
        author="agent",
        storage=store,
    )
    reviewed = source_notes.review_annotation(
        db,
        ReviewAnnotationInput(case_id=case_id, annotation_id=ok_ann.annotation_id, reviewed_text="人工确认通过"),
    )
    assert reviewed.reviewed_text == "人工确认通过"
    rev_ana = analysis.review_analysis(
        db, case_id=case_id, analysis_id=saved_ana.analysis_id,
        version_id=saved_ana.version_id, expected_revision=saved_ana.revision,
        review_status=ReviewStatus.APPROVED,
    )
    assert rev_ana.review_status == ReviewStatus.APPROVED


def test_table_import_splits_currencies_and_skips_same_pair_conflict_on_commit(db, store):
    xlsx = _xlsx_bytes({
        "往来明细": [
            ["交易日期", "付款方", "收款方", "交易金额", "币种", "方向", "摘要", "回单号"],
            ["2025-03-18", "苏州启航建设工程有限公司", "上海华辰智能制造有限公司", Decimal("3500000.00"), "CNY", "付款", "一期", "H1"],
            ["2025-04-02", "苏州启航建设工程有限公司", "上海华辰智能制造有限公司", Decimal("200000.00"), "CNY", "付款", "保1", "H2"],
            ["2025-04-15", "苏州启航建设工程有限公司", "上海华辰智能制造有限公司", Decimal("100000.00"), "CNY", "付款", "保2", "H3"],
            ["2025-06-10", "苏州鑫悦贸易有限公司", "上海华辰智能制造有限公司", Decimal("1200000.00"), "CNY", "付款", "代付", "H4"],
            ["2025-07-25", "苏州启航建设工程有限公司", "上海华辰智能制造有限公司", Decimal("1500000.00"), "CNY", "付款", "二期", "H5"],
            ["2025-08-05", "苏州启航建设工程有限公司", "上海华辰智能制造有限公司", Decimal("50000.00"), "USD", "付款", "美元配件", "H6"],
            ["2025-09-01", "上海华辰智能制造有限公司", "苏州启航建设工程有限公司", Decimal("50000.00"), "CNY", "退款", "差额退回", "H7"],
            ["2025-09-20", "苏州启航建设工程有限公司", "上海华辰智能制造有限公司", "待银行回单确认", "CNY", "付款", "异常行", "H8"],
        ]
    })
    built = factories.build_material_with_storage(db, store, content=xlsx, name="05_标准往来明细台账.xlsx")
    case_id = built["case_id"]
    ws = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="TEST-ONLY 主图"), actor="human", entry="local-web")
    graph_id = ws.graphs[0].id

    compiled = tgi.compile_table_relations(
        db, store, case_id,
        tgi.TableCompileInput(
            material_id=built["material_id"],
            graph_id=graph_id,
            sheet_name="往来明细",
            header_row=1,
            from_col=1,
            to_col=2,
            direction_col=5,
            relation_kind="fund_flow",
            amount_col=3,
            currency_col=4,
            currency="CNY",
            date_col=0,
            label_col=6,
            aggregate_by_pair=True,
        ),
    )

    assert compiled.processed_rows == 8
    assert len(compiled.skipped_rows) == 1
    assert compiled.skipped_rows[0].row_number == 9
    assert compiled.skipped_rows[0].reason == "invalid_amount"
    assert len(compiled.compiled_edges) == 4

    by_key = {(e.from_name, e.to_name, e.currency): e for e in compiled.compiled_edges}
    assert by_key[("苏州启航建设工程有限公司", "上海华辰智能制造有限公司", "CNY")].amount_cents == 530_000_000
    assert by_key[("苏州启航建设工程有限公司", "上海华辰智能制造有限公司", "USD")].amount_cents == 5_000_000
    assert by_key[("苏州鑫悦贸易有限公司", "上海华辰智能制造有限公司", "CNY")].amount_cents == 120_000_000
    assert by_key[("上海华辰智能制造有限公司", "苏州启航建设工程有限公司", "CNY")].amount_cents == 5_000_000

    # commit 时不触发同向同类 422，CNY 主边不被 USD 混算，USD 冲突行安全进入 skipped_rows
    committed = tgi.commit_table_relations(
        db, store, case_id,
        tgi.TableCommitInput(
            material_id=built["material_id"],
            graph_id=graph_id,
            sheet_name="往来明细",
            header_row=1,
            from_col=1,
            to_col=2,
            direction_col=5,
            relation_kind="fund_flow",
            amount_col=3,
            currency_col=4,
            currency="CNY",
            date_col=0,
            label_col=6,
            aggregate_by_pair=True,
            request_key="fix-multi-currency-commit",
        ),
    )
    assert committed.created_relations == 3
    reasons = [s.reason for s in committed.skipped_rows]
    assert reasons == ["invalid_amount", "currency_conflict_same_pair"]
    rel_cny = next(r for r in committed.workspace.relations if r.attrs.amount_cents == 530_000_000)
    assert rel_cny.attrs.currency == "CNY"


def test_table_import_respects_inline_currency_without_currency_col(db, store):
    xlsx = _xlsx_bytes({
        "流水": [
            ["付款方", "收款方", "金额"],
            ["甲公司", "乙公司", "USD 50000"],
            ["甲公司", "乙公司", "CNY 100000"],
        ]
    })
    built = factories.build_material_with_storage(db, store, content=xlsx, name="行内币种.xlsx")
    case_id = built["case_id"]
    ws = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="行内图"), actor="human", entry="local-web")
    compiled = tgi.compile_table_relations(
        db, store, case_id,
        tgi.TableCompileInput(
            material_id=built["material_id"],
            graph_id=ws.graphs[0].id,
            sheet_name="流水",
            header_row=1,
            from_col=0,
            to_col=1,
            amount_col=2,
            currency_col=None,
            currency="EUR",
            aggregate_by_pair=True,
        ),
    )
    assert len(compiled.compiled_edges) == 2
    by_ccy = {e.currency: e.amount_cents for e in compiled.compiled_edges}
    assert by_ccy == {"USD": 5_000_000, "CNY": 10_000_000}


def test_amount_search_extracts_split_currency_column_from_xlsx(db, store):
    xlsx = _xlsx_bytes({
        "往来明细": [
            ["交易日期", "付款方", "收款方", "交易金额", "币种", "摘要"],
            ["2025-03-18", "启航建工", "华辰智造", Decimal("3500000.00"), "CNY", "一期款"],
            ["2025-08-05", "启航建工", "华辰智造", Decimal("50000.00"), "USD", "美元配件"],
            ["2025-09-01", "华辰智造", "启航建工", Decimal("50000.00"), "CNY", "差额退回"],
        ]
    })
    built, status = _run_parse(db, store, name="分列币种台账.xlsx", content=xlsx)
    assert status == "complete"
    case_id = built["case_id"]

    usd_hits = search.search_case(db, case_id=case_id, query="50000", mode=SearchMode.AMOUNT, currency="USD")
    assert usd_hits.total == 1
    assert "美元配件" in usd_hits.hits[0].context

    cny_hits = search.search_case(db, case_id=case_id, query="50000", mode=SearchMode.AMOUNT, currency="CNY")
    assert cny_hits.total == 1
    assert "差额退回" in cny_hits.hits[0].context

    large_cny = search.search_case(db, case_id=case_id, query="3500000", mode=SearchMode.AMOUNT, currency="CNY")
    assert large_cny.total == 1
    assert "一期款" in large_cny.hits[0].context


def test_corrupted_pdf_reports_failed_not_unsupported(db, store):
    built, status = _run_parse(
        db, store,
        name="10_损坏扫描附件（模拟损坏）.pdf",
        content=b"%PDF-1.7\n%TEST-ONLY-CORRUPTED-EOF\n%%broken-xref-stream\n",
    )
    assert status == "failed"
    version = db.get(models.FileVersion, built["file_version_id"])
    assert version.active_parse_run_id is None
    cov = coverage.coverage_for_version(db, version)
    assert cov.failed == 1
    assert cov.unsupported == 0


def test_citation_table_preserves_docx_paragraph_and_txt_line_locations(db, store):
    docx_built, docx_status = _run_parse(db, store, name="起诉状.docx", content=_docx_bytes())
    assert docx_status == "complete"
    case_id = docx_built["case_id"]
    txt_mat = factories.build_material_with_storage(
        db, store, case_id=case_id,
        content="第一行备忘\n\n\n第四行：金额约定\n第五行：说明".encode(), name="备忘.txt",
    )
    txt_run2, txt_job2 = parsing.create_parse_run(
        db, case_id=case_id, file_version_id=txt_mat["file_version_id"]
    )
    pipeline.process_parse_job(get_session_factory(), store, txt_job2)
    db.expire_all()

    docx_segs = db.query(models.Segment).filter(
        models.Segment.parse_run_id == docx_built["parse_run_id"]
    ).all()
    p_seg = next(s for s in docx_segs if s.locator.get("paragraph") == 0)
    t_seg = next(s for s in docx_segs if s.locator.get("cell") == "R0C1")
    txt_segs = sorted(
        db.query(models.Segment).filter(models.Segment.parse_run_id == txt_run2).all(),
        key=lambda seg: seg.locator.get("line_start", 0),
    )

    actor = dict(actor="human", entry="local-web")
    cat = evidence_catalog.add_group(db, AddEvidenceGroupInput(case_id=case_id, title="主证"), **actor)
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(case_id=case_id, group_id=cat.groups[0].id, title="起诉状"), **actor)
    evidence_catalog.add_binding(db, AddEvidenceBindingInput(case_id=case_id, item_id=cat.items[0].id, material_id=docx_built["material_id"]), **actor)
    cat = evidence_catalog.add_item(db, AddEvidenceItemInput(case_id=case_id, group_id=cat.groups[0].id, title="备忘"), **actor)
    evidence_catalog.add_binding(db, AddEvidenceBindingInput(case_id=case_id, item_id=cat.items[1].id, material_id=txt_mat["material_id"]), **actor)

    snap = claims.add_group(db, AddClaimGroupInput(case_id=case_id, title="诉请组", currency="CNY"), **actor)
    gid = snap.groups[0].id
    snap = claims.add_item(db, AddClaimItemInput(
        case_id=case_id, group_id=gid, title="本金", category="principal",
        amount_cents=850_000_000, segment_id=p_seg.id,
    ), **actor)
    snap = claims.add_item(db, AddClaimItemInput(
        case_id=case_id, group_id=gid, title="违约金", category="liquidated_damages",
        amount_cents=63_750_000, segment_id=t_seg.id,
    ), **actor)
    claims.add_item(db, AddClaimItemInput(
        case_id=case_id, group_id=gid, title="备忘款", category="expense",
        amount_cents=100_000, segment_id=txt_segs[1].id,
    ), **actor)

    table = export.chart_citation_table(
        db, case_id, export.CitationRequest(chart_type="claims", target_id=gid, profile="court")
    )
    locs = {row.assertion: row.location for row in table.rows}
    assert any("第1段" == loc for k, loc in locs.items() if "本金" in k)
    assert any("表格 R0C1" == loc for k, loc in locs.items() if "违约金" in k)
    assert any("第4-5行" == loc for k, loc in locs.items() if "备忘款" in k)


def test_cli_save_analysis_accepts_content_file_alone(db, store, tmp_path, test_database):
    built, _ = _run_parse(db, store, name="底稿.txt", content="TEST-ONLY 留痕正文".encode())
    seg = db.query(models.Segment).filter(models.Segment.case_id == built["case_id"]).first()
    md_file = tmp_path / "analysis_report.md"
    md_file.write_text("# 非标表格全量汇总\n\n总计 6,625,334.70 元", encoding="utf-8")

    env = os.environ.copy()
    env["LWB_MIGRATION_URL"] = test_database[0]
    env["LWB_DATABASE_URL"] = test_database[1]
    env["LWB_DATA_ROOT"] = str(store.root)

    def run_cli(*args: str):
        return subprocess.run(
            [sys.executable, "-m", "legal_workbench.cli", *args],
            cwd=BACKEND, env=env, capture_output=True, text=True, timeout=30,
        )

    ok_res = run_cli(
        "save-analysis",
        "--case-id", str(built["case_id"]),
        "--title", "文件传入分析",
        "--content-file", str(md_file),
        "--statement-kind", "agent_analysis",
        "--source-ref", str(seg.id),
    )
    assert ok_res.returncode == 0, ok_res.stderr
    payload = json.loads(ok_res.stdout)
    saved = analysis.get_analysis_version(
        db, case_id=built["case_id"],
        analysis_id=uuid.UUID(payload["analysis_id"]),
        version_id=uuid.UUID(payload["version_id"]),
    )
    assert "6,625,334.70" in saved.content

    both_err = run_cli(
        "save-analysis",
        "--case-id", str(built["case_id"]),
        "--title", "冲突参数",
        "--content", "a",
        "--content-file", str(md_file),
        "--statement-kind", "agent_analysis",
    )
    assert both_err.returncode == 2

    neither_err = run_cli(
        "save-analysis",
        "--case-id", str(built["case_id"]),
        "--title", "缺少参数",
        "--statement-kind", "agent_analysis",
    )
    assert neither_err.returncode == 2

    missing_file_err = run_cli(
        "save-analysis",
        "--case-id", str(built["case_id"]),
        "--title", "缺失文件",
        "--content-file", str(tmp_path / "does_not_exist.md"),
        "--statement-kind", "agent_analysis",
    )
    assert missing_file_err.returncode == 2
    assert "invalid_input" in missing_file_err.stderr


def test_table_import_m2_existing_currency_none_and_l3_hk_usd_prefixes(db, store):
    xlsx = _xlsx_bytes({
        "往来明细": [
            ["付款方", "收款方", "金额", "币种"],
            ["甲公司", "乙公司", "HK$ 100.50", ""],
            ["甲公司", "乙公司", "US$ 200.00", ""],
            ["甲公司", "乙公司", "300.00", "CNY"],
            ["甲公司", "乙公司", "400.00", "USD"],
        ]
    })
    built = factories.build_material_with_storage(db, store, content=xlsx, name="M2与L3测试.xlsx")
    case_id = built["case_id"]
    actor = dict(actor="human", entry="local-web")
    ws = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="M2图"), **actor)
    gid = ws.graphs[0].id
    from legal_workbench.schemas import AddEntityInput, AddGraphRelationInput
    ws = graph.add_entity(db, AddEntityInput(case_id=case_id, kind="org", name="甲公司", graph_id=gid), **actor)
    eid_a = next(e.id for e in ws.entities if e.name == "甲公司")
    ws = graph.add_entity(db, AddEntityInput(case_id=case_id, kind="org", name="乙公司", graph_id=gid), **actor)
    eid_b = next(e.id for e in ws.entities if e.name == "乙公司")
    # 存量无币种关系 (attrs={})
    graph.add_relation(db, AddGraphRelationInput(
        case_id=case_id, from_entity_id=eid_a, to_entity_id=eid_b,
        kind="fund_flow", credibility="suspected", graph_id=gid,
    ), **actor)

    compiled = tgi.compile_table_relations(
        db, store, case_id,
        tgi.TableCompileInput(
            material_id=built["material_id"],
            graph_id=gid,
            sheet_name="往来明细",
            header_row=1,
            from_col=0,
            to_col=1,
            amount_col=2,
            currency_col=3,
            aggregate_by_pair=True,
        ),
    )
    assert {(e.currency, e.amount_cents) for e in compiled.compiled_edges} == {
        ("HKD", 10050), ("USD", 60000), ("CNY", 30000),
    }

    committed = tgi.commit_table_relations(
        db, store, case_id,
        tgi.TableCommitInput(
            material_id=built["material_id"],
            graph_id=gid,
            sheet_name="往来明细",
            header_row=1,
            from_col=0,
            to_col=1,
            amount_col=2,
            currency_col=3,
            aggregate_by_pair=True,
            request_key="m2-existing-none",
        ),
    )
    # 存量无币种关系挂接首条汇总边后，其余两币种汇总边（第3、4、5行）必须全部报入 currency_conflict_same_pair
    assert len(committed.skipped_rows) == 3
    assert {s.row_number for s in committed.skipped_rows} == {3, 4, 5}
    assert all(s.reason == "currency_conflict_same_pair" for s in committed.skipped_rows)


def test_search_m3_does_not_misextract_year_or_receipt_id_adjacent_to_currency(db, store):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "对账"
    ws.append(["年度", "回单号", "币种", "金额", "说明"])
    ws.append([2025, 100001, "CNY", Decimal("50000.25"), "不误抽2025或100001"])
    ws.append([2025, "USD", 50000, "整数年份相邻时优先50000"])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()

    built, status = _run_parse(db, store, name="M3防误抽.xlsx", content=buf.getvalue())
    assert status == "complete"
    case_id = built["case_id"]

    # 2025 和 100001 不得被抽成金额
    assert search.search_case(db, case_id=case_id, query="2025", mode=SearchMode.AMOUNT, currency="CNY").total == 0
    assert search.search_case(db, case_id=case_id, query="100001", mode=SearchMode.AMOUNT, currency="CNY").total == 0
    assert search.search_case(db, case_id=case_id, query="2025", mode=SearchMode.AMOUNT, currency="USD").total == 0
    # 100001 未被 spans 遮蔽，仍可用 identifier 检索到
    assert search.search_case(db, case_id=case_id, query="100001", mode=SearchMode.IDENTIFIER).total == 1
    assert search.search_case(db, case_id=case_id, query="50000.25", mode=SearchMode.AMOUNT, currency="CNY").total == 1
    assert search.search_case(db, case_id=case_id, query="50000", mode=SearchMode.AMOUNT, currency="USD").total == 1

    # L4: 带公式缓存 => 的币种单元格与金额单元格可正常提取并检索
    assert search._parse_tabular_currency("=C2 => CNY") == "CNY"
    header_seg = next(
        s for s in db.query(models.Segment).filter(models.Segment.case_id == case_id).all()
        if "年度" in s.text
    )
    header_seg.text = "A1: =B1 => CNY\nB1: =SUM(C1:D1) => 12345.67"
    db.flush()
    search.extract_exact_values(db, header_seg)
    assert search.search_case(db, case_id=case_id, query="12345.67", mode=SearchMode.AMOUNT, currency="CNY").total == 1


def test_chart_export_l1_handles_malformed_locator_values_safely():
    assert export._format_source_location(None, {"paragraph": "not-an-int"}) == ""
    assert export._format_source_location(None, {"paragraph": True}) == ""
    assert export._format_source_location(None, {"line_start": [], "line_end": {}}) == ""
    assert export._format_source_location(None, {"frame": "2"}) == "第3帧"
