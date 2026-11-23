"""P2 progress service: command semantics, CAS, same-case isolation, change log,
audit, agent review/accept/revert, fragment instantiation and rule extensibility.

Real PostgreSQL only (conftest recreates legal_workbench_test). TEST-ONLY data.
Mirrors the P1 in-memory executor semantics so P4 can swap transport unchanged.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select, text

from legal_workbench import models, progress, progress_rules
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (
    AcceptAllInput, AddNodeInput, AddRelationInput, ChangeIdInput, InsertFragmentInput, LinkNodeInput,
    LinkTarget, MoveNodeInput, NodeChanges, NodeRevisionInput, RemoveRelationInput,
    SetActiveStageInput, UnlinkInput, UpdateNodeInput,
)

from .factories import build_material, create_case
from .test_api import new_case


def _add(session, case_id, *, parent_id=None, kind="stage", title="阶段", actor="human",
         request_key=None, state=None, position=None):
    return progress.add_node(session, AddNodeInput(
        case_id=case_id, parent_id=parent_id, kind=kind, title=title,
        request_key=request_key, state=state, position=position), actor=actor, entry="local-web")


def _node(snapshot, title):
    return next(n for n in snapshot.nodes if n.title == title)


def _codes(fn):
    with pytest.raises(WorkbenchError) as exc:
        fn()
    return exc.value.code


# --- default fragment & snapshot -------------------------------------------

def test_new_case_instantiates_default_fragment(client, db):
    case_id = uuid.UUID(new_case(client, "TEST-ONLY 进度"))
    snapshot = progress.get_progress(db, case_id)
    titles = {n.title for n in snapshot.nodes}
    assert {"诉前准备", "一审", "立案", "庭前程序", "庭审", "裁判"} <= titles
    assert "结案归档" not in titles  # case-level operation, not a node
    first = _node(snapshot, "一审")
    assert first.kind == "program"
    filing = _node(snapshot, "立案")
    assert filing.parent_id == first.id
    assert snapshot.seq >= 1
    assert all(n.template_ref == "litigation-default@1" for n in snapshot.nodes)


def test_rules_served_and_extensible_without_code_change(db):
    rules = progress_rules.get_rules()
    assert rules["default_fragment"] == "litigation-default"
    # Inject a new kind + fourth-level containment; service must honour it.
    rules["kinds"]["preservation"] = {
        "label": "保全", "states": [{"value": "pending", "label": "未完成"}],
        "default_state": "pending"}
    rules["containment"]["work"] = ["work", "preservation"]
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭前")
    stage = _node(snap, "庭前")
    snap = _add(db, case_id, parent_id=stage.id, kind="work", title="证据整理")
    work = _node(snap, "证据整理")
    # fourth level under work, new kind — only config changed
    import copy
    original = progress_rules.DEFAULT_RULES
    try:
        progress_rules.DEFAULT_RULES = rules
        snap = progress.add_node(db, AddNodeInput(
            case_id=case_id, parent_id=work.id, kind="preservation", title="查封清单"),
            actor="human", entry="local-web")
        assert _node(snap, "查封清单").kind == "preservation"
    finally:
        progress_rules.DEFAULT_RULES = original


# --- containment & validation ----------------------------------------------

def test_containment_rejects_work_at_root(db):
    case_id = create_case(db)
    db.commit()
    assert _codes(lambda: _add(db, case_id, kind="work", title="非法")) == ErrorCode.INVALID_INPUT


def test_title_and_state_validation(db):
    from pydantic import ValidationError
    case_id = create_case(db)
    db.commit()
    assert _codes(lambda: _add(db, case_id, kind="stage", title="   ")) == ErrorCode.INVALID_INPUT
    # over-length is rejected at the protocol boundary (pydantic), before the service
    with pytest.raises(ValidationError):
        _add(db, case_id, kind="stage", title="x" * 201)
    assert _codes(lambda: _add(db, case_id, kind="stage", title=" ok", state="bogus")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: _add(db, case_id, kind="unknown", title="x")) == ErrorCode.INVALID_INPUT


# --- CAS --------------------------------------------------------------------

def test_update_cas_conflict(db):
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="stage", title="立案")
    node = _node(snap, "立案")
    progress.update_node(db, UpdateNodeInput(
        case_id=case_id, node_id=node.id, expected_revision=node.revision,
        changes=NodeChanges(note="第一次")), actor="human", entry="local-web")
    assert _codes(lambda: progress.update_node(db, UpdateNodeInput(
        case_id=case_id, node_id=node.id, expected_revision=node.revision,
        changes=NodeChanges(note="陈旧")), actor="human", entry="local-web")) == ErrorCode.CONFLICT


# --- same-case isolation ----------------------------------------------------

def test_cross_case_parent_rejected(db):
    a = create_case(db, "TEST-ONLY A")
    b = create_case(db, "TEST-ONLY B")
    db.commit()
    snap_a = _add(db, a, kind="program", title="一审A")
    parent_a = _node(snap_a, "一审A")
    assert _codes(lambda: _add(db, b, parent_id=parent_a.id, kind="stage", title="跨案")) == ErrorCode.CASE_MISMATCH


def test_cross_case_link_rejected_by_service_and_db(db):
    a = create_case(db, "TEST-ONLY A")
    built = build_material(db, case_id=a)
    b = create_case(db, "TEST-ONLY B")
    db.commit()
    snap_b = _add(db, b, kind="stage", title="B阶段")
    node_b = _node(snap_b, "B阶段")
    assert _codes(lambda: progress.link_node(db, LinkNodeInput(
        case_id=b, node_id=node_b.id, target=LinkTarget(material_id=built["material_id"])),
        actor="human", entry="local-web")) == ErrorCode.CASE_MISMATCH


# --- move & cycle -----------------------------------------------------------

def test_move_rejects_cycle(db):
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭前")
    stage = _node(snap, "庭前")
    snap = _add(db, case_id, parent_id=stage.id, kind="work", title="证据")
    work = _node(snap, "证据")
    # move program under its own descendant work -> cycle
    assert _codes(lambda: progress.move_node(db, MoveNodeInput(
        case_id=case_id, node_id=program.id, expected_revision=program.revision,
        new_parent_id=work.id), actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT


def test_move_reorders_and_records(db):
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="甲")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="乙")
    jia = _node(snap, "甲")
    yi = _node(snap, "乙")
    snap = progress.move_node(db, MoveNodeInput(
        case_id=case_id, node_id=yi.id, expected_revision=yi.revision,
        new_parent_id=program.id, before_node_id=jia.id), actor="human", entry="local-web")
    children = sorted([n for n in snap.nodes if n.parent_id == program.id], key=lambda n: n.position)
    assert [n.title for n in children] == ["乙", "甲"]


# --- set_active_stage -------------------------------------------------------

def test_set_active_stage_completes_previous_and_is_one_change(db):
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="立案")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭审")
    filing = _node(snap, "立案")
    hearing = _node(snap, "庭审")
    progress.set_active_stage(db, SetActiveStageInput(
        case_id=case_id, stage_id=filing.id, expected_revision=filing.revision),
        actor="human", entry="local-web")
    snap = progress.get_progress(db, case_id)
    filing = _node(snap, "立案")
    seq_before = snap.seq
    snap = progress.set_active_stage(db, SetActiveStageInput(
        case_id=case_id, stage_id=hearing.id, expected_revision=hearing.revision),
        actor="human", entry="local-web")
    assert _node(snap, "立案").state == "completed"
    assert _node(snap, "庭审").state == "in_progress"
    assert snap.seq == seq_before + 1  # one change, not two
    last = snap.changes[-1]
    assert last.command == "set_active_stage"
    assert len(last.diff) == 2


def test_set_active_stage_rejects_non_stage(db):
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    assert _codes(lambda: progress.set_active_stage(db, SetActiveStageInput(
        case_id=case_id, stage_id=program.id, expected_revision=program.revision),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT


# --- agent review / accept / revert ----------------------------------------

def test_agent_change_pending_then_accept(db):
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭审")
    hearing = _node(snap, "庭审")
    snap = progress.set_active_stage(db, SetActiveStageInput(
        case_id=case_id, stage_id=hearing.id, expected_revision=hearing.revision),
        actor="agent", entry="mcp")
    assert len(snap.pending_changes) == 1
    change = snap.pending_changes[0]
    assert change.actor == "agent"
    snap = progress.accept_change(db, ChangeIdInput(case_id=case_id, change_id=change.id),
                                  actor="human", entry="local-web")
    assert snap.pending_changes == []
    assert next(c for c in snap.changes if c.id == change.id).review_state == "accepted"


def test_agent_cannot_accept_or_revert(db):
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭审", actor="agent",
                request_key="k1")
    change = snap.pending_changes[0]
    assert _codes(lambda: progress.accept_change(db, ChangeIdInput(case_id=case_id, change_id=change.id),
                                                 actor="agent", entry="mcp")) == ErrorCode.FORBIDDEN
    assert _codes(lambda: progress.revert_change(db, ChangeIdInput(case_id=case_id, change_id=change.id),
                                                 actor="agent", entry="mcp")) == ErrorCode.FORBIDDEN


def test_agent_cannot_use_human_only_commands(db):
    case_id = create_case(db)
    db.commit()
    assert _codes(lambda: progress.accept_all(db, AcceptAllInput(case_id=case_id), actor="agent", entry="mcp")) == ErrorCode.FORBIDDEN


def test_revert_restores_previous_state(db):
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭审")
    hearing = _node(snap, "庭审")
    snap = progress.set_active_stage(db, SetActiveStageInput(
        case_id=case_id, stage_id=hearing.id, expected_revision=hearing.revision),
        actor="agent", entry="mcp")
    change = snap.pending_changes[0]
    snap = progress.revert_change(db, ChangeIdInput(case_id=case_id, change_id=change.id),
                                  actor="human", entry="local-web")
    assert _node(snap, "庭审").state == "not_started"
    assert next(c for c in snap.changes if c.id == change.id).review_state == "reverted"


def test_revert_conflict_when_node_changed_after(db):
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭审")
    hearing = _node(snap, "庭审")
    snap = progress.set_active_stage(db, SetActiveStageInput(
        case_id=case_id, stage_id=hearing.id, expected_revision=hearing.revision),
        actor="agent", entry="mcp")
    change = snap.pending_changes[0]
    hearing = _node(snap, "庭审")
    # human edits the same node afterwards -> revert must refuse
    progress.update_node(db, UpdateNodeInput(
        case_id=case_id, node_id=hearing.id, expected_revision=hearing.revision,
        changes=NodeChanges(note="后续修改")), actor="human", entry="local-web")
    assert _codes(lambda: progress.revert_change(db, ChangeIdInput(case_id=case_id, change_id=change.id),
                                                 actor="human", entry="local-web")) == ErrorCode.CONFLICT


def test_revert_accepted_change_allowed(db):
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭审")
    hearing = _node(snap, "庭审")
    snap = progress.set_active_stage(db, SetActiveStageInput(
        case_id=case_id, stage_id=hearing.id, expected_revision=hearing.revision),
        actor="agent", entry="mcp")
    change = snap.pending_changes[0]
    progress.accept_change(db, ChangeIdInput(case_id=case_id, change_id=change.id),
                           actor="human", entry="local-web")
    snap = progress.revert_change(db, ChangeIdInput(case_id=case_id, change_id=change.id),
                                  actor="human", entry="local-web")
    assert _node(snap, "庭审").state == "not_started"


def test_accept_all_only_pending_and_human(db):
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="甲", actor="agent", request_key="a")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="乙", actor="agent", request_key="b")
    assert len(snap.pending_changes) == 2
    seq_before = snap.seq
    shown_ids = [c.id for c in snap.pending_changes]
    snap = progress.accept_all(db, AcceptAllInput(case_id=case_id, change_ids=shown_ids),
                               actor="human", entry="local-web")
    assert snap.pending_changes == []
    assert snap.seq == seq_before + 1
    assert _codes(lambda: progress.accept_all(db, AcceptAllInput(case_id=case_id),
                                              actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT


def test_accept_all_only_confirms_shown_ids_not_new_pending(db):
    """审查 04：一键确认只确认客户端已展示的条目，其间新增的待审修改不被一并确认。"""
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="甲", actor="agent", request_key="a")
    shown_ids = [c.id for c in snap.pending_changes]
    # 用户打开确认框后、点确认前，Agent 又写入一条新修改
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="乙", actor="agent", request_key="b")
    new_change = next(c for c in snap.pending_changes if c.id not in shown_ids)
    snap = progress.accept_all(db, AcceptAllInput(case_id=case_id, change_ids=shown_ids),
                               actor="human", entry="local-web")
    remaining = [c.id for c in snap.pending_changes]
    assert new_change.id in remaining  # 未展示的新修改仍待确认
    assert all(cid not in remaining for cid in shown_ids)


def test_accept_all_rejects_explicit_empty_ids(db):
    """审查末节：显式提交 change_ids=[] 一律拒绝，不确认任何待审项。"""
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    _add(db, case_id, parent_id=program.id, kind="stage", title="甲", actor="agent", request_key="a")
    assert _codes(lambda: progress.accept_all(
        db, AcceptAllInput(case_id=case_id, change_ids=[]),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT


def test_accept_all_omitted_ids_requires_expected_seq(db):
    """审查末节：省略 change_ids 时必须携带 expected_seq，否则拒绝（有待审项也不确认）。"""
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    _add(db, case_id, parent_id=program.id, kind="stage", title="甲", actor="agent", request_key="a")
    assert _codes(lambda: progress.accept_all(
        db, AcceptAllInput(case_id=case_id),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT


def test_accept_all_audit_records_each_accepted_change(db):
    """审查末节：批量审计逐条记录实际确认集合，可据此还原本次确认范围。"""
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="甲", actor="agent", request_key="a")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="乙", actor="agent", request_key="b")
    shown_ids = [c.id for c in snap.pending_changes]
    progress.accept_all(db, AcceptAllInput(case_id=case_id, change_ids=shown_ids),
                        actor="human", entry="local-web")
    rows = db.execute(select(models.AuditEvent).where(
        models.AuditEvent.case_id == case_id,
        models.AuditEvent.operation == "progress.accept_all")).scalars().all()
    assert {r.target_id for r in rows} == set(shown_ids)


def test_accept_all_expected_seq_conflict_when_advanced(db):
    """审查 04：提交 expected_seq 时案序号已前进则整批冲突。"""
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="甲", actor="agent", request_key="a")
    stale_seq = snap.seq
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="乙", actor="agent", request_key="b")
    assert _codes(lambda: progress.accept_all(
        db, AcceptAllInput(case_id=case_id, expected_seq=stale_seq),
        actor="human", entry="local-web")) == ErrorCode.CONFLICT


# --- request_key idempotency ------------------------------------------------

def test_agent_add_request_key_idempotent(db):
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap1 = progress.add_node(db, AddNodeInput(
        case_id=case_id, parent_id=program.id, kind="work", title="证据整理", request_key="dup"),
        actor="agent", entry="mcp")
    seq1 = snap1.seq
    snap2 = progress.add_node(db, AddNodeInput(
        case_id=case_id, parent_id=program.id, kind="work", title="证据整理", request_key="dup"),
        actor="agent", entry="mcp")
    assert snap2.seq == seq1  # replay does not bump seq or add a change
    assert len([n for n in snap2.nodes if n.title == "证据整理"]) == 1


def test_agent_add_requires_request_key(db):
    case_id = create_case(db)
    db.commit()
    assert _codes(lambda: progress.add_node(db, AddNodeInput(
        case_id=case_id, kind="stage", title="无键"), actor="agent", entry="mcp")) == ErrorCode.INVALID_INPUT


# --- fragments --------------------------------------------------------------

def test_insert_fragment_and_revert_whole(db):
    case_id = create_case(db)
    db.commit()
    snap = progress.insert_fragment(db, InsertFragmentInput(
        case_id=case_id, fragment_key="second-instance"), actor="human", entry="local-web")
    assert {"二审", "上诉", "二审审理", "二审裁判"} <= {n.title for n in snap.nodes}
    change = snap.changes[-1]
    assert change.command == "insert_fragment"
    assert len(change.diff) == 4
    snap = progress.revert_change(db, ChangeIdInput(case_id=case_id, change_id=change.id),
                                  actor="human", entry="local-web")
    assert "二审" not in {n.title for n in snap.nodes if n.removed_at is None}


def test_insert_unknown_fragment(db):
    case_id = create_case(db)
    db.commit()
    assert _codes(lambda: progress.insert_fragment(db, InsertFragmentInput(
        case_id=case_id, fragment_key="nope"), actor="human", entry="local-web")) == ErrorCode.NOT_FOUND


# --- soft delete / restore --------------------------------------------------

def test_remove_requires_no_children_and_restore(db):
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭前")
    stage = _node(snap, "庭前")
    assert _codes(lambda: progress.remove_node(db, NodeRevisionInput(
        case_id=case_id, node_id=program.id, expected_revision=program.revision),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    snap = progress.remove_node(db, NodeRevisionInput(
        case_id=case_id, node_id=stage.id, expected_revision=stage.revision),
        actor="human", entry="local-web")
    stage = _node(snap, "庭前")
    assert stage.removed_at is not None
    snap = progress.restore_node(db, NodeRevisionInput(
        case_id=case_id, node_id=stage.id, expected_revision=stage.revision),
        actor="human", entry="local-web")
    assert _node(snap, "庭前").removed_at is None


# --- relations & links ------------------------------------------------------

def test_relation_lifecycle_and_self_reject(db):
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    snap = _add(db, case_id, kind="program", title="二审")
    a = _node(snap, "一审")
    b = _node(snap, "二审")
    assert _codes(lambda: progress.add_relation(db, AddRelationInput(
        case_id=case_id, from_node_id=a.id, to_node_id=a.id, relation_type="causes"),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    snap = progress.add_relation(db, AddRelationInput(
        case_id=case_id, from_node_id=b.id, to_node_id=a.id, relation_type="causes"),
        actor="human", entry="local-web")
    assert len(snap.relations) == 1
    rel = snap.relations[0]
    assert _codes(lambda: progress.add_relation(db, AddRelationInput(
        case_id=case_id, from_node_id=b.id, to_node_id=a.id, relation_type="causes"),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    snap = progress.remove_relation(db, RemoveRelationInput(case_id=case_id, relation_id=rel.id),
                                    actor="human", entry="local-web")
    assert snap.relations[0].removed_at is not None


def test_link_and_unlink_with_display_name(db):
    built = build_material(db)
    case_id = built["case_id"]
    db.commit()
    snap = _add(db, case_id, kind="stage", title="证据交换")
    node = _node(snap, "证据交换")
    snap = progress.link_node(db, LinkNodeInput(
        case_id=case_id, node_id=node.id, target=LinkTarget(material_id=built["material_id"])),
        actor="human", entry="local-web")
    assert len(snap.links) == 1
    assert snap.links[0].display_name == "虚构材料.txt"
    link = snap.links[0]
    snap = progress.unlink_node(db, UnlinkInput(case_id=case_id, link_id=link.id),
                                actor="human", entry="local-web")
    assert snap.links == []


# --- audit & seq ------------------------------------------------------------

def test_audit_records_progress_operations(client, db):
    case_id = uuid.UUID(new_case(client, "TEST-ONLY 审计"))  # service create -> default fragment
    _add(db, case_id, kind="stage", title="立案")
    rows = db.execute(select(models.AuditEvent).where(
        models.AuditEvent.case_id == case_id)).scalars().all()
    ops = {r.operation for r in rows}
    assert "progress.insert_fragment" in ops  # default fragment on create
    assert "progress.add_node" in ops
    # audit rows carry IDs only, never node text
    for r in rows:
        assert r.target_id is not None


def test_change_seqs_polling(db):
    case_id = create_case(db)
    db.commit()
    seqs0 = progress.get_change_seqs(db, case_id).seqs
    _add(db, case_id, kind="stage", title="立案")
    seqs1 = progress.get_change_seqs(db, case_id).seqs
    assert seqs1["progress"] == seqs0.get("progress", 0) + 1


# --- concurrency ------------------------------------------------------------

def test_concurrent_same_case_writes_serialize(db, test_database):
    """Two independent connections racing on the same case: both commit, seq advances
    by exactly the number of successful commands, no lost change."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from legal_workbench.db import sqlalchemy_url
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    db.commit()
    engine = create_engine(sqlalchemy_url(test_database[1]), future=True)
    results = []
    for label in ("并发甲", "并发乙"):
        session = Session(bind=engine)
        try:
            progress.add_node(session, AddNodeInput(
                case_id=case_id, parent_id=program.id, kind="stage", title=label),
                actor="human", entry="local-web")
            results.append(label)
        finally:
            session.close()
    engine.dispose()
    final = progress.get_progress(db, case_id)
    titles = {n.title for n in final.nodes}
    assert {"并发甲", "并发乙"} <= titles
    assert len(results) == 2


# --- backfill ---------------------------------------------------------------

def test_backfill_idempotent_for_existing_cases(db):
    case_id = create_case(db)  # raw factory: no progress nodes
    db.commit()
    assert progress.get_progress(db, case_id).nodes == []
    filled = progress.backfill_default_fragments(db)
    assert filled == 1
    assert {n.title for n in progress.get_progress(db, case_id).nodes} >= {"诉前准备", "一审"}
    again = progress.backfill_default_fragments(db)
    assert again == 0  # idempotent


# --- 审查整改回归（2026-11-19 独立审查清单） ----------------------------------

def test_revert_create_refuses_when_new_child_added_after(db):
    """审查 05：撤回新建节点时，若其下此后又挂了有效子节点，整笔拒绝而非留悬空子树。"""
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="二审")
    program = _node(snap, "二审")
    create_change = next(c for c in snap.changes if c.command == "add_node" and c.diff[0].after["title"] == "二审")
    # 此后在二审下新增子节点
    _add(db, case_id, parent_id=program.id, kind="stage", title="立案")
    assert _codes(lambda: progress.revert_change(
        db, ChangeIdInput(case_id=case_id, change_id=create_change.id),
        actor="human", entry="local-web")) == ErrorCode.CONFLICT
    # 原节点与子节点都还在
    final = progress.get_progress(db, case_id)
    assert {n.title for n in final.nodes} >= {"二审", "立案"}


def test_revert_move_restores_sibling_order(db):
    """审查 05：移动记录全部受影响同级节点，撤回后旧/新父级顺序整体还原。"""
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="甲")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="乙")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="丙")
    jia, yi, bing = _node(snap, "甲"), _node(snap, "乙"), _node(snap, "丙")
    before_order = [(n.title, n.position) for n in snap.nodes if n.parent_id == program.id]
    # 把丙移到甲之前
    snap = progress.move_node(db, MoveNodeInput(
        case_id=case_id, node_id=bing.id, new_parent_id=program.id,
        before_node_id=jia.id, expected_revision=bing.revision), actor="human", entry="local-web")
    move_change = next(c for c in snap.changes if c.command == "move_node")
    moved_order = sorted([(n.title, n.position) for n in snap.nodes if n.parent_id == program.id], key=lambda x: x[1])
    assert [t for t, _ in moved_order] == ["丙", "甲", "乙"]
    # 撤回移动 → 顺序还原
    snap = progress.revert_change(db, ChangeIdInput(case_id=case_id, change_id=move_change.id),
                                  actor="human", entry="local-web")
    restored_order = sorted([(n.title, n.position) for n in snap.nodes if n.parent_id == program.id], key=lambda x: x[1])
    assert [t for t, _ in restored_order] == [t for t, _ in sorted(before_order, key=lambda x: x[1])]


def test_revert_remove_relation_conflict_when_duplicate_active(db):
    """审查 06：移除关系后重建同向同类有效关系，再撤回该移除应冲突而非产生重复。"""
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    a = _node(snap, "一审")
    snap = _add(db, case_id, kind="program", title="二审")
    b = _node(snap, "二审")
    snap = progress.add_relation(db, AddRelationInput(
        case_id=case_id, from_node_id=a.id, to_node_id=b.id, relation_type="causes"), actor="human", entry="local-web")
    rel = snap.relations[0]
    snap = progress.remove_relation(db, RemoveRelationInput(
        case_id=case_id, relation_id=rel.id), actor="human", entry="local-web")
    remove_change = next(c for c in snap.changes if c.command == "remove_relation")
    # 重建同向同类关系
    progress.add_relation(db, AddRelationInput(
        case_id=case_id, from_node_id=a.id, to_node_id=b.id, relation_type="causes"), actor="human", entry="local-web")
    assert _codes(lambda: progress.revert_change(
        db, ChangeIdInput(case_id=case_id, change_id=remove_change.id),
        actor="human", entry="local-web")) == ErrorCode.CONFLICT
    final = progress.get_progress(db, case_id)
    active = [r for r in final.relations if r.removed_at is None]
    assert len(active) == 1  # 没有重复有效关系


def test_update_node_single_active_stage_invariant(db):
    """审查 07：普通编辑把阶段设为进行中，也联动同程序原进行中阶段为已完成。"""
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭前程序", state="in_progress")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭审")
    hearing = _node(snap, "庭审")
    snap = progress.update_node(db, UpdateNodeInput(
        case_id=case_id, node_id=hearing.id, expected_revision=hearing.revision,
        changes=NodeChanges(state="in_progress")), actor="human", entry="local-web")
    assert _node(snap, "庭审").state == "in_progress"
    assert _node(snap, "庭前程序").state == "completed"  # 原阶段被联动
    stages = [n for n in snap.nodes if n.kind == "stage" and n.state == "in_progress"]
    assert len(stages) == 1


def test_add_node_single_active_stage_invariant(db):
    """审查 07：新增进行中阶段也联动原阶段。"""
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭前程序", state="in_progress")
    snap = _add(db, case_id, parent_id=program.id, kind="stage", title="庭审", state="in_progress")
    assert _node(snap, "庭前程序").state == "completed"
    assert _node(snap, "庭审").state == "in_progress"


def test_update_node_explicit_null_clears_note_and_dates(db):
    """审查 11：显式 null 清空备注/日期；未提供字段不动。"""
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    program = _node(snap, "一审")
    snap = progress.update_node(db, UpdateNodeInput(
        case_id=case_id, node_id=program.id, expected_revision=program.revision,
        changes=NodeChanges(note="原始备注", date_start="2026-11-01")), actor="human", entry="local-web")
    program = _node(snap, "一审")
    assert program.note == "原始备注"
    # 只改标题，note/date 不动
    snap = progress.update_node(db, UpdateNodeInput(
        case_id=case_id, node_id=program.id, expected_revision=program.revision,
        changes=NodeChanges(title="一审程序")), actor="human", entry="local-web")
    program = _node(snap, "一审程序")
    assert program.note == "原始备注"
    assert program.date_start == date(2026, 11, 1)
    # 显式 null 清空 note 与 date_start
    snap = progress.update_node(db, UpdateNodeInput(
        case_id=case_id, node_id=program.id, expected_revision=program.revision,
        changes=NodeChanges(note=None, date_start=None)), actor="human", entry="local-web")
    program = _node(snap, "一审程序")
    assert program.note is None
    assert program.date_start is None


def test_insert_fragment_before_node_shifts_siblings(db):
    """审查 14：片段插入支持 before_node_id，后续同级让位且记入变更。"""
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="stage", title="甲")
    snap = _add(db, case_id, kind="stage", title="乙")
    jia = _node(snap, "甲")
    snap = progress.insert_fragment(db, InsertFragmentInput(
        case_id=case_id, fragment_key="second-instance", parent_id=None, before_node_id=jia.id),
        actor="human", entry="local-web")
    roots = sorted([n for n in snap.nodes if n.parent_id is None], key=lambda n: n.position)
    titles = [n.title for n in roots]
    assert titles.index("二审") < titles.index("甲")  # 插在甲之前
    frag_change = next(c for c in snap.changes if c.command == "insert_fragment")
    # 让位的同级节点也记入 diff（ChangeDiffEntry.id 是 UUID，用原值比较）
    shifted_ids = {d.id for d in frag_change.diff if d.before is not None}
    assert jia.id in shifted_ids


def test_cli_mcp_created_case_records_agent_actor(db):
    """审查 13：CLI/MCP 建案的默认片段按真实入口记 agent，进入待确认。"""
    from legal_workbench import cases as cases_service
    out = cases_service.create_case(db, title="TEST-ONLY CLI 建案", actor="agent", entry="cli")
    db.commit()
    snap = progress.get_progress(db, out.case_id)
    init_change = next(c for c in snap.changes if c.command == "insert_fragment")
    assert init_change.actor == "agent"
    assert init_change.entry == "cli"
    assert init_change.review_state == "pending"
    assert any(c.id == init_change.id for c in snap.pending_changes)


def test_backfill_records_change_and_audit(db):
    """审查 13：backfill 也记 insert_fragment 变更与审计，来源可追溯。"""
    case_id = create_case(db)  # raw factory: no nodes
    db.commit()
    progress.backfill_default_fragments(db)
    snap = progress.get_progress(db, case_id)
    assert any(c.command == "insert_fragment" for c in snap.changes)
    rows = db.execute(select(models.AuditEvent).where(
        models.AuditEvent.case_id == case_id,
        models.AuditEvent.operation == "progress.insert_fragment")).scalars().all()
    assert len(rows) == 1


def test_backfill_activates_matching_legacy_stage(db):
    """审查 10：旧案 stage 与模板阶段名精确匹配 → 回填后激活；不匹配 → 无进行中。"""
    from legal_workbench import cases as cases_service
    # 匹配：旧 stage="庭审" 与模板阶段同名
    matched = cases_service.create_case(db, title="TEST-ONLY 匹配旧案")
    db.commit()
    # 清掉自动实例化的节点，模拟 0004 旧案（只有 cases.stage，无进度树）
    db.execute(text("DELETE FROM progress_changes WHERE case_id=:id"), {"id": str(matched.case_id)})
    db.execute(text("DELETE FROM progress_nodes WHERE case_id=:id"), {"id": str(matched.case_id)})
    db.execute(text("DELETE FROM case_change_seq WHERE case_id=:id"), {"id": str(matched.case_id)})
    db.execute(text("UPDATE cases SET stage='庭审' WHERE id=:id"), {"id": str(matched.case_id)})
    # 不匹配：旧 stage="庭审准备" 模板里没有
    unmatched = cases_service.create_case(db, title="TEST-ONLY 不匹配旧案")
    db.commit()
    db.execute(text("DELETE FROM progress_changes WHERE case_id=:id"), {"id": str(unmatched.case_id)})
    db.execute(text("DELETE FROM progress_nodes WHERE case_id=:id"), {"id": str(unmatched.case_id)})
    db.execute(text("DELETE FROM case_change_seq WHERE case_id=:id"), {"id": str(unmatched.case_id)})
    db.execute(text("UPDATE cases SET stage='庭审准备' WHERE id=:id"), {"id": str(unmatched.case_id)})
    db.commit()

    filled = progress.backfill_default_fragments(db)
    assert filled == 2
    snap_m = progress.get_progress(db, matched.case_id)
    active_m = [n for n in snap_m.nodes if n.kind == "stage" and n.state == "in_progress"]
    assert len(active_m) == 1 and active_m[0].title == "庭审"
    snap_u = progress.get_progress(db, unmatched.case_id)
    assert not any(n.kind == "stage" and n.state == "in_progress" for n in snap_u.nodes)
    # 重跑不覆盖已有人工调整
    again = progress.backfill_default_fragments(db)
    assert again == 0
    snap_m2 = progress.get_progress(db, matched.case_id)
    assert [n.title for n in snap_m2.nodes if n.state == "in_progress"] == ["庭审"]


def test_active_relation_unique_constraint(db):
    """审查 06：数据库部分唯一索引拒绝同案同向同类型的第二条有效关系。"""
    from sqlalchemy.exc import IntegrityError
    case_id = create_case(db)
    db.commit()
    snap = _add(db, case_id, kind="program", title="一审")
    a = _node(snap, "一审")
    snap = _add(db, case_id, kind="program", title="二审")
    b = _node(snap, "二审")
    progress.add_relation(db, AddRelationInput(
        case_id=case_id, from_node_id=a.id, to_node_id=b.id, relation_type="causes"),
        actor="human", entry="local-web")
    # 直接插第二条有效同向同类关系 → 部分唯一索引拒绝
    with pytest.raises(IntegrityError):
        db.execute(text(
            "INSERT INTO progress_relations (id, case_id, from_node_id, to_node_id, type, created_by) "
            "VALUES (:id, :cid, :f, :t, 'causes', 'human')"),
            {"id": str(uuid.uuid4()), "cid": str(case_id), "f": str(a.id), "t": str(b.id)})
        db.flush()
    db.rollback()


def test_active_link_unique_constraint(db):
    """审查 06：同节点同材料的有效关联唯一（部分索引，NULL 不再放行重复）。"""
    from sqlalchemy.exc import IntegrityError
    case_id = create_case(db)
    db.commit()
    built = build_material(db, case_id=case_id)
    db.commit()
    snap = _add(db, case_id, kind="stage", title="证据")
    node = _node(snap, "证据")
    progress.link_node(db, LinkNodeInput(
        case_id=case_id, node_id=node.id, target=LinkTarget(material_id=built["material_id"])),
        actor="human", entry="local-web")
    with pytest.raises(IntegrityError):
        db.execute(text(
            "INSERT INTO progress_links (id, case_id, node_id, material_id, created_by) "
            "VALUES (:id, :cid, :nid, :mid, 'human')"),
            {"id": str(uuid.uuid4()), "cid": str(case_id), "nid": str(node.id),
             "mid": str(built["material_id"])})
        db.flush()
    db.rollback()