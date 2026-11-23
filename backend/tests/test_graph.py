"""P2 graph service: entities/aliases/relations/sources, multi-graph semantics,
CAS, same-case isolation, archive guard, agent review, layout and groups.

Real PostgreSQL only (conftest recreates legal_workbench_test). TEST-ONLY data.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from legal_workbench import cases, graph, graph_rules, models
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (
    AddAliasInput, AddEntityInput, AddSourceInput, AddToGraphInput, ArchiveCaseInput,
    CreateGraphInput, EntityChanges, EntityRevisionInput, GraphAcceptAllInput,
    AddGraphRelationInput, GraphChangeIdInput, GraphChanges, GraphRevisionInput,
    RelationChanges, RelationRevisionInput, RemoveAliasInput,
    RemoveFromGraphInput, RemoveGroupInput, RemoveSourceInput, SaveLayoutInput,
    SetNodeGroupInput, SourceRefInput, UpdateEntityInput, UpdateGraphInput,
    UpdateRelationInput, UpsertGroupInput, LayoutNode,
)

from .graph_factories import build_graph_case


def _codes(fn):
    with pytest.raises(WorkbenchError) as exc:
        fn()
    return exc.value.code


def _entity(session, case_id, *, name="TEST-ONLY 甲公司", kind="org",
            roles=("plaintiff",), actor="human", graph_id=None, request_key=None):
    return graph.add_entity(session, AddEntityInput(
        case_id=case_id, kind=kind, name=name, roles=list(roles),
        graph_id=graph_id, request_key=request_key), actor=actor, entry="local-web")


def _find(rows, name):
    return next(row for row in rows if row.name == name)


def _relation(session, case_id, from_id, to_id, *, kind="equity_control",
              credibility="suspected", actor="human", graph_id=None, request_key=None):
    return graph.add_relation(session, AddGraphRelationInput(
        case_id=case_id, from_entity_id=from_id, to_entity_id=to_id, kind=kind,
        credibility=credibility, graph_id=graph_id, request_key=request_key),
        actor=actor, entry="local-web")


# --- rules and reads --------------------------------------------------------

def test_rules_are_centralised_and_extensible(db):
    rules = graph_rules.get_rules()
    assert {entry["value"] for entry in rules["credibility"]} == {
        "evidenced", "suspected", "disputed", "ruled_out"}
    assert rules["snapshot_stages"] == ["初次沟通", "方案沟通", "起诉前", "庭前", "庭后", "二审"]
    # freeze/restore, consent, review and deletes are never agent commands
    for command in ("create_snapshot", "restore_snapshot", "add_snapshot_consent",
                    "accept_change", "revert_change", "remove_entity", "save_layout"):
        assert command not in rules["agent_commands"]
    # adding a relation kind is a config change, not a schema change
    case = build_graph_case(db)
    from legal_workbench import graph_rules as rules_module
    original = rules_module.DEFAULT_RULES
    custom = rules_module.get_rules()
    custom["relation_kinds"].append({"value": "control_agreement", "label": "控制协议"})
    try:
        rules_module.DEFAULT_RULES = custom
        a = _find(_entity(db, case["case_id"], name="甲").entities, "甲")
        b = _find(_entity(db, case["case_id"], name="乙").entities, "乙")
        snap = _relation(db, case["case_id"], a.id, b.id, kind="control_agreement")
        assert any(row.kind == "control_agreement" for row in snap.relations)
    finally:
        rules_module.DEFAULT_RULES = original


def test_workspace_is_empty_but_complete(db):
    case = build_graph_case(db)
    workspace = graph.get_workspace(db, case["case_id"])
    assert workspace.entities == [] and workspace.relations == []
    assert workspace.evidence.materials  # material picker is served from the case
    assert workspace.archived is False
    # 四域序号固定零填充：没有写入时读作 0，而不是缺键（P3 契约）。
    assert workspace.seqs == {"progress": 0, "graph": 0, "evidence": 0, "claims": 0}


# --- entities, aliases, CAS -------------------------------------------------

def test_entity_alias_lifecycle_and_cas(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    snap = _entity(db, case_id, roles=("plaintiff", "creditor"))
    entity = _find(snap.entities, "TEST-ONLY 甲公司")
    assert entity.roles == ["plaintiff", "creditor"] and entity.revision == 0

    snap = graph.update_entity(db, UpdateEntityInput(
        case_id=case_id, entity_id=entity.id, expected_revision=0,
        changes=EntityChanges(name="TEST-ONLY 甲公司（更名）", note="TEST-ONLY 备注")),
        actor="human", entry="local-web")
    renamed = _find(snap.entities, "TEST-ONLY 甲公司（更名）")
    assert renamed.note == "TEST-ONLY 备注" and renamed.revision == 1

    # stale revision is a conflict, not a silent overwrite
    assert _codes(lambda: graph.update_entity(db, UpdateEntityInput(
        case_id=case_id, entity_id=entity.id, expected_revision=0,
        changes=EntityChanges(note="陈旧")), actor="human", entry="local-web")) == ErrorCode.CONFLICT

    snap = graph.add_alias(db, AddAliasInput(
        case_id=case_id, entity_id=entity.id, alias="TEST-ONLY 甲"), actor="human",
        entry="local-web")
    alias = next(row for row in snap.aliases if row.entity_id == entity.id)
    # duplicate alias (case-insensitive) is rejected by the service
    assert _codes(lambda: graph.add_alias(db, AddAliasInput(
        case_id=case_id, entity_id=entity.id, alias="test-only 甲"),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    snap = graph.remove_alias(db, RemoveAliasInput(case_id=case_id, alias_id=alias.id),
                              actor="human", entry="local-web")
    assert all(row.id != alias.id or row.removed_at for row in snap.aliases)

    snap = graph.remove_entity(db, EntityRevisionInput(
        case_id=case_id, entity_id=entity.id, expected_revision=renamed.revision),
        actor="human", entry="local-web")
    assert _find(snap.entities, "TEST-ONLY 甲公司（更名）").removed_at is not None
    assert snap.notice and "影响 0 张图" in snap.notice
    # writing to a deleted entity is rejected
    assert _codes(lambda: graph.update_entity(db, UpdateEntityInput(
        case_id=case_id, entity_id=entity.id, expected_revision=renamed.revision + 1,
        changes=EntityChanges(note="x")), actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    snap = graph.restore_entity(db, EntityRevisionInput(
        case_id=case_id, entity_id=entity.id, expected_revision=renamed.revision + 1),
        actor="human", entry="local-web")
    assert _find(snap.entities, "TEST-ONLY 甲公司（更名）").removed_at is None


def test_validation_rejects_unknown_vocabulary(db):
    case = build_graph_case(db)
    assert _codes(lambda: _entity(db, case["case_id"], kind="alien")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: _entity(db, case["case_id"], roles=("king",))) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: _entity(db, case["case_id"], name="   ")) == ErrorCode.INVALID_INPUT
    a = _find(_entity(db, case["case_id"], name="甲").entities, "甲")
    b = _find(_entity(db, case["case_id"], name="乙").entities, "乙")
    assert _codes(lambda: _relation(db, case["case_id"], a.id, b.id,
                                    kind="bogus")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: _relation(db, case["case_id"], a.id, a.id)) == ErrorCode.INVALID_INPUT


# --- "evidenced" requires a source (rule 3) ---------------------------------

def test_evidenced_requires_source_and_downgrade_on_last_removal(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    a = _find(_entity(db, case_id, name="甲").entities, "甲")
    b = _find(_entity(db, case_id, name="乙").entities, "乙")
    # cannot create an evidenced relation: there is no source yet
    assert _codes(lambda: _relation(db, case_id, a.id, b.id,
                                    credibility="evidenced")) == ErrorCode.INVALID_INPUT
    snap = _relation(db, case_id, a.id, b.id, credibility="suspected")
    relation = snap.relations[0]
    # cannot upgrade without a source
    assert _codes(lambda: graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=relation.id, expected_revision=relation.revision,
        changes=RelationChanges(credibility="evidenced")),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    snap = graph.add_source(db, AddSourceInput(
        case_id=case_id, relation_id=relation.id,
        ref=SourceRefInput(segment_id=case["segment_id"]), proves="TEST-ONLY 股权比例"),
        actor="human", entry="local-web")
    source = next(row for row in snap.sources if row.relation_id == relation.id)
    snap = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=relation.id, expected_revision=relation.revision,
        changes=RelationChanges(credibility="evidenced")), actor="human", entry="local-web")
    relation = next(row for row in snap.relations if row.id == relation.id)
    assert relation.credibility == "evidenced"
    # removing the last source without a downgrade is refused (no silent downgrade)
    assert _codes(lambda: graph.remove_source(db, RemoveSourceInput(
        case_id=case_id, source_id=source.id), actor="human",
        entry="local-web")) == ErrorCode.INVALID_INPUT
    snap = graph.remove_source(db, RemoveSourceInput(
        case_id=case_id, source_id=source.id, downgrade_to="disputed"),
        actor="human", entry="local-web")
    relation = next(row for row in snap.relations if row.id == relation.id)
    assert relation.credibility == "disputed"
    assert "降级" in (snap.notice or "")
    # a downgrade target may not be "evidenced" again
    assert _codes(lambda: graph.remove_source(db, RemoveSourceInput(
        case_id=case_id, source_id=source.id, downgrade_to="evidenced"),
        actor="human", entry="local-web")) == ErrorCode.NOT_FOUND


def test_source_refs_are_three_way_and_same_case(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    other = build_graph_case(db)
    a = _find(_entity(db, case_id, name="甲").entities, "甲")
    snap = graph.add_source(db, AddSourceInput(
        case_id=case_id, entity_id=a.id, ref=SourceRefInput(material_id=case["material_id"])),
        actor="human", entry="local-web")
    assert snap.labels[str(next(row.id for row in snap.sources
                                if row.entity_id == a.id))].text.endswith("（未编号）")
    assert _codes(lambda: graph.add_source(db, AddSourceInput(
        case_id=case_id, entity_id=a.id,
        ref=SourceRefInput(material_id=other["material_id"])),
        actor="human", entry="local-web")) == ErrorCode.CASE_MISMATCH
    assert _codes(lambda: graph.add_source(db, AddSourceInput(
        case_id=case_id, entity_id=a.id,
        ref=SourceRefInput(segment_id=uuid.uuid4())),
        actor="human", entry="local-web")) == ErrorCode.NOT_FOUND
    # annotation ref resolves too
    snap = graph.add_source(db, AddSourceInput(
        case_id=case_id, entity_id=a.id,
        ref=SourceRefInput(annotation_id=case["annotation_id"])),
        actor="human", entry="local-web")
    assert len([row for row in snap.sources if row.entity_id == a.id]) == 2


# --- multi-graph semantics (D1) ---------------------------------------------

def test_multi_graph_sync_and_remove_from_graph(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    snap = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="股权与实控"),
                              actor="human", entry="local-web")
    first = snap.graphs[0]
    snap = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="资金与担保"),
                              actor="human", entry="local-web")
    second = next(row for row in snap.graphs if row.id != first.id)

    snap = _entity(db, case_id, name="甲", graph_id=first.id)
    entity = _find(snap.entities, "甲")
    assert any(node.entity_id == entity.id and node.graph_id == first.id for node in snap.nodes)
    b = _find(_entity(db, case_id, name="乙").entities, "乙")
    # adding an existing relation to the second graph pulls both endpoints in
    snap = _relation(db, case_id, entity.id, b.id, graph_id=first.id)
    relation = snap.relations[0]
    snap = graph.add_to_graph(db, AddToGraphInput(
        case_id=case_id, graph_id=second.id, relation_ids=[relation.id]),
        actor="human", entry="local-web")
    members = {node.entity_id for node in snap.nodes if node.graph_id == second.id}
    assert {entity.id, b.id} <= members
    assert any(edge.relation_id == relation.id and edge.graph_id == second.id
               for edge in snap.edges)

    # editing the case-level relation is visible from both graphs
    snap = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=relation.id, expected_revision=relation.revision,
        changes=RelationChanges(label="TEST-ONLY 持股 60%")),
        actor="human", entry="local-web")
    relation = next(row for row in snap.relations if row.id == relation.id)
    assert relation.label == "TEST-ONLY 持股 60%"

    # "remove from this graph" only deletes membership, not the case pool
    snap = graph.remove_from_graph(db, RemoveFromGraphInput(
        case_id=case_id, graph_id=second.id, entity_id=entity.id),
        actor="human", entry="local-web")
    assert not any(node.graph_id == second.id and node.entity_id == entity.id
                   for node in snap.nodes)
    assert any(node.graph_id == first.id and node.entity_id == entity.id for node in snap.nodes)
    assert any(row.id == entity.id and row.removed_at is None for row in snap.entities)
    # the edge touching the removed node is gone from that view only
    assert not any(edge.graph_id == second.id for edge in snap.edges)
    assert any(edge.graph_id == first.id and edge.relation_id == relation.id
               for edge in snap.edges)

    # deleting the case-level relation reports the affected graphs first
    impact = graph.get_removal_impact(db, case_id, relation_id=relation.id)
    assert impact.count == 1 and impact.graphs[0].id == first.id
    snap = graph.remove_relation(db, RelationRevisionInput(
        case_id=case_id, relation_id=relation.id, expected_revision=relation.revision),
        actor="human", entry="local-web")
    assert "影响 1 张图" in (snap.notice or "")
    # restoring it needs the entity pool to be intact and no duplicate active relation
    snap = graph.restore_relation(db, RelationRevisionInput(
        case_id=case_id, relation_id=relation.id, expected_revision=relation.revision + 1),
        actor="human", entry="local-web")
    assert next(row for row in snap.relations if row.id == relation.id).removed_at is None


def test_remove_entity_reports_graphs_and_hides_relations(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    snap = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="图一"),
                              actor="human", entry="local-web")
    graph_id = snap.graphs[0].id
    a = _find(_entity(db, case_id, name="甲", graph_id=graph_id).entities, "甲")
    b = _find(_entity(db, case_id, name="乙").entities, "乙")
    snap = _relation(db, case_id, a.id, b.id, graph_id=graph_id)
    relation = snap.relations[0]
    impact = graph.get_removal_impact(db, case_id, entity_id=a.id)
    assert impact.count == 1
    snap = graph.remove_entity(db, EntityRevisionInput(
        case_id=case_id, entity_id=a.id, expected_revision=a.revision),
        actor="human", entry="local-web")
    assert "影响 1 张图" in (snap.notice or "")
    # relation rows survive so restoring the entity restores the picture
    assert any(row.id == relation.id for row in snap.relations)
    assert _codes(lambda: graph.restore_relation(db, RelationRevisionInput(
        case_id=case_id, relation_id=relation.id, expected_revision=relation.revision),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT


# --- layout and groups ------------------------------------------------------

def test_layout_and_group_round_trip(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    snap = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="图"),
                              actor="human", entry="local-web")
    graph_id = snap.graphs[0].id
    snap = _entity(db, case_id, name="甲", graph_id=graph_id)
    entity = _find(snap.entities, "甲")
    snap = graph.save_layout(db, SaveLayoutInput(
        case_id=case_id, graph_id=graph_id,
        nodes=[LayoutNode(node_id=entity.id, x=120.5, y=-40)]),
        actor="human", entry="local-web")
    node = next(row for row in snap.nodes if row.entity_id == entity.id)
    assert (node.x, node.y) == (120.5, -40) and node.revision == 1

    snap = graph.upsert_group(db, UpsertGroupInput(
        case_id=case_id, graph_id=graph_id, title="TEST-ONLY 集团",
        entity_ids=[entity.id]), actor="human", entry="local-web")
    group = snap.groups[0]
    node = next(row for row in snap.nodes if row.entity_id == entity.id)
    assert node.group_id == group.id
    # update with CAS
    snap = graph.upsert_group(db, UpsertGroupInput(
        case_id=case_id, graph_id=graph_id, group_id=group.id,
        expected_revision=group.revision, title="TEST-ONLY 集团（改）",
        collapsed=True), actor="human", entry="local-web")
    group = snap.groups[0]
    assert group.title.endswith("（改）") and group.collapsed is True
    assert _codes(lambda: graph.upsert_group(db, UpsertGroupInput(
        case_id=case_id, graph_id=graph_id, group_id=group.id,
        expected_revision=0, title="陈旧"), actor="human",
        entry="local-web")) == ErrorCode.CONFLICT
    # set_node_group null then remove group keeps the node
    snap = graph.set_node_group(db, SetNodeGroupInput(
        case_id=case_id, graph_id=graph_id, entity_id=entity.id, group_id=None),
        actor="human", entry="local-web")
    assert next(row for row in snap.nodes if row.entity_id == entity.id).group_id is None
    snap = graph.upsert_group(db, UpsertGroupInput(
        case_id=case_id, graph_id=graph_id, group_id=group.id,
        expected_revision=group.revision, title="TEST-ONLY 集团（改）",
        entity_ids=[entity.id]), actor="human", entry="local-web")
    group = snap.groups[0]
    snap = graph.remove_group(db, RemoveGroupInput(
        case_id=case_id, group_id=group.id, expected_revision=group.revision),
        actor="human", entry="local-web")
    assert next(row for row in snap.nodes if row.entity_id == entity.id).group_id is None
    # layout is per graph: a second graph's coordinates are independent
    snap = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="图二"),
                              actor="human", entry="local-web")
    second = next(row for row in snap.graphs if row.id != graph_id)
    snap = graph.add_to_graph(db, AddToGraphInput(
        case_id=case_id, graph_id=second.id, entity_ids=[entity.id]),
        actor="human", entry="local-web")
    node_second = next(row for row in snap.nodes
                       if row.graph_id == second.id and row.entity_id == entity.id)
    assert (node_second.x, node_second.y) == (0.0, 0.0)


def test_graph_rename_and_remove(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    snap = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="旧名"),
                              actor="human", entry="local-web")
    graph_id = snap.graphs[0].id
    snap = graph.update_graph(db, UpdateGraphInput(
        case_id=case_id, graph_id=graph_id, expected_revision=0,
        changes=GraphChanges(title="新名", description="TEST-ONLY")),
        actor="human", entry="local-web")
    assert next(row for row in snap.graphs if row.id == graph_id).title == "新名"
    snap = graph.remove_graph(db, GraphRevisionInput(
        case_id=case_id, graph_id=graph_id, expected_revision=1),
        actor="human", entry="local-web")
    assert next(row for row in snap.graphs if row.id == graph_id).removed_at is not None
    assert _codes(lambda: graph.update_graph(db, UpdateGraphInput(
        case_id=case_id, graph_id=graph_id, expected_revision=2,
        changes=GraphChanges(title="x")), actor="human",
        entry="local-web")) == ErrorCode.INVALID_INPUT


# --- same-case isolation ----------------------------------------------------

def test_cross_case_reference_rejected_by_service_and_database(db):
    first = build_graph_case(db)
    second = build_graph_case(db)
    snap = graph.create_graph(db, CreateGraphInput(case_id=first["case_id"], title="图"),
                              actor="human", entry="local-web")
    graph_id = snap.graphs[0].id
    foreign = _find(_entity(db, second["case_id"], name="外案主体").entities, "外案主体")
    assert _codes(lambda: graph.add_to_graph(db, AddToGraphInput(
        case_id=first["case_id"], graph_id=graph_id, entity_ids=[foreign.id]),
        actor="human", entry="local-web")) == ErrorCode.CASE_MISMATCH
    assert _codes(lambda: _relation(db, first["case_id"],
                                    _find(_entity(db, first["case_id"], name="甲").entities, "甲").id,
                                    foreign.id, graph_id=graph_id)) == ErrorCode.CASE_MISMATCH
    # the composite FK itself refuses a cross-case row
    with pytest.raises(IntegrityError):
        db.execute(text(
            "INSERT INTO relation_graph_nodes (id, case_id, graph_id, entity_id, x, y, revision) "
            "VALUES (:id, :case, :graph, :entity, 0, 0, 0)"),
            {"id": str(uuid.uuid4()), "case": str(first["case_id"]),
             "graph": str(graph_id), "entity": str(foreign.id)})
        db.flush()
    db.rollback()


# --- archive guard ----------------------------------------------------------

def test_archived_case_is_read_only(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    entity = _find(_entity(db, case_id, name="甲").entities, "甲")
    case_row = db.get(models.Case, case_id)
    cases.archive_case(db, ArchiveCaseInput(case_id=case_id, expected_revision=case_row.revision))
    assert graph.get_workspace(db, case_id).archived is True
    assert _codes(lambda: _entity(db, case_id, name="乙")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: graph.create_graph(db, CreateGraphInput(
        case_id=case_id, title="图"), actor="human",
        entry="local-web")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: graph.remove_entity(db, EntityRevisionInput(
        case_id=case_id, entity_id=entity.id, expected_revision=entity.revision),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    db.refresh(case_row)
    cases.unarchive_case(db, ArchiveCaseInput(case_id=case_id,
                                              expected_revision=case_row.revision))
    _entity(db, case_id, name="乙")


# --- agent review (D3 / K5 semantics) ---------------------------------------

def test_agent_change_is_pending_and_human_must_accept_first(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    # agent writes require a request key
    assert _codes(lambda: _entity(db, case_id, name="甲", actor="agent")) == ErrorCode.INVALID_INPUT
    snap = _entity(db, case_id, name="甲", actor="agent", request_key="agent-entity-1")
    pending = [row for row in snap.pending_changes if row.command == "add_entity"]
    assert len(pending) == 1 and pending[0].actor == "agent"
    entity = _find(snap.entities, "甲")
    # a human may not edit an object that only exists pending review
    assert _codes(lambda: graph.update_entity(db, UpdateEntityInput(
        case_id=case_id, entity_id=entity.id, expected_revision=0,
        changes=EntityChanges(note="人工先改")), actor="human",
        entry="local-web")) == ErrorCode.CONFLICT
    # replaying the same request key creates nothing new
    snap = _entity(db, case_id, name="甲（重复）", actor="agent", request_key="agent-entity-1")
    assert len([row for row in snap.entities if row.name.startswith("甲")]) == 1
    # accept makes it part of the confirmed state
    snap = graph.accept_change(db, GraphChangeIdInput(
        case_id=case_id, change_id=pending[0].id), actor="human", entry="local-web")
    assert not [row for row in snap.pending_changes if row.id == pending[0].id]
    snap = graph.update_entity(db, UpdateEntityInput(
        case_id=case_id, entity_id=entity.id, expected_revision=0,
        changes=EntityChanges(note="人工改")), actor="human", entry="local-web")
    assert next(row for row in snap.entities if row.id == entity.id).note == "人工改"


def test_agent_forbidden_commands_and_accept_all_scope(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    entity = _find(_entity(db, case_id, name="甲").entities, "甲")
    snap = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="图"),
                              actor="human", entry="local-web")
    graph_id = snap.graphs[0].id
    for call in (
        lambda: graph.create_snapshot(db, __import__("legal_workbench.schemas", fromlist=["CreateSnapshotInput"]).CreateSnapshotInput(
            case_id=case_id, graph_id=graph_id, stage_label="庭前"), actor="agent", entry="mcp"),
        lambda: graph.add_snapshot_consent(db, __import__("legal_workbench.schemas", fromlist=["AddConsentInput"]).AddConsentInput(
            case_id=case_id, snapshot_id=uuid.uuid4(), action="confirm", consenter="客户",
            consented_at="2026-01-01T00:00:00Z", channel="wechat", source_detail="TEST-ONLY"),
            actor="agent", entry="mcp"),
        lambda: graph.accept_change(db, GraphChangeIdInput(case_id=case_id, change_id=uuid.uuid4()),
                                    actor="agent", entry="mcp"),
        lambda: graph.revert_change(db, GraphChangeIdInput(case_id=case_id, change_id=uuid.uuid4()),
                                    actor="agent", entry="mcp"),
        lambda: graph.remove_entity(db, EntityRevisionInput(
            case_id=case_id, entity_id=entity.id, expected_revision=0), actor="agent", entry="mcp"),
        lambda: graph.save_layout(db, SaveLayoutInput(case_id=case_id, graph_id=graph_id),
                                  actor="agent", entry="mcp"),
    ):
        assert _codes(call) == ErrorCode.FORBIDDEN

    # two agent changes; accept-all must only take the ids the reviewer saw
    _entity(db, case_id, name="乙", actor="agent", request_key="agent-2")
    snap = _entity(db, case_id, name="丙", actor="agent", request_key="agent-3")
    pending = [row for row in snap.pending_changes if row.command == "add_entity"]
    assert len(pending) == 2
    first_id = pending[0].id
    snap = graph.accept_all(db, GraphAcceptAllInput(
        case_id=case_id, change_ids=[first_id]), actor="human", entry="local-web")
    assert not [row for row in snap.pending_changes if row.id == first_id]
    assert len(snap.pending_changes) == 1
    # an empty explicit scope is refused
    assert _codes(lambda: graph.accept_all(db, GraphAcceptAllInput(
        case_id=case_id, change_ids=[]), actor="human",
        entry="local-web")) == ErrorCode.INVALID_INPUT


def test_revert_agent_change_restores_previous_state(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    a = _find(_entity(db, case_id, name="甲").entities, "甲")
    b = _find(_entity(db, case_id, name="乙").entities, "乙")
    # agent creates a relation (pending); human reverts it -> soft deleted
    snap = _relation(db, case_id, a.id, b.id, actor="agent", request_key="agent-rel-1")
    relation = snap.relations[0]
    pending = [row for row in snap.pending_changes if row.command == "add_relation"][0]
    snap = graph.revert_change(db, GraphChangeIdInput(
        case_id=case_id, change_id=pending.id), actor="human", entry="local-web")
    assert next(row for row in snap.relations if row.id == relation.id).removed_at is not None
    assert not snap.pending_changes
    assert _codes(lambda: graph.revert_change(db, GraphChangeIdInput(
        case_id=case_id, change_id=pending.id), actor="human",
        entry="local-web")) == ErrorCode.INVALID_INPUT
