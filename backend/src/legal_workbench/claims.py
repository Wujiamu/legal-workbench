"""Claim amount service: integer cents, groups/items, staged agent candidates.

Same command frame as ``progress.py`` (decision 20261116-界面与Agent共用业务命令):

    begin_write -> lock case row (cases.lock_case_writable) -> lock
    case_change_seq(case_id, 'claims') FOR UPDATE -> validation/CAS -> change
    record -> seq + 1 -> audit -> commit

Lock order is fixed project-wide and shared with archive: maintenance gate ->
case row -> domain seq.  Archiving takes the same case row, so a claim write can
no longer slip in between an archive check and the write; conversely a claim
write in flight blocks the archive until it commits.  Readers take a *shared*
lock on the case row (never on the seq row), which pins one committed state for
the seq, the rows and the change log, and cannot deadlock against writers that
always go case row -> seq.

One deliberate difference from progress: an agent submission is **staged** in
``claim_changes`` and is not written to ``claim_groups`` / ``claim_items`` until
a human accepts it.  That is what the P1 prototype promised ("确认前不写入明细、
不计入合计") and it keeps pending candidates out of every subtotal and
per-currency total without a per-row review flag.  Revert therefore discards the
staged change and leaves the business tables untouched.

Amounts are integer cents end to end.  Direction comes from ``category``
(``deduction`` subtracts); a stored amount is never negative, so a deduction can
never be double-negated.  The accepted range stops at the JavaScript safe
integer because the same number is transported as JSON to the browser, and the
database CHECK enforces the same ceiling so a bypass cannot store a value the
contract cannot represent.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, cases, models
from .errors import ErrorCode, WorkbenchError
from .schemas import (
    CLAIM_MAX_CENTS, AddClaimGroupInput, AddClaimItemInput, ChangeIdInput,
    ClaimAcceptAllInput, ClaimAmountCandidate, ClaimAmountCandidates, ClaimChangeOut,
    ClaimCurrencyTotal, ClaimDiffEntry, ClaimGroupChanges, ClaimGroupOut,
    ClaimGroupRevisionInput, ClaimItemChanges, ClaimItemOut, ClaimItemRevisionInput,
    ClaimSnapshot, ReorderClaimGroupInput, ReorderClaimItemInput, UpdateClaimGroupInput,
    UpdateClaimItemInput,
)

DOMAIN = "claims"

# Agent may submit candidates for groups and items; accept/revert stay human-only.
AGENT_COMMANDS = frozenset({"add_group", "update_group", "add_item", "update_item"})
DEDUCTING_CATEGORY = "deduction"
CANDIDATE_LIMIT = 200
EXCERPT_CHARS = 60
CURRENCY_ORDER = ("CNY", "USD", "EUR")

# A segment may only be cited when its text is final: a run that is still queued
# or running can rewrite (delete) its own segments on retry, which would silently
# destroy the provenance of an accepted amount.  Older published runs stay
# readable on purpose — history is allowed, in-flight work is not.
READABLE_RUN_STATUSES = ("complete", "partial")
TEXT_UNIT_STATUSES = ("extracted", "ocr_done")



def _now() -> datetime:
    return datetime.now(timezone.utc)


def _err(code: ErrorCode, message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError(code, message, details or None)


def _case_or_error(session: Session, case_id: uuid.UUID) -> models.Case:
    row = session.get(models.Case, case_id)
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, "案件不存在", case_id=str(case_id))
    return row


def _lock_seq(session: Session, case_id: uuid.UUID) -> int:
    """Take the claims seq row lock; returns the current seq.

    Callers must already hold the case row lock (``cases.lock_case_writable``).
    First write creates the seq row atomically (INSERT … ON CONFLICT DO NOTHING)
    then takes the row lock, so two concurrent first writes cannot fail on the
    primary key.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    session.execute(
        pg_insert(models.CaseChangeSeq)
        .values(case_id=case_id, domain=DOMAIN, seq=0)
        .on_conflict_do_nothing(index_elements=["case_id", "domain"])
    )
    row = session.execute(
        select(models.CaseChangeSeq)
        .where(models.CaseChangeSeq.case_id == case_id,
               models.CaseChangeSeq.domain == DOMAIN)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    return row.seq


def _bump_seq(session: Session, case_id: uuid.UUID) -> int:
    row = session.execute(
        select(models.CaseChangeSeq)
        .where(models.CaseChangeSeq.case_id == case_id,
               models.CaseChangeSeq.domain == DOMAIN)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    row.seq += 1
    session.flush()
    return row.seq


# --- serialization ----------------------------------------------------------

def _group_dict(row: models.ClaimGroup) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id), "title": row.title,
        "currency": row.currency, "include_in_total": row.include_in_total,
        "position": row.position, "note": row.note,
        "created_by": row.created_by, "updated_by": row.updated_by,
        "request_key": row.request_key,
        "removed_at": row.removed_at.isoformat() if row.removed_at else None,
        "revision": row.revision,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
    }


def _item_dict(row: models.ClaimItem) -> dict[str, Any]:
    return {
        "id": str(row.id), "case_id": str(row.case_id), "group_id": str(row.group_id),
        "title": row.title, "category": row.category, "amount_cents": int(row.amount_cents),
        "calc_note": row.calc_note,
        "segment_id": str(row.segment_id) if row.segment_id else None,
        "material_id": str(row.material_id) if row.material_id else None,
        "annotation_id": str(row.annotation_id) if row.annotation_id else None,
        "position": row.position, "created_by": row.created_by, "updated_by": row.updated_by,
        "request_key": row.request_key,
        "removed_at": row.removed_at.isoformat() if row.removed_at else None,
        "revision": row.revision,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
    }


def _group_out(row: models.ClaimGroup) -> ClaimGroupOut:
    return ClaimGroupOut(
        id=row.id, case_id=row.case_id, title=row.title, currency=row.currency,
        include_in_total=row.include_in_total, position=row.position, note=row.note,
        created_by=row.created_by, updated_by=row.updated_by, request_key=row.request_key,
        removed_at=row.removed_at, revision=row.revision,
        created_at=row.created_at, updated_at=row.updated_at,
    )


def _item_out(row: models.ClaimItem) -> ClaimItemOut:
    return ClaimItemOut(
        id=row.id, case_id=row.case_id, group_id=row.group_id, title=row.title,
        category=row.category, amount_cents=int(row.amount_cents),
        calc_note=row.calc_note, segment_id=row.segment_id, material_id=row.material_id,
        annotation_id=row.annotation_id, position=row.position,
        created_by=row.created_by, updated_by=row.updated_by, request_key=row.request_key,
        removed_at=row.removed_at, revision=row.revision,
        created_at=row.created_at, updated_at=row.updated_at,
    )


def _change_out(row: models.ClaimChange) -> ClaimChangeOut:
    return ClaimChangeOut(
        id=row.id, case_id=row.case_id, seq=row.seq, command=row.command,
        actor=row.actor, entry=row.entry,
        diff=[ClaimDiffEntry(**entry) for entry in row.diff],
        review_state=row.review_state, reviewed_at=row.reviewed_at,
        reverted_by=row.reverted_by, request_key=row.request_key,
        created_at=row.created_at,
    )


def _group_diff(before: models.ClaimGroup | None,
                after: models.ClaimGroup | None) -> dict[str, Any]:
    ref = after or before
    return {
        "target": "group", "id": str(ref.id),
        "before": _group_dict(before) if before else None,
        "after": _group_dict(after) if after else None,
        "before_rev": before.revision if before else None,
        "after_rev": after.revision if after else None,
    }


def _item_diff(before: models.ClaimItem | None,
               after: models.ClaimItem | None) -> dict[str, Any]:
    ref = after or before
    return {
        "target": "item", "id": str(ref.id),
        "before": _item_dict(before) if before else None,
        "after": _item_dict(after) if after else None,
        "before_rev": before.revision if before else None,
        "after_rev": after.revision if after else None,
    }


# --- reads ------------------------------------------------------------------

def _all_groups(session: Session, case_id: uuid.UUID) -> list[models.ClaimGroup]:
    return session.execute(
        select(models.ClaimGroup).where(models.ClaimGroup.case_id == case_id)
        .order_by(models.ClaimGroup.position, models.ClaimGroup.created_at)
        .execution_options(populate_existing=True)
    ).scalars().all()


def _all_items(session: Session, case_id: uuid.UUID) -> list[models.ClaimItem]:
    return session.execute(
        select(models.ClaimItem).where(models.ClaimItem.case_id == case_id)
        .order_by(models.ClaimItem.position, models.ClaimItem.created_at)
        .execution_options(populate_existing=True)
    ).scalars().all()


def _active_groups(session: Session, case_id: uuid.UUID) -> list[models.ClaimGroup]:
    return [row for row in _all_groups(session, case_id) if row.removed_at is None]


def _active_items(session: Session, case_id: uuid.UUID,
                  group_id: uuid.UUID | None = None) -> list[models.ClaimItem]:
    return [row for row in _all_items(session, case_id)
            if row.removed_at is None and (group_id is None or row.group_id == group_id)]


def _signed(item: models.ClaimItem) -> int:
    amount = int(item.amount_cents)
    return -amount if item.category == DEDUCTING_CATEGORY else amount


def _compute_totals(groups: list[models.ClaimGroup],
                    items: list[models.ClaimItem]) -> tuple[dict[str, int], list[ClaimCurrencyTotal]]:
    """Subtotals per group and per-currency totals; pending changes never enter.

    Only active rows take part: a soft-deleted item or group contributes nothing,
    and only groups with ``include_in_total`` count towards the case total, which
    is how overlapping procedures (一审/二审) avoid double counting.
    """
    active_groups = [group for group in groups if group.removed_at is None]
    active_items = [item for item in items if item.removed_at is None]
    subtotals: dict[str, int] = {}
    for group in active_groups:
        subtotals[str(group.id)] = sum(
            _signed(item) for item in active_items if item.group_id == group.id)
    by_currency: dict[str, ClaimCurrencyTotal] = {}
    for group in active_groups:
        if not group.include_in_total:
            continue
        entry = by_currency.get(group.currency)
        if entry is None:
            entry = ClaimCurrencyTotal(currency=group.currency, total_cents=0,
                                       included_group_ids=[])
            by_currency[group.currency] = entry
        entry.total_cents += subtotals[str(group.id)]
        entry.included_group_ids.append(group.id)
    order = {currency: index for index, currency in enumerate(CURRENCY_ORDER)}
    totals = sorted(by_currency.values(),
                    key=lambda entry: (order.get(entry.currency, len(order)), entry.currency))
    return subtotals, totals


def get_claims(session: Session, case_id: uuid.UUID) -> ClaimSnapshot:
    """One committed state for seq + groups + items + change log.

    A *shared* lock on the case row is the snapshot anchor.  Writers take that
    same row exclusively before touching the claims seq, so a reader can never
    observe the seq row from one state and the rows from another — including the
    first write of a brand-new case, where the seq row does not exist yet and
    "no rows, seq 0" must stay true for the whole read.  Reading the seq row
    itself with ``FOR SHARE`` would not cover that case (there is no row to lock)
    and would also invert the lock order relative to writers.
    """
    _case_or_error(session, case_id)
    session.execute(
        select(models.Case.id).where(models.Case.id == case_id)
        .with_for_update(read=True)
    ).scalar_one()
    seq_row = session.execute(
        select(models.CaseChangeSeq)
        .where(models.CaseChangeSeq.case_id == case_id,
               models.CaseChangeSeq.domain == DOMAIN)
    ).scalar_one_or_none()
    groups = _all_groups(session, case_id)
    items = _all_items(session, case_id)
    changes = session.execute(
        select(models.ClaimChange).where(models.ClaimChange.case_id == case_id)
        .order_by(models.ClaimChange.seq)
    ).scalars().all()
    subtotals, totals = _compute_totals(groups, items)
    pending = [row for row in changes if row.review_state == "pending"]
    return ClaimSnapshot(
        case_id=case_id,
        seq=seq_row.seq if seq_row else 0,
        groups=[_group_out(row) for row in groups],
        items=[_item_out(row) for row in items],
        group_subtotals=subtotals,
        totals_by_currency=totals,
        pending_changes=[_change_out(row) for row in pending],
        changes=[_change_out(row) for row in changes],
        pending_count=len(pending),
        excluded_group_count=len([g for g in groups
                                  if g.removed_at is None and not g.include_in_total]),
        included_group_count=len([g for g in groups
                                  if g.removed_at is None and g.include_in_total]),
    )


def _amount_to_cents(value: Decimal | None) -> int | None:
    """Exact decimal → integer cents; None when the value has sub-cent precision."""
    if value is None:
        return None
    cents = Decimal(value).scaleb(2)
    if cents != cents.to_integral_value():
        return None
    result = int(cents)
    if result < 0 or result > CLAIM_MAX_CENTS:
        return None
    return result


def list_amount_candidates(session: Session, case_id: uuid.UUID,
                           query: str | None = None) -> ClaimAmountCandidates:
    """This case's ``exact_values(kind='amount')`` rows with a usable source.

    Scope follows the existing search convention (``search._active_runs``): only
    the file version's *active* parse run contributes candidates, so a stale run
    superseded by a reparse cannot offer an amount that the current text no
    longer contains.  Rows are also restricted to terminal, readable text: a run
    still queued/running may rewrite its own segments on retry.

    The source is returned as stable ids (segment/material), never as a copied
    evidence number: the catalog owns numbering (decision D2).  Rows whose amount
    is not exactly representable in cents come back with ``amount_cents=None`` so
    the UI asks for manual entry instead of silently rounding.

    ``currency IS NOT NULL`` is applied *before* ``LIMIT``: filtering in Python
    let currency-less rows consume the whole page and hide valid candidates.
    ``span`` is re-based onto the returned ``excerpt`` so the caller can highlight
    the number without knowing the segment offsets.
    """
    _case_or_error(session, case_id)
    statement = (
        select(models.ExactValue, models.Segment, models.ParseRun,
               models.FileVersion, models.Material, models.ParseUnit)
        .join(models.Segment, models.Segment.id == models.ExactValue.segment_id)
        .join(models.ParseRun, models.ParseRun.id == models.Segment.parse_run_id)
        .join(models.FileVersion, models.FileVersion.id == models.ParseRun.file_version_id)
        .join(models.Material, models.Material.id == models.FileVersion.material_id)
        .join(models.ParseUnit, models.ParseUnit.id == models.Segment.parse_unit_id)
        .where(models.ExactValue.case_id == case_id,
               models.ExactValue.kind == "amount",
               models.ExactValue.currency.is_not(None),
               # active/current run of its own file version — the search scope.
               models.FileVersion.active_parse_run_id == models.ParseRun.id,
               models.ParseRun.status.in_(READABLE_RUN_STATUSES),
               models.ParseUnit.status.in_(TEXT_UNIT_STATUSES))
        .order_by(models.Material.display_name, models.Segment.id,
                  models.ExactValue.span["start"].astext)
        .limit(CANDIDATE_LIMIT)
    )
    if query:
        needle = f"%{query.strip()}%"
        statement = statement.where(
            models.ExactValue.raw_text.ilike(needle)
            | models.Material.display_name.ilike(needle))
    candidates: list[ClaimAmountCandidate] = []
    for exact, segment, _run, _version, material, _unit in session.execute(statement).all():
        span = exact.span or {}
        start = int(span.get("start", 0))
        end = int(span.get("end", start + len(exact.raw_text)))
        excerpt_start = max(0, start - EXCERPT_CHARS)
        excerpt_end = min(len(segment.text), end + EXCERPT_CHARS)
        candidates.append(ClaimAmountCandidate(
            exact_value_id=exact.id, segment_id=segment.id,
            material_id=material.id, material_title=material.display_name,
            page=segment.locator.get("page") if isinstance(segment.locator, dict) else None,
            locator=segment.locator or {},
            raw_text=exact.raw_text, currency=exact.currency,
            amount_cents=_amount_to_cents(exact.amount_value),
            excerpt=segment.text[excerpt_start:excerpt_end],
            span={"start": start - excerpt_start, "end": end - excerpt_start},
        ))
    return ClaimAmountCandidates(case_id=case_id, candidates=candidates)


# --- validation helpers -----------------------------------------------------

def _text(value: str, label: str) -> str:
    trimmed = (value or "").strip()
    if not trimmed:
        raise _err(ErrorCode.INVALID_INPUT, f"{label}不能为空")
    if len(trimmed) > 200:
        raise _err(ErrorCode.INVALID_INPUT, f"{label}不能超过 200 字")
    return trimmed


def _optional_text(value: str | None, label: str, limit: int = 2000) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    if len(trimmed) > limit:
        raise _err(ErrorCode.INVALID_INPUT, f"{label}不能超过 {limit} 字")
    return trimmed or None


def _currency(value: str) -> str:
    normalized = (value or "").strip().upper()
    if len(normalized) != 3 or not normalized.isalpha() or not normalized.isascii():
        raise _err(ErrorCode.INVALID_INPUT, "币种须为三位字母代码（如 CNY、USD）")
    return normalized


def _amount(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _err(ErrorCode.INVALID_INPUT, "金额必须是整数分")
    if value < 0 or value > CLAIM_MAX_CENTS:
        raise _err(ErrorCode.INVALID_INPUT, "金额超出可精确表示的范围（约 90 万亿元）")
    return value


def _validate_source(session: Session, case_id: uuid.UUID, segment_id: uuid.UUID | None,
                     material_id: uuid.UUID | None,
                     annotation_id: uuid.UUID | None) -> None:
    """At most one source; it must exist, belong to this case, and be readable.

    A segment is only citable when its text is final: the run must have reached a
    readable terminal status and the unit must actually carry extracted text.
    A run that is queued/running rewrites (deletes) its own segments on retry, so
    accepting a reference to it would let the provenance disappear afterwards.
    Older published runs are allowed on purpose — history stays addressable.
    """
    if sum(value is not None for value in (segment_id, material_id, annotation_id)) > 1:
        raise _err(ErrorCode.INVALID_INPUT, "出处只能选择一个：片段、材料或标注")
    checks = (
        (segment_id, models.Segment, "片段"),
        (material_id, models.Material, "材料"),
        (annotation_id, models.Annotation, "标注"),
    )
    for object_id, model, label in checks:
        if object_id is None:
            continue
        row = session.get(model, object_id)
        if row is None:
            raise _err(ErrorCode.NOT_FOUND, f"出处{label}不存在", object_id=str(object_id))
        if row.case_id != case_id:
            raise _err(ErrorCode.CASE_MISMATCH, f"出处{label}不属于本案",
                       object_id=str(object_id))
    if segment_id is not None:
        _require_readable_segment(session, case_id, segment_id)


def _require_readable_segment(session: Session, case_id: uuid.UUID,
                              segment_id: uuid.UUID) -> None:
    row = session.execute(
        select(models.ParseRun.status, models.ParseUnit.status)
        .join(models.ParseUnit, models.ParseUnit.parse_run_id == models.ParseRun.id)
        .join(models.Segment, models.Segment.parse_unit_id == models.ParseUnit.id)
        .where(models.Segment.id == segment_id,
               models.Segment.case_id == case_id)
    ).first()
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, "出处片段不存在", object_id=str(segment_id))
    run_status, unit_status = row
    if run_status not in READABLE_RUN_STATUSES or unit_status not in TEXT_UNIT_STATUSES:
        raise _err(ErrorCode.INVALID_INPUT,
                   "出处片段所属解析尚未完成或没有正文，暂不能引用",
                   object_id=str(segment_id), run_status=run_status,
                   unit_status=unit_status)


def _group_or_error(session: Session, case_id: uuid.UUID,
                    group_id: uuid.UUID) -> models.ClaimGroup:
    row = _fresh(session, models.ClaimGroup, group_id)
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, "诉请分组不存在", group_id=str(group_id))
    if row.case_id != case_id:
        raise _err(ErrorCode.CASE_MISMATCH, "分组不属于本案", group_id=str(group_id))
    return row


def _item_or_error(session: Session, case_id: uuid.UUID,
                   item_id: uuid.UUID) -> models.ClaimItem:
    row = _fresh(session, models.ClaimItem, item_id)
    if row is None:
        raise _err(ErrorCode.NOT_FOUND, "金额项不存在", item_id=str(item_id))
    if row.case_id != case_id:
        raise _err(ErrorCode.CASE_MISMATCH, "金额项不属于本案", item_id=str(item_id))
    return row


def _fresh(session: Session, model: type[models.Base], object_id: uuid.UUID):
    """Load a row bypassing the identity map.

    Write paths run under the case row lock, so the committed revision is the one
    to compare against.  A plain ``session.get`` would hand back the instance the
    read endpoint cached earlier, and a stale ``revision`` makes CAS pass while
    the database holds a newer value — exactly the silent overwrite CAS exists to
    prevent.
    """
    return session.execute(
        select(model).where(model.id == object_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _cas(revision: int, expected: int, *, target: str, object_id: uuid.UUID) -> None:
    if revision != expected:
        raise _err(ErrorCode.CONFLICT, "该记录已被修改，请刷新后重试",
                   target=target, object_id=str(object_id),
                   expected_revision=expected, actual_revision=revision)


def _pending_targets(session: Session, case_id: uuid.UUID) -> dict[str, set[uuid.UUID]]:
    """Ids that already have a pending agent change.

    ``group``/``item`` are the changed rows themselves; ``group_ref`` also holds
    the group an item proposal belongs to, so removing that group cannot strand
    a candidate that would later fail to confirm.
    """
    pending = session.execute(
        select(models.ClaimChange).where(
            models.ClaimChange.case_id == case_id,
            models.ClaimChange.review_state == "pending")
    ).scalars().all()
    targets: dict[str, set[uuid.UUID]] = {"group": set(), "item": set(), "group_ref": set()}
    for change in pending:
        for entry in change.diff:
            targets[entry["target"]].add(uuid.UUID(entry["id"]))
            for side in ("before", "after"):
                snapshot = entry.get(side) or {}
                if snapshot.get("group_id"):
                    targets["group_ref"].add(uuid.UUID(snapshot["group_id"]))
    return targets


def _guard_no_pending(targets: dict[str, set[uuid.UUID]], kind: str, object_id: uuid.UUID) -> None:
    if object_id in targets[kind]:
        raise _err(ErrorCode.CONFLICT, "该记录有待确认的 Agent 修改，请先确认或撤回",
                   target=kind, object_id=str(object_id))


def _next_position(rows: list[models.ClaimGroup] | list[models.ClaimItem]) -> int:
    return max((row.position for row in rows), default=-1) + 1


# --- write frame ------------------------------------------------------------

def _begin(session: Session, case_id: uuid.UUID, actor: str, command: str) -> None:
    if actor == "agent" and command not in AGENT_COMMANDS:
        raise _err(ErrorCode.FORBIDDEN, f"Agent 无权执行命令：{command}")
    cases.lock_case_writable(session, case_id)
    _lock_seq(session, case_id)


def _begin_manual(session: Session, case_id: uuid.UUID) -> None:
    """Accept/revert frame: human-only, still archived-read-only, same lock order."""
    cases.lock_case_writable(session, case_id)
    _lock_seq(session, case_id)


def _record_change(session: Session, case_id: uuid.UUID, command: str, actor: str,
                   entry: str, diff: list[dict[str, Any]], *,
                   request_key: str | None = None,
                   fingerprint: str | None = None) -> models.ClaimChange:
    seq = _bump_seq(session, case_id)
    row = models.ClaimChange(
        id=uuid.uuid4(), case_id=case_id, seq=seq, command=command, actor=actor,
        entry=entry, diff=diff,
        review_state="pending" if actor == "agent" else "none",
        request_key=request_key, request_fingerprint=fingerprint,
    )
    session.add(row)
    session.flush()
    return row


def _audit(session: Session, command: str, case_id: uuid.UUID, target_id: uuid.UUID) -> None:
    audit.record(session, operation=f"claims.{command}", target_type="claims",
                 target_id=target_id, case_id=case_id)


def _ordered_items(items: list[models.ClaimItem],
                   group_id: uuid.UUID) -> list[models.ClaimItem]:
    """Display order inside a group — the same order the waterfall draws."""
    return [item for item in items if item.group_id == group_id]


def _prefix_sums(items: list[models.ClaimItem]) -> list[int]:
    """Running total after each bar of the waterfall (starts at 0)."""
    total = 0
    steps: list[int] = []
    for item in items:
        total += _signed(item)
        steps.append(total)
    return steps


def _assert_totals_fit(session: Session, case_id: uuid.UUID) -> None:
    """Every intermediate waterfall value *and* every subtotal must stay JS-safe.

    Checking only the final subtotal is not enough: the waterfall draws each step,
    so ``MAX_SAFE + 1 - 1`` overflows in the middle while the subtotal is fine.
    A deduction moved to the end of a group is exactly that shape, which is why
    this runs on reorder/accept too and rejects the whole command.
    """
    groups = _all_groups(session, case_id)
    items = _all_items(session, case_id)
    active_items = [item for item in items if item.removed_at is None]
    subtotals, totals = _compute_totals(groups, items)
    for group in groups:
        if group.removed_at is not None:
            continue
        for step in _prefix_sums(_ordered_items(active_items, group.id)):
            if abs(step) > CLAIM_MAX_CENTS:
                raise _err(ErrorCode.INVALID_INPUT,
                           "组内累加超出可精确表示的范围（约 90 万亿元）；"
                           "瀑布图中间步骤会溢出，本次修改未保存")
    for value in list(subtotals.values()) + [entry.total_cents for entry in totals]:
        if abs(value) > CLAIM_MAX_CENTS:
            raise _err(ErrorCode.INVALID_INPUT,
                       "合计超出可精确表示的范围（约 90 万亿元）；本次修改未保存")


def _finish(session: Session, case_id: uuid.UUID,
            change_id: uuid.UUID | None = None) -> ClaimSnapshot:
    """Check, commit, re-read.  Any failure discards the whole command."""
    try:
        _assert_totals_fit(session, case_id)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    snapshot = get_claims(session, case_id)
    if change_id is not None:
        snapshot.last_change_id = change_id
    return snapshot


def _replay_change(session: Session, case_id: uuid.UUID,
                   request_key: str) -> models.ClaimChange | None:
    return session.execute(
        select(models.ClaimChange).where(
            models.ClaimChange.case_id == case_id,
            models.ClaimChange.request_key == request_key)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _request_fingerprint(command: str, actor: str, payload: dict[str, Any]) -> str:
    """Stable digest of what the caller asked for, minus the replay key itself.

    Retrying the *same* submission must return the original change id; reusing a
    key for a different command, actor or payload must be an error rather than a
    silent "already done".  Canonical JSON (sorted keys, stringified UUIDs) keeps
    the digest independent of field order and of ORM/dict representation.
    """
    canonical = json.dumps(
        {"command": command, "actor": actor,
         "payload": {key: (str(value) if isinstance(value, uuid.UUID) else value)
                     for key, value in payload.items() if key != "request_key"}},
        sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _replay_or_conflict(session: Session, case_id: uuid.UUID, request_key: str,
                        command: str, actor: str, payload: dict[str, Any]) -> models.ClaimChange | None:
    """Return the original change for an identical replay; conflict otherwise."""
    replay = _replay_change(session, case_id, request_key)
    if replay is None:
        return None
    expected = _request_fingerprint(command, actor, payload)
    stored = replay.request_fingerprint
    if replay.command != command or replay.actor != actor or stored != expected:
        raise _err(ErrorCode.CONFLICT,
                   "该 request_key 已用于另一次提交（命令、来源或内容不同）",
                   request_key=request_key, change_id=str(replay.id),
                   stored_command=replay.command, stored_actor=replay.actor)
    return replay


def _require_agent_key(actor: str, request_key: str | None) -> None:
    if actor == "agent" and not request_key:
        raise _err(ErrorCode.INVALID_INPUT, "Agent 提交必须带 request_key（幂等键）")


# --- group commands ---------------------------------------------------------

def _stage_or_add_group(session: Session, payload: AddClaimGroupInput, *, actor: str,
                        entry: str) -> ClaimSnapshot:
    _begin(session, payload.case_id, actor, "add_group")
    _require_agent_key(actor, payload.request_key)
    fingerprint = None
    if payload.request_key:
        request_payload = payload.model_dump(mode="json", exclude={"case_id"})
        replay = _replay_or_conflict(session, payload.case_id, payload.request_key,
                                     "add_group", actor, request_payload)
        if replay is not None:
            snapshot = get_claims(session, payload.case_id)
            snapshot.last_change_id = replay.id
            return snapshot
        fingerprint = _request_fingerprint("add_group", actor, request_payload)
        if actor == "human":
            existing = session.execute(
                select(models.ClaimGroup).where(
                    models.ClaimGroup.case_id == payload.case_id,
                    models.ClaimGroup.request_key == payload.request_key)
            ).scalar_one_or_none()
            if existing is not None:
                return get_claims(session, payload.case_id)
    timestamp = _now()
    row = models.ClaimGroup(
        id=uuid.uuid4(), case_id=payload.case_id,
        title=_text(payload.title, "分组名称"),
        currency=_currency(payload.currency),
        include_in_total=bool(payload.include_in_total),
        position=_next_position(_active_groups(session, payload.case_id)),
        note=_optional_text(payload.note, "备注"),
        created_by=actor, updated_by=actor, request_key=payload.request_key,
        removed_at=None, revision=0, created_at=timestamp, updated_at=timestamp,
    )
    if actor == "human":
        session.add(row)
        session.flush()
    change = _record_change(session, payload.case_id, "add_group", actor, entry,
                            [_group_diff(None, row)], request_key=payload.request_key,
                            fingerprint=fingerprint)
    _audit(session, "add_group", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def add_group(session: Session, payload: AddClaimGroupInput, *, actor: str,
              entry: str) -> ClaimSnapshot:
    return _stage_or_add_group(session, payload, actor=actor, entry=entry)


def _group_after_dict(row: models.ClaimGroup, changes: ClaimGroupChanges) -> dict[str, Any]:
    provided = changes.model_fields_set
    snapshot = _group_dict(row)
    if "title" in provided:
        if changes.title is None:
            raise _err(ErrorCode.INVALID_INPUT, "分组名称不能清空")
        snapshot["title"] = _text(changes.title, "分组名称")
    if "currency" in provided:
        if changes.currency is None:
            raise _err(ErrorCode.INVALID_INPUT, "币种不能清空")
        snapshot["currency"] = _currency(changes.currency)
    if "include_in_total" in provided:
        if changes.include_in_total is None:
            raise _err(ErrorCode.INVALID_INPUT, "“计入全案总计”不能清空")
        snapshot["include_in_total"] = bool(changes.include_in_total)
    if "note" in provided:
        snapshot["note"] = _optional_text(changes.note, "备注")
    return snapshot


def _apply_group_snapshot(row: models.ClaimGroup, snapshot: dict[str, Any]) -> None:
    row.title = snapshot["title"]
    row.currency = snapshot["currency"]
    row.include_in_total = bool(snapshot["include_in_total"])
    row.note = snapshot["note"]


def _sourced_currency(session: Session, case_id: uuid.UUID,
                      item_ids: list[uuid.UUID]) -> set[str]:
    """Currencies that a group's *existing* sources state explicitly.

    A claim item that cites a segment inherits that number's currency from the
    exact value that produced it.  Relabelling the group would silently
    re-denominate the amount (100 CNY → 100 USD) while the provenance still
    points at "人民币…", so such a group may not change currency.
    """
    if not item_ids:
        return set()
    rows = session.execute(
        select(models.ExactValue.currency)
        .join(models.Segment, models.Segment.id == models.ExactValue.segment_id)
        .join(models.ClaimItem, models.ClaimItem.segment_id == models.Segment.id)
        .where(models.ClaimItem.case_id == case_id,
               models.ClaimItem.id.in_(item_ids),
               models.ExactValue.kind == "amount",
               models.ExactValue.currency.is_not(None))
    ).scalars().all()
    return {row.upper() for row in rows}


def update_group(session: Session, payload: UpdateClaimGroupInput, *, actor: str,
                 entry: str) -> ClaimSnapshot:
    _begin(session, payload.case_id, actor, "update_group")
    _require_agent_key(actor, payload.request_key)
    fingerprint = None
    if payload.request_key:
        request_payload = payload.model_dump(mode="json", exclude={"case_id"})
        replay = _replay_or_conflict(session, payload.case_id, payload.request_key,
                                     "update_group", actor, request_payload)
        if replay is not None:
            snapshot = get_claims(session, payload.case_id)
            snapshot.last_change_id = replay.id
            return snapshot
        fingerprint = _request_fingerprint("update_group", actor, request_payload)
    row = _group_or_error(session, payload.case_id, payload.group_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "分组已删除")
    _guard_no_pending(_pending_targets(session, payload.case_id), "group", row.id)
    _cas(row.revision, payload.expected_revision, target="group", object_id=row.id)
    before = _group_dict(row)
    before_rev = row.revision
    after = _group_after_dict(row, payload.changes)
    if after["currency"] != before["currency"]:
        active_items = _active_items(session, payload.case_id, row.id)
        explicit = _sourced_currency(session, payload.case_id,
                                     [item.id for item in active_items])
        if explicit and after["currency"] not in explicit:
            raise _err(ErrorCode.INVALID_INPUT,
                       "该分组已有带明确币种的出处，不能直接改币种；"
                       "请先移除或改写这些金额项的出处",
                       currency=after["currency"], sourced_currencies=sorted(explicit))
    timestamp = _now()
    if actor == "human":
        _apply_group_snapshot(row, after)
        row.revision += 1
        row.updated_by = actor
        row.updated_at = timestamp
        session.flush()
        after = _group_dict(row)
        after_rev = row.revision
        diff = [{"target": "group", "id": str(row.id), "before": before, "after": after,
                 "before_rev": before_rev, "after_rev": after_rev}]
    else:
        # Staged: the business row is untouched, so the diff carries the proposal.
        after = dict(after, revision=before_rev + 1, updated_by=actor,
                     updated_at=timestamp.isoformat())
        diff = [{"target": "group", "id": str(row.id), "before": before, "after": after,
                 "before_rev": before_rev, "after_rev": before_rev + 1}]
    change = _record_change(session, payload.case_id, "update_group", actor, entry, diff,
                            request_key=payload.request_key, fingerprint=fingerprint)
    _audit(session, "update_group", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def reorder_groups(session: Session, payload: ReorderClaimGroupInput, *, actor: str,
                   entry: str) -> ClaimSnapshot:
    _begin(session, payload.case_id, actor, "reorder_groups")
    groups = _active_groups(session, payload.case_id)
    index = next((position for position, row in enumerate(groups) if row.id == payload.group_id), -1)
    if index < 0:
        raise _err(ErrorCode.NOT_FOUND, "诉请分组不存在", group_id=str(payload.group_id))
    row = groups[index]
    _cas(row.revision, payload.expected_revision, target="group", object_id=row.id)
    target = index - 1 if payload.direction == "up" else index + 1
    if target < 0 or target >= len(groups):
        raise _err(ErrorCode.INVALID_INPUT,
                   "已经是第一个分组" if payload.direction == "up" else "已经是最后一个分组")
    other = groups[target]
    before_row, before_other = _group_dict(row), _group_dict(other)
    timestamp = _now()
    row.position, other.position = other.position, row.position
    for affected in (row, other):
        affected.revision += 1
        affected.updated_by = actor
        affected.updated_at = timestamp
    session.flush()
    diff = [
        {"target": "group", "id": str(row.id), "before": before_row, "after": _group_dict(row),
         "before_rev": before_row["revision"], "after_rev": row.revision},
        {"target": "group", "id": str(other.id), "before": before_other,
         "after": _group_dict(other), "before_rev": before_other["revision"],
         "after_rev": other.revision},
    ]
    change = _record_change(session, payload.case_id, "reorder_groups", actor, entry, diff)
    _audit(session, "reorder_groups", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def remove_group(session: Session, payload: ClaimGroupRevisionInput, *, actor: str,
                 entry: str) -> ClaimSnapshot:
    _begin(session, payload.case_id, actor, "remove_group")
    row = _group_or_error(session, payload.case_id, payload.group_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "分组已删除")
    targets = _pending_targets(session, payload.case_id)
    _guard_no_pending(targets, "group", row.id)
    _guard_no_pending(targets, "group_ref", row.id)
    items = _active_items(session, payload.case_id, row.id)
    for item in items:
        _guard_no_pending(targets, "item", item.id)
    _cas(row.revision, payload.expected_revision, target="group", object_id=row.id)
    timestamp = _now()
    before = _group_dict(row)
    before_rev = row.revision
    row.removed_at = timestamp
    row.revision += 1
    row.updated_by = actor
    row.updated_at = timestamp
    diff = [{"target": "group", "id": str(row.id), "before": before, "after": _group_dict(row),
             "before_rev": before_rev, "after_rev": row.revision}]
    # Cascade soft delete so no active item is left pointing at a deleted group.
    for item in items:
        item_before = _item_dict(item)
        item_before_rev = item.revision
        item.removed_at = timestamp
        item.revision += 1
        item.updated_by = actor
        item.updated_at = timestamp
        diff.append({"target": "item", "id": str(item.id), "before": item_before,
                     "after": _item_dict(item), "before_rev": item_before_rev,
                     "after_rev": item.revision})
    session.flush()
    change = _record_change(session, payload.case_id, "remove_group", actor, entry, diff)
    _audit(session, "remove_group", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


# --- item commands ----------------------------------------------------------

def _stage_or_add_item(session: Session, payload: AddClaimItemInput, *, actor: str,
                       entry: str) -> ClaimSnapshot:
    _begin(session, payload.case_id, actor, "add_item")
    _require_agent_key(actor, payload.request_key)
    fingerprint = None
    if payload.request_key:
        request_payload = payload.model_dump(mode="json", exclude={"case_id"})
        replay = _replay_or_conflict(session, payload.case_id, payload.request_key,
                                     "add_item", actor, request_payload)
        if replay is not None:
            snapshot = get_claims(session, payload.case_id)
            snapshot.last_change_id = replay.id
            return snapshot
        fingerprint = _request_fingerprint("add_item", actor, request_payload)
        if actor == "human":
            existing = session.execute(
                select(models.ClaimItem).where(
                    models.ClaimItem.case_id == payload.case_id,
                    models.ClaimItem.request_key == payload.request_key)
            ).scalar_one_or_none()
            if existing is not None:
                return get_claims(session, payload.case_id)
    group = _group_or_error(session, payload.case_id, payload.group_id)
    if group.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "分组已删除")
    _validate_source(session, payload.case_id, payload.segment_id, payload.material_id,
                     payload.annotation_id)
    timestamp = _now()
    row = models.ClaimItem(
        id=uuid.uuid4(), case_id=payload.case_id, group_id=group.id,
        title=_text(payload.title, "金额项名称"), category=payload.category,
        amount_cents=_amount(payload.amount_cents),
        calc_note=_optional_text(payload.calc_note, "计算说明"),
        segment_id=payload.segment_id, material_id=payload.material_id,
        annotation_id=payload.annotation_id,
        position=_next_position(_active_items(session, payload.case_id, group.id)),
        created_by=actor, updated_by=actor, request_key=payload.request_key,
        removed_at=None, revision=0, created_at=timestamp, updated_at=timestamp,
    )
    if actor == "human":
        session.add(row)
        session.flush()
    change = _record_change(session, payload.case_id, "add_item", actor, entry,
                            [_item_diff(None, row)], request_key=payload.request_key,
                            fingerprint=fingerprint)
    _audit(session, "add_item", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def add_item(session: Session, payload: AddClaimItemInput, *, actor: str,
             entry: str) -> ClaimSnapshot:
    return _stage_or_add_item(session, payload, actor=actor, entry=entry)


def _item_after_dict(session: Session, row: models.ClaimItem,
                     changes: ClaimItemChanges) -> dict[str, Any]:
    provided = changes.model_fields_set
    snapshot = _item_dict(row)
    if "title" in provided:
        if changes.title is None:
            raise _err(ErrorCode.INVALID_INPUT, "金额项名称不能清空")
        snapshot["title"] = _text(changes.title, "金额项名称")
    if "category" in provided:
        if changes.category is None:
            raise _err(ErrorCode.INVALID_INPUT, "金额类别不能清空")
        snapshot["category"] = changes.category
    if "amount_cents" in provided:
        if changes.amount_cents is None:
            raise _err(ErrorCode.INVALID_INPUT, "金额不能清空")
        snapshot["amount_cents"] = _amount(int(changes.amount_cents))
    if "calc_note" in provided:
        snapshot["calc_note"] = _optional_text(changes.calc_note, "计算说明")
    if {"segment_id", "material_id", "annotation_id"} & provided:
        _validate_source(session, row.case_id, changes.segment_id, changes.material_id,
                         changes.annotation_id)
        snapshot["segment_id"] = str(changes.segment_id) if changes.segment_id else None
        snapshot["material_id"] = str(changes.material_id) if changes.material_id else None
        snapshot["annotation_id"] = str(changes.annotation_id) if changes.annotation_id else None
    return snapshot


def _apply_item_snapshot(row: models.ClaimItem, snapshot: dict[str, Any]) -> None:
    row.title = snapshot["title"]
    row.category = snapshot["category"]
    row.amount_cents = int(snapshot["amount_cents"])
    row.calc_note = snapshot["calc_note"]
    row.segment_id = uuid.UUID(snapshot["segment_id"]) if snapshot["segment_id"] else None
    row.material_id = uuid.UUID(snapshot["material_id"]) if snapshot["material_id"] else None
    row.annotation_id = (uuid.UUID(snapshot["annotation_id"])
                         if snapshot["annotation_id"] else None)


def update_item(session: Session, payload: UpdateClaimItemInput, *, actor: str,
                entry: str) -> ClaimSnapshot:
    _begin(session, payload.case_id, actor, "update_item")
    _require_agent_key(actor, payload.request_key)
    fingerprint = None
    if payload.request_key:
        request_payload = payload.model_dump(mode="json", exclude={"case_id"})
        replay = _replay_or_conflict(session, payload.case_id, payload.request_key,
                                     "update_item", actor, request_payload)
        if replay is not None:
            snapshot = get_claims(session, payload.case_id)
            snapshot.last_change_id = replay.id
            return snapshot
        fingerprint = _request_fingerprint("update_item", actor, request_payload)
    row = _item_or_error(session, payload.case_id, payload.item_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "金额项已删除")
    _guard_no_pending(_pending_targets(session, payload.case_id), "item", row.id)
    group = _group_or_error(session, payload.case_id, row.group_id)
    if group.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "分组已删除")
    _cas(row.revision, payload.expected_revision, target="item", object_id=row.id)
    before = _item_dict(row)
    before_rev = row.revision
    after = _item_after_dict(session, row, payload.changes)
    timestamp = _now()
    if actor == "human":
        _apply_item_snapshot(row, after)
        row.revision += 1
        row.updated_by = actor
        row.updated_at = timestamp
        session.flush()
        after = _item_dict(row)
        diff = [{"target": "item", "id": str(row.id), "before": before, "after": after,
                 "before_rev": before_rev, "after_rev": row.revision}]
    else:
        after = dict(after, revision=before_rev + 1, updated_by=actor,
                     updated_at=timestamp.isoformat())
        diff = [{"target": "item", "id": str(row.id), "before": before, "after": after,
                 "before_rev": before_rev, "after_rev": before_rev + 1}]
    change = _record_change(session, payload.case_id, "update_item", actor, entry, diff,
                            request_key=payload.request_key, fingerprint=fingerprint)
    _audit(session, "update_item", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def reorder_items(session: Session, payload: ReorderClaimItemInput, *, actor: str,
                  entry: str) -> ClaimSnapshot:
    _begin(session, payload.case_id, actor, "reorder_items")
    row = _item_or_error(session, payload.case_id, payload.item_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "金额项已删除")
    items = _active_items(session, payload.case_id, row.group_id)
    index = next((position for position, entry_row in enumerate(items)
                  if entry_row.id == row.id), -1)
    _cas(row.revision, payload.expected_revision, target="item", object_id=row.id)
    target = index - 1 if payload.direction == "up" else index + 1
    if index < 0 or target < 0 or target >= len(items):
        raise _err(ErrorCode.INVALID_INPUT,
                   "已经是第一项" if payload.direction == "up" else "已经是最后一项")
    other = items[target]
    before_row, before_other = _item_dict(row), _item_dict(other)
    timestamp = _now()
    row.position, other.position = other.position, row.position
    for affected in (row, other):
        affected.revision += 1
        affected.updated_by = actor
        affected.updated_at = timestamp
    session.flush()
    diff = [
        {"target": "item", "id": str(row.id), "before": before_row, "after": _item_dict(row),
         "before_rev": before_row["revision"], "after_rev": row.revision},
        {"target": "item", "id": str(other.id), "before": before_other,
         "after": _item_dict(other), "before_rev": before_other["revision"],
         "after_rev": other.revision},
    ]
    change = _record_change(session, payload.case_id, "reorder_items", actor, entry, diff)
    _audit(session, "reorder_items", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def remove_item(session: Session, payload: ClaimItemRevisionInput, *, actor: str,
                entry: str) -> ClaimSnapshot:
    _begin(session, payload.case_id, actor, "remove_item")
    row = _item_or_error(session, payload.case_id, payload.item_id)
    if row.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "金额项已删除")
    _guard_no_pending(_pending_targets(session, payload.case_id), "item", row.id)
    _cas(row.revision, payload.expected_revision, target="item", object_id=row.id)
    before = _item_dict(row)
    before_rev = row.revision
    timestamp = _now()
    row.removed_at = timestamp
    row.revision += 1
    row.updated_by = actor
    row.updated_at = timestamp
    session.flush()
    change = _record_change(session, payload.case_id, "remove_item", actor, entry,
                            [{"target": "item", "id": str(row.id), "before": before,
                              "after": _item_dict(row), "before_rev": before_rev,
                              "after_rev": row.revision}])
    _audit(session, "remove_item", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


def restore_item(session: Session, payload: ClaimItemRevisionInput, *, actor: str,
                 entry: str) -> ClaimSnapshot:
    _begin(session, payload.case_id, actor, "restore_item")
    row = _item_or_error(session, payload.case_id, payload.item_id)
    if row.removed_at is None:
        raise _err(ErrorCode.INVALID_INPUT, "金额项未删除")
    group = _group_or_error(session, payload.case_id, row.group_id)
    if group.removed_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "分组已删除，请先恢复分组")
    _cas(row.revision, payload.expected_revision, target="item", object_id=row.id)
    before = _item_dict(row)
    before_rev = row.revision
    timestamp = _now()
    row.removed_at = None
    row.position = _next_position(_active_items(session, payload.case_id, row.group_id))
    row.revision += 1
    row.updated_by = actor
    row.updated_at = timestamp
    session.flush()
    change = _record_change(session, payload.case_id, "restore_item", actor, entry,
                            [{"target": "item", "id": str(row.id), "before": before,
                              "after": _item_dict(row), "before_rev": before_rev,
                              "after_rev": row.revision}])
    _audit(session, "restore_item", payload.case_id, row.id)
    return _finish(session, payload.case_id, change.id)


# --- human review -----------------------------------------------------------

def _change_or_error(session: Session, case_id: uuid.UUID,
                     change_id: uuid.UUID) -> models.ClaimChange:
    change = _fresh(session, models.ClaimChange, change_id)
    if change is None:
        raise _err(ErrorCode.NOT_FOUND, "变更记录不存在", change_id=str(change_id))
    if change.case_id != case_id:
        raise _err(ErrorCode.CASE_MISMATCH, "变更记录不属于本案", change_id=str(change_id))
    return change


def _apply_pending_change(session: Session, change: models.ClaimChange) -> dict[str, Any]:
    """Write a staged agent proposal into the business tables.

    CAS against the revision recorded when the proposal was staged, so a human
    edit in between is a conflict, not a silent overwrite.  Any failure rolls
    back the whole accept (no half-applied group/item).

    Every accept re-validates what the staging step validated: the source must
    still exist, still belong to this case and still be readable.  A run that
    went back to ``running`` (or a group deleted out of band) therefore fails
    here instead of writing a reference that the next parse retry would destroy.

    Returns the *actual* before/after record so the caller can store a
    reconstructable human change instead of re-labelling the agent's proposal.
    """
    if len(change.diff) != 1:
        raise _err(ErrorCode.CONFLICT, "变更内容不完整，无法确认", change_id=str(change.id))
    entry = change.diff[0]
    snapshot = entry["after"]
    timestamp = _now()
    if entry["target"] == "group":
        if change.command == "add_group":
            object_id = uuid.UUID(entry["id"])
            if _fresh(session, models.ClaimGroup, object_id) is not None:
                raise _err(ErrorCode.CONFLICT, "该分组已存在，无法重复确认",
                           group_id=str(object_id))
            row = models.ClaimGroup(
                id=object_id, case_id=change.case_id, title=snapshot["title"],
                currency=_currency(snapshot["currency"]),
                include_in_total=bool(snapshot["include_in_total"]),
                position=_next_position(_active_groups(session, change.case_id)),
                note=snapshot["note"], created_by="agent", updated_by="human",
                request_key=change.request_key, removed_at=None,
                revision=int(snapshot["revision"]), created_at=timestamp,
                updated_at=timestamp,
            )
            session.add(row)
            session.flush()
            return _group_diff(None, row)
        row = _group_or_error(session, change.case_id, uuid.UUID(entry["id"]))
        if row.removed_at is not None:
            raise _err(ErrorCode.CONFLICT, "分组已删除，无法确认", group_id=str(row.id))
        _cas(row.revision, int(entry["before_rev"]), target="group", object_id=row.id)
        if _currency(snapshot["currency"]) != row.currency:
            explicit = _sourced_currency(
                session, change.case_id,
                [item.id for item in _active_items(session, change.case_id, row.id)])
            if explicit and _currency(snapshot["currency"]) not in explicit:
                raise _err(ErrorCode.CONFLICT,
                           "分组已有带明确币种的出处，确认会改变这些金额的含义",
                           group_id=str(row.id), sourced_currencies=sorted(explicit))
        before = _group_dict(row)
        before_rev = row.revision
        _apply_group_snapshot(row, snapshot)
        row.revision += 1
        row.updated_by = "human"
        row.updated_at = timestamp
        session.flush()
        return {"target": "group", "id": str(row.id), "before": before,
                "after": _group_dict(row), "before_rev": before_rev,
                "after_rev": row.revision}
    if change.command == "add_item":
        object_id = uuid.UUID(entry["id"])
        if _fresh(session, models.ClaimItem, object_id) is not None:
            raise _err(ErrorCode.CONFLICT, "该金额项已存在，无法重复确认",
                       item_id=str(object_id))
        group = _group_or_error(session, change.case_id, uuid.UUID(snapshot["group_id"]))
        if group.removed_at is not None:
            raise _err(ErrorCode.CONFLICT, "分组已删除，无法确认", group_id=str(group.id))
        segment_id = uuid.UUID(snapshot["segment_id"]) if snapshot["segment_id"] else None
        material_id = uuid.UUID(snapshot["material_id"]) if snapshot["material_id"] else None
        annotation_id = (uuid.UUID(snapshot["annotation_id"])
                         if snapshot["annotation_id"] else None)
        _validate_source(session, change.case_id, segment_id, material_id, annotation_id)
        row = models.ClaimItem(
            id=object_id, case_id=change.case_id, group_id=group.id,
            title=snapshot["title"], category=snapshot["category"],
            amount_cents=_amount(int(snapshot["amount_cents"])),
            calc_note=snapshot["calc_note"],
            segment_id=segment_id, material_id=material_id, annotation_id=annotation_id,
            position=_next_position(_active_items(session, change.case_id, group.id)),
            created_by="agent", updated_by="human", request_key=change.request_key,
            removed_at=None, revision=int(snapshot["revision"]), created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(row)
        session.flush()
        return _item_diff(None, row)
    row = _item_or_error(session, change.case_id, uuid.UUID(entry["id"]))
    if row.removed_at is not None:
        raise _err(ErrorCode.CONFLICT, "金额项已删除，无法确认", item_id=str(row.id))
    group = _group_or_error(session, change.case_id, row.group_id)
    if group.removed_at is not None:
        raise _err(ErrorCode.CONFLICT, "分组已删除，无法确认", group_id=str(group.id))
    _cas(row.revision, int(entry["before_rev"]), target="item", object_id=row.id)
    _validate_source(
        session, change.case_id,
        uuid.UUID(snapshot["segment_id"]) if snapshot["segment_id"] else None,
        uuid.UUID(snapshot["material_id"]) if snapshot["material_id"] else None,
        uuid.UUID(snapshot["annotation_id"]) if snapshot["annotation_id"] else None)
    before = _item_dict(row)
    before_rev = row.revision
    _apply_item_snapshot(row, snapshot)
    row.revision += 1
    row.updated_by = "human"
    row.updated_at = timestamp
    session.flush()
    return {"target": "item", "id": str(row.id), "before": before,
            "after": _item_dict(row), "before_rev": before_rev,
            "after_rev": row.revision}


def accept_change(session: Session, payload: ChangeIdInput, *, actor: str,
                  entry: str) -> ClaimSnapshot:
    """Confirm one staged candidate.

    The confirmation itself is a human write, so it produces its own
    ``claim_changes`` row carrying the *actual* before/after values plus the
    same-transaction audit.  The agent's proposal row is left untouched (only its
    review state moves), so the review history can always be rebuilt.
    """
    if actor != "human":
        raise _err(ErrorCode.FORBIDDEN, "只有承办人可以确认 Agent 候选")
    _begin_manual(session, payload.case_id)
    change = _change_or_error(session, payload.case_id, payload.change_id)
    if change.review_state != "pending":
        raise _err(ErrorCode.INVALID_INPUT, "该变更不处于待确认状态")
    try:
        diff = _apply_pending_change(session, change)
        change.review_state = "accepted"
        change.reviewed_at = _now()
        record = _record_change(session, payload.case_id, "accept_change", actor, entry,
                                [diff])
        _audit(session, "accept_change", payload.case_id, change.id)
        return _finish(session, payload.case_id, record.id)
    except BaseException:
        session.rollback()
        raise


def accept_all(session: Session, payload: ClaimAcceptAllInput, *, actor: str,
               entry: str) -> ClaimSnapshot:
    """Confirm the exact set the client displayed — nothing less, nothing more."""
    if actor != "human":
        raise _err(ErrorCode.FORBIDDEN, "只有承办人可以确认 Agent 候选")
    case_id = payload.case_id
    _begin_manual(session, case_id)
    seq = _lock_seq(session, case_id)
    # Same rule as progress: only confirm what the client actually displayed.
    ids_provided = "change_ids" in payload.model_fields_set
    if ids_provided and not payload.change_ids:
        raise _err(ErrorCode.INVALID_INPUT, "确认范围不能为空：请提交已展示的变更 ID")
    if not ids_provided and payload.expected_seq is None:
        raise _err(ErrorCode.INVALID_INPUT, "一键确认须提交已展示的变更 ID 或操作序号")
    if payload.expected_seq is not None and payload.expected_seq != seq:
        raise _err(ErrorCode.CONFLICT, "待确认列表已变化，请刷新后重新审核",
                   expected_seq=payload.expected_seq, actual_seq=seq)
    # Read *all* pending changes of this case, then verify the submitted set
    # against it.  Filtering the query by id first would hide omissions, which is
    # how a partial batch used to look complete.
    pending = session.execute(
        select(models.ClaimChange).where(
            models.ClaimChange.case_id == case_id,
            models.ClaimChange.review_state == "pending")
        .order_by(models.ClaimChange.seq)
    ).scalars().all()
    if not pending:
        raise _err(ErrorCode.INVALID_INPUT, "没有待确认的 Agent 候选")
    if ids_provided:
        # The submitted set must *equal* the pending set: every id resolves to a
        # pending change of this case, and no pending change was left out.
        # A stale/foreign/omitted id used to leave the displayed set half applied
        # while the client believed the whole batch went through.
        found = {change.id for change in pending}
        submitted = list(payload.change_ids)
        submitted_set = set(submitted)
        missing = [str(change_id) for change_id in submitted if change_id not in found]
        omitted = [str(change_id) for change_id in found if change_id not in submitted_set]
        if missing or omitted:
            raise _err(ErrorCode.CONFLICT,
                       "确认范围与当前待确认列表不一致，请刷新后重新审核",
                       unknown_change_ids=missing, omitted_change_ids=omitted,
                       submitted_count=len(submitted), pending_count=len(found))
        if len(submitted) != len(found):
            raise _err(ErrorCode.CONFLICT, "确认范围包含重复的变更 ID",
                       submitted_count=len(submitted), distinct_count=len(found))
        pending = [change for change in pending if change.id in submitted_set]
    try:
        records: list[models.ClaimChange] = []
        for change in pending:
            diff = _apply_pending_change(session, change)
            change.review_state = "accepted"
            change.reviewed_at = _now()
            # One human change row per confirmed object, holding the actual
            # before/after values, so the batch is reconstructable afterwards.
            records.append(_record_change(session, case_id, "accept_all", actor, entry,
                                          [diff]))
            _audit(session, "accept_all", case_id, change.id)
        snapshot = _finish(session, case_id, records[-1].id if records else None)
        return snapshot
    except BaseException:
        session.rollback()
        raise


def revert_change(session: Session, payload: ChangeIdInput, *, actor: str,
                  entry: str) -> ClaimSnapshot:
    """Discard a pending candidate.  The business rows were never written."""
    if actor != "human":
        raise _err(ErrorCode.FORBIDDEN, "只有承办人可以撤回 Agent 候选")
    _begin_manual(session, payload.case_id)
    change = _change_or_error(session, payload.case_id, payload.change_id)
    if change.review_state != "pending":
        raise _err(ErrorCode.INVALID_INPUT, "该变更不处于待确认状态，无需撤回")
    timestamp = _now()
    change.review_state = "reverted"
    change.reviewed_at = timestamp
    record = _record_change(session, payload.case_id, "revert_change", actor, entry, [])
    change.reverted_by = record.id
    _audit(session, "revert_change", payload.case_id, change.id)
    return _finish(session, payload.case_id, record.id)
