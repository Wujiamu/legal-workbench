"""P2 audit and change-log coverage: every write command leaves one audit row and
one change row, and the operation vocabulary stays a single source of truth.

TEST-ONLY data, real PostgreSQL.
"""
from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import select, text

from legal_workbench import (audit, evidence_catalog, graph, graph_rules, models)
from legal_workbench.schemas import (
    AddAliasInput, AddEntityInput, AddEvidenceBindingInput, AddEvidenceGroupInput,
    AddEvidenceItemInput, AddSourceInput, CreateGraphInput, CreateSnapshotInput,
    EntityRevisionInput, AddGraphRelationInput, LayoutNode, RelationChanges,
    RelationRevisionInput, RemoveAliasInput, RemoveEvidenceBindingInput,
    RemoveSourceInput, SaveLayoutInput, SourceRefInput, UpdateEntityInput,
    UpdateRelationInput,
)

from .graph_factories import build_graph_case


def _ops(db, case_id) -> list[str]:
    return db.execute(select(models.AuditEvent.operation).where(
        models.AuditEvent.case_id == case_id).order_by(
        models.AuditEvent.created_at)).scalars().all()


def test_every_graph_write_records_audit_and_change(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    snap = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="TEST-ONLY 图"),
                              actor="human", entry="local-web")
    graph_id = snap.graphs[0].id
    snap = graph.add_entity(db, AddEntityInput(
        case_id=case_id, kind="org", name="TEST-ONLY 甲", graph_id=graph_id),
        actor="human", entry="local-web")
    a = snap.entities[0]
    snap = graph.add_alias(db, AddAliasInput(
        case_id=case_id, entity_id=a.id, alias="TEST-ONLY 别称"),
        actor="human", entry="local-web")
    alias = snap.aliases[0]
    graph.remove_alias(db, RemoveAliasInput(
        case_id=case_id, alias_id=alias.id), actor="human", entry="local-web")
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
        ref=SourceRefInput(segment_id=case["segment_id"])), actor="human", entry="local-web")
    source = next(row for row in snap.sources if row.relation_id == relation.id)
    snap = graph.update_relation(db, UpdateRelationInput(
        case_id=case_id, relation_id=relation.id, expected_revision=0,
        changes=RelationChanges(credibility="evidenced")), actor="human", entry="local-web")
    relation = next(row for row in snap.relations if row.id == relation.id)
    graph.save_layout(db, SaveLayoutInput(
        case_id=case_id, graph_id=graph_id,
        nodes=[LayoutNode(node_id=a.id, x=1, y=2)]), actor="human", entry="local-web")
    snap = graph.update_entity(db, UpdateEntityInput(
        case_id=case_id, entity_id=a.id, expected_revision=a.revision,
        changes=__import__("legal_workbench.schemas", fromlist=["EntityChanges"]).EntityChanges(
            note="TEST-ONLY 备注")), actor="human", entry="local-web")
    graph.remove_source(db, RemoveSourceInput(
        case_id=case_id, source_id=source.id, downgrade_to="disputed"),
        actor="human", entry="local-web")
    graph.remove_relation(db, RelationRevisionInput(
        case_id=case_id, relation_id=relation.id,
        expected_revision=next(row.revision for row in
                               graph.get_workspace(db, case_id).relations
                               if row.id == relation.id)),
        actor="human", entry="local-web")
    graph.restore_relation(db, RelationRevisionInput(
        case_id=case_id, relation_id=relation.id,
        expected_revision=next(row.revision for row in
                               graph.get_workspace(db, case_id).relations
                               if row.id == relation.id)),
        actor="human", entry="local-web")
    graph.remove_entity(db, EntityRevisionInput(
        case_id=case_id, entity_id=b.id,
        expected_revision=next(row.revision for row in
                               graph.get_workspace(db, case_id).entities
                               if row.id == b.id)),
        actor="human", entry="local-web")
    graph.restore_entity(db, EntityRevisionInput(
        case_id=case_id, entity_id=b.id,
        expected_revision=next(row.revision for row in
                               graph.get_workspace(db, case_id).entities
                               if row.id == b.id)),
        actor="human", entry="local-web")
    graph.create_snapshot(db, CreateSnapshotInput(
        case_id=case_id, graph_id=graph_id, stage_label="庭前"),
        actor="human", entry="local-web")

    operations = _ops(db, case_id)
    expected = {
        "graph.create_graph", "graph.add_entity", "graph.add_alias", "graph.remove_alias",
        "graph.add_relation", "graph.add_source", "graph.update_relation",
        "graph.save_layout", "graph.update_entity", "graph.remove_source",
        "graph.remove_relation", "graph.restore_relation", "graph.remove_entity",
        "graph.restore_entity", "graph.create_snapshot",
    }
    assert expected <= set(operations), sorted(expected - set(operations))
    # every graph change row carries a command that also reached the audit log
    commands = db.execute(select(models.GraphChange.command).where(
        models.GraphChange.case_id == case_id)).scalars().all()
    assert {f"graph.{command}" for command in commands} <= set(operations)


def test_every_evidence_write_records_audit_and_change(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    catalog = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="第一组"), actor="human", entry="local-web")
    group = catalog.groups[0]
    catalog = evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=group.id, title="TEST-ONLY 条目"),
        actor="human", entry="local-web")
    item = catalog.items[0]
    catalog = evidence_catalog.add_binding(db, AddEvidenceBindingInput(
        case_id=case_id, item_id=item.id, material_id=case["material_id"]),
        actor="human", entry="local-web")
    binding = catalog.bindings[0]
    evidence_catalog.remove_binding(db, RemoveEvidenceBindingInput(
        case_id=case_id, binding_id=binding.id), actor="human", entry="local-web")
    catalog = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="第二组"), actor="human", entry="local-web")
    second = next(row for row in catalog.groups if row.title == "第二组")
    evidence_catalog.reorder_groups(db, __import__(
        "legal_workbench.schemas", fromlist=["ReorderEvidenceGroupsInput"]
    ).ReorderEvidenceGroupsInput(case_id=case_id, group_ids=[second.id, group.id]),
        actor="human", entry="local-web")

    operations = _ops(db, case_id)
    assert {"evidence.add_group", "evidence.add_item", "evidence.add_binding",
            "evidence.remove_binding", "evidence.reorder_groups"} <= set(operations)
    commands = db.execute(select(models.EvidenceChange.command).where(
        models.EvidenceChange.case_id == case_id)).scalars().all()
    assert {f"evidence.{command}" for command in commands} <= set(operations)


def test_audit_vocabulary_matches_the_rules_and_the_database(db):
    """The three lists (service, audit.OPERATIONS, DB CHECK) must agree exactly."""
    declared = {op for op in audit.OPERATIONS if op.startswith(("graph.", "evidence."))}
    rules_ops = {f"graph.{name}" for name in graph_rules.COMMANDS}
    rules_ops |= {f"evidence.{name}" for name in graph_rules.EVIDENCE_COMMANDS}
    assert declared == rules_ops, sorted(declared ^ rules_ops)

    # each declared operation is really used by the matching service module
    sources = {"graph": open(graph.__file__, encoding="utf-8").read(),
               "evidence": open(evidence_catalog.__file__, encoding="utf-8").read()}
    for operation in declared:
        domain, command = operation.split(".", 1)
        assert f'"{command}"' in sources[domain], operation

    # and the database CHECK accepts every one of them
    case = build_graph_case(db)
    case_id = case["case_id"]
    for operation in sorted(declared):
        db.execute(text(
            "INSERT INTO audit_events (id, case_id, actor, operation, target_type, "
            "target_id) VALUES (:id, :c, 'local-web', :op, 'graph', :t)"),
            {"id": str(uuid.uuid4()), "c": str(case_id), "op": operation,
             "t": str(uuid.uuid4())})
    db.commit()
    stored = db.execute(select(models.AuditEvent.operation).where(
        models.AuditEvent.case_id == case_id)).scalars().all()
    assert set(stored) == declared

    # 白名单是前端用的扁平联合（graph 9 + evidence 前缀 5 = 14）；
    # 每个名字都要能在对应域的命令表里找到（evidence 名带 ``_evidence`` 中缀）。
    assert len(graph_rules.AGENT_COMMANDS) == 14
    graph_agent = {name for name in graph_rules.AGENT_COMMANDS
                   if "_evidence_" not in name and not name.startswith("import_evidence_")}
    assert graph_agent <= set(graph_rules.COMMANDS)
    assert set(graph_rules.EVIDENCE_AGENT_COMMANDS) <= set(graph_rules.EVIDENCE_COMMANDS)


def test_agent_whitelist_is_a_subset_of_all_commands():
    graph_agent = {name for name in graph_rules.AGENT_COMMANDS
                   if "_evidence_" not in name and not name.startswith("import_evidence_")}
    assert graph_agent < set(graph_rules.COMMANDS)
    assert set(graph_rules.EVIDENCE_AGENT_COMMANDS) < set(graph_rules.EVIDENCE_COMMANDS)
    # the two whitelists are disjoint: each service guards with its own names
    assert graph_agent.isdisjoint(graph_rules.EVIDENCE_COMMANDS)
    assert set(graph_rules.EVIDENCE_AGENT_COMMANDS).isdisjoint(graph_rules.COMMANDS)
    human_only = {"create_snapshot", "restore_snapshot", "add_snapshot_consent",
                  "accept_change", "accept_all", "revert_change", "save_layout",
                  "remove_entity", "remove_relation", "remove_alias", "remove_source",
                  "remove_graph", "remove_from_graph", "remove_group"}
    assert human_only.isdisjoint(graph_rules.AGENT_COMMANDS)
    evidence_human_only = {"confirm_item", "reject_item", "confirm_binding",
                           "reject_binding", "accept_change", "accept_all",
                           "revert_change", "remove_group", "remove_item",
                           "remove_binding", "reorder_groups", "move_item",
                           "update_group"}
    assert evidence_human_only.isdisjoint(graph_rules.EVIDENCE_AGENT_COMMANDS)
    # 9 graph + 5 evidence = 14 agent commands (change record §4 rule 7);
    # AGENT_COMMANDS 是前端使用的扁平联合，证据域另有自己的短名白名单。
    assert len(graph_rules.AGENT_COMMANDS) == 14
    assert len(graph_rules.EVIDENCE_AGENT_COMMANDS) == 5
