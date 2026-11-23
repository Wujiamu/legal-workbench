"""P2 consistency checks: graph/evidence invariants are reported, not repaired.

Soft-deleted entities/relations are a design choice (restoring the entity brings
the picture back), so they are not reported; missing rows, cross-case rows and
"evidenced without a source" are.
"""
from __future__ import annotations

import uuid

import pytest

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from legal_workbench import consistency, graph, models
from legal_workbench.schemas import (
    AddEntityInput, AddSourceInput, CreateGraphInput, AddGraphRelationInput, SourceRefInput,
)

from .graph_factories import build_graph_case


def _graph_issues(report):
    """Only P2 issues: the fixture materials have no files on disk, so the P1
    object checks legitimately report unrelated problems here."""
    return [issue for issue in report.issues
            if issue.kind.startswith("graph_") or issue.kind.startswith("evidence_")]


def _setup(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    snap = graph.create_graph(db, CreateGraphInput(case_id=case_id, title="图"),
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
    graph.add_source(db, AddSourceInput(
        case_id=case_id, relation_id=relation.id,
        ref=SourceRefInput(segment_id=case["segment_id"])), actor="human", entry="local-web")
    return case, graph_id, a, b, relation


def test_clean_graph_case_reports_no_issues(db, store):
    case, *_ = _setup(db)
    report = consistency.check_consistency(db, store, case_id=case["case_id"])
    assert _graph_issues(report) == []
    assert report.checked_graph_entities == 2
    assert report.checked_graph_sources == 1


def test_evidenced_without_source_is_reported(db, store):
    case, graph_id, a, b, relation = _setup(db)
    # raw SQL bypasses the service on purpose: consistency must catch the state
    db.execute(text("UPDATE graph_sources SET removed_at=now() WHERE relation_id=:id"),
               {"id": str(relation.id)})
    db.execute(text("UPDATE case_relations SET credibility='evidenced' WHERE id=:id"),
               {"id": str(relation.id)})
    db.commit()
    report = consistency.check_consistency(db, store, case_id=case["case_id"])
    kinds = {issue.kind for issue in _graph_issues(report)}
    assert "graph_evidenced_without_source" in kinds


def test_soft_deleted_entity_is_not_reported_and_fks_block_dangling_rows(db, store):
    case, graph_id, a, b, relation = _setup(db)
    db.execute(text("UPDATE case_entities SET removed_at=now() WHERE id=:id"),
               {"id": str(a.id)})
    db.commit()
    report = consistency.check_consistency(db, store, case_id=case["case_id"])
    assert _graph_issues(report) == []

    # soft delete keeps the rows, so a restored entity brings the picture back
    db.execute(text("UPDATE case_entities SET removed_at=NULL WHERE id=:id"), {"id": str(a.id)})
    db.commit()
    assert _graph_issues(consistency.check_consistency(
        db, store, case_id=case["case_id"])) == []

    # cross-case / dangling rows cannot be produced at all: the composite FKs hold
    for statement, params in (
        ("DELETE FROM case_relations WHERE id=:id", {"id": str(relation.id)}),
        ("DELETE FROM case_entities WHERE id=:id", {"id": str(b.id)}),
    ):
        with pytest.raises(IntegrityError):
            db.execute(text(statement), params)
            db.flush()
        db.rollback()


def test_snapshot_payload_integrity_and_version(db, store):
    case, graph_id, *_ = _setup(db)
    snapshot_id = uuid.uuid4()
    db.execute(text(
        "INSERT INTO relation_graph_snapshots "
        "(id, case_id, graph_id, version_no, stage_label, payload, excluded_pending_count, "
        " created_by) VALUES (:id, :case, :graph, 1, '庭前', '{}'::jsonb, 0, 'human')"),
        {"id": str(snapshot_id), "case": str(case["case_id"]), "graph": str(graph_id)})
    db.commit()
    report = consistency.check_consistency(db, store, case_id=case["case_id"])
    kinds = {issue.kind for issue in _graph_issues(report)}
    assert "graph_snapshot_payload_incomplete" in kinds
    assert report.checked_snapshots == 1


def test_evidence_item_in_removed_group_and_duplicate_positions(db, store):
    case = build_graph_case(db)
    case_id = case["case_id"]
    group_id = uuid.uuid4()
    db.execute(text(
        "INSERT INTO evidence_groups (id, case_id, position, title, created_by, revision, "
        "removed_at) VALUES (:id, :case, 0, '已删除组', 'human', 1, now())"),
        {"id": str(group_id), "case": str(case_id)})
    for position in (0, 0):
        db.execute(text(
            "INSERT INTO evidence_items (id, case_id, group_id, position, title, source_kind, "
            "review_state, created_by, revision) VALUES "
            "(:id, :case, :group, :pos, '条目', 'manual', 'confirmed', 'human', 0)"),
            {"id": str(uuid.uuid4()), "case": str(case_id), "group": str(group_id),
             "pos": position})
    db.commit()
    report = consistency.check_consistency(db, store, case_id=case_id)
    kinds = {issue.kind for issue in _graph_issues(report)}
    assert "evidence_item_in_removed_group" in kinds
    assert "evidence_item_duplicate_position" in kinds
    assert report.checked_evidence_items == 2


def test_report_serialisation_includes_new_counters(db, store):
    case, *_ = _setup(db)
    payload = consistency.check_consistency(
        db, store, case_id=case["case_id"]).to_dict()
    assert {"graph_entities", "graph_sources", "graph_snapshots",
            "evidence_items"} <= set(payload["checked"])
