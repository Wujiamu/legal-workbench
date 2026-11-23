"""Reviewer probes, run under pytest so conftest recreates legal_workbench_test safely.

Read-only review: no business code is modified; these probes only exercise the
service against the throwaway test database.
"""
from __future__ import annotations

import threading
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from legal_workbench import models, progress, cases
from legal_workbench.db import sqlalchemy_url
from legal_workbench.errors import WorkbenchError
from legal_workbench.schemas import (
    AddNodeInput, ChangeIdInput, InsertFragmentInput, LinkNodeInput, LinkTarget,
    MoveNodeInput, NodeChanges, NodeRevisionInput, SetActiveStageInput,
    UnlinkInput, UpdateNodeInput,
)

from .factories import build_material, create_case as raw_case


def _node(snap, title):
    return next(n for n in snap.nodes if n.title == title and n.removed_at is None)


def test_probe_stage_revert_restores_both(db):
    cid = cases.create_case(db, title="TEST-ONLY probe-stage").case_id
    snap = progress.get_progress(db, cid)
    filing = _node(snap, "立案")
    progress.set_active_stage(db, SetActiveStageInput(
        case_id=cid, stage_id=filing.id, expected_revision=filing.revision),
        actor="human", entry="local-web")
    snap = progress.get_progress(db, cid)
    hearing = _node(snap, "庭审")
    snap = progress.set_active_stage(db, SetActiveStageInput(
        case_id=cid, stage_id=hearing.id, expected_revision=hearing.revision),
        actor="agent", entry="mcp")
    ch = snap.pending_changes[0]
    snap = progress.revert_change(db, ChangeIdInput(case_id=cid, change_id=ch.id),
                                  actor="human", entry="local-web")
    assert _node(snap, "立案").state == "in_progress"   # previous stage restored
    assert _node(snap, "庭审").state == "not_started"   # target stage restored


def test_probe_unlink_revert_restores_original_id(db):
    cid = cases.create_case(db, title="TEST-ONLY probe-link").case_id
    built = build_material(db, case_id=cid)
    db.commit()
    snap = progress.add_node(db, AddNodeInput(case_id=cid, kind="stage", title="证据"),
                             actor="human", entry="local-web")
    n = _node(snap, "证据")
    snap = progress.link_node(db, LinkNodeInput(
        case_id=cid, node_id=n.id, target=LinkTarget(material_id=built["material_id"])),
        actor="agent", entry="mcp")
    lid = snap.links[0].id
    ch_add = [c for c in snap.changes if c.command == "link"][-1]
    snap = progress.unlink_node(db, UnlinkInput(case_id=cid, link_id=lid),
                                actor="human", entry="local-web")
    ch_unlink = [c for c in snap.changes if c.command == "unlink"][-1]
    snap = progress.revert_change(db, ChangeIdInput(case_id=cid, change_id=ch_unlink.id),
                                  actor="human", entry="local-web")
    assert any(l.id == lid for l in snap.links), "revert unlink must restore original id"
    # now revert the agent link-add while the link exists again -> deletes it again
    snap = progress.revert_change(db, ChangeIdInput(case_id=cid, change_id=ch_add.id),
                                  actor="human", entry="local-web")
    assert snap.links == []


def test_probe_move_to_removed_parent_no_crash(db):
    cid = cases.create_case(db, title="TEST-ONLY probe-cycle").case_id
    snap = progress.add_node(db, AddNodeInput(case_id=cid, kind="program", title="P"),
                             actor="human", entry="local-web")
    p = _node(snap, "P")
    snap = progress.add_node(db, AddNodeInput(case_id=cid, parent_id=p.id, kind="stage", title="S"),
                             actor="human", entry="local-web")
    st = _node(snap, "S")
    snap = progress.add_node(db, AddNodeInput(case_id=cid, parent_id=st.id, kind="work", title="W"),
                             actor="human", entry="local-web")
    w = _node(snap, "W")
    progress.remove_node(db, NodeRevisionInput(case_id=cid, node_id=w.id, expected_revision=w.revision),
                         actor="human", entry="local-web")
    snap = progress.get_progress(db, cid)
    st = next(n for n in snap.nodes if n.title == "S")
    progress.remove_node(db, NodeRevisionInput(case_id=cid, node_id=st.id, expected_revision=st.revision),
                         actor="human", entry="local-web")
    snap = progress.get_progress(db, cid)
    p = _node(snap, "P")
    try:
        progress.move_node(db, MoveNodeInput(case_id=cid, node_id=p.id,
                                             expected_revision=p.revision, new_parent_id=st.id),
                           actor="human", entry="local-web")
        raise AssertionError("move onto removed parent was accepted")
    except WorkbenchError as e:
        assert e.code.value in ("not_found", "invalid_input")


def test_probe_concurrent_first_writes(db, test_database):
    """Four threads race to create the seq row + first nodes on a fresh case."""
    cid = raw_case(db, "TEST-ONLY probe-race")
    db.commit()
    engine = create_engine(sqlalchemy_url(test_database[1]), future=True)
    errs = []

    def worker(label):
        session = Session(bind=engine)
        try:
            progress.add_node(session, AddNodeInput(case_id=cid, kind="stage", title=label),
                              actor="human", entry="local-web")
        except Exception as e:  # noqa: BLE001
            errs.append((label, repr(e)))
        finally:
            session.close()

    ts = [threading.Thread(target=worker, args=(f"race-{i}",)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    engine.dispose()
    snap = progress.get_progress(db, cid)
    seqrows = db.execute(select(models.CaseChangeSeq).where(
        models.CaseChangeSeq.case_id == cid)).scalars().all()
    print("RACE errs:", errs)
    print("RACE seq:", snap.seq, "changes:", len(snap.changes),
          "seq_rows:", len(seqrows), "change_seqs:", sorted(c.seq for c in snap.changes))
    assert not errs, f"concurrent first writes failed: {errs}"
    assert snap.seq == 4 and len(snap.changes) == 4
    assert len(seqrows) == 1
    assert sorted(c.seq for c in snap.changes) == [1, 2, 3, 4]


def test_probe_revert_human_none_change(db):
    cid = cases.create_case(db, title="TEST-ONLY probe-none").case_id
    snap = progress.add_node(db, AddNodeInput(case_id=cid, kind="stage", title="人工阶段"),
                             actor="human", entry="local-web")
    ch = snap.changes[-1]
    assert ch.review_state == "none"
    snap = progress.revert_change(db, ChangeIdInput(case_id=cid, change_id=ch.id),
                                  actor="human", entry="local-web")
    removed = [n.title for n in snap.nodes if n.removed_at is not None]
    print("NONE-REVERT removed:", removed, "state:", 
          next(c.review_state for c in snap.changes if c.id == ch.id))
    assert "人工阶段" in removed  # backend allows reverting human 'none' changes


def test_probe_fragment_revert_whole_refusal(db):
    cid = cases.create_case(db, title="TEST-ONLY probe-frag").case_id
    snap = progress.insert_fragment(db, InsertFragmentInput(case_id=cid, fragment_key="second-instance"),
                                    actor="agent", entry="mcp")
    ch = snap.pending_changes[0]
    child = _node(snap, "上诉")
    progress.update_node(db, UpdateNodeInput(case_id=cid, node_id=child.id,
                                             expected_revision=child.revision,
                                             changes=NodeChanges(note="后续")),
                         actor="human", entry="local-web")
    try:
        progress.revert_change(db, ChangeIdInput(case_id=cid, change_id=ch.id),
                               actor="human", entry="local-web")
        raise AssertionError("partial fragment revert was accepted")
    except WorkbenchError as e:
        assert e.code.value == "conflict"
    snap = progress.get_progress(db, cid)
    assert all(n.removed_at is None for n in snap.nodes
               if n.title in {"二审", "上诉", "二审审理", "二审裁判"})


def test_probe_revert_remove_then_restore_position(db):
    """Revert of remove_node restores node; check position/parent integrity."""
    cid = cases.create_case(db, title="TEST-ONLY probe-remove").case_id
    snap = progress.add_node(db, AddNodeInput(case_id=cid, kind="program", title="P"),
                             actor="human", entry="local-web")
    p = _node(snap, "P")
    snap = progress.add_node(db, AddNodeInput(case_id=cid, parent_id=p.id, kind="stage", title="A"),
                             actor="human", entry="local-web")
    snap = progress.add_node(db, AddNodeInput(case_id=cid, parent_id=p.id, kind="stage", title="B"),
                             actor="human", entry="local-web")
    a = _node(snap, "A")
    snap = progress.remove_node(db, NodeRevisionInput(case_id=cid, node_id=a.id,
                                                      expected_revision=a.revision),
                                actor="agent", entry="mcp")
    ch = snap.pending_changes[0]
    snap = progress.revert_change(db, ChangeIdInput(case_id=cid, change_id=ch.id),
                                  actor="human", entry="local-web")
    a2 = _node(snap, "A")
    b2 = _node(snap, "B")
    print("REMOVE-REVERT positions A,B:", a2.position, b2.position)
    assert a2.removed_at is None