"""Shared change-log framework for the graph and evidence domains (D4).

``progress_changes`` stays exactly as it is (accepted, reviewed, in production).
The new domains share this module for the parts that must behave identically:

* the per-case/domain sequence row (``case_change_seq``) is created atomically
  and locked ``FOR UPDATE`` before any write, so concurrent writers serialize on
  the same row and the change ``seq`` is gap-free per case;
* one change row is written per command with the before/after diff;
* agent writes become ``pending``; accept/revert is human-only;
* accept-all only touches the change ids the reviewer actually saw (or the
  sequence they opened the dialog with).

Domain-specific reverse application stays in each service, because what "undo"
means differs per target type.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from . import models
from .errors import ErrorCode, WorkbenchError

DOMAINS = ("progress", "graph", "evidence", "claims")


def now() -> datetime:
    return datetime.now(timezone.utc)


def _err(code: ErrorCode, message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError(code, message, details or None)


def lock_seq(session: Session, case_id: uuid.UUID, domain: str) -> int:
    """Create-if-absent then lock the domain sequence row; return the current seq.

    ``ON CONFLICT DO NOTHING`` keeps two concurrent first writers from failing on
    the primary key (same pattern as the reviewed progress service).
    """
    session.execute(
        pg_insert(models.CaseChangeSeq)
        .values(case_id=case_id, domain=domain, seq=0)
        .on_conflict_do_nothing(index_elements=["case_id", "domain"])
    )
    row = session.execute(
        select(models.CaseChangeSeq)
        .where(models.CaseChangeSeq.case_id == case_id, models.CaseChangeSeq.domain == domain)
        .with_for_update()
    ).scalar_one()
    return row.seq


def bump_seq(session: Session, case_id: uuid.UUID, domain: str) -> int:
    row = session.execute(
        select(models.CaseChangeSeq)
        .where(models.CaseChangeSeq.case_id == case_id, models.CaseChangeSeq.domain == domain)
        .with_for_update()
    ).scalar_one()
    row.seq += 1
    session.flush()
    return row.seq


def record(session: Session, model: type, *, case_id: uuid.UUID, domain: str,
           command: str, actor: str, entry: str,
           diff: list[dict[str, Any]]) -> Any:
    """Append one change row. Agent commands are pending review."""
    seq = bump_seq(session, case_id, domain)
    row = model(
        id=uuid.uuid4(), case_id=case_id, seq=seq, command=command, actor=actor,
        entry=entry, diff=diff,
        review_state="pending" if actor == "agent" else "none",
    )
    session.add(row)
    session.flush()
    return row


def list_changes(session: Session, model: type, case_id: uuid.UUID) -> list[Any]:
    return session.execute(
        select(model).where(model.case_id == case_id).order_by(model.seq)
    ).scalars().all()


def pending_changes(session: Session, model: type, case_id: uuid.UUID) -> list[Any]:
    return [row for row in list_changes(session, model, case_id) if row.review_state == "pending"]


def pending_addition_ids(changes: list[Any]) -> set[tuple[str, str]]:
    """(target, id) pairs that a pending agent change *created*.

    Human writes must not land on an object that only exists pending review: the
    object is not part of the confirmed state and would be silently excluded from
    the next snapshot (P1 pendingAdditionGuard).
    """
    created: set[tuple[str, str]] = set()
    for change in changes:
        if change.review_state != "pending":
            continue
        for item in change.diff:
            if item.get("before") is None and item.get("after") is not None:
                created.add((item["target"], str(item["id"])))
    return created


def accept_one(session: Session, model: type, *, case_id: uuid.UUID, domain: str,
               change_id: uuid.UUID) -> Any:
    change = session.get(model, change_id)
    if change is None or change.case_id != case_id:
        raise _err(ErrorCode.NOT_FOUND, "变更记录不存在", change_id=str(change_id))
    if change.review_state != "pending":
        raise _err(ErrorCode.INVALID_INPUT, "该变更不处于待确认状态")
    change.review_state = "accepted"
    change.reviewed_at = now()
    bump_seq(session, case_id, domain)
    return change


def accept_all(session: Session, model: type, *, case_id: uuid.UUID, domain: str,
               change_ids: list[uuid.UUID], expected_seq: int | None,
               ids_provided: bool) -> list[Any]:
    """Accept only the reviewed scope; never sweep up changes that arrived later."""
    current = lock_seq(session, case_id, domain)
    if ids_provided and not change_ids:
        raise _err(ErrorCode.INVALID_INPUT, "确认范围不能为空：请提交已展示的变更 ID")
    if not ids_provided and expected_seq is None:
        raise _err(ErrorCode.INVALID_INPUT, "一键确认须提交已展示的变更 ID 或操作序号")
    if expected_seq is not None and expected_seq != current:
        raise _err(ErrorCode.CONFLICT, "待确认列表已变化，请刷新后重新审核",
                   expected_seq=expected_seq, actual_seq=current)
    query = select(model).where(model.case_id == case_id, model.review_state == "pending")
    if ids_provided:
        query = query.where(model.id.in_(change_ids))
    pending = session.execute(query).scalars().all()
    if not pending:
        raise _err(ErrorCode.INVALID_INPUT, "没有待确认的 Agent 修改")
    timestamp = now()
    for change in pending:
        change.review_state = "accepted"
        change.reviewed_at = timestamp
    bump_seq(session, case_id, domain)
    return pending


def mark_reverted(session: Session, change: Any, revert_record_id: uuid.UUID) -> None:
    change.review_state = "reverted"
    change.reviewed_at = now()
    change.reverted_by = revert_record_id
