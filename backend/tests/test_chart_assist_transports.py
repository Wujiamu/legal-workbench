"""HTTP, CLI and MCP transports for chart export, graph assist, coverage matrix and table import."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from legal_workbench import evidence_catalog, graph, mcp_server, models
from legal_workbench.db import get_session_factory
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (
    AddEntityInput, AddEvidenceBindingInput, AddEvidenceGroupInput, AddEvidenceItemInput,
    AddGraphRelationInput, AddSourceInput, CreateGraphInput, CreateSnapshotInput, SourceRefInput,
)

from . import factories
from .graph_factories import build_segment
from .test_api import new_case
from .test_claims import _group, _item

# 访问围栏由 test_agent_access_gate.py 覆盖；本文件显式放开以验证工具业务行为。
pytestmark = pytest.mark.usefixtures("agent_access_open")

BACKEND_DIR = Path(__file__).resolve().parents[1]


@contextmanager
def _committed():
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    finally:
        session.close()


def _cli(test_database, data_root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "LWB_DATABASE_URL": test_database[1],
        "LWB_MIGRATION_URL": test_database[0],
        "LWB_DATA_ROOT": str(data_root),
        "PYTHONPATH": str(BACKEND_DIR / "src"),
    }
    return subprocess.run(
        [sys.executable, "-m", "legal_workbench.cli", *args],
        cwd=BACKEND_DIR, capture_output=True, text=True, env=env,
    )


def test_chart_export_http_cli_and_mcp_round_trip(client, db, test_database, tmp_path):
    case_id = new_case(client, "TEST-ONLY 图表导出入口")
    case_uuid = uuid.UUID(case_id)
    foreign_id = new_case(client, "TEST-ONLY 他案")

    with _committed() as session:
        mat = factories.build_material(session, case_id=case_uuid, relative_source="虚构/主合同.txt")
        seg_id = build_segment(
            session, case_id=case_uuid, material_id=mat["material_id"],
            file_version_id=mat["file_version_id"],
            text="TEST-ONLY 甲公司向乙公司出借人民币500万元，丙公司承担连带保证责任。",
            page=2,
        )
        ws = graph.create_graph(session, CreateGraphInput(case_id=case_uuid, title="主图"),
                                actor="human", entry="local-web")
        graph_id = ws.graphs[0].id
        ws = graph.add_entity(session, AddEntityInput(case_id=case_uuid, kind="org", name="甲公司",
                                                      graph_id=graph_id),
                              actor="human", entry="local-web")
        a_id = next(e.id for e in ws.entities if e.name == "甲公司")
        ws = graph.add_entity(session, AddEntityInput(case_id=case_uuid, kind="org", name="乙公司",
                                                      graph_id=graph_id),
                              actor="human", entry="local-web")
        b_id = next(e.id for e in ws.entities if e.name == "乙公司")
        ws = graph.add_relation(
            session,
            AddGraphRelationInput(
                case_id=case_uuid, from_entity_id=a_id, to_entity_id=b_id,
                kind="debt", credibility="suspected", label="借款500万元", graph_id=graph_id,
            ),
            actor="human", entry="local-web",
        )
        rel_id = ws.relations[0].id
        graph.add_source(
            session,
            AddSourceInput(case_id=case_uuid, relation_id=rel_id,
                           ref=SourceRefInput(segment_id=seg_id), proves="借贷事实"),
            actor="human", entry="local-web",
        )
        ws = graph.create_snapshot(
            session,
            CreateSnapshotInput(case_id=case_uuid, graph_id=graph_id, stage_label="起诉前"),
            actor="human", entry="local-web",
        )
        snap_id = ws.snapshots[0].id
        cat = evidence_catalog.add_group(
            session, AddEvidenceGroupInput(case_id=case_uuid, title="第一组"),
            actor="human", entry="local-web",
        )
        cat = evidence_catalog.add_item(
            session, AddEvidenceItemInput(case_id=case_uuid, group_id=cat.groups[0].id, title="借款合同"),
            actor="human", entry="local-web",
        )
        evidence_catalog.add_binding(
            session, AddEvidenceBindingInput(case_id=case_uuid, item_id=cat.items[0].id,
                                             material_id=mat["material_id"]),
            actor="human", entry="local-web",
        )
        grp = _group(session, case_uuid).groups[0]
        _item(session, case_uuid, grp.id, 500_000_00, segment_id=seg_id)

    # 1. HTTP preflight blocks suspected_in_court unless excluded or waived
    preflight = client.post(f"/api/v1/cases/{case_id}/charts/preflight", json={
        "chart_type": "graph", "target_id": str(graph_id),
        "profile": "court", "snapshot_id": str(snap_id),
    })
    assert preflight.status_code == 200, preflight.text
    issues = preflight.json()["issues"]
    assert any(i["code"] == "suspected_in_court" and i["severity"] == "blocker" for i in issues)

    blocked = client.post(f"/api/v1/cases/{case_id}/charts/citation-table", json={
        "chart_type": "graph", "target_id": str(graph_id),
        "profile": "court", "snapshot_id": str(snap_id), "format": "json",
    })
    assert blocked.status_code == 409

    md_resp = client.post(f"/api/v1/cases/{case_id}/charts/citation-table", json={
        "chart_type": "graph", "target_id": str(graph_id),
        "profile": "court", "snapshot_id": str(snap_id), "format": "md",
        "waivers": [{"code": "suspected_in_court", "target_id": str(rel_id), "reason": "TEST-ONLY 待举证说明"}],
    })
    assert md_resp.status_code == 200
    assert md_resp.headers["content-type"].startswith("text/markdown")
    assert "证据1-1" in md_resp.text and "TEST-ONLY 待举证说明" in md_resp.text

    csv_resp = client.post(f"/api/v1/cases/{case_id}/charts/citation-table", json={
        "chart_type": "claims", "target_id": str(grp.id), "profile": "court", "format": "csv",
    })
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "证据1-1" in csv_resp.text

    bundle_resp = client.get(
        f"/api/v1/cases/{case_id}/charts/audit-bundle",
        params={"chart_type": "graph", "target_id": str(graph_id),
                "profile": "court", "snapshot_id": str(snap_id)},
    )
    assert bundle_resp.status_code == 200
    assert "甲公司向乙公司出借人民币500万元" in bundle_resp.text

    # 2. CLI & MCP export-chart-audit-bundle for graph & claims + cross-case guard
    cli_graph = _cli(
        test_database, tmp_path / "data",
        "export-chart-audit-bundle", "--case-id", case_id,
        "--chart-type", "graph", "--target-id", str(graph_id),
        "--profile", "court", "--snapshot-id", str(snap_id),
    )
    assert cli_graph.returncode == 0, cli_graph.stderr
    cli_data = json.loads(cli_graph.stdout)
    assert cli_data["chart_meta"]["snapshot_version"] == 1
    assert any(a["sources"] and "500万元" in (a["sources"][0]["context"] or "")
               for a in cli_data["assertions"])

    cli_claims = _cli(
        test_database, tmp_path / "data",
        "export-chart-audit-bundle", "--case-id", case_id,
        "--chart-type", "claims", "--target-id", str(grp.id), "--profile", "court",
    )
    assert cli_claims.returncode == 0, cli_claims.stderr
    assert json.loads(cli_claims.stdout)["assertions"][0]["amount_cents"] == 500_000_00

    tools = asyncio.run(mcp_server.server.list_tools())
    assert "export-chart-audit-bundle" in {t.name for t in tools}
    mcp_claims = mcp_server.export_chart_audit_bundle(case_id, "claims", str(grp.id), "court")
    assert mcp_claims["assertions"][0]["sources"][0]["evidence_labels"] == ["证据1-1 第2页"]

    with pytest.raises(WorkbenchError) as err:
        mcp_server.export_chart_audit_bundle(foreign_id, "claims", str(grp.id), "court")
    assert err.value.code == ErrorCode.CASE_MISMATCH


def test_assist_audit_coverage_and_table_import_http(client, api, store, tmp_path):
    case_id = new_case(client, "TEST-ONLY 辅助与表格HTTP")
    case_uuid = uuid.UUID(case_id)
    foreign_id = new_case(client, "TEST-ONLY 隔离他案")

    csv_bytes = (
        "付款方,收款方,方向,金额,日期,摘要\n"
        "海天建设有限公司,星河建材有限公司,支出,1234.56,2026-01-10,货款\n"
        "海天建设有限公司,星河建材有限公司,支出,bad_amount,2026-01-11,异常行\n"
    ).encode("utf-8")

    with _committed() as session:
        mat = factories.build_material(
            session, case_id=case_uuid, relative_source="虚构/起诉状.txt",
        )
        session.get(models.Material, mat["material_id"]).category = "诉状"
        seg_id = build_segment(
            session, case_id=case_uuid, material_id=mat["material_id"],
            file_version_id=mat["file_version_id"],
            text="原告：海天建设有限公司\n被告：星河建材有限公司（以下简称“星河建材”）\n海天建设有限公司联系电话13812345678。",
            page=1,
        )
        # Ensure active_parse_run_id is set on FileVersion so search/assist queries see it
        seg_row = session.get(models.Segment, seg_id)
        fv = session.get(models.FileVersion, mat["file_version_id"])
        fv.active_parse_run_id = seg_row.parse_run_id

        table_mat = factories.build_material_with_storage(
            session, store, case_id=case_uuid, content=csv_bytes, name="银行流水.csv",
        )
        session.get(models.Material, table_mat["material_id"]).category = "财务凭证"
        graph.create_graph(session, CreateGraphInput(case_id=case_uuid, title="流水图"),
                           actor="human", entry="local-web")

    # Coverage matrix
    cov_resp = client.get(f"/api/v1/cases/{case_id}/materials/coverage-matrix")
    assert cov_resp.status_code == 200, cov_resp.text
    cov_body = cov_resp.json()
    assert cov_body["totals"]["total"] == 2

    # Audit & entity candidates
    audit_resp = client.get(f"/api/v1/cases/{case_id}/graphs/assist/audit", params={"min_mentions": 1})
    assert audit_resp.status_code == 200, audit_resp.text
    assert any(o["name"] == "海天建设有限公司" for o in audit_resp.json()["orphan_orgs"])

    cand_resp = client.get(f"/api/v1/cases/{case_id}/graphs/assist/entity-candidates")
    assert cand_resp.status_code == 200, cand_resp.text
    names = {c["name"]: c for c in cand_resp.json()["candidates"]}
    assert "星河建材有限公司" in names
    assert "星河建材" in names["星河建材有限公司"]["candidate_aliases"]

    # Table import upload preview + material preview + compile + commit
    up_resp = client.post(
        f"/api/v1/cases/{case_id}/graphs/import-table/preview-upload",
        data={"header_row": "1"},
        files={"file": ("流水.csv", csv_bytes, "text/csv")},
    )
    assert up_resp.status_code == 200, up_resp.text
    assert up_resp.json()["headers"] == ["付款方", "收款方", "方向", "金额", "日期", "摘要"]

    prev_resp = client.post(f"/api/v1/cases/{case_id}/graphs/import-table/preview", json={
        "material_id": str(table_mat["material_id"]), "header_row": 1,
    })
    assert prev_resp.status_code == 200, prev_resp.text

    comp_resp = client.post(f"/api/v1/cases/{case_id}/graphs/import-table/compile", json={
        "material_id": str(table_mat["material_id"]),
        "from_col": 0, "to_col": 1, "direction_col": 2,
        "amount_col": 3, "date_col": 4, "relation_kind": "fund_flow",
    })
    assert comp_resp.status_code == 200, comp_resp.text
    comp = comp_resp.json()
    assert len(comp["compiled_edges"]) == 1
    assert comp["compiled_edges"][0]["amount_cents"] == 123456
    assert len(comp["skipped_rows"]) == 1 and comp["skipped_rows"][0]["row_number"] == 3

    ws_before = client.get(f"/api/v1/cases/{case_id}/graphs").json()
    graph_id = ws_before["graphs"][0]["id"]

    # Origin guard blocks write with foreign Origin
    bad_origin = client.post(
        f"/api/v1/cases/{case_id}/graphs/import-table/commit",
        headers={"Origin": "https://foreign.example"},
        json={
            "material_id": str(table_mat["material_id"]), "graph_id": graph_id,
            "from_col": 0, "to_col": 1, "amount_col": 3, "relation_kind": "fund_flow",
        },
    )
    assert bad_origin.status_code == 403

    # Cross-case material_id rejected
    cross = client.post(f"/api/v1/cases/{foreign_id}/graphs/import-table/preview", json={
        "material_id": str(table_mat["material_id"]),
    })
    assert cross.status_code == 404 and cross.json()["code"] == "case_mismatch"

    commit_resp = client.post(f"/api/v1/cases/{case_id}/graphs/import-table/commit", json={
        "material_id": str(table_mat["material_id"]), "graph_id": graph_id,
        "from_col": 0, "to_col": 1, "direction_col": 2,
        "amount_col": 3, "date_col": 4, "relation_kind": "fund_flow",
    })
    assert commit_resp.status_code == 200, commit_resp.text
    c_body = commit_resp.json()
    assert c_body["created_entities"] == 2 and c_body["created_relations"] == 1
    assert c_body["workspace"]["relations"][0]["credibility"] == "evidenced"

    # Archived case rejects commit
    ov = client.get(f"/api/v1/cases/{case_id}").json()
    arch = client.post(f"/api/v1/cases/{case_id}/archive",
                       json={"case_id": case_id, "expected_revision": ov["revision"]})
    assert arch.status_code == 200
    arch_commit = client.post(f"/api/v1/cases/{case_id}/graphs/import-table/commit", json={
        "material_id": str(table_mat["material_id"]), "graph_id": graph_id,
        "from_col": 0, "to_col": 1, "amount_col": 3, "relation_kind": "fund_flow",
    })
    assert arch_commit.status_code == 422

    # Cross-case HTTP 404 checks for charts / assist / coverage-matrix / import-table commit
    cross_preflight = client.post(f"/api/v1/cases/{foreign_id}/charts/preflight", json={
        "chart_type": "graph", "target_id": graph_id, "profile": "client",
    })
    assert cross_preflight.status_code == 404 and cross_preflight.json()["code"] == "case_mismatch"
    cross_cov = client.get(f"/api/v1/cases/{foreign_id}/materials/coverage-matrix", params={"graph_id": graph_id})
    assert cross_cov.status_code == 404 and cross_cov.json()["code"] == "case_mismatch"
    cross_audit = client.get(f"/api/v1/cases/{foreign_id}/graphs/assist/audit", params={"graph_id": graph_id})
    assert cross_audit.status_code == 404 and cross_audit.json()["code"] == "case_mismatch"
    cross_commit = client.post(f"/api/v1/cases/{foreign_id}/graphs/import-table/commit", json={
        "material_id": str(table_mat["material_id"]), "graph_id": graph_id,
        "from_col": 0, "to_col": 1, "amount_col": 3, "relation_kind": "fund_flow",
    })
    assert cross_commit.status_code == 404 and cross_commit.json()["code"] == "case_mismatch"
