"""P2 concurrency: the shared case write lock, CAS 409, gap-free change seq and
idempotent ``request_key`` replays.

Two independent connections synchronize on the same contention point, the way the
archive/race tests do — a lock held by one connection must make the other wait,
and the loser must see a stale revision rather than overwrite it.
TEST-ONLY data, real PostgreSQL.
"""
from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from legal_workbench import evidence_catalog, graph, models
from legal_workbench.db import sqlalchemy_url
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (
    AddEntityInput, AddEvidenceGroupInput, AddEvidenceItemInput, CreateGraphInput,
    EntityChanges, AddGraphRelationInput, ReorderEvidenceGroupsInput, UpdateEntityInput,
)

from .graph_factories import build_graph_case


def _engine(test_database):
    return create_engine(sqlalchemy_url(test_database[1]), future=True)


def _hold_case_lock(engine, case_id) -> Session:
    session = Session(bind=engine)
    session.execute(select(models.Case).where(
        models.Case.id == case_id).with_for_update()).scalar_one()
    return session


def _run(fn, results, key):
    def target():
        try:
            results.append((key, fn()))
        except WorkbenchError as exc:
            results.append((key, exc.code))
        except Exception as exc:  # noqa: BLE001 - carry unexpected errors back
            results.append((key, f"error:{type(exc).__name__}:{exc}"))

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def test_graph_write_waits_for_the_case_lock(db, test_database):
    case = build_graph_case(db)
    case_id = case["case_id"]
    db.commit()
    engine = _engine(test_database)
    blocker = _hold_case_lock(engine, case_id)
    results: list = []
    try:
        def add():
            session = Session(bind=engine)
            try:
                return graph.add_entity(session, AddEntityInput(
                    case_id=case_id, kind="org", name="TEST-ONLY 等锁主体"),
                    actor="human", entry="local-web")
            finally:
                session.close()

        thread = _run(add, results, "add")
        thread.join(0.8)
        assert thread.is_alive(), "关系图写入没有等待同案写锁"
        blocker.commit()
        thread.join(15)
    finally:
        blocker.rollback()
        blocker.close()
        engine.dispose()
    assert len(results) == 1 and not isinstance(results[0][1], ErrorCode), results
    assert any(row.name == "TEST-ONLY 等锁主体" for row in results[0][1].entities)


def test_evidence_write_waits_for_the_case_lock(db, test_database):
    case = build_graph_case(db)
    case_id = case["case_id"]
    db.commit()
    engine = _engine(test_database)
    blocker = _hold_case_lock(engine, case_id)
    results: list = []
    try:
        def add():
            session = Session(bind=engine)
            try:
                return evidence_catalog.add_group(session, AddEvidenceGroupInput(
                    case_id=case_id, title="TEST-ONLY 等锁分组"),
                    actor="human", entry="local-web")
            finally:
                session.close()

        thread = _run(add, results, "add")
        thread.join(0.8)
        assert thread.is_alive(), "证据目录写入没有等待同案写锁"
        blocker.commit()
        thread.join(15)
    finally:
        blocker.rollback()
        blocker.close()
        engine.dispose()
    assert len(results) == 1 and not isinstance(results[0][1], ErrorCode), results


def test_concurrent_cas_only_one_wins(db, test_database):
    """Two writers holding the same revision: the second gets 409, not a lost update."""
    case = build_graph_case(db)
    case_id = case["case_id"]
    snap = graph.add_entity(db, AddEntityInput(
        case_id=case_id, kind="org", name="TEST-ONLY 甲"), actor="human", entry="local-web")
    entity = snap.entities[0]
    db.commit()
    engine = _engine(test_database)
    results: list = []
    try:
        def rename(name):
            session = Session(bind=engine)
            try:
                return graph.update_entity(session, UpdateEntityInput(
                    case_id=case_id, entity_id=entity.id, expected_revision=0,
                    changes=EntityChanges(name=name)), actor="human", entry="local-web")
            finally:
                session.close()

        threads = [_run(lambda: rename("TEST-ONLY 乙"), results, "a"),
                   _run(lambda: rename("TEST-ONLY 丙"), results, "b")]
        for thread in threads:
            thread.join(20)
    finally:
        engine.dispose()
    wins = [key for key, value in results if not isinstance(value, ErrorCode)]
    codes = [value for _, value in results if isinstance(value, ErrorCode)]
    assert len(wins) == 1 and codes == [ErrorCode.CONFLICT], results
    db.expire_all()
    assert db.get(models.CaseEntity, entity.id).revision == 1


def test_change_seq_is_gap_free_per_case_and_domain(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    for index in range(4):
        graph.add_entity(db, AddEntityInput(
            case_id=case_id, kind="org", name=f"TEST-ONLY 主体{index}"),
            actor="human", entry="local-web")
    catalog = evidence_catalog.add_group(db, AddEvidenceGroupInput(
        case_id=case_id, title="TEST-ONLY 第一组"), actor="human", entry="local-web")
    evidence_catalog.add_item(db, AddEvidenceItemInput(
        case_id=case_id, group_id=catalog.groups[0].id, title="TEST-ONLY 条目"),
        actor="human", entry="local-web")

    graph_seqs = db.execute(select(models.GraphChange.seq).where(
        models.GraphChange.case_id == case_id).order_by(models.GraphChange.seq)).scalars().all()
    evidence_seqs = db.execute(select(models.EvidenceChange.seq).where(
        models.EvidenceChange.case_id == case_id).order_by(
        models.EvidenceChange.seq)).scalars().all()
    assert graph_seqs == list(range(1, len(graph_seqs) + 1))
    assert evidence_seqs == list(range(1, len(evidence_seqs) + 1))
    # each domain keeps its own counter on the shared table
    seqs = dict(db.execute(select(models.CaseChangeSeq.domain, models.CaseChangeSeq.seq).where(
        models.CaseChangeSeq.case_id == case_id)).all())
    assert seqs["graph"] == graph_seqs[-1]
    assert seqs["evidence"] == evidence_seqs[-1]
    # progress is untouched by the new domains
    assert "progress" not in seqs


def test_request_key_replay_is_idempotent_under_retry(db):
    case = build_graph_case(db)
    case_id = case["case_id"]
    snap = graph.create_graph(db, CreateGraphInput(
        case_id=case_id, title="TEST-ONLY 图", request_key="graph-1"),
        actor="agent", entry="mcp")
    assert len(snap.graphs) == 1
    graph_id = snap.graphs[0].id
    for _ in range(2):
        snap = graph.create_graph(db, CreateGraphInput(
            case_id=case_id, title="TEST-ONLY 图（重放）", request_key="graph-1"),
            actor="agent", entry="mcp")
        assert len(snap.graphs) == 1
    # a different key with the same title is a new graph
    snap = graph.create_graph(db, CreateGraphInput(
        case_id=case_id, title="TEST-ONLY 图", request_key="graph-2"),
        actor="agent", entry="mcp")
    assert len(snap.graphs) == 2
    assert len([row for row in snap.graphs if row.id == graph_id]) == 1


def test_duplicate_relation_is_rejected_by_the_database(db):
    """The same triple cannot be inserted twice even if a caller skips the service."""
    case = build_graph_case(db)
    case_id = case["case_id"]
    snap = graph.add_entity(db, AddEntityInput(
        case_id=case_id, kind="org", name="TEST-ONLY 甲"), actor="human", entry="local-web")
    a = snap.entities[0]
    snap = graph.add_entity(db, AddEntityInput(
        case_id=case_id, kind="org", name="TEST-ONLY 乙"), actor="human", entry="local-web")
    b = next(row for row in snap.entities if row.name == "TEST-ONLY 乙")
    graph.add_relation(db, AddGraphRelationInput(
        case_id=case_id, from_entity_id=a.id, to_entity_id=b.id, kind="equity_control",
        credibility="suspected"), actor="human", entry="local-web")
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        db.execute(text(
            "INSERT INTO case_relations (id, case_id, from_entity_id, to_entity_id, directed, "
            "kind, credibility, attrs, created_by, updated_by, revision) VALUES "
            "(:id, :c, :a, :b, true, 'equity_control', 'suspected', '{}'::jsonb, "
            "'human', 'human', 0)"),
            {"id": str(uuid.uuid4()), "c": str(case_id), "a": str(a.id), "b": str(b.id)})
        db.flush()
    db.rollback()
