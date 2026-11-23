"""Billable-hour service: persisted timer and manual entries (0.25h granularity).

Mirrors the P1 prototype semantics: the timer rounds elapsed time up to the next
quarter hour, manual entries must be a multiple of 0.25h, delete is soft, and a
case has at most one running timer. Archived cases are read-only (archive itself
requires no running timer, so a timer cannot be left open).
"""
from __future__ import annotations

import math
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import audit, cases, models
from .errors import ErrorCode, WorkbenchError
from .schemas import (AddTimeEntryInput, RemoveTimeEntryInput, StartTimerInput,
                      StopTimerInput, TimeDayOutput, TimeEntryOut, TimeSummaryRow)

QUARTER_HOURS = Decimal("0.25")
QUARTER_SECONDS = 900


def _err(code: ErrorCode, message: str) -> WorkbenchError:
    return WorkbenchError(code, message)


def _local_zone():
    """与 :func:`_local_today` 同源的本地时区（本机单用户口径）。"""
    return datetime.now().astimezone().tzinfo or timezone.utc


def _local_today() -> date:
    return datetime.now().astimezone().date()


def _split_timer_segments(started_at: datetime,
                          stopped_at: datetime) -> list[tuple[date, datetime, datetime]]:
    """按本地日历日把一段计时切成若干段，并保证单段不超过 24 小时。

    边界取「本地次日零点」，因此 ``work_date`` 是这段实际发生的本地日期；
    单段再以 24 小时封顶（夏令时回拨的 25 小时日、或时钟异常都不会撞
    ``hours <= 24`` 约束）。返回 ``(work_date, 段起点, 段终点)``，段首尾相接。
    """
    tz = _local_zone()
    segments: list[tuple[date, datetime, datetime]] = []
    cursor = started_at
    while cursor < stopped_at:
        local = cursor.astimezone(tz)
        next_midnight = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=tz)
        end = min(stopped_at, next_midnight, cursor + timedelta(hours=24))
        if end <= cursor:  # 防御：时区或时钟异常时不制造空段/死循环
            break
        segments.append((local.date(), cursor, end))
        cursor = end
    if not segments:
        segments.append((started_at.astimezone(tz).date(), started_at, stopped_at))
    return segments


def _quarter_allocations(started_at: datetime, stopped_at: datetime,
                         segments: list[tuple[date, datetime, datetime]]) -> list[tuple[date, datetime, datetime, int]]:
    """把一次计时的计价刻钟数按本地日期边界分配。

    总时长先截到整秒（时钟精度就是秒，亚秒抖动不应多算一个刻钟），再整体
    向上取整到 0.25h；各段按「截至段终点的累计刻钟数」取差，因此各行之和
    恰好等于总时长取整后的刻钟数——跨日拆行不会重复或遗漏工时。某段落在
    同一刻钟内时差值为 0，并入上一段（这段时间不足一个计价单位）。
    """
    elapsed_seconds = max(0, int((stopped_at - started_at).total_seconds()))
    total = max(1, math.ceil(elapsed_seconds / QUARTER_SECONDS))
    allocations: list[int] = []
    previous = 0
    for _, _, end in segments:
        cumulative = min(total, math.ceil(
            max(0, int((end - started_at).total_seconds())) / QUARTER_SECONDS))
        allocations.append(max(0, cumulative - previous))
        previous = max(previous, cumulative)
    allocations[-1] += total - sum(allocations)
    merged: list[tuple[date, datetime, datetime, int]] = []
    for (work_date, start, end), quarters in zip(segments, allocations):
        if quarters == 0 and merged:
            last_date, last_start, _, last_quarters = merged[-1]
            merged[-1] = (last_date, last_start, end, last_quarters)
            continue
        merged.append((work_date, start, end, quarters))
    return merged


def _entry_out(row: models.TimeEntry) -> TimeEntryOut:
    return TimeEntryOut(
        id=row.id, case_id=row.case_id, node_id=row.node_id, node_title=row.node_title,
        work_date=row.work_date, hours=float(row.hours), source=row.source,
        started_at=row.started_at, stopped_at=row.stopped_at, note=row.note,
        created_at=row.created_at)


def running_entry(session: Session, case_id: uuid.UUID) -> models.TimeEntry | None:
    """进行中的计时（只认 timer；补录条目没有 stopped_at，不能被当成计时）。"""
    return session.execute(select(models.TimeEntry).where(
        models.TimeEntry.case_id == case_id,
        models.TimeEntry.source == "timer",
        models.TimeEntry.stopped_at.is_(None),
        models.TimeEntry.removed_at.is_(None))).scalar_one_or_none()


def start_timer(session: Session, payload: StartTimerInput) -> TimeEntryOut:
    # 维护门禁 → 案件行锁（刷新归档状态）→ 归档复核；同案「先查后写」不再可能。
    cases.lock_case_writable(session, payload.case_id)
    if running_entry(session, payload.case_id) is not None:
        raise _err(ErrorCode.INVALID_INPUT, "已有进行中的计时，请先停止")
    now = datetime.now(timezone.utc)
    row = models.TimeEntry(
        id=uuid.uuid4(), case_id=payload.case_id, node_id=payload.node_id,
        node_title=payload.node_title.strip(), work_date=_local_today(),
        hours=Decimal("0"), source="timer", started_at=now, stopped_at=None,
        note=None, created_by=audit.current_actor())
    session.add(row)
    session.flush()
    audit.record(session, operation="time.start", target_type="time_entry",
                 target_id=row.id, case_id=payload.case_id)
    session.commit()
    session.refresh(row)
    return _entry_out(row)


def stop_timer(session: Session, payload: StopTimerInput) -> TimeEntryOut:
    cases.lock_case_writable(session, payload.case_id)
    row = session.get(models.TimeEntry, payload.entry_id)
    if row is None or row.case_id != payload.case_id or row.removed_at is not None:
        raise _err(ErrorCode.NOT_FOUND, "工时记录不存在")
    if row.source != "timer":
        raise _err(ErrorCode.INVALID_INPUT, "该记录是补录工时，不是计时记录")
    if row.stopped_at is not None:
        raise _err(ErrorCode.INVALID_INPUT, "该计时已停止")
    now = datetime.now(timezone.utc)
    # 跨日拆分：按本地日历日（单段再以 24h 封顶）落成多行，总时长只取整一次，
    # 各行按累计刻钟数分配，行的 started_at/stopped_at 首尾相接。
    segments = _quarter_allocations(row.started_at, now,
                                    _split_timer_segments(row.started_at, now))
    first_date, first_start, first_stop, first_quarters = segments[0]
    row.work_date = first_date
    row.started_at = first_start
    row.stopped_at = first_stop
    row.hours = QUARTER_HOURS * first_quarters
    for work_date, start, stop, quarters in segments[1:]:
        session.add(models.TimeEntry(
            id=uuid.uuid4(), case_id=payload.case_id, node_id=row.node_id,
            node_title=row.node_title, work_date=work_date,
            hours=QUARTER_HOURS * quarters, source="timer",
            started_at=start, stopped_at=stop, note=row.note,
            created_by=row.created_by))
    session.flush()
    audit.record(session, operation="time.stop", target_type="time_entry",
                 target_id=row.id, case_id=payload.case_id)
    session.commit()
    session.refresh(row)
    return _entry_out(row)


def add_manual(session: Session, payload: AddTimeEntryInput) -> TimeEntryOut:
    cases.lock_case_writable(session, payload.case_id)
    hours = Decimal(str(payload.hours))
    if (hours * 4) != (hours * 4).to_integral_value():
        raise _err(ErrorCode.INVALID_INPUT, "时长须为 0.25 小时的整数倍")
    row = models.TimeEntry(
        id=uuid.uuid4(), case_id=payload.case_id, node_id=payload.node_id,
        node_title=payload.node_title.strip(), work_date=payload.work_date,
        hours=hours, source="manual", started_at=None, stopped_at=None,
        note=payload.note, created_by=audit.current_actor())
    session.add(row)
    session.flush()
    audit.record(session, operation="time.add", target_type="time_entry",
                 target_id=row.id, case_id=payload.case_id)
    session.commit()
    session.refresh(row)
    return _entry_out(row)


def remove_entry(session: Session, payload: RemoveTimeEntryInput) -> TimeEntryOut:
    cases.lock_case_writable(session, payload.case_id)
    row = session.get(models.TimeEntry, payload.entry_id)
    if row is None or row.case_id != payload.case_id or row.removed_at is not None:
        raise _err(ErrorCode.NOT_FOUND, "工时记录不存在")
    row.removed_at = datetime.now(timezone.utc)
    session.flush()
    audit.record(session, operation="time.remove", target_type="time_entry",
                 target_id=row.id, case_id=payload.case_id)
    session.commit()
    session.refresh(row)
    return _entry_out(row)


def day(session: Session, case_id: uuid.UUID, work_date: date) -> TimeDayOutput:
    """Day view plus a whole-case summary grouped by work title."""
    rows = session.execute(select(models.TimeEntry).where(
        models.TimeEntry.case_id == case_id,
        models.TimeEntry.work_date == work_date,
        models.TimeEntry.removed_at.is_(None))
        .order_by(models.TimeEntry.created_at, models.TimeEntry.id)).scalars().all()
    total = sum((row.hours for row in rows), Decimal("0"))
    # 汇总只计已完成/补录记录：进行中的计时（hours=0）不计入标题汇总。
    summary_rows = session.execute(
        select(models.TimeEntry.node_title, func.sum(models.TimeEntry.hours),
               func.count(models.TimeEntry.id))
        .where(models.TimeEntry.case_id == case_id,
               models.TimeEntry.removed_at.is_(None),
               or_(models.TimeEntry.source == "manual",
                   models.TimeEntry.stopped_at.is_not(None)))
        .group_by(models.TimeEntry.node_title)
        .order_by(func.sum(models.TimeEntry.hours).desc(), models.TimeEntry.node_title)).all()
    return TimeDayOutput(
        work_date=work_date,
        entries=[_entry_out(row) for row in rows],
        total_hours=float(total),
        summary=[TimeSummaryRow(title=title, hours=float(hours or 0), entries=count)
                 for title, hours, count in summary_rows])
