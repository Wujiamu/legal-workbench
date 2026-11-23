"""归档与写入的互斥、跨日计时拆分（独立复核 2026-11-20）。

真实 PostgreSQL（conftest 重建 legal_workbench_test），全部 TEST-ONLY 虚构数据。
两条独立连接在同一争用点同步，验证「锁后复核」而不是「先查后写」。
"""
from __future__ import annotations

import io
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from legal_workbench import cases, models, progress, time_entries
from legal_workbench.db import sqlalchemy_url
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (AcceptAllInput, AddNodeInput, AddTimeEntryInput,
                                     ArchiveCaseInput, ChangeIdInput, RemoveTimeEntryInput,
                                     StartTimerInput, StopTimerInput)

from .factories import create_case


def _codes(fn):
    with pytest.raises(WorkbenchError) as exc:
        fn()
    return exc.value.code


def _engine(test_database):
    return create_engine(sqlalchemy_url(test_database[1]), future=True)


def _hold_case_lock(engine, case_id) -> Session:
    """另一条连接持有案件行锁，模拟正在进行的同案写入。"""
    session = Session(bind=engine)
    session.execute(select(models.Case).where(models.Case.id == case_id).with_for_update()).scalar_one()
    return session


def _run_in_thread(fn, results: list, key: str):
    def target():
        try:
            results.append((key, fn()))
        except WorkbenchError as exc:  # 服务层错误按错误码记录
            results.append((key, exc.code))
        except Exception as exc:  # noqa: BLE001 - 线程里的意外要带回主线程
            results.append((key, f"error:{type(exc).__name__}:{exc}"))

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def _session(engine) -> Session:
    return Session(bind=engine)


# --- 1. 归档后确认/撤回 -------------------------------------------------------


def test_archived_case_rejects_accept_accept_all_and_revert(db, client):
    """归档后 accept_change / accept_all / revert_change 一律拒绝，树与待确认集不变。"""
    case_id = create_case(db)
    db.commit()
    snap = progress.add_node(db, AddNodeInput(
        case_id=case_id, parent_id=None, kind="program", title="TEST-ONLY 一审"),
        actor="human", entry="local-web")
    program = next(node for node in snap.nodes if node.title == "TEST-ONLY 一审")
    snap = progress.add_node(db, AddNodeInput(
        case_id=case_id, parent_id=program.id, kind="stage", title="TEST-ONLY 庭审",
        request_key="agent-archive-1"), actor="agent", entry="mcp")
    change = snap.pending_changes[0]
    seq_before_archive = snap.seq

    cases.archive_case(db, ArchiveCaseInput(case_id=case_id, expected_revision=0))

    before = progress.get_progress(db, case_id)
    assert _codes(lambda: progress.accept_change(
        db, ChangeIdInput(case_id=case_id, change_id=change.id),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: progress.accept_all(
        db, AcceptAllInput(case_id=case_id, expected_seq=seq_before_archive),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: progress.revert_change(
        db, ChangeIdInput(case_id=case_id, change_id=change.id),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT

    after = progress.get_progress(db, case_id)
    assert [(node.id, node.revision, node.state) for node in after.nodes] == \
           [(node.id, node.revision, node.state) for node in before.nodes]
    assert [item.id for item in after.pending_changes] == [item.id for item in before.pending_changes]


def test_progress_write_read_before_archive_is_rejected_after_commit(db, client, test_database):
    """前置检查已通过、归档随后提交：写入必须在锁后复核被拒（旧实现会写进归档案件）。"""
    case_id = create_case(db)
    db.commit()
    engine = _engine(test_database)
    late = Session(bind=engine)
    try:
        stale = late.get(models.Case, case_id)
        assert stale.archived_at is None  # 旧实现的前置检查在这里就已通过
        cases.archive_case(db, ArchiveCaseInput(case_id=case_id, expected_revision=0))
        assert _codes(lambda: progress.add_node(late, AddNodeInput(
            case_id=case_id, parent_id=None, kind="stage", title="TEST-ONLY 迟到写入"),
            actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    finally:
        late.rollback()
        late.close()
        engine.dispose()
    titles = [node.title for node in progress.get_progress(db, case_id).nodes]
    assert "TEST-ONLY 迟到写入" not in titles


def test_review_write_read_before_archive_is_rejected_after_commit(db, client, test_database):
    """确认/撤回同样必须在案件行锁后复核：预检通过、归档随后提交时仍被拒。

    旧实现（accept_change/accept_all/revert_change 只做 session.get 预检）会
    在缓存了 archived_at IS NULL 的会话里把变更确认进已归档案件。
    """
    case_id = create_case(db)
    db.commit()
    snap = progress.add_node(db, AddNodeInput(
        case_id=case_id, parent_id=None, kind="stage", title="TEST-ONLY 迟到确认",
        request_key="agent-late-accept-1"), actor="agent", entry="cli")
    change_id = snap.last_change_id
    seq_before = progress.get_progress(db, case_id).seq
    db.rollback()  # 释放快照读的 FOR SHARE，避免与归档写锁互等

    engine = _engine(test_database)
    late = Session(bind=engine)
    try:
        stale = late.get(models.Case, case_id)
        assert stale.archived_at is None  # 旧实现的前置检查在这里就已通过
        cases.archive_case(db, ArchiveCaseInput(case_id=case_id, expected_revision=0))
        assert _codes(lambda: progress.accept_change(
            late, ChangeIdInput(case_id=case_id, change_id=change_id),
            actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
        assert _codes(lambda: progress.accept_all(
            late, AcceptAllInput(case_id=case_id, expected_seq=seq_before),
            actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
        assert _codes(lambda: progress.revert_change(
            late, ChangeIdInput(case_id=case_id, change_id=change_id),
            actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    finally:
        late.rollback()
        late.close()
        engine.dispose()
    after = progress.get_progress(db, case_id)
    assert [c.review_state for c in after.changes if c.id == change_id] == ["pending"]


def test_upload_write_read_before_archive_is_rejected_after_commit(db, client, test_database):
    """网页上传同样要在锁后复核归档：预检通过、归档随后提交时不得落盘/落库。"""
    case_id = create_case(db)
    db.commit()
    engine = _engine(test_database)
    late = Session(bind=engine)
    try:
        # 必须持有强引用：SQLAlchemy 身份映射是弱引用，断言里现取现丢会让下一次
        # get() 重新查库，竞态就消失了（这正是旧实现侥幸通过的原因）。
        stale = late.get(models.Case, case_id)
        assert stale.archived_at is None
        cases.archive_case(db, ArchiveCaseInput(case_id=case_id, expected_revision=0))
        # 用迟到会话直接走服务层（HTTP 依赖会另开连接，拿不到这条会话的缓存）。
        from legal_workbench import imports
        from legal_workbench.storage import Storage
        storage = Storage(root=Path(client.app.state.storage.root))
        assert _codes(lambda: imports.create_upload_intake(
            late, storage, case_id=case_id, source_label="TEST-ONLY",
            request_key="late-upload-1",
            uploads=[("迟到上传.txt", io.BytesIO(b"TEST-ONLY late upload"))]
        )) == ErrorCode.INVALID_INPUT
    finally:
        late.rollback()
        late.close()
        engine.dispose()
    assert db.execute(select(models.IntakeItem).where(
        models.IntakeItem.case_id == case_id)).scalars().all() == []
    assert list(client.app.state.storage.iter_case_files(case_id)) == []


# --- 2. 同案写锁与归档互斥 ---------------------------------------------------


def test_archive_waits_for_in_flight_case_write(db, client, test_database):
    """归档要拿同案写锁：另一条连接持锁期间归档必须等待，释放后才提交。"""
    case_id = create_case(db)
    db.commit()
    engine = _engine(test_database)
    blocker = _hold_case_lock(engine, case_id)
    results: list = []
    try:
        def archive():
            session = _session(engine)
            try:
                return cases.archive_case(session, ArchiveCaseInput(
                    case_id=case_id, expected_revision=0))
            finally:
                session.close()

        thread = _run_in_thread(archive, results, "archive")
        thread.join(0.8)
        assert thread.is_alive(), "归档没有等待同案写锁"
        blocker.commit()
        thread.join(15)
    finally:
        blocker.rollback()
        blocker.close()
        engine.dispose()
    assert len(results) == 1 and not isinstance(results[0][1], ErrorCode), results
    assert results[0][1].archived_at is not None
    db.expire_all()
    assert db.get(models.Case, case_id).archived_at is not None


def test_two_connections_start_only_one_running_timer(db, client, test_database):
    """同案双启动只能有一个成功：两条连接在争用点同步，最终只有一条活动计时。"""
    case_id = create_case(db)
    db.commit()
    engine = _engine(test_database)
    blocker = _hold_case_lock(engine, case_id)
    results: list = []
    try:
        def start():
            session = _session(engine)
            try:
                return time_entries.start_timer(session, StartTimerInput(
                    case_id=case_id, node_title="TEST-ONLY 并发计时"))
            finally:
                session.close()

        threads = [_run_in_thread(start, results, "a"), _run_in_thread(start, results, "b")]
        for thread in threads:
            thread.join(0.8)
        assert all(thread.is_alive() for thread in threads), "启动没有等待同案写锁"
        blocker.commit()
        for thread in threads:
            thread.join(15)
    finally:
        blocker.rollback()
        blocker.close()
        engine.dispose()
    assert sum(1 for _, value in results if not isinstance(value, ErrorCode)) == 1, results
    assert [value for _, value in results if isinstance(value, ErrorCode)] == [ErrorCode.INVALID_INPUT]
    db.expire_all()
    assert time_entries.running_entry(db, case_id) is not None
    count = db.scalar(select(func.count()).select_from(models.TimeEntry).where(
        models.TimeEntry.case_id == case_id,
        models.TimeEntry.source == "timer",
        models.TimeEntry.stopped_at.is_(None),
        models.TimeEntry.removed_at.is_(None)))
    assert count == 1


def test_concurrent_archive_and_start_never_both_succeed(db, client, test_database):
    """归档与开始计时互斥：不可能既归档成功又留下活动计时。"""
    case_id = create_case(db)
    db.commit()
    engine = _engine(test_database)
    blocker = _hold_case_lock(engine, case_id)
    results: list = []
    try:
        def start():
            session = _session(engine)
            try:
                return time_entries.start_timer(session, StartTimerInput(
                    case_id=case_id, node_title="TEST-ONLY 归档竞态"))
            finally:
                session.close()

        def archive():
            session = _session(engine)
            try:
                return cases.archive_case(session, ArchiveCaseInput(
                    case_id=case_id, expected_revision=0))
            finally:
                session.close()

        threads = [_run_in_thread(start, results, "start"),
                   _run_in_thread(archive, results, "archive")]
        for thread in threads:
            thread.join(0.8)
        assert all(thread.is_alive() for thread in threads), "两个写操作没有在案件行锁上排队"
        blocker.commit()
        for thread in threads:
            thread.join(15)
    finally:
        blocker.rollback()
        blocker.close()
        engine.dispose()
    winners = [key for key, value in results if not isinstance(value, ErrorCode)]
    assert len(winners) == 1, results
    assert [value for _, value in results if isinstance(value, ErrorCode)] == [ErrorCode.INVALID_INPUT]
    db.expire_all()
    archived = db.get(models.Case, case_id).archived_at is not None
    running = time_entries.running_entry(db, case_id) is not None
    assert archived != running, (archived, running)


def test_concurrent_remove_only_one_succeeds(db, client, test_database):
    """同一条工时的并发删除只有一次成功，另一次是 not_found。"""
    case_id = create_case(db)
    db.commit()
    entry = time_entries.add_manual(db, AddTimeEntryInput(
        case_id=case_id, node_title="TEST-ONLY 并发删除",
        work_date=datetime.now().astimezone().date(), hours=0.25))
    db.commit()
    engine = _engine(test_database)
    blocker = _hold_case_lock(engine, case_id)
    results: list = []
    try:
        def remove():
            session = _session(engine)
            try:
                return time_entries.remove_entry(session, RemoveTimeEntryInput(
                    case_id=case_id, entry_id=entry.id))
            finally:
                session.close()

        threads = [_run_in_thread(remove, results, "a"), _run_in_thread(remove, results, "b")]
        for thread in threads:
            thread.join(0.8)
        assert all(thread.is_alive() for thread in threads), "删除没有等待同案写锁"
        blocker.commit()
        for thread in threads:
            thread.join(15)
    finally:
        blocker.rollback()
        blocker.close()
        engine.dispose()
    assert sum(1 for _, value in results if not isinstance(value, ErrorCode)) == 1, results
    assert [value for _, value in results if isinstance(value, ErrorCode)] == [ErrorCode.NOT_FOUND]


# --- 3. 跨日计时 -------------------------------------------------------------


def test_stop_timer_splits_25_hours_across_local_days(db, client):
    """25 小时计时：按本地日期拆行，总工时仍是 25.00h，单行不超过 24h，审计保留。"""
    case_id = create_case(db)
    db.commit()
    started = time_entries.start_timer(db, StartTimerInput(
        case_id=case_id, node_title="TEST-ONLY 长时计时"))
    row = db.get(models.TimeEntry, started.id)
    row.started_at = datetime.now(timezone.utc) - timedelta(hours=25)
    db.commit()

    stopped = time_entries.stop_timer(db, StopTimerInput(
        case_id=case_id, entry_id=started.id))
    assert stopped.stopped_at is not None

    rows = db.execute(select(models.TimeEntry).where(
        models.TimeEntry.case_id == case_id,
        models.TimeEntry.removed_at.is_(None)).order_by(models.TimeEntry.work_date)).scalars().all()
    assert len(rows) == 2, [(r.work_date, r.hours) for r in rows]
    assert sum((r.hours for r in rows), Decimal("0")) == Decimal("25.00")
    assert all(Decimal("0") < r.hours <= Decimal("24") for r in rows)
    assert all(r.started_at is not None and r.stopped_at is not None for r in rows)
    assert all(r.source == "timer" for r in rows)
    assert len({r.work_date for r in rows}) == 2
    assert time_entries.running_entry(db, case_id) is None

    audit_ops = db.execute(text(
        "SELECT operation FROM audit_events WHERE case_id = :case_id ORDER BY created_at"),
        {"case_id": case_id}).scalars().all()
    assert "time.stop" in audit_ops


def test_split_segments_use_local_midnight_and_cap_24h():
    """分段口径（固定时刻，不依赖本机时钟）：本地零点切分 + 单段 24h 封顶。"""
    tz = time_entries._local_zone()
    start = datetime(2026, 3, 1, 23, 30, tzinfo=tz)
    stop = datetime(2026, 3, 2, 0, 30, tzinfo=tz)
    segments = time_entries._split_timer_segments(start, stop)
    assert [seg[0] for seg in segments] == [date(2026, 3, 1), date(2026, 3, 2)]
    assert segments[0][2] == segments[1][1] == datetime(2026, 3, 2, 0, 0, tzinfo=tz)

    allocated = time_entries._quarter_allocations(start, stop, segments)
    assert [quarters for *_, quarters in allocated] == [2, 2]  # 各 0.5h，合计 1.00h

    # 25 小时：跨一次零点，合计仍是 100 个刻钟（25.00h），单段不超过 96。
    long_start = datetime(2026, 3, 1, 8, 0, tzinfo=tz)
    long_stop = long_start + timedelta(hours=25)
    long_segments = time_entries._split_timer_segments(long_start, long_stop)
    long_alloc = time_entries._quarter_allocations(long_start, long_stop, long_segments)
    assert len(long_segments) == 2
    assert sum(quarters for *_, quarters in long_alloc) == 100
    assert all(quarters <= 96 for *_, quarters in long_alloc)

    # 单段封顶：同一本地日内的 30 小时不会产生 >24h 的行（时钟异常防御）。
    capped = time_entries._split_timer_segments(long_start, long_start + timedelta(hours=30))
    assert all(seg[2] - seg[1] <= timedelta(hours=24) for seg in capped)


def test_stop_timer_same_day_keeps_single_row(db, client):
    """未跨日的计时仍只有一行：拆分逻辑不得改变既有单行口径。"""
    case_id = create_case(db)
    db.commit()
    started = time_entries.start_timer(db, StartTimerInput(
        case_id=case_id, node_title="TEST-ONLY 当日计时"))
    row = db.get(models.TimeEntry, started.id)
    row.started_at = datetime.now(timezone.utc) - timedelta(minutes=16)
    db.commit()
    stopped = time_entries.stop_timer(db, StopTimerInput(
        case_id=case_id, entry_id=started.id))
    assert stopped.hours == 0.5
    rows = db.execute(select(models.TimeEntry).where(
        models.TimeEntry.case_id == case_id,
        models.TimeEntry.removed_at.is_(None))).scalars().all()
    assert len(rows) == 1
    assert rows[0].hours == Decimal("0.50")


# --- 4. 备份维护门禁 ---------------------------------------------------------


def test_time_and_archive_writes_are_gated_by_backup_maintenance(db, client):
    """备份维护期间开始计时与归档都必须被维护门禁挡住（503 maintenance）。"""
    case_id = create_case(db)
    db.commit()
    db.execute(text("UPDATE maintenance_state SET active=true, reason='TEST-ONLY 演练' WHERE id=1"))
    db.commit()
    try:
        assert _codes(lambda: time_entries.start_timer(db, StartTimerInput(
            case_id=case_id, node_title="TEST-ONLY 维护中"))) == ErrorCode.MAINTENANCE
        assert _codes(lambda: cases.archive_case(db, ArchiveCaseInput(
            case_id=case_id, expected_revision=0))) == ErrorCode.MAINTENANCE
    finally:
        db.rollback()
        db.execute(text("UPDATE maintenance_state SET active=false, reason=NULL WHERE id=1"))
        db.commit()
