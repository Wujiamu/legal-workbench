"""Case progress service: command + validation + CAS + change log + audit.

Shared by HTTP (actor=human), MCP and CLI (actor=agent). Every write runs the
same fixed order inside one short transaction (plan 6.2 / decision
20261116-界面与Agent共用业务命令):

    begin_write -> lock case progress row FOR UPDATE -> same-case & rule checks
    -> node CAS -> write change record -> seq + 1 -> audit -> commit -> snapshot

The actor is decided by the entry point, never self-reported by the caller.
Agent writes become pending review; accept/revert are human-only.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import audit, cases, domain_changes, models, progress_rules
from .db import begin_write
from .errors import ErrorCode, WorkbenchError
from .schemas import (
    AcceptAllInput, AddNodeInput, AddRelationInput, ChangeDiffEntry, ChangeIdInput, ChangeSeqsOutput,
    InsertFragmentInput, LinkNodeInput, MoveNodeInput, NodeChanges, NodeRevisionInput,
    ProgressChangeOut, ProgressLinkOut, ProgressNodeOut, ProgressRelationOut,
    ProgressSnapshot, RemoveRelationInput, SetActiveStageInput, UnlinkInput,
    UpdateNodeInput,
)

DOMAIN = "progress"
ARCHIVED_MESSAGE = "案件已归档，进度只读；如需修改请先取消归档"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _date_to_dt(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _dt_to_date(value: datetime | None) -> date | None:
    return value.date() if value is not None else None


def _err(code: ErrorCode, message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError(code, message, details or None)


def _case_or_error(session: Session, case_id: uuid.UUID) -> models.Case:
    row = session.get(models.Case, case_id)
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, "案件不存在", case_id=str(case_id))
    return row


def _lock_case(session: Session, case_id: uuid.UUID) -> int:
    """Serialize same-case progress writes; returns the current seq.

    审查 08：首次写入的序号行用 INSERT … ON CONFLICT DO NOTHING 原子建立，
    再 SELECT … FOR UPDATE 取锁。并发首写不会因裸 INSERT 撞主键而整笔失败。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    session.execute(
        pg_insert(models.CaseChangeSeq)
        .values(case_id=case_id, domain=DOMAIN, seq=0)
        .on_conflict_do_nothing(index_elements=["case_id", "domain"])
    )
    row = session.execute(
        select(models.CaseChangeSeq)
        .where(models.CaseChangeSeq.case_id == case_id, models.CaseChangeSeq.domain == DOMAIN)
        .with_for_update()
    ).scalar_one()
    return row.seq


def _bump_seq(session: Session, case_id: uuid.UUID) -> int:
    row = session.execute(
        select(models.CaseChangeSeq)
        .where(models.CaseChangeSeq.case_id == case_id, models.CaseChangeSeq.domain == DOMAIN)
        .with_for_update()
    ).scalar_one()
    row.seq += 1
    session.flush()
    return row.seq


# --- serialization ----------------------------------------------------------

def _node_dict(row: models.ProgressNode) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id),
        "parent_id": str(row.parent_id) if row.parent_id else None,
        "position": row.position, "kind": row.kind, "state": row.state,
        "title": row.title, "note": row.note,
        "date_start": row.date_start.date().isoformat() if row.date_start else None,
        "date_end": row.date_end.date().isoformat() if row.date_end else None,
        "template_ref": row.template_ref, "created_by": row.created_by,
        "updated_by": row.updated_by, "request_key": row.request_key,
        "removed_at": row.removed_at.isoformat() if row.removed_at else None,
        "revision": row.revision,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
    }


def _relation_dict(row: models.ProgressRelation) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id),
        "from_node_id": str(row.from_node_id), "to_node_id": str(row.to_node_id),
        "type": row.type, "note": row.note, "created_by": row.created_by,
        "removed_at": row.removed_at.isoformat() if row.removed_at else None,
        "created_at": row.created_at.isoformat(),
    }


def _link_dict(row: models.ProgressLink, display_name: str) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id), "node_id": str(row.node_id),
        "material_id": str(row.material_id) if row.material_id else None,
        "analysis_id": str(row.analysis_id) if row.analysis_id else None,
        "annotation_id": str(row.annotation_id) if row.annotation_id else None,
        "created_by": row.created_by, "created_at": row.created_at.isoformat(),
        "display_name": display_name,
    }


def _node_out(row: models.ProgressNode) -> ProgressNodeOut:
    return ProgressNodeOut(
        id=row.id, case_id=row.case_id, parent_id=row.parent_id, position=row.position,
        kind=row.kind, state=row.state, title=row.title, note=row.note,
        date_start=_dt_to_date(row.date_start), date_end=_dt_to_date(row.date_end),
        template_ref=row.template_ref, created_by=row.created_by, updated_by=row.updated_by,
        request_key=row.request_key, removed_at=row.removed_at, revision=row.revision,
        created_at=row.created_at, updated_at=row.updated_at,
    )


def _change_out(row: models.ProgressChange) -> ProgressChangeOut:
    return ProgressChangeOut(
        id=row.id, case_id=row.case_id, seq=row.seq, command=row.command,
        actor=row.actor, entry=row.entry,
        diff=[ChangeDiffEntry(**entry) for entry in row.diff],
        review_state=row.review_state, reviewed_at=row.reviewed_at,
        reverted_by=row.reverted_by, created_at=row.created_at,
    )


def _link_display_name(session: Session, row: models.ProgressLink) -> str:
    if row.material_id:
        material = session.get(models.Material, row.material_id)
        return material.display_name if material else "（材料已不存在）"
    if row.analysis_id:
        analysis = session.get(models.Analysis, row.analysis_id)
        return analysis.title if analysis else "（分析已不存在）"
    if row.annotation_id:
        annotation = session.get(models.Annotation, row.annotation_id)
        if annotation is None:
            return "（标注已不存在）"
        version = session.get(models.FileVersion, annotation.file_version_id)
        material = session.get(models.Material, version.material_id) if version else None
        return material.display_name if material else "（标注）"
    return ""


def get_progress(session: Session, case_id: uuid.UUID) -> ProgressSnapshot:
    _case_or_error(session, case_id)
    # 自洽快照（审查 09）：读取期间取案序号行的共享锁（FOR SHARE），
    # 写命令持 FOR UPDATE 会与之互斥，因此序号与节点/关系/关联/变更
    # 必然取自同一已提交状态，不会读到「序号 1 却含序号 2 内容」的交错。
    # 读读不互斥，不影响并发读取性能。
    seq_row = session.execute(
        select(models.CaseChangeSeq)
        .where(models.CaseChangeSeq.case_id == case_id, models.CaseChangeSeq.domain == DOMAIN)
        .with_for_update(read=True)
    ).scalar_one_or_none()
    seq = seq_row.seq if seq_row else 0
    nodes = session.execute(
        select(models.ProgressNode).where(models.ProgressNode.case_id == case_id)
        .order_by(models.ProgressNode.position, models.ProgressNode.created_at)
    ).scalars().all()
    relations = session.execute(
        select(models.ProgressRelation).where(models.ProgressRelation.case_id == case_id)
    ).scalars().all()
    links = session.execute(
        select(models.ProgressLink).where(models.ProgressLink.case_id == case_id)
    ).scalars().all()
    changes = session.execute(
        select(models.ProgressChange).where(models.ProgressChange.case_id == case_id)
        .order_by(models.ProgressChange.seq)
    ).scalars().all()
    return ProgressSnapshot(
        case_id=case_id, seq=seq,
        nodes=[_node_out(row) for row in nodes],
        relations=[ProgressRelationOut(
            id=row.id, case_id=row.case_id, from_node_id=row.from_node_id,
            to_node_id=row.to_node_id, type=row.type, note=row.note,
            created_by=row.created_by, removed_at=row.removed_at, created_at=row.created_at,
        ) for row in relations],
        links=[ProgressLinkOut(
            id=row.id, case_id=row.case_id, node_id=row.node_id,
            material_id=row.material_id, analysis_id=row.analysis_id,
            annotation_id=row.annotation_id, created_by=row.created_by,
            created_at=row.created_at, display_name=_link_display_name(session, row),
        ) for row in links],
        pending_changes=[_change_out(row) for row in changes if row.review_state == "pending"],
        changes=[_change_out(row) for row in changes],
    )


def get_change_seqs(session: Session, case_id: uuid.UUID) -> ChangeSeqsOutput:
    """Per-domain change counters for the polling client.

    All four domains are always present (``0`` when the case never wrote to that
    domain): the graph/evidence/claims sequence rows are created lazily on the
    first write, and a frontend that only ever sees ``progress`` would treat a
    missing key as "unchanged".  Older progress-only callers keep working — the
    ``progress`` key means exactly what it did before.
    """
    _case_or_error(session, case_id)
    rows = session.execute(
        select(models.CaseChangeSeq).where(models.CaseChangeSeq.case_id == case_id)
    ).scalars().all()
    seqs = {domain: 0 for domain in domain_changes.DOMAINS}
    seqs.update({row.domain: row.seq for row in rows})
    return ChangeSeqsOutput(seqs=seqs)


# --- helpers ----------------------------------------------------------------

def _active_children(session: Session, case_id: uuid.UUID,
                     parent_id: uuid.UUID | None) -> list[models.ProgressNode]:
    return session.execute(
        select(models.ProgressNode)
        .where(models.ProgressNode.case_id == case_id,
               models.ProgressNode.parent_id == parent_id,
               models.ProgressNode.removed_at.is_(None))
        .order_by(models.ProgressNode.position)
    ).scalars().all()


def _node_or_error(session: Session, case_id: uuid.UUID,
                   node_id: uuid.UUID) -> models.ProgressNode:
    row = session.get(models.ProgressNode, node_id)
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, "节点不存在", node_id=str(node_id))
    if row.case_id != case_id:
        raise _err(ErrorCode.CASE_MISMATCH, "节点不属于本案", node_id=str(node_id))
    return row


def _parent_kind(session: Session, case_id: uuid.UUID, parent_id: uuid.UUID | None) -> str:
    if parent_id is None:
        return "case"
    return _node_or_error(session, case_id, parent_id).kind


def _cas(row: models.ProgressNode, expected_revision: int) -> None:
    if row.revision != expected_revision:
        raise _err(ErrorCode.CONFLICT, "节点已被修改，请刷新后重试",
                   node_id=str(row.id), expected_revision=expected_revision,
                   actual_revision=row.revision)


def _validate_new_node(rules: dict[str, Any], session: Session, case_id: uuid.UUID,
                       kind: str, title: str, state: str | None,
                       parent_id: uuid.UUID | None) -> None:
    rule = progress_rules.kind_rule(rules, kind)
    if rule is None:
        raise _err(ErrorCode.INVALID_INPUT, f"未知节点种类：{kind}")
    trimmed = title.strip()
    if not trimmed or len(trimmed) > progress_rules.TITLE_MAX:
        raise _err(ErrorCode.INVALID_INPUT, f"标题需为 1–{progress_rules.TITLE_MAX} 字")
    if state is not None and not progress_rules.valid_state(rules, kind, state):
        raise _err(ErrorCode.INVALID_INPUT, f"种类「{rule['label']}」没有状态：{state}")
    if parent_id is not None:
        parent = _node_or_error(session, case_id, parent_id)
        if parent.removed_at is not None:
            raise _err(ErrorCode.NOT_FOUND, "父节点已删除")
    allowed = progress_rules.allowed_children(rules, _parent_kind(session, case_id, parent_id))
    if kind not in allowed:
        raise _err(ErrorCode.INVALID_INPUT, f"「{rule['label']}」不能放在该位置（包含规则）")


def _enforce_single_active_stage(session: Session, case_id: uuid.UUID, row: models.ProgressNode,
                                 actor: str, diff: list[dict[str, Any]]) -> None:
    """K3：同一程序最多一个进行中阶段。任何把阶段置为进行中的路径都联动原阶段，
    并把连带修改记入同一条变更（审查 07：覆盖新增/编辑/移入/恢复/撤回）。"""
    if row.kind != "stage" or row.state != "in_progress" or row.removed_at is not None:
        return
    for sibling in _active_children(session, case_id, row.parent_id):
        if sibling.id != row.id and sibling.kind == "stage" and sibling.state == "in_progress":
            sib_before = _node_dict(sibling)
            sib_rev = sibling.revision
            sibling.state = "completed"
            sibling.revision += 1
            sibling.updated_by = actor
            sibling.updated_at = _now()
            diff.append({"target": "node", "id": str(sibling.id), "before": sib_before,
                         "after": _node_dict(sibling), "before_rev": sib_rev,
                         "after_rev": sibling.revision})


def _insert_position(session: Session, case_id: uuid.UUID, parent_id: uuid.UUID | None,
                     position: int | None) -> int:
    siblings = _active_children(session, case_id, parent_id)
    target = len(siblings) if position is None else max(0, min(position, len(siblings)))
    for sibling in siblings:
        if sibling.position >= target:
            sibling.position += 1
    session.flush()
    return target


def _record_change(session: Session, case_id: uuid.UUID, command: str, actor: str,
                   entry: str, diff: list[dict[str, Any]]) -> models.ProgressChange:
    seq = _bump_seq(session, case_id)
    row = models.ProgressChange(
        id=uuid.uuid4(), case_id=case_id, seq=seq, command=command, actor=actor,
        entry=entry, diff=diff,
        review_state="pending" if actor == "agent" else "none",
    )
    session.add(row)
    session.flush()
    return row


def _node_diff(before: models.ProgressNode | None,
               after: models.ProgressNode | None) -> dict[str, Any]:
    ref = after or before
    return {
        "target": "node", "id": str(ref.id),
        "before": _node_dict(before) if before else None,
        "after": _node_dict(after) if after else None,
        "before_rev": before.revision if before else None,
        "after_rev": after.revision if after else None,
    }


def _relation_diff(before: models.ProgressRelation | None,
                   after: models.ProgressRelation | None) -> dict[str, Any]:
    ref = after or before
    return {
        "target": "relation", "id": str(ref.id),
        "before": _relation_dict(before) if before else None,
        "after": _relation_dict(after) if after else None,
        "before_rev": None, "after_rev": None,
    }


def _audit(session: Session, command: str, case_id: uuid.UUID, target_id: uuid.UUID) -> None:
    audit.record(session, operation=f"progress.{command}", target_type="progress",
                 target_id=target_id, case_id=case_id)


def _begin(session: Session, case_id: uuid.UUID, actor: str, entry: str,
           command: str) -> dict[str, Any]:
    """Common write preamble: guard agent permission, lock, return rules.

    归档守卫与所有同案写入共用 ``cases.lock_case_writable``：维护门禁 →
    案件行 ``FOR UPDATE``（刷新 ORM 缓存）→ 复核 ``archived_at``，然后再取
    进度序号行。先前只在锁前用 ``session.get`` 查归档，归档若在两步之间提交，
    写入仍会落到已归档案件；缓存过 ``archived_at IS NULL`` 的会话也不会看到
    新值。锁顺序固定为案件行 → 序号行，与归档一致。
    """
    rules = progress_rules.get_rules()
    if actor == "agent" and command not in progress_rules.AGENT_COMMANDS:
        raise _err(ErrorCode.FORBIDDEN, f"Agent 无权执行命令：{command}")
    cases.lock_case_writable(session, case_id, archived_message=ARCHIVED_MESSAGE)
    _lock_case(session, case_id)
    return rules


def _finish(session: Session, case_id: uuid.UUID,
            change_id: uuid.UUID | None = None) -> ProgressSnapshot:
    session.commit()
    snapshot = get_progress(session, case_id)
    # 审查 N3：写命令响应携带本次变更 ID，前端按此入撤销栈（并发下不错认）。
    if change_id is not None:
        snapshot.last_change_id = change_id
    return snapshot


# --- commands ---------------------------------------------------------------

def add_node(session: Session, payload: AddNodeInput, *, actor: str, entry: str) -> ProgressSnapshot:
    rules = _begin(session, payload.case_id, actor, entry, "add_node")
    if actor == "agent" and not payload.request_key:
        raise _err(ErrorCode.INVALID_INPUT, "Agent 新建节点必须带 request_key")
    if payload.request_key:
        existing = session.execute(
            select(models.ProgressNode).where(
                models.ProgressNode.case_id == payload.case_id,
                models.ProgressNode.request_key == payload.request_key)
        ).scalar_one_or_none()
        if existing is not None:
            return get_progress(session, payload.case_id)  # idempotent replay
    _validate_new_node(rules, session, payload.case_id, payload.kind, payload.title,
                       payload.state, payload.parent_id)
    # 审查 N6：记录让位同级的修改前状态，撤回新建时整体还原（与 move/fragment 同口径）。
    siblings_before = {s.id: _node_dict(s) for s in _active_children(session, payload.case_id, payload.parent_id)}
    position = _insert_position(session, payload.case_id, payload.parent_id, payload.position)
    timestamp = _now()
    row = models.ProgressNode(
        id=uuid.uuid4(), case_id=payload.case_id, parent_id=payload.parent_id,
        position=position, kind=payload.kind,
        state=payload.state or progress_rules.default_state(rules, payload.kind),
        title=payload.title.strip(), note=payload.note,
        date_start=_date_to_dt(payload.date_start), date_end=_date_to_dt(payload.date_end),
        template_ref=None, created_by=actor, updated_by=actor,
        request_key=payload.request_key, removed_at=None, revision=0,
        created_at=timestamp, updated_at=timestamp,
    )
    session.add(row)
    session.flush()
    diff = [_node_diff(None, row)]
    for sibling_id, sib_before in siblings_before.items():
        sibling = session.get(models.ProgressNode, sibling_id)
        if sibling.position != sib_before["position"]:
            sibling.revision += 1
            sibling.updated_by = actor
            sibling.updated_at = timestamp
            diff.append({"target": "node", "id": str(sibling_id), "before": sib_before,
                         "after": _node_dict(sibling), "before_rev": sib_before["revision"],
                         "after_rev": sibling.revision})
    session.flush()
    _enforce_single_active_stage(session, payload.case_id, row, actor, diff)
    change = _record_change(session, payload.case_id, "add_node", actor, entry, diff)
    _audit(session, "add_node", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def update_node(session: Session, payload: UpdateNodeInput, *, actor: str, entry: str) -> ProgressSnapshot:
    rules = _begin(session, payload.case_id, actor, entry, "update_node")
    row = _node_or_error(session, payload.case_id, payload.node_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "节点已删除")
    _cas(row, payload.expected_revision)
    changes = payload.changes
    provided = changes.model_fields_set  # 区分「未提供」与「显式清空」（审查 11）
    if "title" in provided:
        if changes.title is None:
            raise _err(ErrorCode.INVALID_INPUT, "标题不能清空")
        trimmed = changes.title.strip()
        if not trimmed or len(trimmed) > progress_rules.TITLE_MAX:
            raise _err(ErrorCode.INVALID_INPUT, f"标题需为 1–{progress_rules.TITLE_MAX} 字")
    if "state" in provided:
        if changes.state is None or not progress_rules.valid_state(rules, row.kind, changes.state):
            raise _err(ErrorCode.INVALID_INPUT, f"种类「{row.kind}」没有状态：{changes.state}")
    before = _node_dict(row)
    before_rev = row.revision
    diff: list[dict[str, Any]] = []
    if "title" in provided:
        row.title = changes.title.strip()
    if "state" in provided:
        row.state = changes.state
    if "note" in provided:
        row.note = changes.note  # 显式 null 清空
    if "date_start" in provided:
        row.date_start = _date_to_dt(changes.date_start)
    if "date_end" in provided:
        row.date_end = _date_to_dt(changes.date_end)
    row.revision += 1
    row.updated_by = actor
    row.updated_at = _now()
    session.flush()
    diff.append({"target": "node", "id": str(row.id), "before": before, "after": _node_dict(row),
                 "before_rev": before_rev, "after_rev": row.revision})
    # K3：普通编辑把阶段设为进行中时联动原阶段（审查 07）。
    _enforce_single_active_stage(session, payload.case_id, row, actor, diff)
    change = _record_change(session, payload.case_id, "update_node", actor, entry, diff)
    _audit(session, "update_node", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def set_active_stage(session: Session, payload: SetActiveStageInput, *, actor: str, entry: str) -> ProgressSnapshot:
    rules = _begin(session, payload.case_id, actor, entry, "set_active_stage")
    row = _node_or_error(session, payload.case_id, payload.stage_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "节点已删除")
    if row.kind != "stage":
        raise _err(ErrorCode.INVALID_INPUT, "只有阶段可以设为进行中")
    _cas(row, payload.expected_revision)
    diff: list[dict[str, Any]] = []
    # At most one in-progress stage per program (K3); the previous one is completed.
    for sibling in _active_children(session, payload.case_id, row.parent_id):
        if sibling.id != row.id and sibling.kind == "stage" and sibling.state == "in_progress":
            before = _node_dict(sibling)
            before_rev = sibling.revision
            sibling.state = "completed"
            sibling.revision += 1
            sibling.updated_by = actor
            sibling.updated_at = _now()
            diff.append({"target": "node", "id": str(sibling.id), "before": before,
                         "after": _node_dict(sibling), "before_rev": before_rev,
                         "after_rev": sibling.revision})
    before = _node_dict(row)
    before_rev = row.revision
    row.state = "in_progress"
    row.revision += 1
    row.updated_by = actor
    row.updated_at = _now()
    session.flush()
    diff.append({"target": "node", "id": str(row.id), "before": before, "after": _node_dict(row),
                 "before_rev": before_rev, "after_rev": row.revision})
    change = _record_change(session, payload.case_id, "set_active_stage", actor, entry, diff)
    _audit(session, "set_active_stage", payload.case_id, change.id)
    return _finish(session, payload.case_id, change.id)


def move_node(session: Session, payload: MoveNodeInput, *, actor: str, entry: str) -> ProgressSnapshot:
    rules = _begin(session, payload.case_id, actor, entry, "move_node")
    row = _node_or_error(session, payload.case_id, payload.node_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "节点已删除")
    _cas(row, payload.expected_revision)
    new_parent_id = payload.new_parent_id
    if new_parent_id == row.id:
        raise _err(ErrorCode.INVALID_INPUT, "不能移动到自身之下")
    if new_parent_id is not None:
        parent = _node_or_error(session, payload.case_id, new_parent_id)
        if parent.removed_at is not None:
            raise _err(ErrorCode.NOT_FOUND, "目标父节点已删除")
        cursor: uuid.UUID | None = new_parent_id
        while cursor is not None:
            if cursor == row.id:
                raise _err(ErrorCode.INVALID_INPUT, "移动会形成循环")
            cursor = session.get(models.ProgressNode, cursor).parent_id
    allowed = progress_rules.allowed_children(rules, _parent_kind(session, payload.case_id, new_parent_id))
    if row.kind not in allowed:
        raise _err(ErrorCode.INVALID_INPUT, "目标位置不允许该种类（包含规则）")
    before = _node_dict(row)
    before_rev = row.revision
    old_parent_id = row.parent_id
    # 记录所有受影响的同级节点（旧父级重排＋新父级让位），撤回时整体还原（审查 05）。
    affected: dict[uuid.UUID, dict[str, Any]] = {}
    for sibling in _active_children(session, payload.case_id, old_parent_id):
        if sibling.id != row.id:
            affected[sibling.id] = _node_dict(sibling)
    for sibling in _active_children(session, payload.case_id, new_parent_id):
        if sibling.id != row.id and sibling.id not in affected:
            affected[sibling.id] = _node_dict(sibling)
    for index, sibling in enumerate(
            [n for n in _active_children(session, payload.case_id, old_parent_id) if n.id != row.id]):
        sibling.position = index
    new_siblings = [n for n in _active_children(session, payload.case_id, new_parent_id) if n.id != row.id]
    target = len(new_siblings)
    if payload.before_node_id:
        anchor = next((n for n in new_siblings if n.id == payload.before_node_id), None)
        if anchor is None:
            raise _err(ErrorCode.INVALID_INPUT, "参照节点不在目标位置")
        target = anchor.position
    for sibling in new_siblings:
        if sibling.position >= target:
            sibling.position += 1
    row.parent_id = new_parent_id
    row.position = target
    row.revision += 1
    row.updated_by = actor
    row.updated_at = _now()
    session.flush()
    diff = [{"target": "node", "id": str(row.id), "before": before, "after": _node_dict(row),
             "before_rev": before_rev, "after_rev": row.revision}]
    for sibling_id, sib_before in affected.items():
        sibling = session.get(models.ProgressNode, sibling_id)
        if _node_dict(sibling) != sib_before:
            sibling.revision += 1
            sibling.updated_by = actor
            sibling.updated_at = _now()
            diff.append({"target": "node", "id": str(sibling_id), "before": sib_before,
                         "after": _node_dict(sibling), "before_rev": sib_before["revision"],
                         "after_rev": sibling.revision})
    session.flush()
    _enforce_single_active_stage(session, payload.case_id, row, actor, diff)
    change = _record_change(session, payload.case_id, "move_node", actor, entry, diff)
    _audit(session, "move_node", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def _walk_fragment(session: Session, rules: dict[str, Any], case_id: uuid.UUID,
                   spec: dict[str, Any], parent_id: uuid.UUID | None, position: int,
                   template_ref: str, actor: str, timestamp: datetime,
                   created: list[models.ProgressNode]) -> None:
    row = models.ProgressNode(
        id=uuid.uuid4(), case_id=case_id, parent_id=parent_id, position=position,
        kind=spec["kind"], state=spec.get("state") or progress_rules.default_state(rules, spec["kind"]),
        title=spec["title"], note=None, date_start=None, date_end=None,
        template_ref=template_ref, created_by=actor, updated_by=actor,
        request_key=None, removed_at=None, revision=0,
        created_at=timestamp, updated_at=timestamp,
    )
    session.add(row)
    session.flush()
    created.append(row)
    for index, child in enumerate(spec.get("children", [])):
        _walk_fragment(session, rules, case_id, child, row.id, index, template_ref,
                       actor, timestamp, created)


def _check_fragment(rules: dict[str, Any], specs: list[dict[str, Any]], parent_kind: str) -> None:
    for spec in specs:
        if progress_rules.kind_rule(rules, spec["kind"]) is None:
            raise _err(ErrorCode.INVALID_INPUT, f"未知节点种类：{spec['kind']}")
        if spec["kind"] not in progress_rules.allowed_children(rules, parent_kind):
            raise _err(ErrorCode.INVALID_INPUT, f"片段种类「{spec['kind']}」不能放在该位置")
        _check_fragment(rules, spec.get("children", []), spec["kind"])


def insert_fragment(session: Session, payload: InsertFragmentInput, *, actor: str, entry: str) -> ProgressSnapshot:
    rules = _begin(session, payload.case_id, actor, entry, "insert_fragment")
    fragment = rules["fragments"].get(payload.fragment_key)
    if fragment is None:
        raise _err(ErrorCode.NOT_FOUND, f"未知片段：{payload.fragment_key}")
    if payload.parent_id is not None:
        parent = _node_or_error(session, payload.case_id, payload.parent_id)
        if parent.removed_at is not None:
            raise _err(ErrorCode.NOT_FOUND, "父节点已删除")
    _check_fragment(rules, fragment["roots"], _parent_kind(session, payload.case_id, payload.parent_id))
    template_ref = f"{payload.fragment_key}@{fragment['version']}"
    timestamp = _now()
    created: list[models.ProgressNode] = []
    siblings = _active_children(session, payload.case_id, payload.parent_id)
    base = len(siblings)
    shifted: dict[uuid.UUID, dict[str, Any]] = {}
    if payload.before_node_id:
        anchor = next((n for n in siblings if n.id == payload.before_node_id), None)
        if anchor is None:
            raise _err(ErrorCode.INVALID_INPUT, "参照节点不在目标位置")
        base = anchor.position
        for sibling in siblings:
            if sibling.position >= base:
                shifted[sibling.id] = _node_dict(sibling)
                sibling.position += len(fragment["roots"])
        session.flush()
    for index, root in enumerate(fragment["roots"]):
        _walk_fragment(session, rules, payload.case_id, root, payload.parent_id,
                       base + index, template_ref, actor, timestamp, created)
    diff = [_node_diff(None, row) for row in created]
    for sibling_id, sib_before in shifted.items():
        sibling = session.get(models.ProgressNode, sibling_id)
        sibling.revision += 1
        sibling.updated_by = actor
        sibling.updated_at = timestamp
        diff.append({"target": "node", "id": str(sibling_id), "before": sib_before,
                     "after": _node_dict(sibling), "before_rev": sib_before["revision"],
                     "after_rev": sibling.revision})
    session.flush()
    change = _record_change(session, payload.case_id, "insert_fragment", actor, entry, diff)
    _audit(session, "insert_fragment", payload.case_id, created[0].id)
    return _finish(session, payload.case_id, change.id)


def remove_node(session: Session, payload: NodeRevisionInput, *, actor: str, entry: str) -> ProgressSnapshot:
    _begin(session, payload.case_id, actor, entry, "remove_node")
    row = _node_or_error(session, payload.case_id, payload.node_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "节点已删除")
    _cas(row, payload.expected_revision)
    if _active_children(session, payload.case_id, row.id):
        raise _err(ErrorCode.INVALID_INPUT, "请先移除或移动子节点")
    before = _node_dict(row)
    before_rev = row.revision
    row.removed_at = _now()
    row.revision += 1
    row.updated_by = actor
    row.updated_at = _now()
    session.flush()
    diff = {"target": "node", "id": str(row.id), "before": before, "after": _node_dict(row),
            "before_rev": before_rev, "after_rev": row.revision}
    change = _record_change(session, payload.case_id, "remove_node", actor, entry, [diff])
    _audit(session, "remove_node", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def restore_node(session: Session, payload: NodeRevisionInput, *, actor: str, entry: str) -> ProgressSnapshot:
    _begin(session, payload.case_id, actor, entry, "restore_node")
    row = _node_or_error(session, payload.case_id, payload.node_id)
    if row.removed_at is None:
        raise _err(ErrorCode.INVALID_INPUT, "节点未删除")
    _cas(row, payload.expected_revision)
    if row.parent_id is not None:
        parent = _node_or_error(session, payload.case_id, row.parent_id)
        if parent.removed_at is not None:
            raise _err(ErrorCode.INVALID_INPUT, "父节点已删除，请先恢复父节点")
    before = _node_dict(row)
    before_rev = row.revision
    row.removed_at = None
    row.position = len(_active_children(session, payload.case_id, row.parent_id))
    row.revision += 1
    row.updated_by = actor
    row.updated_at = _now()
    session.flush()
    diff = [{"target": "node", "id": str(row.id), "before": before, "after": _node_dict(row),
             "before_rev": before_rev, "after_rev": row.revision}]
    _enforce_single_active_stage(session, payload.case_id, row, actor, diff)
    change = _record_change(session, payload.case_id, "restore_node", actor, entry, diff)
    _audit(session, "restore_node", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def add_relation(session: Session, payload: AddRelationInput, *, actor: str, entry: str) -> ProgressSnapshot:
    rules = _begin(session, payload.case_id, actor, entry, "add_relation")
    source = _node_or_error(session, payload.case_id, payload.from_node_id)
    target = _node_or_error(session, payload.case_id, payload.to_node_id)
    if source.removed_at is not None or target.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "节点已删除")
    if source.id == target.id:
        raise _err(ErrorCode.INVALID_INPUT, "关系不能指向自身")
    if not progress_rules.valid_relation_type(rules, payload.relation_type):
        raise _err(ErrorCode.INVALID_INPUT, f"未知关系类型：{payload.relation_type}")
    duplicate = session.execute(
        select(models.ProgressRelation).where(
            models.ProgressRelation.case_id == payload.case_id,
            models.ProgressRelation.from_node_id == source.id,
            models.ProgressRelation.to_node_id == target.id,
            models.ProgressRelation.type == payload.relation_type,
            models.ProgressRelation.removed_at.is_(None))
    ).scalar_one_or_none()
    if duplicate is not None:
        raise _err(ErrorCode.INVALID_INPUT, "该关系已存在")
    row = models.ProgressRelation(
        id=uuid.uuid4(), case_id=payload.case_id, from_node_id=source.id,
        to_node_id=target.id, type=payload.relation_type, note=payload.note,
        created_by=actor, removed_at=None,
    )
    session.add(row)
    session.flush()
    change = _record_change(session, payload.case_id, "add_relation", actor, entry,
                            [_relation_diff(None, row)])
    _audit(session, "add_relation", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def remove_relation(session: Session, payload: RemoveRelationInput, *, actor: str, entry: str) -> ProgressSnapshot:
    _begin(session, payload.case_id, actor, entry, "remove_relation")
    row = session.get(models.ProgressRelation, payload.relation_id)
    if row is None or row.case_id != payload.case_id or row.removed_at is not None:
        raise _err(ErrorCode.NOT_FOUND, "关系不存在")
    before = _relation_dict(row)
    row.removed_at = _now()
    session.flush()
    # after=None 表示「该关系被移除」，撤回时据此走恢复分支（审查 06）。
    change = _record_change(session, payload.case_id, "remove_relation", actor, entry,
                            [{"target": "relation", "id": str(row.id), "before": before,
                              "after": None, "before_rev": None, "after_rev": None}])
    _audit(session, "remove_relation", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def link_node(session: Session, payload: LinkNodeInput, *, actor: str, entry: str) -> ProgressSnapshot:
    _begin(session, payload.case_id, actor, entry, "link")
    row = _node_or_error(session, payload.case_id, payload.node_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "节点已删除")
    target = payload.target
    material_id = target.material_id
    analysis_id = target.analysis_id
    annotation_id = target.annotation_id
    display_name = ""
    if material_id:
        material = session.get(models.Material, material_id)
        if material is None:
            raise _err(ErrorCode.NOT_FOUND, "关联对象不存在")
        if material.case_id != payload.case_id:
            raise _err(ErrorCode.CASE_MISMATCH, "关联对象不属于本案")
        display_name = material.display_name
    elif analysis_id:
        analysis = session.get(models.Analysis, analysis_id)
        if analysis is None:
            raise _err(ErrorCode.NOT_FOUND, "关联对象不存在")
        if analysis.case_id != payload.case_id:
            raise _err(ErrorCode.CASE_MISMATCH, "关联对象不属于本案")
        display_name = analysis.title
    else:
        annotation = session.get(models.Annotation, annotation_id)
        if annotation is None:
            raise _err(ErrorCode.NOT_FOUND, "关联对象不存在")
        if annotation.case_id != payload.case_id:
            raise _err(ErrorCode.CASE_MISMATCH, "关联对象不属于本案")
        version = session.get(models.FileVersion, annotation.file_version_id)
        material = session.get(models.Material, version.material_id) if version else None
        display_name = material.display_name if material else "（标注）"
    duplicate = session.execute(
        select(models.ProgressLink).where(
            models.ProgressLink.node_id == row.id,
            models.ProgressLink.material_id == material_id,
            models.ProgressLink.analysis_id == analysis_id,
            models.ProgressLink.annotation_id == annotation_id)
    ).scalar_one_or_none()
    if duplicate is not None:
        raise _err(ErrorCode.INVALID_INPUT, "该关联已存在")
    link = models.ProgressLink(
        id=uuid.uuid4(), case_id=payload.case_id, node_id=row.id,
        material_id=material_id, analysis_id=analysis_id, annotation_id=annotation_id,
        created_by=actor,
    )
    session.add(link)
    session.flush()
    change = _record_change(session, payload.case_id, "link", actor, entry,
                            [{"target": "link", "id": str(link.id), "before": None,
                              "after": _link_dict(link, display_name), "before_rev": None, "after_rev": None}])
    _audit(session, "link", payload.case_id, link.id)
    return _finish(session, payload.case_id, change.id)


def unlink_node(session: Session, payload: UnlinkInput, *, actor: str, entry: str) -> ProgressSnapshot:
    _begin(session, payload.case_id, actor, entry, "unlink")
    link = session.get(models.ProgressLink, payload.link_id)
    if link is None or link.case_id != payload.case_id:
        raise _err(ErrorCode.NOT_FOUND, "关联不存在")
    before = _link_dict(link, _link_display_name(session, link))
    session.delete(link)
    session.flush()
    change = _record_change(session, payload.case_id, "unlink", actor, entry,
                            [{"target": "link", "id": str(link.id), "before": before,
                              "after": None, "before_rev": None, "after_rev": None}])
    _audit(session, "unlink", payload.case_id, link.id)
    return _finish(session, payload.case_id, change.id)


def accept_change(session: Session, payload: ChangeIdInput, *, actor: str, entry: str) -> ProgressSnapshot:
    if actor != "human":
        raise _err(ErrorCode.FORBIDDEN, "只有承办人可以确认 Agent 修改")
    # 归档后进度只读同样覆盖确认/撤回：与其它同案写入共用案件行锁，锁后复核。
    cases.lock_case_writable(session, payload.case_id, archived_message=ARCHIVED_MESSAGE)
    _lock_case(session, payload.case_id)
    change = session.get(models.ProgressChange, payload.change_id)
    if change is None or change.case_id != payload.case_id:
        raise _err(ErrorCode.NOT_FOUND, "变更记录不存在")
    if change.review_state != "pending":
        raise _err(ErrorCode.INVALID_INPUT, "该变更不处于待确认状态")
    change.review_state = "accepted"
    change.reviewed_at = _now()
    _bump_seq(session, payload.case_id)
    _audit(session, "accept_change", payload.case_id, change.id)
    return _finish(session, payload.case_id)


def accept_all(session: Session, payload: AcceptAllInput, *, actor: str, entry: str) -> ProgressSnapshot:
    if actor != "human":
        raise _err(ErrorCode.FORBIDDEN, "只有承办人可以确认 Agent 修改")
    case_id = payload.case_id
    cases.lock_case_writable(session, case_id, archived_message=ARCHIVED_MESSAGE)
    seq = _lock_case(session, case_id)
    # 审查 04 / 末节：只确认客户端已展示的条目。区分「显式空集合」与「省略 IDs」：
    # 显式提交 change_ids=[] 一律拒绝；省略 IDs 时必须携带匹配的 expected_seq，
    # 否则无法证明确认范围经过人工核对。提交 IDs 时按集合过滤，未展示项不被确认。
    ids_provided = "change_ids" in payload.model_fields_set
    if ids_provided and not payload.change_ids:
        raise _err(ErrorCode.INVALID_INPUT, "确认范围不能为空：请提交已展示的变更 ID")
    if not ids_provided and payload.expected_seq is None:
        raise _err(ErrorCode.INVALID_INPUT, "一键确认须提交已展示的变更 ID 或操作序号")
    if payload.expected_seq is not None and payload.expected_seq != seq:
        raise _err(ErrorCode.CONFLICT, "待确认列表已变化，请刷新后重新审核",
                   expected_seq=payload.expected_seq, actual_seq=seq)
    query = select(models.ProgressChange).where(
        models.ProgressChange.case_id == case_id,
        models.ProgressChange.review_state == "pending")
    if ids_provided:
        query = query.where(models.ProgressChange.id.in_(payload.change_ids))
    pending = session.execute(query).scalars().all()
    if not pending:
        raise _err(ErrorCode.INVALID_INPUT, "没有待确认的 Agent 修改")
    timestamp = _now()
    for change in pending:
        change.review_state = "accepted"
        change.reviewed_at = timestamp
    _bump_seq(session, case_id)
    # 批量审计逐条记录实际确认集合，可据此还原本次确认范围（审查末节）。
    for change in pending:
        _audit(session, "accept_all", case_id, change.id)
    return _finish(session, case_id)


def revert_change(session: Session, payload: ChangeIdInput, *, actor: str, entry: str) -> ProgressSnapshot:
    if actor != "human":
        raise _err(ErrorCode.FORBIDDEN, "只有承办人可以撤回 Agent 修改")
    cases.lock_case_writable(session, payload.case_id, archived_message=ARCHIVED_MESSAGE)
    _lock_case(session, payload.case_id)
    change = session.get(models.ProgressChange, payload.change_id)
    if change is None or change.case_id != payload.case_id:
        raise _err(ErrorCode.NOT_FOUND, "变更记录不存在")
    if change.review_state not in ("pending", "accepted", "none"):
        raise _err(ErrorCode.INVALID_INPUT, "该变更已撤回或不可撤回")
    # 整体核对：任一受影响节点此后被改过则拒绝，不做半截撤回（审查 03/05）。
    for item in change.diff:
        if item["target"] != "node":
            continue
        current = session.get(models.ProgressNode, uuid.UUID(item["id"]))
        if current is None:
            raise _err(ErrorCode.CONFLICT, "节点已被后续修改，无法撤回，请人工处理")
        if item["after_rev"] is not None and current.revision != item["after_rev"]:
            raise _err(ErrorCode.CONFLICT, "节点已被后续修改，无法撤回，请人工处理")
    # 撤回新建＝软删除：若这些节点下此后又挂了有效子节点，整笔拒绝（审查 05）。
    created_ids = {uuid.UUID(item["id"]) for item in change.diff
                   if item["target"] == "node" and item["before"] is None and item["after"] is not None}
    # 审查 N4：软删型逆操作（撤回恢复/「撤回的撤回」把现有节点软删）同样检查其下有效子节点。
    # 含 remove_node 本身（before 有效→after 软删）与 revert_change 的恢复逆操作
    # （before 软删→after 有效，撤回它即把节点重新软删）。
    softdelete_ids = {uuid.UUID(item["id"]) for item in change.diff
                      if item["target"] == "node" and item["after"] is not None
                      and item["before"] is not None
                      and (item["after"].get("removed_at") is not None)
                      != (item["before"].get("removed_at") is not None)}
    guard_ids = created_ids | softdelete_ids
    if guard_ids:
        for node in session.execute(
                select(models.ProgressNode).where(
                    models.ProgressNode.case_id == payload.case_id,
                    models.ProgressNode.removed_at.is_(None))).scalars().all():
            if node.parent_id in guard_ids and node.id not in guard_ids:
                raise _err(ErrorCode.CONFLICT, "该节点下已有后续新增的子节点，无法整体撤回，请人工处理")
    # 撤回移动＝还原父级与顺序：目标父级须仍有效，且不成环（审查 05）。
    for item in change.diff:
        if item["target"] != "node" or item["before"] is None:
            continue
        before_parent = item["before"]["parent_id"]
        if before_parent:
            parent = session.get(models.ProgressNode, uuid.UUID(before_parent))
            if parent is None or parent.removed_at is not None:
                raise _err(ErrorCode.CONFLICT, "原父节点已删除，无法撤回移动，请人工处理")
    reverse_diff: list[dict[str, Any]] = []
    timestamp = _now()
    for item in change.diff:
        if item["target"] == "relation":
            current = session.get(models.ProgressRelation, uuid.UUID(item["id"]))
            if current is None:
                continue
            before = _relation_dict(current)
            if item["before"] is None and item["after"] is not None:
                current.removed_at = timestamp
            elif item["before"] is not None and item["after"] is None:
                # 恢复旧关系前查重：同向同类型已有有效关系则冲突（审查 06）。
                dup = session.execute(
                    select(models.ProgressRelation).where(
                        models.ProgressRelation.case_id == payload.case_id,
                        models.ProgressRelation.from_node_id == current.from_node_id,
                        models.ProgressRelation.to_node_id == current.to_node_id,
                        models.ProgressRelation.type == current.type,
                        models.ProgressRelation.removed_at.is_(None),
                        models.ProgressRelation.id != current.id)).scalar_one_or_none()
                if dup is not None:
                    raise _err(ErrorCode.CONFLICT, "已存在同向同类有效关系，无法撤回该移除，请人工处理")
                # 审查 N5：端点此后已被软删则拒绝恢复，避免悬空关系。
                for endpoint_id in (current.from_node_id, current.to_node_id):
                    endpoint = session.get(models.ProgressNode, endpoint_id)
                    if endpoint is None or endpoint.removed_at is not None:
                        raise _err(ErrorCode.CONFLICT, "关系端点已删除，无法撤回该移除，请人工处理")
                current.removed_at = None
            elif item["before"] is not None:
                _apply_relation_snapshot(current, item["before"])
            reverse_diff.append({"target": "relation", "id": str(current.id), "before": before,
                                 "after": _relation_dict(current), "before_rev": None, "after_rev": None})
            continue
        if item["target"] == "link":
            if item["before"] is None and item["after"] is not None:
                current = session.get(models.ProgressLink, uuid.UUID(item["id"]))
                if current is not None:
                    before = _link_dict(current, _link_display_name(session, current))
                    session.delete(current)
                    reverse_diff.append({"target": "link", "id": item["id"], "before": before,
                                         "after": None, "before_rev": None, "after_rev": None})
            elif item["before"] is not None and item["after"] is None:
                snap = item["before"]
                dup = session.execute(
                    select(models.ProgressLink).where(
                        models.ProgressLink.node_id == uuid.UUID(snap["node_id"]),
                        models.ProgressLink.material_id == (uuid.UUID(snap["material_id"]) if snap["material_id"] else None),
                        models.ProgressLink.analysis_id == (uuid.UUID(snap["analysis_id"]) if snap["analysis_id"] else None),
                        models.ProgressLink.annotation_id == (uuid.UUID(snap["annotation_id"]) if snap["annotation_id"] else None))).scalar_one_or_none()
                if dup is not None:
                    raise _err(ErrorCode.CONFLICT, "已存在同节点同对象的有效关联，无法撤回该移除，请人工处理")
                # 审查 N5：关联节点此后已被软删则拒绝恢复，避免悬空关联。
                link_node_row = session.get(models.ProgressNode, uuid.UUID(snap["node_id"]))
                if link_node_row is None or link_node_row.removed_at is not None:
                    raise _err(ErrorCode.CONFLICT, "关联节点已删除，无法撤回该移除，请人工处理")
                restored = _restore_link(session, payload.case_id, snap)
                reverse_diff.append({"target": "link", "id": str(restored.id), "before": None,
                                     "after": _link_dict(restored, _link_display_name(session, restored)),
                                     "before_rev": None, "after_rev": None})
            continue
        current = session.get(models.ProgressNode, uuid.UUID(item["id"]))
        before = _node_dict(current)
        before_rev = current.revision
        if item["before"] is None and item["after"] is not None:
            current.removed_at = timestamp  # revert create = soft delete
        elif item["before"] is not None and item["after"] is not None:
            # 审查 N4：按 before/after 的 removed_at 判断恢复/软删（remove_node 的
            # diff after 非 null，只是 removed_at 置位），不能只看 after is None。
            before_removed = item["before"].get("removed_at") is not None
            after_removed = item["after"].get("removed_at") is not None
            if not before_removed and after_removed:
                # 撤回删除＝恢复：还原修改前快照（含 state/position），被占用则顺延让位。
                _apply_node_snapshot(current, item["before"])
                target_pos = item["before"]["position"]
                occupied = session.execute(
                    select(models.ProgressNode).where(
                        models.ProgressNode.case_id == payload.case_id,
                        models.ProgressNode.parent_id == current.parent_id,
                        models.ProgressNode.removed_at.is_(None),
                        models.ProgressNode.id != current.id,
                        models.ProgressNode.position >= target_pos)).scalars().all()
                for sibling in occupied:
                    # 审查 R3-1：让位同级记入 reverse_diff（与 add/move/fragment 同口径），
                    # 历史可追溯、二次撤回可还原。
                    sib_before = _node_dict(sibling)
                    sib_rev = sibling.revision
                    sibling.position += 1
                    sibling.revision += 1
                    sibling.updated_by = "human"
                    sibling.updated_at = timestamp
                    reverse_diff.append({"target": "node", "id": str(sibling.id),
                                         "before": sib_before, "after": _node_dict(sibling),
                                         "before_rev": sib_rev, "after_rev": sibling.revision})
                current.position = target_pos
            elif before_removed and not after_removed:
                current.removed_at = timestamp  # 撤回恢复＝软删
            else:
                _apply_node_snapshot(current, item["before"])
        elif item["before"] is not None:
            _apply_node_snapshot(current, item["before"])
        current.revision = before_rev + 1
        current.updated_by = "human"
        current.updated_at = timestamp
        reverse_diff.append({"target": "node", "id": str(current.id), "before": before,
                             "after": _node_dict(current), "before_rev": before_rev,
                             "after_rev": current.revision})
    session.flush()
    # 撤回可能把某阶段恢复为进行中：联动同程序其他进行中阶段（审查 07/R4-2）。
    # 跳过本次刚恢复/刚置 in_progress 的节点自身与同批恢复者，避免自联动抵消
    # （否则刚恢复的甲会被以甲为基准的联动又置 completed，最终无进行中阶段）。
    # 先收集要联动的同级，再统一改状态：避免边遍历边 append reverse_diff
    # 把刚置 completed 的同级又当成「恢复为进行中」重复处理（R4-2）。
    # restored_in_progress 只含主循环真正恢复/置为进行中的节点，不含让位同级
    # （让位只改 position，state 不变；若其原本 in_progress 会误入集合导致漏联动）。
    restored_in_progress = {
        uuid.UUID(item["id"]) for item in reverse_diff
        if item["target"] == "node" and item["after"] and item["after"]["state"] == "in_progress"
        and item["before"] is not None and item["before"].get("removed_at") is not None
    }
    cascade_targets: list[models.ProgressNode] = []
    for node_id in restored_in_progress:
        node = session.get(models.ProgressNode, node_id)
        if node is None or node.kind != "stage":
            continue
        for sibling in _active_children(session, payload.case_id, node.parent_id):
            if (sibling.id != node.id and sibling.id not in restored_in_progress
                    and sibling.kind == "stage" and sibling.state == "in_progress"
                    and sibling not in cascade_targets):
                cascade_targets.append(sibling)
    for sibling in cascade_targets:
        sib_before = _node_dict(sibling)
        sib_rev = sibling.revision
        sibling.state = "completed"
        sibling.revision += 1
        sibling.updated_by = "human"
        sibling.updated_at = timestamp
        reverse_diff.append({"target": "node", "id": str(sibling.id), "before": sib_before,
                             "after": _node_dict(sibling), "before_rev": sib_rev,
                             "after_rev": sibling.revision})
    session.flush()
    change.review_state = "reverted"
    change.reviewed_at = timestamp
    revert_record = _record_change(session, payload.case_id, "revert_change", actor, entry, reverse_diff)
    change.reverted_by = revert_record.id
    _audit(session, "revert_change", payload.case_id, change.id)
    return _finish(session, payload.case_id, revert_record.id)


def _apply_node_snapshot(row: models.ProgressNode, snapshot: dict[str, Any]) -> None:
    row.parent_id = uuid.UUID(snapshot["parent_id"]) if snapshot["parent_id"] else None
    row.position = snapshot["position"]
    row.kind = snapshot["kind"]
    row.state = snapshot["state"]
    row.title = snapshot["title"]
    row.note = snapshot["note"]
    row.date_start = datetime.fromisoformat(snapshot["date_start"]) if snapshot["date_start"] else None
    row.date_end = datetime.fromisoformat(snapshot["date_end"]) if snapshot["date_end"] else None
    row.template_ref = snapshot["template_ref"]
    row.request_key = snapshot["request_key"]
    row.removed_at = datetime.fromisoformat(snapshot["removed_at"]) if snapshot["removed_at"] else None


def _apply_relation_snapshot(row: models.ProgressRelation, snapshot: dict[str, Any]) -> None:
    row.from_node_id = uuid.UUID(snapshot["from_node_id"])
    row.to_node_id = uuid.UUID(snapshot["to_node_id"])
    row.type = snapshot["type"]
    row.note = snapshot["note"]
    row.removed_at = datetime.fromisoformat(snapshot["removed_at"]) if snapshot["removed_at"] else None


def _restore_link(session: Session, case_id: uuid.UUID, snapshot: dict[str, Any]) -> models.ProgressLink:
    row = models.ProgressLink(
        id=uuid.UUID(snapshot["id"]), case_id=case_id, node_id=uuid.UUID(snapshot["node_id"]),
        material_id=uuid.UUID(snapshot["material_id"]) if snapshot["material_id"] else None,
        analysis_id=uuid.UUID(snapshot["analysis_id"]) if snapshot["analysis_id"] else None,
        annotation_id=uuid.UUID(snapshot["annotation_id"]) if snapshot["annotation_id"] else None,
        created_by=snapshot["created_by"],
    )
    session.add(row)
    session.flush()
    return row


def instantiate_default_for_new_case(session: Session, case_id: uuid.UUID, *,
                                     actor: str = "human", entry: str = "local-web") -> None:
    """K14: instantiate the default fragment inside the caller's transaction.

    Used by cases.create_case so a new case and its progress tree commit together.
    The caller has already taken begin_write; we lock the case seq row and record
    one insert_fragment change. actor/entry reflect the real creating transport
    (审查 13)：网页 human/local-web、CLI agent/cli、MCP agent/mcp；自动初始化
    与人工修改按同一复核语义记录（agent 建案的默认片段也进入待确认）。
    """
    rules = progress_rules.get_rules()
    fragment = rules["fragments"][rules["default_fragment"]]
    _lock_case(session, case_id)
    timestamp = _now()
    created: list[models.ProgressNode] = []
    template_ref = f"{rules['default_fragment']}@{fragment['version']}"
    for index, root in enumerate(fragment["roots"]):
        _walk_fragment(session, rules, case_id, root, None, index, template_ref,
                       actor, timestamp, created)
    _record_change(session, case_id, "insert_fragment", actor, entry,
                   [_node_diff(None, row) for row in created])
    _audit(session, "insert_fragment", case_id, created[0].id)


def backfill_default_fragments(session: Session) -> int:
    """K14: instantiate the default fragment for cases that have no progress nodes.

    Run once after migration 0005 (scripts/lwb migrate calls this). Idempotent:
    cases that already have nodes are skipped. Returns the number of cases filled.

    审查 10：旧案 cases.stage 与模板阶段名精确匹配时，回填后激活该阶段；
    不匹配则保持默认（无进行中）。已有人工调整的案件不会被重跑覆盖。
    """
    rules = progress_rules.get_rules()
    fragment = rules["fragments"][rules["default_fragment"]]
    cases = session.execute(select(models.Case)).scalars().all()
    filled = 0
    for case in cases:
        case_id = case.id
        existing = session.execute(
            select(models.ProgressNode.id).where(models.ProgressNode.case_id == case_id).limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            continue
        begin_write(session)
        _lock_case(session, case_id)
        timestamp = _now()
        created: list[models.ProgressNode] = []
        template_ref = f"{rules['default_fragment']}@{fragment['version']}"
        for index, root in enumerate(fragment["roots"]):
            _walk_fragment(session, rules, case_id, root, None, index, template_ref,
                           "human", timestamp, created)
        diff = [_node_diff(None, row) for row in created]
        # 旧阶段名精确匹配模板阶段 → 激活（审查 10）。
        old_stage = (case.stage or "").strip()
        if old_stage:
            match = next((row for row in created if row.kind == "stage" and row.title == old_stage), None)
            if match is not None:
                before = _node_dict(match)
                before_rev = match.revision
                match.state = "in_progress"
                match.revision += 1
                match.updated_by = "human"
                match.updated_at = timestamp
                diff.append({"target": "node", "id": str(match.id), "before": before,
                             "after": _node_dict(match), "before_rev": before_rev,
                             "after_rev": match.revision})
        _record_change(session, case_id, "insert_fragment", "human", "local-web", diff)
        _audit(session, "insert_fragment", case_id, created[0].id)
        session.commit()
        filled += 1
    return filled