"""P2 snapshots: freeze (excluding pending), immutability, drift, diff, restore
into the working draft, and append-only customer consents (D3).

TEST-ONLY synthetic data; real PostgreSQL.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from legal_workbench import graph, models
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (
    AddConsentInput, AddEntityInput, AddSourceInput, CreateGraphInput, CreateSnapshotInput,
    EntityChanges, GraphChangeIdInput, RelationChanges, RemoveFromGraphInput,
    RemoveSourceInput, RestoreSnapshotInput,
    SaveLayoutInput, SourceRefInput, UpdateEntityInput, UpdateRelationInput, LayoutNode,
    AddGraphRelationInput,
)

from .graph_factories import build_graph_case


def _codes(fn):
    with pytest.raises(WorkbenchError) as exc:
        fn()
    return exc.value.code


def _setup(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    snap = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="TEST-ONLY 股权图"),
                              actor="human", entry="local-web")
    graph_id = snap.graphs[0].id
    snap = graph.add_entity(db, AddEntityInput(
        case_id=case_id, kind="org", name="TEST-ONLY 甲", graph_id=graph_id),
        actor="human", entry="local-web")
    a = snap.entities[0]
    snap = graph.add_entity(db, AddEntityInput(
        case_id=case_id, kind="org", name="TEST-ONLY 乙", graph_id=graph_id),
        actor="human", entry="local-web")
    b = next(row for row in snap.entities if row.name == "TEST-ONLY 乙")
    snap = graph.add_relation(db, AddGraphRelationInput(
        case_id=case_id, from_entity_id=a.id, to_entity_id=b.id, kind="equity_control",
        credibility="suspected", graph_id=graph_id), actor="human", entry="local-web")
    relation = snap.relations[0]
    snap = graph.add_source(db, AddSourceInput(
        case_id=case_id, relation_id=relation.id,
        ref=SourceRefInput(segment_id=case["segment_id"]), proves="TEST-ONLY 股权"),
        actor="human", entry="local-web")
    source = next(row for row in snap.sources if row.relation_id == relation.id)
    snap = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=relation.id, expected_revision=0,
        changes=RelationChanges(credibility="evidenced")), actor="human", entry="local-web")
    relation = next(row for row in snap.relations if row.id == relation.id)
    return case, graph_id, a, b, relation, source


def _freeze(db, case_id, graph_id, stage="庭前", **kwargs):
    return graph.create_snapshot(db, CreateSnapshotInput(
        case_id=case_id, graph_id=graph_id, stage_label=stage, **kwargs),
        actor="human", entry="local-web")


# --- freeze -----------------------------------------------------------------

def test_freeze_captures_membership_layout_sources_and_labels(db):
    case, graph_id, a, b, relation, source = _setup(db)
    graph.save_layout(db, SaveLayoutInput(
        case_id=case["case_id"], graph_id=graph_id,
        nodes=[LayoutNode(node_id=a.id, x=10, y=20)]), actor="human", entry="local-web")
    snap = _freeze(db, case["case_id"], graph_id)
    assert len(snap.snapshots) == 1
    frozen = snap.snapshots[0]
    assert frozen.version_no == 1 and frozen.stage_label == "庭前"
    assert frozen.excluded_pending_count == 0
    payload = frozen.payload
    assert payload["graph"]["id"] == str(graph_id)
    assert {node["entity_id"] for node in payload["nodes"]} == {str(a.id), str(b.id)}
    assert payload["edges"][0]["credibility"] == "evidenced"
    assert payload["edges"][0]["sources"][0]["proves"] == "TEST-ONLY 股权"
    # the frozen label is the text at freeze time (no catalog yet -> unnumbered)
    assert payload["edges"][0]["sources"][0]["label"].endswith("（未编号）")
    assert next(node for node in payload["nodes"]
                if node["entity_id"] == str(a.id))["x"] == 10

    # Also verify RelationGraphNode.id is accepted by save_layout for frontend canvas compatibility
    graph_node_b = next(node for node in snap.nodes if node.entity_id == b.id)
    updated_ws = graph.save_layout(db, SaveLayoutInput(
        case_id=case["case_id"], graph_id=graph_id,
        nodes=[LayoutNode(node_id=graph_node_b.id, x=300, y=220)]), actor="human", entry="local-web")
    assert next(node for node in updated_ws.nodes if node.id == graph_node_b.id).x == 300

    snap = _freeze(db, case["case_id"], graph_id, stage="自定义阶段")
    assert [row.version_no for row in snap.snapshots] == [1, 2]
    assert graph.list_snapshots(db, case["case_id"], graph_id)[1].stage_label == "自定义阶段"
    assert _codes(lambda: _freeze(db, case["case_id"], graph_id, stage="  ")) == ErrorCode.INVALID_INPUT


def test_freeze_excludes_pending_agent_changes(db):
    case, graph_id, a, b, relation, source = _setup(db)
    # an agent proposes a new entity in this graph
    graph.add_entity(db, AddEntityInput(
        case_id=case["case_id"], kind="person", name="TEST-ONLY 待确认",
        graph_id=graph_id, request_key="agent-entity-pending"),
        actor="agent", entry="mcp")
    # freezing without acknowledging the pending count is refused
    with pytest.raises(WorkbenchError) as exc:
        _freeze(db, case["case_id"], graph_id)
    assert exc.value.code == ErrorCode.CONFLICT
    assert exc.value.details["pending_count"] >= 1
    snap = _freeze(db, case["case_id"], graph_id, acknowledge_pending=True)
    frozen = snap.snapshots[0]
    assert frozen.excluded_pending_count >= 1
    names = {node["name"] for node in frozen.payload["nodes"]}
    assert "TEST-ONLY 待确认" not in names
    assert "已确认状态" in (snap.notice or "")


def test_pending_catalog_change_also_blocks_freeze(db):
    from legal_workbench import evidence_catalog
    from legal_workbench.schemas import (AddEvidenceBindingInput, AddEvidenceGroupInput,
                                         AddEvidenceItemInput)
    case, graph_id, a, b, relation, source = _setup(db)
    catalog = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case["case_id"], title="第一组"), actor="human", entry="local-web")
    catalog = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case["case_id"], group_id=catalog.groups[0].id, title="Agent 条目",
        request_key="agent-item-1"), actor="agent", entry="mcp")
    # 只有绑定了本图所示材料的待确认条目才会移动这里的编号（P1 同口径）。
    evidence_catalog.add_binding(db, AddEvidenceBindingInput(
        case_id=case["case_id"], item_id=catalog.items[0].id,
        material_id=case["material_id"], request_key="agent-bind-1"),
        actor="agent", entry="mcp")
    with pytest.raises(WorkbenchError) as exc:
        _freeze(db, case["case_id"], graph_id)
    assert exc.value.code == ErrorCode.CONFLICT
    # 「按已确认状态继续冻结」允许继续，并把待确认条数记入快照。
    frozen = _freeze(db, case["case_id"], graph_id, acknowledge_pending=True)
    assert frozen.snapshots[0].excluded_pending_count >= 1


# --- immutability -----------------------------------------------------------

def test_snapshot_rows_are_immutable_in_the_database(db):
    case, graph_id, *_ = _setup(db)
    snap = _freeze(db, case["case_id"], graph_id)
    snapshot_id = snap.snapshots[0].id
    for statement in (
        "UPDATE relation_graph_snapshots SET note='改写' WHERE id=:id",
        "DELETE FROM relation_graph_snapshots WHERE id=:id",
    ):
        with pytest.raises(DBAPIError):
            db.execute(text(statement), {"id": str(snapshot_id)})
            db.flush()
        db.rollback()
    assert db.get(models.RelationGraphSnapshot, snapshot_id) is not None


# --- drift and diff ---------------------------------------------------------

def test_working_draft_changes_do_not_touch_snapshot_and_drift_is_reported(db):
    from legal_workbench import evidence_catalog
    from legal_workbench.schemas import (AddEvidenceBindingInput, AddEvidenceGroupInput,
                                         AddEvidenceItemInput)
    case, graph_id, a, b, relation, source = _setup(db)
    snap = _freeze(db, case["case_id"], graph_id)
    frozen_before = snap.snapshots[0].payload
    # build a confirmed catalog that now covers the source
    catalog = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case["case_id"], title="第一组：合同"), actor="human", entry="local-web")
    catalog = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case["case_id"], group_id=catalog.groups[0].id, title="合同文本"),
        actor="human", entry="local-web")
    evidence_catalog.add_binding(db, AddEvidenceBindingInput(
        case_id=case["case_id"], item_id=catalog.items[0].id,
        material_id=case["material_id"], page_start=1, page_end=5),
        actor="human", entry="local-web")
    # rename the entity and change the relation after freezing
    graph.update_entity(db, UpdateEntityInput(
        case_id=case["case_id"], entity_id=a.id, expected_revision=a.revision,
        changes=EntityChanges(name="TEST-ONLY 甲（更名）")), actor="human", entry="local-web")
    graph.update_relation(db, UpdateRelationInput(
        case_id=case["case_id"], relation_id=relation.id, expected_revision=relation.revision,
        changes=RelationChanges(credibility="disputed")), actor="human", entry="local-web")

    current = graph.get_snapshot(db, case["case_id"], snap.snapshots[0].id)
    assert current.payload == frozen_before  # snapshot body untouched
    assert "TEST-ONLY 甲（更名）" not in {node["name"] for node in current.payload["nodes"]}

    diff = graph.diff_snapshots(db, case["case_id"], snap.snapshots[0].id, None)
    assert diff.target == "current"
    changed = {(entry.target, entry.change) for entry in diff.entries}
    assert ("entity", "changed") in changed
    assert ("relation", "changed") in changed
    # the label drifted: frozen text was unnumbered, the current catalog numbers it
    assert diff.label_drift
    assert "证据1-1" in (diff.label_drift[0].current or "")
    assert diff.label_drift[0].frozen.endswith("（未编号）")


def test_snapshot_to_snapshot_diff_detects_add_remove_change(db):
    case, graph_id, a, b, relation, source = _setup(db)
    _freeze(db, case["case_id"], graph_id, stage="起诉前")
    graph.update_relation(db, UpdateRelationInput(
        case_id=case["case_id"], relation_id=relation.id, expected_revision=relation.revision,
        changes=RelationChanges(credibility="ruled_out")), actor="human", entry="local-web")
    graph.remove_source(db, RemoveSourceInput(
        case_id=case["case_id"], source_id=source.id), actor="human", entry="local-web")
    snap = _freeze(db, case["case_id"], graph_id, stage="庭前")
    first, second = snap.snapshots[0].id, snap.snapshots[1].id
    diff = graph.diff_snapshots(db, case["case_id"], first, second)
    assert diff.target == str(second)
    assert any(entry.target == "relation" and entry.change == "changed"
               for entry in diff.entries)
    assert any(entry.target == "source" and entry.change == "removed"
               for entry in diff.entries)
    # removing a node between two snapshots shows up as a removed entity
    graph.remove_from_graph(db, RemoveFromGraphInput(
        case_id=case["case_id"], graph_id=graph_id, entity_id=b.id),
        actor="human", entry="local-web")
    snap = _freeze(db, case["case_id"], graph_id, stage="庭后")
    third = snap.snapshots[2].id
    diff = graph.diff_snapshots(db, case["case_id"], second, third)
    assert any(entry.target == "entity" and entry.change == "removed"
               for entry in diff.entries)


# --- restore into the working draft ----------------------------------------

def test_restore_snapshot_rebuilds_working_draft_and_is_revertible(db):
    case, graph_id, a, b, relation, source = _setup(db)
    snap = _freeze(db, case["case_id"], graph_id)
    snapshot_id = snap.snapshots[0].id
    # mutate after the freeze
    graph.update_relation(db, UpdateRelationInput(
        case_id=case["case_id"], relation_id=relation.id, expected_revision=relation.revision,
        changes=RelationChanges(credibility="disputed")), actor="human", entry="local-web")
    snap = graph.remove_source(db, RemoveSourceInput(
        case_id=case["case_id"], source_id=source.id, downgrade_to="ruled_out"),
        actor="human", entry="local-web")
    graph.remove_from_graph(db, RemoveFromGraphInput(
        case_id=case["case_id"], graph_id=graph_id, entity_id=b.id),
        actor="human", entry="local-web")
    assert graph.get_workspace(db, case["case_id"]).edges == []

    snap = graph.restore_snapshot(db, RestoreSnapshotInput(
        case_id=case["case_id"], snapshot_id=snapshot_id), actor="human", entry="local-web")
    restored_relation = next(row for row in snap.relations if row.id == relation.id)
    assert restored_relation.credibility == "evidenced"
    assert next(row for row in snap.sources if row.id == source.id).removed_at is None
    assert any(edge.graph_id == graph_id for edge in snap.edges)
    assert any(node.entity_id == b.id for node in snap.nodes)
    # the snapshot itself is unchanged
    assert graph.get_snapshot(db, case["case_id"], snapshot_id).payload["edges"][0][
        "credibility"] == "evidenced"

    # revert the restore -> back to the modified state
    revert_id = snap.last_change_id
    snap = graph.revert_change(db, GraphChangeIdInput(
        case_id=case["case_id"], change_id=revert_id), actor="human", entry="local-web")
    assert next(row for row in snap.relations if row.id == relation.id).credibility == "disputed"
    assert next(row for row in snap.sources if row.id == source.id).removed_at is not None


def test_restore_is_blocked_while_pending_changes_exist(db):
    case, graph_id, *_ = _setup(db)
    snap = _freeze(db, case["case_id"], graph_id)
    graph.add_entity(db, AddEntityInput(
        case_id=case["case_id"], kind="person", name="TEST-ONLY 待确认",
        graph_id=graph_id, request_key="agent-pending-restore"),
        actor="agent", entry="mcp")
    assert _codes(lambda: graph.restore_snapshot(db, RestoreSnapshotInput(
        case_id=case["case_id"], snapshot_id=snap.snapshots[0].id),
        actor="human", entry="local-web")) == ErrorCode.CONFLICT


def test_snapshot_and_consent_changes_cannot_be_reverted(db):
    case, graph_id, *_ = _setup(db)
    snap = _freeze(db, case["case_id"], graph_id)
    change_id = snap.last_change_id
    assert _codes(lambda: graph.revert_change(db, GraphChangeIdInput(
        case_id=case["case_id"], change_id=change_id), actor="human",
        entry="local-web")) == ErrorCode.INVALID_INPUT


# --- customer consent (append-only) ----------------------------------------

def test_consent_is_append_only_with_amend_and_revoke(db):
    case, graph_id, *_ = _setup(db)
    snap = _freeze(db, case["case_id"], graph_id)
    snapshot_id = snap.snapshots[0].id
    moment = datetime(2026, 1, 5, 10, 30, tzinfo=timezone.utc)
    snap = graph.add_snapshot_consent(db, AddConsentInput(
        case_id=case["case_id"], snapshot_id=snapshot_id, action="confirm",
        consenter="TEST-ONLY 客户", consented_at=moment, channel="wechat",
        source_detail="TEST-ONLY 微信语音确认", note="例如：客户部分同意"),
        actor="human", entry="local-web")
    consent = snap.consents[0]
    assert consent.action == "confirm" and consent.note == "例如：客户部分同意"
    assert consent.consented_at == moment

    # amend points at the original record and keeps it
    snap = graph.add_snapshot_consent(db, AddConsentInput(
        case_id=case["case_id"], snapshot_id=snapshot_id, action="amend",
        parent_consent_id=consent.id, consenter="TEST-ONLY 客户", consented_at=moment,
        channel="written", source_detail="TEST-ONLY 书面补充"), actor="human", entry="local-web")
    assert len(snap.consents) == 2
    assert snap.consents[1].parent_consent_id == consent.id
    # revoke points at the original record too
    snap = graph.add_snapshot_consent(db, AddConsentInput(
        case_id=case["case_id"], snapshot_id=snapshot_id, action="revoke",
        parent_consent_id=consent.id, consenter="TEST-ONLY 客户", consented_at=moment,
        channel="phone", source_detail="TEST-ONLY 电话撤回"), actor="human", entry="local-web")
    assert [row.action for row in snap.consents] == ["confirm", "amend", "revoke"]
    # first consent cannot carry a parent; amend/revoke cannot omit it
    assert _codes(lambda: graph.add_snapshot_consent(db, AddConsentInput(
        case_id=case["case_id"], snapshot_id=snapshot_id, action="confirm",
        parent_consent_id=consent.id, consenter="x", consented_at=moment,
        channel="wechat", source_detail="x"), actor="human",
        entry="local-web")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: graph.add_snapshot_consent(db, AddConsentInput(
        case_id=case["case_id"], snapshot_id=snapshot_id, action="amend",
        consenter="x", consented_at=moment, channel="wechat", source_detail="x"),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: graph.add_snapshot_consent(db, AddConsentInput(
        case_id=case["case_id"], snapshot_id=snapshot_id, action="confirm",
        consenter="x", consented_at=moment, channel="telepathy", source_detail="x"),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    # records cannot be rewritten or deleted, even by raw SQL
    for statement in ("UPDATE snapshot_consents SET note='改写' WHERE id=:id",
                      "DELETE FROM snapshot_consents WHERE id=:id"):
        with pytest.raises(DBAPIError):
            db.execute(text(statement), {"id": str(consent.id)})
            db.flush()
        db.rollback()
    assert len(graph.get_workspace(db, case["case_id"]).consents) == 3


def test_consent_requires_snapshot_in_same_case_and_human_actor(db):
    case, graph_id, *_ = _setup(db)
    other = build_graph_case(db)
    snap = _freeze(db, case["case_id"], graph_id)
    other_graph = graph.create_graph(db, CreateGraphInput(
        case_id=other["case_id"], title="外案图"), actor="human", entry="local-web")
    other_snapshot = _freeze(db, other["case_id"], other_graph.graphs[0].id)
    moment = datetime(2026, 1, 5, tzinfo=timezone.utc)
    # consent on another case's snapshot is a case mismatch
    assert _codes(lambda: graph.add_snapshot_consent(db, AddConsentInput(
        case_id=case["case_id"], snapshot_id=other_snapshot.snapshots[0].id,
        action="confirm", consenter="x", consented_at=moment, channel="meeting",
        source_detail="x"), actor="human", entry="local-web")) == ErrorCode.CASE_MISMATCH
    # evidence material must belong to the case
    assert _codes(lambda: graph.add_snapshot_consent(db, AddConsentInput(
        case_id=case["case_id"], snapshot_id=snap.snapshots[0].id, action="confirm",
        consenter="x", consented_at=moment, channel="meeting", source_detail="x",
        evidence_material_id=other["material_id"]), actor="human",
        entry="local-web")) == ErrorCode.CASE_MISMATCH
    assert _codes(lambda: graph.add_snapshot_consent(db, AddConsentInput(
        case_id=case["case_id"], snapshot_id=snap.snapshots[0].id, action="confirm",
        consenter="x", consented_at=moment, channel="meeting", source_detail="x"),
        actor="agent", entry="mcp")) == ErrorCode.FORBIDDEN


def test_snapshot_progress_node_must_be_same_case(db):
    from legal_workbench import progress
    from legal_workbench.schemas import AddNodeInput
    case, graph_id, *_ = _setup(db)
    other = build_graph_case(db)
    foreign = progress.add_node(db, AddNodeInput(
        case_id=other["case_id"], kind="stage", title="TEST-ONLY 外案阶段"),
        actor="human", entry="local-web")
    foreign_node = foreign.nodes[0].id
    assert _codes(lambda: _freeze(db, case["case_id"], graph_id,
                                  progress_node_id=foreign_node)) == ErrorCode.CASE_MISMATCH
    own = progress.add_node(db, AddNodeInput(
        case_id=case["case_id"], kind="stage", title="TEST-ONLY 本案阶段"),
        actor="human", entry="local-web")
    own_node = own.nodes[0].id
    snap = _freeze(db, case["case_id"], graph_id, progress_node_id=own_node)
    assert snap.snapshots[0].progress_node_id == own_node
