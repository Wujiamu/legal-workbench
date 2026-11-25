"""P3 graph/evidence transports: HTTP (human), MCP and CLI (agent), same service.

Checks that the actor is fixed by the entry point, path and body identifiers must
agree, write commands return the full workspace, conflicts surface as 409 with
the shared error envelope, the Origin guard applies, agent writes are pending and
freeze/restore, consent and accept/revert stay human-only.  Real PostgreSQL;
TEST-ONLY synthetic data.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import uuid
from contextlib import contextmanager

import pytest

from legal_workbench import evidence_catalog, graph
from legal_workbench.db import get_session_factory
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (
    AddConsentInput, AddEvidenceBindingInput, AddEvidenceGroupInput, AddEvidenceItemInput,
    AddEntityInput, AddSourceInput, CreateGraphInput, CreateSnapshotInput,
    AddGraphRelationInput, ImportEvidenceCatalogInput, ImportCatalogRow, ImportBindingRow,
    RelationChanges, SourceRefInput, UpdateRelationInput,
)

from . import factories
from .test_api import new_case

# 访问围栏由 test_agent_access_gate.py 覆盖；本文件显式放开以验证工具业务行为。
pytestmark = pytest.mark.usefixtures("agent_access_open")


def _session():
    """A short-lived session; callers must use it as a context manager.

    A session left open with an uncommitted insert keeps a KEY SHARE lock on the
    parent ``cases`` row, which silently blocks every later case-level write
    (archive, guards) in the same test — so build-then-commit in one step.
    """
    return get_session_factory()()


@contextmanager
def _committed():
    session = _session()
    try:
        yield session
        session.commit()
    finally:
        session.close()


def _codes(fn):
    with pytest.raises(WorkbenchError) as exc:
        fn()
    return exc.value.code


def _workspace(client, case_id):
    response = client.get(f"/api/v1/cases/{case_id}/graphs")
    assert response.status_code == 200, response.text
    return response.json()


def _post(client, url, payload):
    response = client.post(url, json=payload)
    return response


def _entity(body, name):
    return next(item for item in body["entities"] if item["name"] == name)


def test_http_rules_workspace_and_origin_guard(client, tmp_path):
    rules = client.get("/api/v1/graph/rules")
    assert rules.status_code == 200
    body = rules.json()["rules"]
    assert {"evidenced", "suspected", "disputed", "ruled_out"} == {
        entry["value"] for entry in body["credibility"]}
    # Accept/revert/freeze are not in the agent whitelist.
    assert "create_snapshot" not in body["agent_commands"]
    assert "accept_change" not in body["agent_commands"]
    assert "add_entity" in body["agent_commands"]

    case_id = new_case(client, "TEST-ONLY 关系图")
    workspace = _workspace(client, case_id)
    assert workspace["case_id"] == case_id
    assert workspace["archived"] is False
    assert workspace["seqs"]["graph"] == 0
    assert workspace["evidence"]["numbers"] == {}

    # A write without the same-origin Origin header is refused (fresh client, no
    # Origin header: the `client` fixture sets one for every request).
    from fastapi.testclient import TestClient

    from legal_workbench.app import app
    from legal_workbench.storage import Storage

    app.state.storage = Storage(root=tmp_path / "data-origin")
    with TestClient(app, base_url="http://127.0.0.1:8765") as bare:
        resp = bare.post(f"/api/v1/cases/{case_id}/graph/entities",
                         json={"case_id": case_id, "kind": "person", "name": "甲"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"
    assert set(resp.json()) == {"code", "message", "details", "request_id"}


def test_http_entity_relation_source_and_downgrade(client):
    case_id = new_case(client, "TEST-ONLY 主体关系")
    created = _post(client, f"/api/v1/cases/{case_id}/graph/graphs",
                    {"case_id": case_id, "title": "股权结构"})
    assert created.status_code == 200, created.text
    graph_id = created.json()["graphs"][0]["id"]

    for name in ("甲公司", "乙公司"):
        resp = _post(client, f"/api/v1/cases/{case_id}/graph/entities",
                     {"case_id": case_id, "kind": "org", "name": name,
                      "roles": ["defendant"], "graph_id": graph_id})
        assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {item["name"] for item in body["entities"]} == {"甲公司", "乙公司"}
    assert len(body["nodes"]) == 2  # both entities joined the current graph
    assert body["entities"][0]["created_by"] == "human"
    assert body["pending_changes"] == []
    assert body["changes"][-1]["entry"] == "local-web"
    assert body["last_change_id"] == body["changes"][-1]["id"]

    source_entity = _entity(body, "甲公司")
    target_entity = _entity(body, "乙公司")
    relation = _post(client, f"/api/v1/cases/{case_id}/graph/relations", {
        "case_id": case_id, "from_entity_id": source_entity["id"],
        "to_entity_id": target_entity["id"], "kind": "equity_control",
        "credibility": "suspected", "graph_id": graph_id,
        "attrs": {"share_pct": 60}})
    assert relation.status_code == 200, relation.text
    rel = relation.json()["relations"][0]
    assert rel["credibility"] == "suspected"
    assert len(relation.json()["edges"]) == 1

    # "evidenced" without a source is rejected by the service, not the UI.
    resp = client.patch(f"/api/v1/cases/{case_id}/graph/relations/{rel['id']}", json={
        "case_id": case_id, "relation_id": rel["id"], "expected_revision": rel["revision"],
        "changes": {"credibility": "evidenced"}})
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_input"

    with _committed() as session:
        material = factories.build_material(session, case_id=uuid.UUID(case_id))
    source = _post(client, f"/api/v1/cases/{case_id}/graph/sources", {
        "case_id": case_id, "relation_id": rel["id"], "proves": "工商档案",
        "ref": {"material_id": str(material["material_id"])}})
    assert source.status_code == 200, source.text
    source_id = source.json()["sources"][0]["id"]
    # P4 needs a locatable reference, not just a label.
    label = source.json()["labels"][source_id]
    assert label["material_id"] == str(material["material_id"])
    assert label["file_version_id"] == str(material["file_version_id"])
    assert label["text"].endswith("（未编号）")

    resp = client.patch(f"/api/v1/cases/{case_id}/graph/relations/{rel['id']}", json={
        "case_id": case_id, "relation_id": rel["id"], "expected_revision": rel["revision"],
        "changes": {"credibility": "evidenced"}})
    assert resp.status_code == 200, resp.text
    rel = next(item for item in resp.json()["relations"] if item["id"] == rel["id"])
    assert rel["credibility"] == "evidenced"

    # Deleting the last confirmed source without a downgrade is refused.
    resp = _post(client, f"/api/v1/cases/{case_id}/graph/sources/{source_id}/remove",
                 {"case_id": case_id, "source_id": source_id})
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_input"
    resp = _post(client, f"/api/v1/cases/{case_id}/graph/sources/{source_id}/remove",
                 {"case_id": case_id, "source_id": source_id, "downgrade_to": "suspected"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["notice"] and "降级" in body["notice"]
    assert next(item for item in body["relations"] if item["id"] == rel["id"])["credibility"] == "suspected"


def test_http_path_body_mismatch_is_case_mismatch(client):
    a = new_case(client, "TEST-ONLY A")
    b = new_case(client, "TEST-ONLY B")
    resp = _post(client, f"/api/v1/cases/{a}/graph/entities",
                 {"case_id": b, "kind": "person", "name": "错案"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "case_mismatch"

    resp = _post(client, f"/api/v1/cases/{a}/graph/entities",
                 {"case_id": a, "kind": "person", "name": "甲"})
    entity_id = resp.json()["entities"][0]["id"]
    other = str(uuid.uuid4())
    resp = client.patch(f"/api/v1/cases/{a}/graph/entities/{other}", json={
        "case_id": a, "entity_id": entity_id, "expected_revision": 0,
        "changes": {"note": "x"}})
    assert resp.status_code == 404
    assert resp.json()["code"] == "case_mismatch"

    # A source must belong to the same case as the entity.
    with _committed() as session:
        foreign = factories.build_material(session, case_id=uuid.UUID(b))
    resp = _post(client, f"/api/v1/cases/{a}/graph/sources", {
        "case_id": a, "entity_id": entity_id, "proves": "跨案",
        "ref": {"material_id": str(foreign["material_id"])}})
    assert resp.status_code == 404
    assert resp.json()["code"] == "case_mismatch"


def test_http_conflict_and_case_seq_domains(client):
    case_id = new_case(client, "TEST-ONLY 冲突与序号")
    resp = _post(client, f"/api/v1/cases/{case_id}/graph/entities",
                 {"case_id": case_id, "kind": "person", "name": "甲"})
    entity = resp.json()["entities"][0]
    payload = {"case_id": case_id, "entity_id": entity["id"],
               "expected_revision": entity["revision"], "changes": {"note": "第一次"}}
    assert client.patch(f"/api/v1/cases/{case_id}/graph/entities/{entity['id']}",
                        json=payload).status_code == 200
    stale = client.patch(f"/api/v1/cases/{case_id}/graph/entities/{entity['id']}", json=payload)
    assert stale.status_code == 409
    assert stale.json()["code"] == "conflict"

    seqs = client.get(f"/api/v1/cases/{case_id}/changes/seq").json()["seqs"]
    assert set(seqs) == {"progress", "graph", "evidence", "claims"}
    assert seqs["graph"] >= 2
    assert seqs["evidence"] == 0 and seqs["claims"] == 0
    # Progress-only callers still read the same key.
    assert seqs["progress"] >= 1


def test_http_snapshot_consent_and_diff(client):
    case_id = new_case(client, "TEST-ONLY 快照")
    graph_id = _post(client, f"/api/v1/cases/{case_id}/graph/graphs",
                     {"case_id": case_id, "title": "股权结构"}).json()["graphs"][0]["id"]
    resp = _post(client, f"/api/v1/cases/{case_id}/graph/entities",
                 {"case_id": case_id, "kind": "org", "name": "甲公司", "graph_id": graph_id})
    entity = resp.json()["entities"][0]
    resp = _post(client, f"/api/v1/cases/{case_id}/graph/entities",
                 {"case_id": case_id, "kind": "org", "name": "乙公司", "graph_id": graph_id})
    second = _entity(resp.json(), "乙公司")
    resp = _post(client, f"/api/v1/cases/{case_id}/graph/relations", {
        "case_id": case_id, "from_entity_id": entity["id"], "to_entity_id": second["id"],
        "kind": "equity_control", "credibility": "suspected", "graph_id": graph_id})
    relation = resp.json()["relations"][0]

    frozen = _post(client, f"/api/v1/cases/{case_id}/graph/graphs/{graph_id}/snapshots",
                   {"case_id": case_id, "graph_id": graph_id, "stage_label": "起诉前"})
    assert frozen.status_code == 200, frozen.text
    snapshot = frozen.json()["snapshots"][0]
    assert snapshot["version_no"] == 1
    assert snapshot["created_by"] == "human"
    assert snapshot["payload"]["nodes"]

    listed = client.get(f"/api/v1/cases/{case_id}/graphs/{graph_id}/snapshots")
    assert [item["id"] for item in listed.json()["snapshots"]] == [snapshot["id"]]

    # Change the working draft, then compare against the frozen snapshot.
    resp = client.patch(f"/api/v1/cases/{case_id}/graph/relations/{relation['id']}", json={
        "case_id": case_id, "relation_id": relation["id"],
        "expected_revision": relation["revision"],
        "changes": {"credibility": "disputed"}})
    assert resp.status_code == 200
    diff = client.get(f"/api/v1/cases/{case_id}/snapshots/{snapshot['id']}/diff")
    assert diff.status_code == 200
    body = diff.json()
    assert body["target"] == "current"
    assert any(item["target"] == "relation" and item["change"] == "changed"
               for item in body["entries"])

    consent = _post(client, f"/api/v1/cases/{case_id}/graph/snapshots/{snapshot['id']}/consents", {
        "case_id": case_id, "snapshot_id": snapshot["id"], "action": "confirm",
        "consenter": "张三", "consented_at": "2026-11-20T10:00:00+08:00",
        "channel": "wechat", "source_detail": "微信确认", "note": "客户部分同意"})
    assert consent.status_code == 200, consent.text
    record = consent.json()["consents"][0]
    assert record["created_by"] == "human"
    assert record["note"] == "客户部分同意"
    # Amend appends; the original stays.
    amend = _post(client, f"/api/v1/cases/{case_id}/graph/snapshots/{snapshot['id']}/consents", {
        "case_id": case_id, "snapshot_id": snapshot["id"], "action": "amend",
        "parent_consent_id": record["id"], "consenter": "张三",
        "consented_at": "2026-11-21T10:00:00+08:00", "channel": "phone",
        "source_detail": "电话更正", "note": None})
    assert amend.status_code == 200, amend.text
    consents = amend.json()["consents"]
    assert len(consents) == 2
    assert consents[0]["action"] == "confirm" and consents[0]["note"] == "客户部分同意"

    # Restoring the snapshot is human-only and recorded as a change.
    restored = _post(client, f"/api/v1/cases/{case_id}/graph/snapshots/{snapshot['id']}/restore",
                     {"case_id": case_id, "snapshot_id": snapshot["id"]})
    assert restored.status_code == 200, restored.text
    rel = next(item for item in restored.json()["relations"] if item["id"] == relation["id"])
    assert rel["credibility"] == "suspected"


def test_agent_service_cannot_freeze_consent_or_review(db):
    """Service-level role guard, independent of which transport is used."""
    case_id = factories.create_case(db)
    db.commit()
    workspace = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="图"),
                                   actor="human", entry="local-web")
    graph_id = workspace.graphs[0].id
    assert _codes(lambda: graph.create_snapshot(
        db, CreateSnapshotInput(case_id=case_id, graph_id=graph_id, stage_label="起诉前"),
        actor="agent", entry="mcp")) == ErrorCode.FORBIDDEN
    snapshot = graph.create_snapshot(
        db, CreateSnapshotInput(case_id=case_id, graph_id=graph_id, stage_label="起诉前"),
        actor="human", entry="local-web")
    snapshot_id = snapshot.snapshots[0].id
    assert _codes(lambda: graph.restore_snapshot(
        db, __import__("legal_workbench.schemas", fromlist=["RestoreSnapshotInput"])
        .RestoreSnapshotInput(case_id=case_id, snapshot_id=snapshot_id),
        actor="agent", entry="cli")) == ErrorCode.FORBIDDEN
    assert _codes(lambda: graph.add_snapshot_consent(
        db, AddConsentInput(case_id=case_id, snapshot_id=snapshot_id, action="confirm",
                            consenter="张三", consented_at=__import__("datetime").datetime.now(),
                            channel="phone", source_detail="电话"), actor="agent",
        entry="mcp")) == ErrorCode.FORBIDDEN
    assert _codes(lambda: graph.accept_all(
        db, __import__("legal_workbench.schemas", fromlist=["GraphAcceptAllInput"])
        .GraphAcceptAllInput(case_id=case_id, change_ids=[uuid.uuid4()]),
        actor="agent", entry="mcp")) == ErrorCode.FORBIDDEN


def test_mcp_graph_tools_are_whitelisted_only():
    from legal_workbench import mcp_server

    tools = asyncio.run(mcp_server.server.list_tools())
    names = {tool.name for tool in tools}
    assert {"get_graph", "get_graph_rules", "graph_add_entity", "graph_update_entity",
            "graph_add_alias", "graph_add_relation", "graph_update_relation",
            "graph_add_source", "graph_create_graph", "graph_add_to_graph",
            "graph_removal_impact", "get_evidence_catalog", "evidence_add_group",
            "evidence_add_item", "evidence_update_item", "evidence_add_binding",
            "evidence_import_catalog"} <= names
    # The agent surface is exactly the whitelist plus read-only queries: no
    # freeze/restore, no consent, no accept/revert, no evidence review.
    forbidden = ("freeze", "restore", "consent", "accept", "revert", "confirm", "reject")
    assert not [name for name in names if any(word in name for word in forbidden)]
    # Snapshot access is read-only; it cannot mutate or freeze anything.
    assert "get_graph_snapshot" in names
    assert not [name for name in names if "graph_snapshot" in name and name != "get_graph_snapshot"]


def test_mcp_agent_write_is_pending_and_idempotent(db):
    from legal_workbench import mcp_server

    case_id = str(factories.create_case(db))
    db.commit()
    result = mcp_server.graph_add_entity(case_id, "person", "张三", "mcp-1")
    entity = result["entities"][0]
    assert entity["created_by"] == "agent"
    assert result["pending_changes"] and result["pending_changes"][0]["entry"] == "mcp"
    assert result["seqs"]["graph"] == 1
    # Same request_key replays without a second row or a new seq.
    again = mcp_server.graph_add_entity(case_id, "person", "张三", "mcp-1")
    assert len(again["entities"]) == 1
    assert again["seqs"]["graph"] == 1

    # The same entity at both ends is refused by the service (agent entry is not a bypass).
    with pytest.raises(WorkbenchError) as exc:
        mcp_server.graph_add_relation(
            case_id, entity["id"], entity["id"], "other", "suspected", "mcp-2")
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert len(mcp_server.get_graph(case_id)["relations"]) == 0


def test_cli_graph_and_evidence_commands(db, tmp_path):
    from legal_workbench import cli, cases

    session = _session()
    try:
        case_id = str(cases.create_case(session, title="TEST-ONLY CLI 关系图").case_id)
    finally:
        session.close()
    parser = cli.build_parser()

    # Freeze/restore/consent/accept must not be reachable from the CLI.
    actions = set(parser._subparsers._group_actions[0].choices)  # type: ignore[attr-defined]
    assert {"graph", "graph-add-entity", "graph-add-relation", "evidence-add-group",
            "evidence-add-binding"} <= actions
    assert not [name for name in actions if any(
        word in name for word in ("snapshot", "consent", "accept", "revert", "confirm"))]

    created = cli.dispatch(parser.parse_args([
        "graph-add-entity", "--case-id", case_id, "--kind", "person",
        "--name", "甲", "--request-key", "cli-e1"]))
    entity = created.entities[0]
    assert entity.created_by == "agent"
    assert created.pending_changes[0].entry == "cli"

    second = cli.dispatch(parser.parse_args([
        "graph-add-entity", "--case-id", case_id, "--kind", "org",
        "--name", "乙公司", "--request-key", "cli-e2"]))
    target = next(item for item in second.entities if item.name == "乙公司")
    relation = cli.dispatch(parser.parse_args([
        "graph-add-relation", "--case-id", case_id, "--from-entity-id", str(entity.id),
        "--to-entity-id", str(target.id), "--kind", "contract",
        "--credibility", "suspected", "--request-key", "cli-r1"]))
    assert relation.relations[0].credibility == "suspected"

    group = cli.dispatch(parser.parse_args([
        "evidence-add-group", "--case-id", case_id, "--title", "第一组：合同",
        "--request-key", "cli-g1"]))
    group_id = group.groups[0].id
    item = cli.dispatch(parser.parse_args([
        "evidence-add-item", "--case-id", case_id, "--group-id", str(group_id),
        "--title", "合同原件", "--request-key", "cli-i1"]))
    # Agent 条目进入待确认（review_state=pending）；人工确认后才参与编号。
    assert item.items[0].review_state == "pending"
    assert item.items[0].created_by == "agent"

    # Reads round-trip.
    read = cli.dispatch(parser.parse_args(["graph", "--case-id", case_id]))
    assert {row.title for row in read.graphs} == set()
    catalog = cli.dispatch(parser.parse_args(["evidence-catalog", "--case-id", case_id]))
    assert str(item.items[0].id) not in catalog.numbers
    assert catalog.pending_changes and catalog.pending_changes[0].actor == "agent"

    # Import draft through a JSON file, still pending.
    rows_file = tmp_path / "rows.json"
    rows_file.write_text(json.dumps({"rows": [{
        "group_title": "导入组", "item_title": "外部目录条目",
        "item_purpose": "证明合同成立", "bindings": []}]}), encoding="utf-8")
    with _committed() as session:
        material = factories.build_material(session, case_id=uuid.UUID(case_id))
    imported = cli.dispatch(parser.parse_args([
        "evidence-import-catalog", "--case-id", case_id,
        "--import-material-id", str(material["material_id"]),
        "--rows-file", str(rows_file), "--request-key", "cli-imp1"]))
    pending_items = [row for row in imported.items if row.source_kind == "imported"]
    assert pending_items and pending_items[0].review_state == "pending"
    # Pending drafts never shift the working-draft numbers.
    assert str(pending_items[0].id) not in imported.numbers


def test_evidence_catalog_http_numbering_export_and_import(client, tmp_path):
    case_id = new_case(client, "TEST-ONLY 证据目录")
    with _committed() as session:
        material = factories.build_material(session, case_id=uuid.UUID(case_id))

    group = _post(client, f"/api/v1/cases/{case_id}/evidence-catalog/groups",
                  {"case_id": case_id, "title": "第一组：合同"})
    assert group.status_code == 200, group.text
    group_id = group.json()["groups"][0]["id"]
    item = _post(client, f"/api/v1/cases/{case_id}/evidence-catalog/items",
                 {"case_id": case_id, "group_id": group_id, "title": "合同原件"})
    item_id = item.json()["items"][0]["id"]
    assert item.json()["numbers"][item_id] == "证据1-1"

    bound = _post(client, f"/api/v1/cases/{case_id}/evidence-catalog/bindings", {
        "case_id": case_id, "item_id": item_id,
        "material_id": str(material["material_id"]), "page_start": 1, "page_end": 3})
    assert bound.status_code == 200, bound.text
    assert bound.json()["bindings"][0]["review_state"] == "confirmed"

    exported = client.get(f"/api/v1/cases/{case_id}/evidence-catalog/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "attachment" in exported.headers["content-disposition"]
    text = exported.content.decode("utf-8-sig")
    assert "证据1-1" in text and "合同原件" in text
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][:5] == ["组号", "组名", "序号", "编号", "证据名称"]

    # External catalog preview: parse only, nothing written.
    catalog_csv = ("组名,证据名称,证明目的,对应材料,页码\n"
                   "第二组：往来,证据2-1 转账记录,证明付款,银行流水,5-6\n").encode("utf-8")
    preview = client.post(
        f"/api/v1/cases/{case_id}/evidence-catalog/import/preview",
        files={"file": ("目录.csv", catalog_csv, "text/csv")})
    assert preview.status_code == 200, preview.text
    drafts = preview.json()["rows"]
    assert drafts[0]["group_title"] == "第二组：往来"
    assert drafts[0]["item_title"] == "转账记录"  # "证据2-1" prefix stripped
    assert drafts[0]["page_start"] == 5 and drafts[0]["page_end"] == 6
    assert preview.json()["warnings"]
    # Preview did not create anything.
    assert len(_workspace(client, case_id)["evidence"]["groups"]) == 1

    # Committing an import creates pending drafts that do not shift numbers.
    imported = _post(client, f"/api/v1/cases/{case_id}/evidence-catalog/import", {
        "case_id": case_id, "import_material_id": str(material["material_id"]),
        "request_key": "http-imp-1",
        "rows": [{"group_title": "第二组：往来", "item_title": "转账记录",
                  "item_purpose": "证明付款",
                  "bindings": [{"material_id": str(material["material_id"]),
                                "page_start": 5, "page_end": 6}]}]})
    assert imported.status_code == 200, imported.text
    body = imported.json()
    drafts_items = [row for row in body["items"] if row["source_kind"] == "imported"]
    assert drafts_items[0]["review_state"] == "pending"
    assert str(drafts_items[0]["id"]) not in body["numbers"]
    assert body["numbers"][item_id] == "证据1-1"

    # Human confirms the draft item; then it takes part in numbering.
    confirmed = _post(
        client,
        f"/api/v1/cases/{case_id}/evidence-catalog/items/{drafts_items[0]['id']}/confirm",
        {"case_id": case_id, "item_id": drafts_items[0]["id"],
         "expected_revision": drafts_items[0]["revision"]})
    assert confirmed.status_code == 200, confirmed.text
    numbers = confirmed.json()["numbers"]
    assert numbers[str(drafts_items[0]["id"])] == "证据2-1"
    # The pending binding still does not count until confirmed.
    pending_bindings = [row for row in confirmed.json()["bindings"]
                        if row["review_state"] == "pending"]
    assert pending_bindings
    resp = _post(client,
                 f"/api/v1/cases/{case_id}/evidence-catalog/bindings/"
                 f"{pending_bindings[0]['id']}/confirm",
                 {"case_id": case_id, "binding_id": pending_bindings[0]["id"],
                  "expected_revision": 0})
    assert resp.status_code == 200, resp.text

    # Reorder changes the displayed number immediately.
    second_group = _post(client, f"/api/v1/cases/{case_id}/evidence-catalog/groups",
                         {"case_id": case_id, "title": "第三组：其他"})
    group_ids = [row["id"] for row in second_group.json()["groups"]]
    assert len(group_ids) == 3
    reordered = _post(client, f"/api/v1/cases/{case_id}/evidence-catalog/groups/reorder",
                      {"case_id": case_id, "group_ids": list(reversed(group_ids))})
    assert reordered.status_code == 200, reordered.text
    assert reordered.json()["numbers"][item_id] == "证据3-1"


def test_evidence_agent_change_review_round_trip(client):
    """Agent evidence writes surface a change id the human review endpoint accepts."""
    from legal_workbench import evidence_catalog, graph
    from legal_workbench.db import get_session_factory
    from legal_workbench.schemas import AddEvidenceGroupInput, AddEntityInput

    case_id = new_case(client, "TEST-ONLY 目录复核")
    case_uuid = uuid.UUID(case_id)
    with _committed() as session:
        material = factories.build_material(session, case_id=case_uuid)
    # Agent creates a group through the shared service (MCP/CLI path).
    session = get_session_factory()()
    try:
        evidence_catalog.add_group(
            session, AddEvidenceGroupInput(case_id=case_uuid, title="Agent 组",
                                          request_key="ag-1"),
            actor="agent", entry="mcp")
    finally:
        session.close()
    catalog = client.get(f"/api/v1/cases/{case_id}/evidence-catalog").json()
    assert [c["command"] for c in catalog["pending_changes"]] == ["add_group"]
    change_id = catalog["pending_changes"][0]["id"]
    assert catalog["pending_changes"][0]["actor"] == "agent"

    accepted = _post(client,
                     f"/api/v1/cases/{case_id}/evidence-catalog/changes/{change_id}/accept",
                     {"case_id": case_id, "change_id": change_id})
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["pending_changes"] == []
    assert [c["review_state"] for c in body["changes"] if c["id"] == change_id] == ["accepted"]

    # Revert is human-only and appends a reverse change.
    reverted = _post(client,
                     f"/api/v1/cases/{case_id}/evidence-catalog/changes/{change_id}/revert",
                     {"case_id": case_id, "change_id": change_id})
    assert reverted.status_code == 200, reverted.text
    assert reverted.json()["groups"][0]["removed_at"] is not None


def test_archived_case_guard_survives_concurrent_archive(client, db, test_database):
    """P2 口径：先拿案件行锁再复核归档，不能先查后写。

    前置检查在归档提交前通过，随后归档提交；迟到写入必须在锁后复核被拒。
    """
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session as OrmSession

    from legal_workbench import cases, models
    from legal_workbench.db import sqlalchemy_url
    from legal_workbench.schemas import ArchiveCaseInput, AddEntityInput, AddEvidenceGroupInput

    case_id = new_case(client, "TEST-ONLY 归档竞态")
    case_uuid = uuid.UUID(case_id)
    engine = create_engine(sqlalchemy_url(test_database[1]), future=True)
    late = OrmSession(bind=engine)
    try:
        stale = late.get(models.Case, case_uuid)
        assert stale.archived_at is None  # 旧实现的前置检查在这里就已通过
        with _committed() as session:
            cases.archive_case(session, ArchiveCaseInput(case_id=case_uuid, expected_revision=0))
        # 真实请求每个都用新会话；这里用 expire 模拟，保留「先读过旧值」的形态。
        late.expire_all()
        assert _codes(lambda: graph.add_entity(
            late, AddEntityInput(case_id=case_uuid, kind="person", name="迟到主体"),
            actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
        late.rollback()
        assert _codes(lambda: evidence_catalog.add_group(
            late, AddEvidenceGroupInput(case_id=case_uuid, title="迟到组", request_key="late-1"),
            actor="agent", entry="mcp")) == ErrorCode.INVALID_INPUT
    finally:
        late.rollback()
        late.close()
        engine.dispose()
    workspace = _workspace(client, case_id)
    assert [e["name"] for e in workspace["entities"] if e["name"] == "迟到主体"] == []
    assert workspace["evidence"]["groups"] == []


def test_http_archived_case_is_read_only(client):
    case_id = new_case(client, "TEST-ONLY 归档只读")
    resp = _post(client, f"/api/v1/cases/{case_id}/graph/entities",
                 {"case_id": case_id, "kind": "person", "name": "甲"})
    assert resp.status_code == 200
    archived = client.post(f"/api/v1/cases/{case_id}/archive",
                           json={"case_id": case_id, "expected_revision": 0})
    assert archived.status_code == 200, archived.text
    resp = _post(client, f"/api/v1/cases/{case_id}/graph/entities",
                 {"case_id": case_id, "kind": "person", "name": "乙"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_input"
    resp = _post(client, f"/api/v1/cases/{case_id}/evidence-catalog/groups",
                 {"case_id": case_id, "title": "归档后"})
    assert resp.status_code == 422
    # Reads still work.
    assert _workspace(client, case_id)["archived"] is True


def test_evidence_import_preview_parses_docx_and_xlsx(tmp_path):
    from docx import Document
    from openpyxl import Workbook

    document = Document()
    table = document.add_table(rows=3, cols=3)
    headers = ["组名", "证据名称", "证明目的"]
    for index, value in enumerate(headers):
        table.cell(0, index).text = value
    table.cell(1, 0).text = "第一组：合同"
    table.cell(1, 1).text = "证据1-1 合同书"
    table.cell(1, 2).text = "证明合同成立"
    docx_path = tmp_path / "目录.docx"
    document.save(docx_path)
    rows = evidence_catalog.preview_catalog_bytes(docx_path.read_bytes(), "目录.docx")
    assert rows[0]["item_title"] == "合同书"
    assert rows[0]["group_title"] == "第一组：合同"

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["证据名称", "对应材料", "页码"])
    sheet.append(["银行流水", "流水.pdf", "3"])
    xlsx_path = tmp_path / "目录.xlsx"
    workbook.save(xlsx_path)
    rows = evidence_catalog.preview_catalog_bytes(xlsx_path.read_bytes(), "目录.xlsx")
    assert rows[0]["item_title"] == "银行流水"
    assert rows[0]["material_title"] == "流水.pdf"
    assert (rows[0]["page_start"], rows[0]["page_end"]) == (3, 3)

    with pytest.raises(WorkbenchError) as exc:
        evidence_catalog.preview_catalog_bytes(b"nope", "目录.pdf")
    assert exc.value.code == ErrorCode.UNSUPPORTED_FORMAT
