"""Case service shared by HTTP, CLI and MCP transports."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit, models
from .db import begin_write, update_with_revision
from .errors import ErrorCode, WorkbenchError
from .schemas import (ArchiveCaseInput, CaseOverview, CaseScheduleInput, CaseSummary,
                      CreateCaseOutput, DashboardDeadlineItem, DashboardDeadlinesOutput,
                      DashboardOutput, Deadline, ListCasesOutput)


def create_case(session: Session, *, title: str, description: str | None = None,
                actor: str = "human", entry: str = "local-web") -> CreateCaseOutput:
    title = title.strip()
    if not title:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "案件标题不能为空")
    begin_write(session)
    row = models.Case(id=uuid.uuid4(), title=title, description=description, revision=0)
    session.add(row)
    session.flush()
    audit.record(session, operation="case.create", target_type="case", target_id=row.id,
                 case_id=row.id)
    # K14: instantiate the default progress fragment in the same transaction.
    from . import progress
    progress.instantiate_default_for_new_case(session, row.id, actor=actor, entry=entry)
    session.commit()
    return CreateCaseOutput(case_id=row.id, revision=row.revision)


def list_cases(session: Session, *, offset: int = 0, limit: int = 50,
               archived: bool | None = None) -> ListCasesOutput:
    """归档过滤：None=全部、False=仅办理中、True=仅已归档。"""
    rows_query = select(models.Case)
    count_query = select(func.count()).select_from(models.Case)
    if archived is True:
        rows_query = rows_query.where(models.Case.archived_at.is_not(None))
        count_query = count_query.where(models.Case.archived_at.is_not(None))
    elif archived is False:
        rows_query = rows_query.where(models.Case.archived_at.is_(None))
        count_query = count_query.where(models.Case.archived_at.is_(None))
    total = session.execute(count_query).scalar_one()
    rows = session.execute(
        rows_query.order_by(models.Case.created_at, models.Case.id)
        .offset(offset).limit(limit)
    ).scalars().all()
    return ListCasesOutput(
        items=[CaseSummary(case_id=row.id, title=row.title, description=row.description,
                           revision=row.revision, created_at=row.created_at,
                           archived_at=row.archived_at) for row in rows],
        offset=offset, limit=limit, total=total,
    )


def _progress_summary(session: Session, case_id: uuid.UUID) -> tuple[list[str], list[str]]:
    """P4：从进度树取全部进行中阶段与其下（含嵌套）未完成工作（K14/K3）。

    只读，不触发写锁；并行程序各有一个进行中阶段时全部返回，而非只取第一个。
    未完成工作收集每个进行中阶段的全部后代 work 节点（不限于直接子级）。
    """
    active_stages = session.execute(
        select(models.ProgressNode)
        .where(models.ProgressNode.case_id == case_id,
               models.ProgressNode.removed_at.is_(None),
               models.ProgressNode.kind == "stage",
               models.ProgressNode.state == "in_progress")
        .order_by(models.ProgressNode.position)
    ).scalars().all()
    if not active_stages:
        return [], []
    all_nodes = session.execute(
        select(models.ProgressNode)
        .where(models.ProgressNode.case_id == case_id,
               models.ProgressNode.removed_at.is_(None))
    ).scalars().all()
    children: dict[uuid.UUID, list[models.ProgressNode]] = {}
    for node in all_nodes:
        if node.parent_id is not None:
            children.setdefault(node.parent_id, []).append(node)
    for siblings in children.values():
        siblings.sort(key=lambda n: n.position)
    work: list[str] = []
    for stage in active_stages:
        # 栈用逆序压入、pop 取首，保证同级按 position 正序；嵌套工作按树的先序就地展开。
        stack = list(reversed(children.get(stage.id, [])))
        while stack:
            node = stack.pop()
            if node.kind == "work" and node.state == "pending":
                work.append(node.title)
            stack.extend(reversed(children.get(node.id, [])))
    return [s.title for s in active_stages], work


def _overview(session: Session, row: models.Case) -> CaseOverview:
    active_stages, pending_work = _progress_summary(session, row.id)
    return CaseOverview(case_id=row.id, title=row.title, description=row.description,
                        revision=row.revision, created_at=row.created_at, stage=row.stage,
                        next_action=row.next_action, deadlines=row.deadlines,
                        active_stage=active_stages[0] if active_stages else None,
                        active_stages=active_stages, pending_work=pending_work,
                        archived_at=row.archived_at)


def get_case(session: Session, case_id: uuid.UUID) -> CaseOverview:
    row = session.get(models.Case, case_id)
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "案件不存在")
    return _overview(session, row)


def dashboard(session: Session, *, offset: int, limit: int) -> DashboardOutput:
    deadline = func.jsonb_array_elements(models.Case.deadlines).column_valued("deadline")
    next_due = (select(func.min(deadline.op("->>")("due")))
                .where(deadline.op("->>")("completed") != "true").correlate(models.Case)
                .scalar_subquery())
    rows = session.scalars(select(models.Case)
                           .where(models.Case.archived_at.is_(None))
                           .order_by(next_due.asc().nulls_last(), models.Case.title, models.Case.id)
                           .offset(offset).limit(limit)).all()
    total = session.scalar(select(func.count()).select_from(models.Case)
                           .where(models.Case.archived_at.is_(None)))
    return DashboardOutput(items=[_overview(session, row) for row in rows], total=total,
                           offset=offset, limit=limit)


def shanghai_today() -> date:
    """“今天”的唯一口径：本地日历日期按 ``Asia/Shanghai`` 计算。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def dashboard_deadlines(session: Session) -> DashboardDeadlinesOutput:
    """首页跨案期限时间轴的只读投影。

    直接读 ``cases.deadlines`` JSONB，**不**经过 ``/dashboard`` 的 50 条分页，
    因此第 2 页及以后案件的期限同样完整；已归档案件（``archived_at`` 非空）
    不参与。窗口裁剪、完成状态显隐都在前端做，接口只按日期升序给全量。
    """
    rows = session.execute(
        select(models.Case.id, models.Case.title, models.Case.deadlines)
        .where(models.Case.archived_at.is_(None))
    ).all()
    items: list[DashboardDeadlineItem] = []
    for case_id, case_title, deadlines in rows:
        for raw in deadlines or []:
            try:
                deadline = Deadline.model_validate(raw)
            except ValidationError:
                # 存量 JSONB 若有不符合契约的条目，跳过它而不是让首页整体 500。
                continue
            items.append(DashboardDeadlineItem(
                case_id=case_id, case_title=case_title, title=deadline.title,
                due=deadline.due, kind=deadline.kind, completed=deadline.completed))
    # 同日多条保持稳定次序（案件标题 → 事项标题 → id），前端再按日期分组堆叠。
    items.sort(key=lambda item: (item.due, item.case_title, item.title, str(item.case_id)))
    return DashboardDeadlinesOutput(items=items, as_of=shanghai_today(),
                                    case_total=len(rows))


def update_schedule(session: Session, payload: CaseScheduleInput) -> CaseOverview:
    lock_case_writable(session, payload.case_id)
    changed = update_with_revision(
        session, models.Case, payload.case_id, payload.expected_revision,
        stage=(payload.stage or "").strip() or None,
        next_action=(payload.next_action or "").strip() or None,
        deadlines=[item.model_dump(mode="json") for item in payload.deadlines])
    if not changed:
        raise WorkbenchError(ErrorCode.CONFLICT, "案件办理信息已变化")
    result = get_case(session, payload.case_id)
    session.commit()
    return result


def ensure_case_writable(session: Session, case_id: uuid.UUID) -> models.Case:
    """Reject writes to an archived case; returns the row for further checks.

    只做只读预检，**不能**作为写入前的最终判断：归档可能在预检与写入之间提交。
    所有写路径必须改用 :func:`lock_case_writable`，在案件行锁内复核。
    """
    row = session.get(models.Case, case_id)
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "案件不存在")
    if row.archived_at is not None:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "案件已归档，内容只读；如需修改请先取消归档")
    return row


def _lock_case_row(session: Session, case_id: uuid.UUID) -> models.Case | None:
    """Take the same-case write lock and read the *committed* row.

    ``populate_existing`` is required: a plain ``SELECT … FOR NO KEY UPDATE`` returns
    the identity-mapped instance without refreshing it, so a session that read
    ``archived_at IS NULL`` earlier would keep the stale value and pass the
    post-lock check.  With it, the row is re-populated from the locked read.
    """
    return session.execute(
        select(models.Case).where(models.Case.id == case_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def lock_case_writable(session: Session, case_id: uuid.UUID, *,
                       archived_message: str | None = None) -> models.Case:
    """同案写入的唯一串行点：维护门禁 → 案件行锁 → 归档复核。

    顺序固定，全项目一致：``begin_write``（备份维护标志 + 共享咨询锁）→
    ``SELECT … FOR NO KEY UPDATE`` 案件行（``populate_existing`` 刷新 ORM 缓存）→
    复核 ``archived_at``。归档与任何同案写入（进度、确认/撤回、材料、导入、
    办理信息、工时、分析）都排在这把锁上，因此「先查后写」的竞态不再存在：
    拿到锁后看到的一定是最新已提交状态。

    ``archived_message`` 让进度命令保留自己的「进度只读」措辞，其余入口用默认值。
    """
    begin_write(session)
    row = _lock_case_row(session, case_id)
    if row is None:
        # 拒绝写入时释放本次事务（含行锁）；调用方此时尚无待提交的业务写入。
        session.rollback()
        raise WorkbenchError(ErrorCode.NOT_FOUND, "案件不存在")
    if row.archived_at is not None:
        session.rollback()
        raise WorkbenchError(
            ErrorCode.INVALID_INPUT,
            archived_message or "案件已归档，内容只读；如需修改请先取消归档")
    return row


def lock_case_shared(session: Session, case_id: uuid.UUID) -> models.Case:
    """同案「图 + 证据目录」一致读的唯一串行点（只读，不检查归档）。

    顺序固定：``SELECT … FOR SHARE`` 案件行 → 再读两个领域的数据与序号。
    所有 graph/evidence 写入都先取案件行 ``FOR NO KEY UPDATE``（见
    :func:`lock_case_writable`），因此共享锁拿到后看到的一定是某个已提交
    写入完成后的状态：工作区数据、目录编号与 seq 不会来自两个不同事务。
    读取方**不得**在持有本锁后再申请写锁或域序号写锁（避免反向死锁）。
    """
    row = session.execute(
        select(models.Case).where(models.Case.id == case_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "案件不存在")
    return row


def lock_case_for_import(session: Session, case_id: uuid.UUID) -> models.Case:
    """Keep the case writable until this import transaction has committed."""
    begin_write(session)
    row = lock_case_shared(session, case_id)
    if row.archived_at is not None:
        session.rollback()
        raise WorkbenchError(ErrorCode.INVALID_INPUT,
                             "案件已归档，内容只读；如需修改请先取消归档")
    return row


def archive_case(session: Session, payload: ArchiveCaseInput) -> CaseOverview:
    """结案归档：案件级操作（非进度命令），进度与材料写入随后被拒。

    与所有同案写入共用 :func:`lock_case_writable` 的串行点：拿锁后复核归档状态、
    复核没有活动计时，再 CAS 落库。并发下不可能既归档成功又留下活动计时。
    """
    begin_write(session)
    row = _lock_case_row(session, payload.case_id)
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "案件不存在")
    if row.archived_at is not None:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "案件已归档")
    running = session.execute(select(models.TimeEntry.id).where(
        models.TimeEntry.case_id == payload.case_id,
        models.TimeEntry.source == "timer",
        models.TimeEntry.stopped_at.is_(None),
        models.TimeEntry.removed_at.is_(None))).first()
    if running is not None:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "有进行中的计时，请先停止计时再归档")
    changed = update_with_revision(
        session, models.Case, payload.case_id, payload.expected_revision,
        archived_at=datetime.now(timezone.utc))
    if not changed:
        raise WorkbenchError(ErrorCode.CONFLICT, "案件已变化，请刷新后重试")
    audit.record(session, operation="case.archive", target_type="case",
                 target_id=payload.case_id, case_id=payload.case_id)
    result = get_case(session, payload.case_id)
    session.commit()
    return result


def unarchive_case(session: Session, payload: ArchiveCaseInput) -> CaseOverview:
    """取消归档：恢复进度与材料写入。"""
    begin_write(session)
    row = _lock_case_row(session, payload.case_id)
    if row is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, "案件不存在")
    if row.archived_at is None:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "案件未归档")
    changed = update_with_revision(
        session, models.Case, payload.case_id, payload.expected_revision, archived_at=None)
    if not changed:
        raise WorkbenchError(ErrorCode.CONFLICT, "案件已变化，请刷新后重试")
    audit.record(session, operation="case.unarchive", target_type="case",
                 target_id=payload.case_id, case_id=payload.case_id)
    result = get_case(session, payload.case_id)
    session.commit()
    return result
