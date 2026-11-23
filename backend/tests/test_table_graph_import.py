"""Tests for deterministic table column mapping and relation-graph import (Wave 2B)."""
from __future__ import annotations

import io
from datetime import date

import openpyxl
import pytest
from sqlalchemy import select

from legal_workbench import cases, graph, models, table_graph_import as tgi
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (
    AddAliasInput,
    AddEntityInput,
    AddGraphRelationInput,
    ArchiveCaseInput,
    CreateGraphInput,
    GraphAcceptAllInput,
)

from .factories import build_material_with_storage, create_case


def _codes(fn):
    with pytest.raises(WorkbenchError) as exc:
        fn()
    return exc.value.code


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


def _setup_case_graph(db, store, *, content: bytes, filename: str):
    mat = build_material_with_storage(db, store, content=content, name=filename)
    case_id = mat["case_id"]
    ws = graph.create_graph(
        db,
        CreateGraphInput(case_id=case_id, title="TEST-ONLY 资金流水图"),
        actor="human",
        entry="local-web",
    )
    return case_id, mat["material_id"], ws.graphs[0].id


def test_preview_xlsx_multisheet_and_csv_encodings(db, store):
    rows_sheet2 = [
        ["TEST-ONLY 某银行账户明细"],
        ["交易日期", "", "方向", "金额", "备注"],
        *[
            [f"2026-01-{(i % 28) + 1:02d}", f"TEST-ONLY 对方{i}有限公司", "支出", f"{100 + i}.50", f"摘要{i}"]
            for i in range(1, 26)
        ],
    ]
    xlsx_data = _xlsx_bytes({
        "封面": [],
        "银行流水": rows_sheet2,
        "汇总": [["项目", ""], ["合计", 999]],
    })
    case_id, mat_id, _ = _setup_case_graph(db, store, content=xlsx_data, filename="TEST-ONLY流水.xlsx")

    # Auto-picks first non-empty sheet ("银行流水") and inspects header_row=2
    preview = tgi.preview_table_columns(
        db, store, case_id, material_id=mat_id, header_row=2,
    )
    assert preview.sheets == ["封面", "银行流水", "汇总"]
    assert preview.selected_sheet == "银行流水"
    assert preview.header_row == 2
    assert preview.headers == ["交易日期", "列2", "方向", "金额", "备注"]
    assert preview.total_rows == 25
    assert len(preview.sample_rows) == 20
    assert preview.sample_rows[0][1] == "TEST-ONLY 对方1有限公司"

    # Explicit sheet selection via raw bytes (blank col 2 header becomes 列2 from data row width)
    preview_summary = tgi.preview_table_columns(
        db, store, case_id, file_bytes=xlsx_data, filename="raw.xlsx", sheet_name="汇总", header_row=1,
    )
    assert preview_summary.selected_sheet == "汇总"
    assert preview_summary.headers == ["项目", "列2"]
    assert preview_summary.sample_rows == [["合计", "999"]]
    assert preview_summary.total_rows == 1

    # UTF-8 with BOM CSV
    csv_utf8 = ("\ufeff日期,对方名称,金额\n2026-03-01,TEST-ONLY 甲公司,123.45\n").encode("utf-8")
    mat_utf8 = build_material_with_storage(db, store, case_id=case_id, content=csv_utf8, name="utf8.csv")
    out_utf8 = tgi.preview_table_columns(db, store, case_id, material_id=mat_utf8["material_id"])
    assert out_utf8.sheets == ["CSV"]
    assert out_utf8.selected_sheet == "CSV"
    assert out_utf8.headers == ["日期", "对方名称", "金额"]
    assert out_utf8.sample_rows == [["2026-03-01", "TEST-ONLY 甲公司", "123.45"]]

    # GB18030 CSV
    csv_gb = "日期,对方名称,金额\n2026-03-02,TEST-ONLY 乙公司,¥500.00\n".encode("gb18030")
    mat_gb = build_material_with_storage(db, store, case_id=case_id, content=csv_gb, name="gb.csv")
    out_gb = tgi.preview_table_columns(db, store, case_id, material_id=mat_gb["material_id"])
    assert out_gb.selected_sheet == "CSV"
    assert out_gb.sample_rows == [["2026-03-02", "TEST-ONLY 乙公司", "¥500.00"]]

    # Unknown sheet name rejected
    assert _codes(lambda: tgi.preview_table_columns(
        db, store, case_id, material_id=mat_id, sheet_name="不存在的工作表",
    )) == ErrorCode.INVALID_INPUT


def test_preview_rejects_oversize_corrupt_and_integrity_failures(db, store):
    case_id = create_case(db)
    db.commit()

    # > 10 MB rejected
    oversize = b"a,b\n" + b"1,2\n" * (3 * 1024 * 1024)
    assert len(oversize) > tgi.MAX_IMPORT_BYTES
    assert _codes(lambda: tgi.preview_table_columns(
        db, store, case_id, file_bytes=oversize, filename="big.csv",
    )) == ErrorCode.INVALID_INPUT

    # Unsupported suffix
    assert _codes(lambda: tgi.preview_table_columns(
        db, store, case_id, file_bytes=b"hello", filename="bad.txt",
    )) == ErrorCode.UNSUPPORTED_FORMAT

    # Corrupt XLSX -> PARSE_FAILED
    assert _codes(lambda: tgi.preview_table_columns(
        db, store, case_id, file_bytes=b"not a zip xlsx", filename="broken.xlsx",
    )) == ErrorCode.PARSE_FAILED

    # Invalid binary bytes claiming .csv -> PARSE_FAILED
    assert _codes(lambda: tgi.preview_table_columns(
        db, store, case_id, file_bytes=b"\x81\x00\xff\xfe", filename="bin.csv",
    )) == ErrorCode.PARSE_FAILED

    # Integrity failure (stored status & tampered bytes) -> INTEGRITY_ERROR
    mat = build_material_with_storage(
        db, store, case_id=case_id, content="日期,对方,金额\n2026-01-01,甲,100\n".encode("utf-8"), name="ok.csv",
    )
    stored_obj = db.get(models.StoredObject, mat["stored_object_id"])
    stored_obj.integrity_status = "integrity_error"
    db.commit()
    assert _codes(lambda: tgi.preview_table_columns(
        db, store, case_id, material_id=mat["material_id"],
    )) == ErrorCode.INTEGRITY_ERROR

    stored_obj.integrity_status = "ok"
    db.commit()
    store.managed_path(mat["relative_path"]).write_bytes(b"tampered")
    assert _codes(lambda: tgi.preview_table_columns(
        db, store, case_id, material_id=mat["material_id"],
    )) == ErrorCode.INTEGRITY_ERROR


def test_compile_bank_statement_directions_decimals_skipped_and_filter(db, store):
    csv_text = "\n".join([
        "TEST-ONLY 对账单导出",
        "交易日期,对方户名,收支方向,交易金额,摘要",
        "2026-01-05,TEST-ONLY 乙公司,支出,¥0.01,货款一",
        "2026-01-06,TEST-ONLY 乙公司,out,RMB 0.02元,货款二",
        "2026-01-10,TEST-ONLY 乙公司,debit,\"￥１，２３４．５６\",货款三",
        "2026-01-12,TEST-ONLY 丙银行,收入,500.005,贷款发放",
        "2026-01-15,TEST-ONLY 张三,支出,-250.25,退款冲正",
        "2026-01-18,TEST-ONLY 错账方,支出,待核实金额,异常行",
        "2026-01-19,,支出,100.00,缺少对方",
        "2026-01-20,甲公司别名,支出,88.00,别名自转",
        "2026-01-21,TEST-ONLY 乙公司,转入,100.00,转入测试",
        "2026-01-22,TEST-ONLY 错账方二,支出,NaN,非法数值",
        "2026-01-23,TEST-ONLY 错账方三,支出,100万元,非标单位",
    ])
    case_id, mat_id, graph_id = _setup_case_graph(
        db, store, content=csv_text.encode("utf-8-sig"), filename="bank.csv",
    )
    ws = graph.add_entity(
        db,
        AddEntityInput(case_id=case_id, kind="org", name="TEST-ONLY 甲公司"),
        actor="human",
        entry="local-web",
    )
    jia = next(e for e in ws.entities if e.name == "TEST-ONLY 甲公司")
    graph.add_alias(
        db,
        AddAliasInput(case_id=case_id, entity_id=jia.id, alias="甲公司别名"),
        actor="human",
        entry="local-web",
    )

    compiled = tgi.compile_table_relations(
        db,
        store,
        case_id,
        tgi.TableCompileInput(
            material_id=mat_id,
            graph_id=graph_id,
            header_row=2,
            from_constant="TEST-ONLY 甲公司",
            to_col=1,
            direction_col=2,
            amount_col=3,
            date_col=0,
            label_col=4,
            currency="CNY",
        ),
    )

    assert compiled.processed_rows == 11
    assert compiled.selected_sheet == "CSV"

    # Skipped rows with exact 1-based row numbers; skipped parties never leak into new_entities
    assert [(s.row_number, s.reason) for s in compiled.skipped_rows] == [
        (8, "invalid_amount"),
        (9, "missing_target_entity"),
        (10, "same_source_and_target"),
        (12, "invalid_amount"),
        (13, "invalid_amount"),
    ]
    assert compiled.skipped_rows[0].raw_row[3] == "待核实金额"

    new_names = {e.name: e.entity_kind for e in compiled.new_entities}
    assert new_names == {
        "TEST-ONLY 乙公司": "org",
        "TEST-ONLY 丙银行": "org",
        "TEST-ONLY 张三": "person",
    }
    assert "TEST-ONLY 错账方" not in new_names
    assert "TEST-ONLY 错账方二" not in new_names

    edges = {(e.from_name, e.to_name): e for e in compiled.compiled_edges}
    # 0.01 + 0.02 + 1234.56 = 1234.59 -> exact 123459 cents
    out_to_yi = edges[("TEST-ONLY 甲公司", "TEST-ONLY 乙公司")]
    assert out_to_yi.amount_cents == 123459
    assert out_to_yi.currency == "CNY"
    assert out_to_yi.tx_count == 3
    assert out_to_yi.row_numbers == [3, 4, 5]
    assert out_to_yi.date_start == "2026-01-05"
    assert out_to_yi.date_end == "2026-01-10"
    assert out_to_yi.proves == "CSV 第3-5行（共3笔）"
    assert out_to_yi.from_entity_id == jia.id
    assert out_to_yi.to_entity_id is None

    # 500.005 rounds HALF_UP to 500.01 -> 50001 cents; "收入" flips from/to
    bing_to_jia = edges[("TEST-ONLY 丙银行", "TEST-ONLY 甲公司")]
    assert bing_to_jia.amount_cents == 50001
    assert bing_to_jia.proves == "CSV 第6行"

    # Negative amount flips from/to
    san_to_jia = edges[("TEST-ONLY 张三", "TEST-ONLY 甲公司")]
    assert san_to_jia.amount_cents == 25025
    assert san_to_jia.tx_count == 1

    yi_to_jia = edges[("TEST-ONLY 乙公司", "TEST-ONLY 甲公司")]
    assert yi_to_jia.amount_cents == 10000
    assert yi_to_jia.row_numbers == [11]

    # filter_min_amount="300.00" keeps only rows >= 30000 cents (rows 5 and 6)
    filtered = tgi.compile_table_relations(
        db,
        store,
        case_id,
        tgi.TableCompileInput(
            material_id=mat_id,
            header_row=2,
            from_constant="TEST-ONLY 甲公司",
            to_col=1,
            direction_col=2,
            amount_col=3,
            date_col=0,
            filter_min_amount="300.00",
        ),
    )
    assert len(filtered.compiled_edges) == 2
    filtered_map = {(e.from_name, e.to_name): e.amount_cents for e in filtered.compiled_edges}
    assert filtered_map == {
        ("TEST-ONLY 甲公司", "TEST-ONLY 乙公司"): 123456,
        ("TEST-ONLY 丙银行", "TEST-ONLY 甲公司"): 50001,
    }
    assert {e.name for e in filtered.new_entities} == {"TEST-ONLY 乙公司", "TEST-ONLY 丙银行"}

    # Invalid compile inputs
    assert _codes(lambda: tgi.compile_table_relations(
        db, store, case_id, tgi.TableCompileInput(material_id=mat_id, to_col=1),
    )) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: tgi.compile_table_relations(
        db, store, case_id,
        tgi.TableCompileInput(material_id=mat_id, from_constant="A", to_col=1, relation_kind="bogus"),
    )) == ErrorCode.INVALID_INPUT


def test_compile_and_commit_share_pct_and_non_aggregated_mode(db, store):
    rows = [
        ["股东", "目标公司", "持股比例", "日期"],
        ["TEST-ONLY 股东甲", "TEST-ONLY 目标有限公司", "51.5%", "2026/02/01"],
        ["TEST-ONLY 股东乙", "TEST-ONLY 目标有限公司", "150%", "2026/02/01"],
        ["TEST-ONLY 股东丙", "TEST-ONLY 目标有限公司", "50%%", "2026/02/01"],
        ["TEST-ONLY 股东甲", "TEST-ONLY 目标有限公司", "48.5", "2026/02/05"],
    ]
    xlsx_data = _xlsx_bytes({"股权结构": rows})
    case_id, mat_id, graph_id = _setup_case_graph(db, store, content=xlsx_data, filename="equity.xlsx")

    commit_out = tgi.commit_table_relations(
        db,
        store,
        case_id,
        tgi.TableCommitInput(
            material_id=mat_id,
            graph_id=graph_id,
            from_col=0,
            to_col=1,
            share_pct_col=2,
            date_col=3,
            relation_kind="equity_control",
            aggregate_by_pair=False,
        ),
    )
    assert [(s.row_number, s.reason) for s in commit_out.skipped_rows] == [
        (3, "invalid_share_pct"),
        (4, "invalid_share_pct"),
    ]
    # Two rows for the same pair in non-aggregated mode create 1 relation without duplicate error
    assert commit_out.created_entities == 2
    assert commit_out.created_relations == 1
    assert commit_out.attached_sources == 1
    rel = commit_out.workspace.relations[0]
    assert rel.kind == "equity_control"
    assert rel.attrs.share_pct == 51.5
    assert rel.attrs.currency is None
    assert rel.credibility == "evidenced"


def test_commit_creates_evidenced_relations_attaches_existing_and_preserves_audit(db, store):
    xlsx_1 = _xlsx_bytes({
        "流水": [
            ["日期", "付款方", "收款方", "金额", "摘要"],
            ["2026-04-01", "TEST-ONLY 甲公司", "TEST-ONLY 乙公司", "¥1,000.00", "第一笔"],
            ["2026-04-03", "TEST-ONLY 甲公司", "TEST-ONLY 乙公司", "¥200.50", "第二笔"],
            ["2026-04-04", "TEST-ONLY 错行", "TEST-ONLY 乙公司", "bad_amt", "跳过"],
            ["2026-04-05", "TEST-ONLY 甲公司", "TEST-ONLY 乙公司", "¥50.00", "第三笔"],
            ["2026-04-06", "TEST-ONLY 乙公司", "TEST-ONLY 丙事务所", "500.00", "服务费"],
            ["2026-04-07", "TEST-ONLY 甲公司", "TEST-ONLY 丁法院", "300.00", "诉讼费"],
        ],
    })
    case_id, mat1_id, graph_id = _setup_case_graph(db, store, content=xlsx_1, filename="flow1.xlsx")

    ws = graph.add_entity(db, AddEntityInput(case_id=case_id, kind="org", name="TEST-ONLY 乙公司"),
                          actor="human", entry="local-web")
    yi = next(e for e in ws.entities if e.name == "TEST-ONLY 乙公司")
    ws = graph.add_entity(db, AddEntityInput(case_id=case_id, kind="org", name="TEST-ONLY 丙事务所"),
                          actor="human", entry="local-web")
    bing = next(e for e in ws.entities if e.name == "TEST-ONLY 丙事务所")
    ws = graph.add_entity(db, AddEntityInput(case_id=case_id, kind="org", name="TEST-ONLY 丁法院"),
                          actor="human", entry="local-web")
    ding = next(e for e in ws.entities if e.name == "TEST-ONLY 丁法院")
    ws = graph.add_entity(db, AddEntityInput(case_id=case_id, kind="org", name="TEST-ONLY 甲公司"),
                          actor="human", entry="local-web")
    jia = next(e for e in ws.entities if e.name == "TEST-ONLY 甲公司")

    # Pre-create suspected 乙->丙 and disputed 甲->丁 (disputed must stay disputed!)
    ws = graph.add_relation(
        db,
        AddGraphRelationInput(
            case_id=case_id, from_entity_id=yi.id, to_entity_id=bing.id,
            kind="fund_flow", credibility="suspected",
        ),
        actor="human", entry="local-web",
    )
    suspected_rel = ws.relations[0]
    ws = graph.add_relation(
        db,
        AddGraphRelationInput(
            case_id=case_id, from_entity_id=jia.id, to_entity_id=ding.id,
            kind="fund_flow", credibility="disputed",
        ),
        actor="human", entry="local-web",
    )
    disputed_rel = next(r for r in ws.relations if r.id != suspected_rel.id)
    seq_before = ws.seqs["graph"]

    # Add a new entity 戊公司 in second sheet? No, let's test new entity creation too via csv2 later;
    # here test existing entities + new relation + existing suspected/disputed relations!
    commit_out = tgi.commit_table_relations(
        db,
        store,
        case_id,
        tgi.TableCommitInput(
            material_id=mat1_id,
            graph_id=graph_id,
            from_col=1,
            to_col=2,
            amount_col=3,
            date_col=0,
            label_col=4,
            request_key="import-run-1",
        ),
    )

    assert commit_out.created_entities == 0
    assert commit_out.created_relations == 1  # 甲 -> 乙
    assert commit_out.attached_sources == 3  # 甲->乙 + 乙->丙 + 甲->丁
    assert len(commit_out.skipped_rows) == 1
    assert commit_out.skipped_rows[0].row_number == 4

    ws_after = commit_out.workspace
    assert ws_after.seqs["graph"] > seq_before

    new_rel = next(r for r in ws_after.relations if r.from_entity_id == jia.id and r.to_entity_id == yi.id)
    assert new_rel.credibility == "evidenced"
    assert new_rel.revision == 1
    assert new_rel.attrs.amount_cents == 125050
    assert new_rel.attrs.currency == "CNY"
    assert new_rel.attrs.date_start == date(2026, 4, 1)
    assert new_rel.attrs.date_end == date(2026, 4, 5)

    # Suspected relation upgraded to evidenced; disputed relation kept disputed!
    assert next(r for r in ws_after.relations if r.id == suspected_rel.id).credibility == "evidenced"
    assert next(r for r in ws_after.relations if r.id == disputed_rel.id).credibility == "disputed"

    assert {edge.relation_id for edge in ws_after.edges if edge.graph_id == graph_id} == {
        new_rel.id, suspected_rel.id, disputed_rel.id,
    }
    assert {n.entity_id for n in ws_after.nodes if n.graph_id == graph_id} == {jia.id, yi.id, bing.id, ding.id}

    sources_by_rel = {s.relation_id: s for s in ws_after.sources}
    assert sources_by_rel[new_rel.id].material_id == mat1_id
    # Non-contiguous rows 2, 3, 5 -> 第2-3、5行（共3笔）
    assert sources_by_rel[new_rel.id].proves == "流水 第2-3、5行（共3笔）"
    assert sources_by_rel[suspected_rel.id].proves == "流水 第6行"

    audit_ops = db.scalars(
        select(models.AuditEvent.operation).where(models.AuditEvent.case_id == case_id)
    ).all()
    assert "graph.add_relation" in audit_ops
    assert "graph.add_source" in audit_ops
    assert "graph.update_relation" in audit_ops
    assert "graph.add_to_graph" in audit_ops

    # Idempotent replay attaches nothing twice
    replay = tgi.commit_table_relations(
        db,
        store,
        case_id,
        tgi.TableCommitInput(
            material_id=mat1_id,
            graph_id=graph_id,
            from_col=1,
            to_col=2,
            amount_col=3,
            date_col=0,
            label_col=4,
            request_key="import-run-1",
        ),
    )
    assert replay.created_entities == 0
    assert replay.created_relations == 0
    assert replay.attached_sources == 0

    # A second material creating a new entity 戊公司 and confirming 甲->乙
    csv2 = (
        "付款方,收款方,金额\n"
        "TEST-ONLY 甲公司,TEST-ONLY 乙公司,1250.50\n"
        "TEST-ONLY 甲公司,TEST-ONLY 戊企业,600.00\n"
    ).encode("utf-8")
    mat2 = build_material_with_storage(db, store, case_id=case_id, content=csv2, name="receipt.csv")
    second_attach = tgi.commit_table_relations(
        db,
        store,
        case_id,
        tgi.TableCommitInput(
            material_id=mat2["material_id"],
            graph_id=graph_id,
            from_col=0,
            to_col=1,
            amount_col=2,
        ),
    )
    assert second_attach.created_entities == 1
    assert second_attach.created_relations == 1
    assert second_attach.attached_sources == 2
    assert len([s for s in second_attach.workspace.sources if s.relation_id == new_rel.id]) == 2


def test_agent_commit_creates_pending_changes_accepted_cleanly(db, store):
    csv_data = "付款方,收款方,金额\nTEST-ONLY 甲公司,TEST-ONLY 乙公司,300.00\n".encode("utf-8")
    case_id, mat_id, graph_id = _setup_case_graph(db, store, content=csv_data, filename="agent.csv")

    payload = tgi.TableCommitInput(
        material_id=mat_id, graph_id=graph_id, from_col=0, to_col=1, amount_col=2,
    )
    assert _codes(lambda: tgi.commit_table_relations(
        db, store, case_id, payload, actor="agent", entry="mcp",
    )) == ErrorCode.INVALID_INPUT

    out = tgi.commit_table_relations(
        db,
        store,
        case_id,
        payload.model_copy(update={"request_key": "agent-import-1"}),
        actor="agent",
        entry="mcp",
    )
    assert out.created_entities == 2
    assert out.created_relations == 1
    assert out.attached_sources == 1
    assert len(out.workspace.pending_changes) == 5

    accepted = graph.accept_all(
        db,
        GraphAcceptAllInput(
            case_id=case_id,
            change_ids=[c.id for c in out.workspace.pending_changes],
        ),
        actor="human",
        entry="local-web",
    )
    assert accepted.pending_changes == []
    assert accepted.relations[0].credibility == "evidenced"


def test_archived_case_rejects_commit_and_cross_case_raises_case_mismatch(db, store):
    csv_data = "付款方,收款方,金额\nTEST-ONLY 甲公司,TEST-ONLY 乙公司,100.00\n".encode("utf-8")
    case1_id, mat1_id, graph1_id = _setup_case_graph(db, store, content=csv_data, filename="c1.csv")
    _, mat2_id, graph2_id = _setup_case_graph(db, store, content=csv_data, filename="c2.csv")

    assert _codes(lambda: tgi.preview_table_columns(
        db, store, case1_id, material_id=mat2_id,
    )) == ErrorCode.CASE_MISMATCH
    assert _codes(lambda: tgi.compile_table_relations(
        db, store, case1_id, tgi.TableCompileInput(material_id=mat2_id, from_col=0, to_col=1),
    )) == ErrorCode.CASE_MISMATCH
    assert _codes(lambda: tgi.commit_table_relations(
        db, store, case1_id, tgi.TableCommitInput(material_id=mat2_id, graph_id=graph1_id, from_col=0, to_col=1),
    )) == ErrorCode.CASE_MISMATCH

    assert _codes(lambda: tgi.compile_table_relations(
        db, store, case1_id, tgi.TableCompileInput(material_id=mat1_id, graph_id=graph2_id, from_col=0, to_col=1),
    )) == ErrorCode.CASE_MISMATCH
    assert _codes(lambda: tgi.commit_table_relations(
        db, store, case1_id, tgi.TableCommitInput(material_id=mat1_id, graph_id=graph2_id, from_col=0, to_col=1),
    )) == ErrorCode.CASE_MISMATCH

    case_row = db.get(models.Case, case1_id)
    cases.archive_case(db, ArchiveCaseInput(case_id=case1_id, expected_revision=case_row.revision))
    assert tgi.preview_table_columns(db, store, case1_id, material_id=mat1_id).total_rows == 1
    with pytest.raises(WorkbenchError):
        tgi.commit_table_relations(
            db,
            store,
            case1_id,
            tgi.TableCommitInput(material_id=mat1_id, graph_id=graph1_id, from_col=0, to_col=1, amount_col=2),
        )
    # Even a file whose rows are all skipped must still reject commit on archived case
    bad_csv = "付款方,收款方,金额\n,乙,100\n".encode("utf-8")
    bad_mat = build_material_with_storage(db, store, case_id=case1_id, content=bad_csv, name="empty.csv")
    with pytest.raises(WorkbenchError):
        tgi.commit_table_relations(
            db,
            store,
            case1_id,
            tgi.TableCommitInput(material_id=bad_mat["material_id"], graph_id=graph1_id, from_col=0, to_col=1, amount_col=2),
        )
    ws = graph.get_workspace(db, case1_id)
    assert ws.entities == [] and ws.relations == []


def test_ac12_amount_overflow_ruled_out_retry_promotion_and_csv_delimiters(db, store):
    # 1. Extreme scientific notation / 30-digit serial cleanly becomes skipped_rows (no 500 crash)
    csv_semi = (
        "付款方;收款方;金额\r"
        "TEST-ONLY 甲公司;TEST-ONLY 乙公司;1e30\r"
        "TEST-ONLY 甲公司;TEST-ONLY 乙公司;123456789012345678901234567890\r"
        "TEST-ONLY 甲公司;TEST-ONLY 乙公司;500.00\r"
    ).encode("utf-8")
    case_id, mat_id, graph_id = _setup_case_graph(db, store, content=csv_semi, filename="semi.csv")
    prev = tgi.preview_table_columns(db, store, case_id, material_id=mat_id)
    assert prev.headers == ["付款方", "收款方", "金额"]
    comp = tgi.compile_table_relations(
        db, store, case_id,
        tgi.TableCompileInput(material_id=mat_id, from_col=0, to_col=1, amount_col=2),
    )
    assert len(comp.compiled_edges) == 1 and comp.compiled_edges[0].amount_cents == 50000
    assert [(r.row_number, r.reason) for r in comp.skipped_rows] == [
        (2, "invalid_amount"),
        (3, "invalid_amount"),
    ]

    # 2. Existing ruled_out relation must not be looked up / attached / re-added to canvas
    ws = graph.add_entity(db, AddEntityInput(case_id=case_id, kind="org", name="TEST-ONLY 甲公司"), actor="human", entry="local-web")
    ws = graph.add_entity(db, AddEntityInput(case_id=case_id, kind="org", name="TEST-ONLY 乙公司"), actor="human", entry="local-web")
    e_map = {e.name: e.id for e in ws.entities}
    ws = graph.add_relation(
        db,
        AddGraphRelationInput(
            case_id=case_id, from_entity_id=e_map["TEST-ONLY 甲公司"], to_entity_id=e_map["TEST-ONLY 乙公司"],
            kind="fund_flow", credibility="ruled_out", label="已排除旧流水",
        ),
        actor="human", entry="local-web",
    )
    ruled_id = ws.relations[0].id
    committed = tgi.commit_table_relations(
        db, store, case_id,
        tgi.TableCommitInput(material_id=mat_id, graph_id=graph_id, from_col=0, to_col=1, amount_col=2, request_key="k-ruled-1"),
    )
    assert committed.created_relations == 0 and committed.attached_sources == 0
    assert any(r.reason == "existing_ruled_out" for r in committed.skipped_rows)
    assert all(s.relation_id != ruled_id for s in committed.workspace.sources)

    # 3. Retry when source already attached on a suspected relation promotes suspected -> evidenced
    ws2 = graph.add_entity(db, AddEntityInput(case_id=case_id, kind="org", name="TEST-ONLY 丙公司"), actor="human", entry="local-web")
    e_map2 = {e.name: e.id for e in ws2.entities}
    ws2 = graph.add_relation(
        db,
        AddGraphRelationInput(
            case_id=case_id, from_entity_id=e_map["TEST-ONLY 甲公司"], to_entity_id=e_map2["TEST-ONLY 丙公司"],
            kind="fund_flow", credibility="suspected", label="半完成关系",
        ),
        actor="human", entry="local-web",
    )
    sus_rel = next(r for r in ws2.relations if r.to_entity_id == e_map2["TEST-ONLY 丙公司"])
    from legal_workbench.schemas import AddSourceInput, SourceRefInput
    csv2 = "付款方,收款方,金额\nTEST-ONLY 甲公司,TEST-ONLY 丙公司,200.00\n".encode("utf-8")
    mat2 = build_material_with_storage(db, store, case_id=case_id, content=csv2, name="retry.csv")["material_id"]
    graph.add_source(
        db,
        AddSourceInput(case_id=case_id, relation_id=sus_rel.id, ref=SourceRefInput(material_id=mat2), proves="半途出处"),
        actor="human", entry="local-web",
    )
    retried = tgi.commit_table_relations(
        db, store, case_id,
        tgi.TableCommitInput(material_id=mat2, graph_id=graph_id, from_col=0, to_col=1, amount_col=2),
    )
    promoted = next(r for r in retried.workspace.relations if r.id == sus_rel.id)
    assert promoted.credibility == "evidenced"
