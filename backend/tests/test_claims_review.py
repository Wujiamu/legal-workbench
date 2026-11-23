"""诉请金额 P2 独立审查整改的针对性回归（真实 PostgreSQL、两个连接、TEST-ONLY）。

覆盖审查确认的六类问题，每条都以用户可见结果与数据状态断言：

1. 归档与同案写入的互斥（锁顺序：维护锁 → case 行锁 → 领域 seq）、读快照首写一致、
   ORM 旧缓存下的归档/CAS/seq；
2. 引用不可变（同案 RESTRICT）、真实重解析/失败重试、read_source 持旧段、
   显式引用只接受可读终态段、接受操作重验、候选 span 与 active 口径；
3. 组内逐步累加（瀑布图前缀和）可精确表示、坏类型拒绝、数据库金额上限与
   consistency 契约一致、改币种不静默换算；
4. accept_all 逐项验证请求 ID、每个写入产生可重建的人工变更记录与同事务审计；
5. Agent 变更幂等（同 request_key 重放、不同操作/来源/payload 不得误认）；
6. 迁移降级只清理 claims 领域。
"""
from __future__ import annotations

import threading
import time
import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from legal_workbench import cases, claims, consistency, models, parsing, search
from legal_workbench.db import get_session_factory, sqlalchemy_url
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.parsing import pipeline
from legal_workbench.schemas import (
    AddClaimGroupInput, AddClaimItemInput, ArchiveCaseInput, ChangeIdInput,
    ClaimAcceptAllInput, ClaimGroupChanges, ClaimGroupRevisionInput, ClaimItemChanges,
    ClaimItemRevisionInput, ReorderClaimGroupInput, ReorderClaimItemInput,
    UpdateClaimGroupInput, UpdateClaimItemInput, CLAIM_MAX_CENTS,
)
from legal_workbench.storage import Storage

from . import factories
from .factories import create_case


def cents(amount: str) -> int:
    return int(Decimal(amount.replace(",", "")) * 100)


def _group(db, case_id, title="对被告一主诉请", *, currency="CNY", actor="human",
           request_key=None):
    return claims.add_group(db, AddClaimGroupInput(
        case_id=case_id, title=title, currency=currency, request_key=request_key),
        actor=actor, entry="local-web")


def _item(db, case_id, group_id, amount_cents, *, category="principal", title="金额项",
          actor="human", request_key=None, segment_id=None):
    return claims.add_item(db, AddClaimItemInput(
        case_id=case_id, group_id=group_id, title=title, category=category,
        amount_cents=amount_cents, segment_id=segment_id, request_key=request_key),
        actor=actor, entry="local-web")


def _find(snapshot, title):
    return next(row for row in snapshot.items if row.title == title)


def _codes(fn):
    with pytest.raises(WorkbenchError) as exc:
        fn()
    return exc.value.code


def _source(db, *, case_id, text_body, raw, amount, currency="CNY", run_status="complete",
            name="TEST-ONLY 审查合同.txt", active=True):
    """案件 → 材料 → 解析运行/单元 → 片段 → 金额精确值；可指定运行状态与是否 active。"""
    intake_id = factories.create_intake(db, case_id)
    intake_item_id = factories.create_intake_item(
        db, case_id, intake_id, f"虚构/{uuid.uuid4().hex}.txt")
    object_id = factories.create_stored_object(db, case_id, content=uuid.uuid4().bytes)
    material_id = factories.create_material(db, case_id, intake_item_id, display_name=name)
    version_id = factories.create_file_version(db, case_id, material_id, object_id)
    run = models.ParseRun(
        id=uuid.uuid4(), case_id=case_id, file_version_id=version_id,
        parser_version="test", config={}, sequence=0, status=run_status, coverage={})
    unit = models.ParseUnit(
        id=uuid.uuid4(), case_id=case_id, parse_run_id=run.id, unit_key="page-1",
        status="extracted", method="native")
    db.add_all([run, unit])
    db.flush()
    start = text_body.index(raw)
    segment = models.Segment(
        id=uuid.uuid4(), case_id=case_id, parse_run_id=run.id, parse_unit_id=unit.id,
        locator={"page": 3}, text=text_body, normalized_text=text_body, offset_map=[])
    db.add(segment)
    db.flush()
    exact = models.ExactValue(
        id=uuid.uuid4(), case_id=case_id, segment_id=segment.id, kind="amount",
        raw_text=raw, canonical_value={"value": str(amount), "currency": currency},
        amount_value=amount, currency=currency,
        span={"start": start, "end": start + len(raw)})
    db.add(exact)
    db.flush()
    if active:
        db.execute(text("UPDATE file_versions SET active_parse_run_id = :run WHERE id = :v"),
                   {"run": run.id, "v": version_id})
    return {"case_id": case_id, "material_id": material_id, "file_version_id": version_id,
            "run_id": run.id, "segment_id": segment.id, "exact_value_id": exact.id}


# --- 1. 归档互斥、锁顺序、读快照 -------------------------------------------

def test_archive_waits_for_claim_write_on_the_case_row(db, app_engine):
    """两个连接：诉请写入持有 case 行锁时，归档必须排队；释放后归档生效且只读。"""
    case_id = create_case(db)
    db.commit()
    revision = db.execute(text("SELECT revision FROM cases WHERE id = :id"),
                          {"id": case_id}).scalar_one()

    # 主连接：进入同案写入的串行点（维护锁 → case 行锁）。
    cases.lock_case_writable(db, case_id)
    outcome: dict[str, object] = {}

    def archive() -> None:
        session = Session(bind=app_engine)
        try:
            outcome["result"] = cases.archive_case(
                session, ArchiveCaseInput(case_id=case_id, expected_revision=revision))
        except BaseException as exc:  # pragma: no cover - 失败时用于诊断
            outcome["error"] = exc
        finally:
            session.close()

    worker = threading.Thread(target=archive, daemon=True)
    worker.start()
    time.sleep(1.0)
    assert worker.is_alive(), "归档未被同案写入的 case 行锁挡住：锁顺序没有覆盖归档"

    # 主连接在持锁期间完成一笔诉请写入并提交。
    snapshot = _group(db, case_id, "归档竞态组")
    assert snapshot.groups[0].title == "归档竞态组"
    worker.join(10)
    assert not worker.is_alive()
    assert "error" not in outcome, outcome.get("error")
    assert outcome["result"].archived_at is not None

    # 归档提交后同一案件的诉请写入一律拒绝。
    assert _codes(lambda: _group(db, case_id, "归档后")) == ErrorCode.INVALID_INPUT
    db.rollback()


def test_write_rejects_archive_committed_after_the_session_cached_the_case(db, app_engine):
    """ORM 旧缓存：本连接先读过案件，另一连接归档提交后，写入必须看到归档。

    这里用普通 ``SELECT``（不加锁）填充身份映射，模拟「读接口先读过、写入随后到达」
    的请求级会话；归档在另一连接提交后，写入侧的 ``populate_existing`` 必须刷新。
    """
    case_id = create_case(db)
    db.commit()
    cached = db.get(models.Case, case_id)  # 只读缓存，无行锁
    assert cached.archived_at is None
    revision = db.execute(text("SELECT revision FROM cases WHERE id = :id"),
                          {"id": case_id}).scalar_one()

    other = Session(bind=app_engine)
    cases.archive_case(other, ArchiveCaseInput(case_id=case_id, expected_revision=revision))
    other.close()

    assert cached.archived_at is None  # 缓存仍是旧值
    assert _codes(lambda: _group(db, case_id, "旧缓存后写入")) == ErrorCode.INVALID_INPUT
    db.rollback()


def test_stale_orm_row_cannot_pass_cas_after_another_connection_committed(db, app_engine):
    """ORM 旧缓存：另一连接提交后，本连接的 CAS 必须按数据库当前修订号判断。"""
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, 100)
    item = snapshot.items[0]
    db.commit()
    stale = db.get(models.ClaimItem, item.id)  # 只读缓存，无行锁
    assert stale.revision == item.revision

    other = Session(bind=app_engine)
    claims.update_item(other, UpdateClaimItemInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision,
        changes=ClaimItemChanges(amount_cents=777)), actor="human", entry="local-web")
    other.close()

    assert _codes(lambda: claims.update_item(db, UpdateClaimItemInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision,
        changes=ClaimItemChanges(amount_cents=888)), actor="human",
        entry="local-web")) == ErrorCode.CONFLICT
    db.rollback()
    assert claims.get_claims(db, case_id).items[0].amount_cents == 777
    db.rollback()


def test_first_write_race_does_not_return_rows_with_seq_zero(db, app_engine, monkeypatch):
    """全新案件：读快照暂停在 seq 之后，另一连接的首写不得插进来。

    读侧以 case 行共享锁固定快照；写入方的首写（建 seq 行 + 首个分组）必须等读侧
    结束，所以读侧要么看到「无行 + seq 0」，要么看到「有行 + seq 1」，不会混。
    """
    case_id = create_case(db)
    db.commit()

    real_all_items = claims._all_items
    reader_paused = threading.Event()
    writer_done = threading.Event()
    state: dict[str, object] = {}

    def pausing_all_items(session, case_id_arg):
        reader_paused.set()
        writer_done.wait(1.5)  # 给写入方足够时间尝试提交
        state["writer_done_while_reading"] = writer_done.is_set()
        return real_all_items(session, case_id_arg)

    def write_first() -> None:
        reader_paused.wait(5)
        session = Session(bind=app_engine)
        try:
            state["written"] = _group(session, case_id, "首写组")
        except BaseException as exc:  # pragma: no cover - 失败时用于诊断
            state["error"] = exc
        finally:
            writer_done.set()
            session.close()

    monkeypatch.setattr(claims, "_all_items", pausing_all_items)
    worker = threading.Thread(target=write_first, daemon=True)
    worker.start()
    snapshot = claims.get_claims(db, case_id)

    assert state.get("writer_done_while_reading") is False, \
        "首写在读快照期间提交：读侧可能返回已有条目配 seq=0"
    assert snapshot.seq == 0 and snapshot.groups == [] and snapshot.items == []

    # 读事务结束（释放案件行共享锁）后，排队中的首写才能提交。
    db.rollback()
    worker.join(10)
    assert not worker.is_alive()
    assert state.get("error") is None, state.get("error")
    after = claims.get_claims(db, case_id)
    assert after.seq == 1 and [group.title for group in after.groups] == ["首写组"]


# --- 2. 引用不可变、重解析、候选口径 ---------------------------------------

def test_segment_reference_is_restrict_so_provenance_cannot_vanish(db):
    case_id = create_case(db)
    db.commit()
    source = _source(db, case_id=case_id, text_body="第一条 合同价款为人民币1,280,000元。",
                     raw="人民币1,280,000元", amount=Decimal("1280000"))
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, cents("1,280,000.00"), title="合同价款",
                     segment_id=source["segment_id"])
    assert snapshot.items[0].segment_id == source["segment_id"]

    # 先删精确值（它同样引用片段），再删片段：金额项的出处必须由数据库拒绝删除。
    db.execute(text("DELETE FROM exact_values WHERE segment_id = :sid"),
               {"sid": source["segment_id"]})
    with pytest.raises(IntegrityError) as exc:
        db.execute(text("DELETE FROM segments WHERE id = :sid"), {"sid": source["segment_id"]})
    assert "fk_claim_items_segment" in str(exc.value)
    db.rollback()

    item = _find(claims.get_claims(db, case_id), "合同价款")
    assert item.segment_id == source["segment_id"]
    assert item.amount_cents == cents("1,280,000.00")


def test_real_reparse_creates_new_run_and_keeps_old_source_usable(db, tmp_path):
    """同一文件版本真实重解析：新建运行、旧段与出处不变、read_source 仍读旧段。"""
    storage = Storage(root=tmp_path / "data")
    built = factories.build_material_with_storage(
        db, storage, content="第一条 合同价款为人民币1,280,000元。".encode(),
        name="TEST-ONLY 重解析.txt")
    case_id = built["case_id"]

    run1, job1 = parsing.create_parse_run(
        db, case_id=case_id, file_version_id=built["file_version_id"])
    assert pipeline.process_parse_job(get_session_factory(), storage, job1) == "complete"
    segment1 = db.execute(select(models.Segment).where(models.Segment.parse_run_id == run1)
                          ).scalars().one()
    exact1 = db.execute(select(models.ExactValue).where(
        models.ExactValue.segment_id == segment1.id)).scalars().one()

    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, cents("1,280,000.00"), title="合同价款",
                     segment_id=segment1.id)

    run2, job2 = parsing.create_parse_run(
        db, case_id=case_id, file_version_id=built["file_version_id"])
    assert pipeline.process_parse_job(get_session_factory(), storage, job2) == "complete"
    assert run2 != run1
    version = db.get(models.FileVersion, built["file_version_id"])
    assert version.active_parse_run_id == run2

    # 旧段未被删除，出处与金额保持原值；旧段仍可读回原文。
    assert db.get(models.Segment, segment1.id) is not None
    item = _find(claims.get_claims(db, case_id), "合同价款")
    assert item.segment_id == segment1.id and item.amount_cents == cents("1,280,000.00")
    read = search.read_source(db, case_id=case_id, source_ref=segment1.id)
    assert "1,280,000" in read.text and read.parse_run_id == run1

    # 候选按 active/current 口径：只剩新运行的精确值。
    candidates = claims.list_amount_candidates(db, case_id).candidates
    assert [candidate.segment_id for candidate in candidates] == [
        db.execute(select(models.Segment).where(models.Segment.parse_run_id == run2)
                   ).scalars().one().id]
    assert exact1.id not in [candidate.exact_value_id for candidate in candidates]

    # 重试已完成的运行：幂等发布，不删除旧段、不影响出处。
    assert pipeline.process_parse_job(get_session_factory(), storage, job2) == "complete"
    assert db.get(models.Segment, segment1.id) is not None
    assert _find(claims.get_claims(db, case_id), "合同价款").segment_id == segment1.id


def test_failed_run_retry_leaves_published_segments_and_references_intact(db, tmp_path):
    storage = Storage(root=tmp_path / "data")
    built = factories.build_material_with_storage(
        db, storage, content="第一段 金额100.10元\n\n第二段 金额200.20元".encode(),
        name="TEST-ONLY 重试.txt")
    case_id = built["case_id"]
    run1, job1 = parsing.create_parse_run(
        db, case_id=case_id, file_version_id=built["file_version_id"])
    assert pipeline.process_parse_job(get_session_factory(), storage, job1) == "complete"
    published = db.execute(select(models.Segment).where(
        models.Segment.parse_run_id == run1)).scalars().all()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, 10010, title="已发布段出处",
                     segment_id=published[0].id)

    # 第二次运行在第一个单元后崩溃：非终态运行只改自己的工作单元。
    run2, job2 = parsing.create_parse_run(
        db, case_id=case_id, file_version_id=built["file_version_id"])

    def boom(stage: str) -> None:
        if stage == "unit:1":
            raise RuntimeError("TEST-ONLY 注入失败")

    with pytest.raises(RuntimeError):
        pipeline.process_parse_job(get_session_factory(), storage, job2, fault_inject=boom)
    assert pipeline.process_parse_job(get_session_factory(), storage, job2) == "complete"

    for segment in published:
        assert db.get(models.Segment, segment.id) is not None
    item = _find(claims.get_claims(db, case_id), "已发布段出处")
    assert item.segment_id == published[0].id
    read = search.read_source(db, case_id=case_id, source_ref=published[0].id)
    assert read.text == published[0].text


def test_explicit_source_requires_a_readable_terminal_run(db):
    case_id = create_case(db)
    db.commit()
    running = _source(db, case_id=case_id, text_body="运行中：人民币1,000元。",
                      raw="人民币1,000元", amount=Decimal("1000"), run_status="running",
                      name="TEST-ONLY 运行中.txt")
    failed = _source(db, case_id=case_id, text_body="失败：人民币2,000元。",
                     raw="人民币2,000元", amount=Decimal("2000"), run_status="failed",
                     name="TEST-ONLY 失败.txt")
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    assert _codes(lambda: _item(db, case_id, group_id, 100000,
                                segment_id=running["segment_id"])) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: _item(db, case_id, group_id, 200000,
                                segment_id=failed["segment_id"])) == ErrorCode.INVALID_INPUT
    db.rollback()
    assert claims.get_claims(db, case_id).items == []


def test_accept_revalidates_the_source_run_state(db):
    """接受操作重验出处：暂存后运行状态不再可读，确认只能冲突。"""
    case_id = create_case(db)
    db.commit()
    source = _source(db, case_id=case_id, text_body="第一条 人民币500元。",
                     raw="人民币500元", amount=Decimal("500"))
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, 50000, actor="agent", request_key="rev-1",
                     title="候选出处", segment_id=source["segment_id"])
    pending = snapshot.pending_changes[0]
    db.execute(text("UPDATE parse_runs SET status = 'failed' WHERE id = :id"),
               {"id": source["run_id"]})
    db.commit()
    assert _codes(lambda: claims.accept_change(
        db, ChangeIdInput(case_id=case_id, change_id=pending.id),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    db.rollback()
    assert claims.get_claims(db, case_id).pending_count == 1


def test_candidate_span_is_relative_to_the_returned_excerpt(db):
    prefix = "前言" * 40
    suffix = "后记" * 40
    body = f"{prefix}合同价款人民币1,280,000元。{suffix}"
    case_id = create_case(db)
    db.commit()
    _source(db, case_id=case_id, text_body=body, raw="人民币1,280,000元",
            amount=Decimal("1280000"))
    candidate = claims.list_amount_candidates(db, case_id).candidates[0]
    start = candidate.span["start"]
    end = candidate.span["end"]
    assert candidate.excerpt[start:end] == "人民币1,280,000元"
    assert start > 0 and end < len(candidate.excerpt)  # 两侧都保留了上下文


def test_candidates_filter_currency_before_limit(db, monkeypatch):
    """currency 为空的精确值不能占掉 LIMIT 名额。"""
    monkeypatch.setattr(claims, "CANDIDATE_LIMIT", 2)
    case_id = create_case(db)
    db.commit()
    source = _source(db, case_id=case_id, text_body="人民币1,280,000元。",
                     raw="人民币1,280,000元", amount=Decimal("1280000"))
    for offset in range(2):
        db.add(models.ExactValue(
            id=uuid.uuid4(), case_id=case_id, segment_id=source["segment_id"], kind="amount",
            raw_text=f"无币种{offset}", canonical_value={}, amount_value=None, currency=None,
            span={"start": 0, "end": 1}))
    db.flush()
    candidates = claims.list_amount_candidates(db, case_id).candidates
    assert [candidate.currency for candidate in candidates] == ["CNY"]


# --- 3. 逐步累加、类型、上限、币种 -----------------------------------------

def test_waterfall_prefix_sum_must_stay_exact_at_every_step(db):
    """最终小计安全不等于中间步骤安全：移动抵扣会溢出时必须原子拒绝。"""
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    _item(db, case_id, group_id, CLAIM_MAX_CENTS, title="上限本金")
    _item(db, case_id, group_id, 300, category="deduction", title="抵扣")
    snapshot = _item(db, case_id, group_id, 200, title="追加")
    deduction = _find(snapshot, "抵扣")
    before = [(item.title, item.position) for item in snapshot.items]

    # 移到末尾后前缀和为 MAX、MAX+200（溢出）、MAX-100，最终小计本身是安全的。
    assert _codes(lambda: claims.reorder_items(db, ReorderClaimItemInput(
        case_id=case_id, item_id=deduction.id, expected_revision=deduction.revision,
        direction="down"), actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    db.rollback()
    after = claims.get_claims(db, case_id)
    assert [(item.title, item.position) for item in after.items] == before
    assert after.group_subtotals[str(group_id)] == CLAIM_MAX_CENTS - 300 + 200


def test_accept_is_rejected_atomically_when_prefix_sum_would_overflow(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    _item(db, case_id, group_id, CLAIM_MAX_CENTS, title="上限本金")
    snapshot = _item(db, case_id, group_id, 500, actor="agent", request_key="over-1",
                     title="候选溢出")
    pending = snapshot.pending_changes[0]
    assert _codes(lambda: claims.accept_change(
        db, ChangeIdInput(case_id=case_id, change_id=pending.id),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    db.rollback()
    after = claims.get_claims(db, case_id)
    assert after.pending_count == 1
    assert [item.title for item in after.items] == ["上限本金"]
    assert after.seq == snapshot.seq  # 拒绝不推进序号


@pytest.mark.parametrize("bad", [True, 1.5, "100", Decimal("100")])
def test_amount_request_fields_reject_non_integer_cents(bad):
    with pytest.raises(ValidationError):
        AddClaimItemInput(case_id=uuid.uuid4(), group_id=uuid.uuid4(), title="坏类型",
                          category="principal", amount_cents=bad)
    with pytest.raises(ValidationError):
        ClaimItemChanges(amount_cents=bad)


def test_database_and_consistency_enforce_the_published_amount_ceiling(db, store,
                                                                      migration_engine):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    _item(db, case_id, group_id, 1, title="一分钱")
    db.add(models.ClaimItem(
        id=uuid.uuid4(), case_id=case_id, group_id=group_id, title="越界",
        category="principal", amount_cents=CLAIM_MAX_CENTS + 1, position=9,
        created_by="human", updated_by="human", revision=0))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    # 绕过数据库检查的历史坏数据（模拟旧版本写入）必须被一致性检查报出。
    # 应用角色只有 DML 权限，改约束要用迁移连接（测试库，TEST-ONLY）。
    with migration_engine.begin() as conn:
        conn.execute(text("ALTER TABLE claim_items DROP CONSTRAINT ck_claim_items_amount"))
    db.execute(text("INSERT INTO claim_items (id, case_id, group_id, title, category, "
                    "amount_cents, position, created_by, updated_by, revision) "
                    "VALUES (:id, :case_id, :group_id, '历史越界', 'principal', :amount, 9, "
                    "'human', 'human', 0)"),
               {"id": uuid.uuid4(), "case_id": case_id, "group_id": group_id,
                "amount": CLAIM_MAX_CENTS + 1})
    db.commit()
    report = consistency.check_consistency(db, store, case_id=case_id)
    assert "claims_amount_out_of_range" in [issue.kind for issue in report.issues]
    db.execute(text("DELETE FROM claim_items WHERE title = '历史越界'"))
    db.commit()
    with migration_engine.begin() as conn:
        conn.execute(text("ALTER TABLE claim_items ADD CONSTRAINT ck_claim_items_amount "
                          "CHECK (amount_cents BETWEEN 0 AND 9007199254740991)"))

    # 0.01 元与上限本身都必须能存、能读回（公开契约的两端）。上限单独一组并排除
    # 在按币种总计之外：同币种再加一分钱会让全案合计越过上限，那是应拒绝的情况。
    snapshot = _item(db, case_id, group_id, 1, title="一分钱二")
    assert _find(snapshot, "一分钱二").amount_cents == 1
    snapshot = _group(db, case_id, "上限组")
    ceiling_group = snapshot.groups[-1]
    snapshot = claims.update_group(db, UpdateClaimGroupInput(
        case_id=case_id, group_id=ceiling_group.id,
        expected_revision=ceiling_group.revision,
        changes=ClaimGroupChanges(include_in_total=False)), actor="human", entry="local-web")
    snapshot = _item(db, case_id, ceiling_group.id, CLAIM_MAX_CENTS, title="上限")
    assert _find(snapshot, "上限").amount_cents == CLAIM_MAX_CENTS
    assert snapshot.group_subtotals[str(ceiling_group.id)] == CLAIM_MAX_CENTS
    # 再加一分钱越界：整笔拒绝，不写入。
    assert _codes(lambda: _item(db, case_id, ceiling_group.id, 1,
                                title="越界一分")) == ErrorCode.INVALID_INPUT
    db.rollback()
    assert _find(claims.get_claims(db, case_id), "上限").amount_cents == CLAIM_MAX_CENTS


def test_group_currency_change_cannot_relabel_an_explicit_source(db):
    case_id = create_case(db)
    db.commit()
    source = _source(db, case_id=case_id, text_body="合同价款人民币1,280,000元。",
                     raw="人民币1,280,000元", amount=Decimal("1280000"), currency="CNY")
    snapshot = _group(db, case_id)
    group = snapshot.groups[0]
    snapshot = _item(db, case_id, group.id, cents("1,280,000.00"), title="合同价款",
                     segment_id=source["segment_id"])
    group = next(row for row in snapshot.groups if row.id == group.id)
    assert _codes(lambda: claims.update_group(db, UpdateClaimGroupInput(
        case_id=case_id, group_id=group.id, expected_revision=group.revision,
        changes=ClaimGroupChanges(currency="USD")), actor="human",
        entry="local-web")) == ErrorCode.INVALID_INPUT
    db.rollback()
    assert claims.get_claims(db, case_id).groups[0].currency == "CNY"

    # 没有明确币种出处的分组仍可改币种。
    other = _group(db, case_id, "无出处组")
    other_group = other.groups[-1]
    snapshot = claims.update_group(db, UpdateClaimGroupInput(
        case_id=case_id, group_id=other_group.id, expected_revision=other_group.revision,
        changes=ClaimGroupChanges(currency="USD")), actor="human", entry="local-web")
    assert [row.currency for row in snapshot.groups if row.id == other_group.id] == ["USD"]


# --- 4. 确认：逐项验证 + 人工变更记录 --------------------------------------

def test_accept_all_requires_the_exact_displayed_set(db, app_engine):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    for index in range(3):
        snapshot = _item(db, case_id, group_id, 100 * (index + 1), actor="agent",
                         request_key=f"bulk-{index}", title=f"候选{index}")
    ids = [change.id for change in snapshot.pending_changes]
    assert len(ids) == 3

    # 缺一项：整体冲突，一项都不确认。
    assert _codes(lambda: claims.accept_all(db, ClaimAcceptAllInput(
        case_id=case_id, change_ids=ids[:2]), actor="human",
        entry="local-web")) == ErrorCode.CONFLICT
    db.rollback()
    assert claims.get_claims(db, case_id).pending_count == 3

    # 外案 ID：整体冲突。
    foreign_case = create_case(db, "TEST-ONLY 外案")
    db.commit()
    foreign = _group(db, foreign_case)
    foreign_snapshot = _item(db, foreign_case, foreign.groups[0].id, 999, actor="agent",
                             request_key="foreign-1", title="外案候选")
    foreign_id = foreign_snapshot.pending_changes[0].id
    assert _codes(lambda: claims.accept_all(db, ClaimAcceptAllInput(
        case_id=case_id, change_ids=ids + [foreign_id]), actor="human",
        entry="local-web")) == ErrorCode.CONFLICT
    db.rollback()
    assert claims.get_claims(db, case_id).pending_count == 3

    # 过期 ID（已确认过的）：整体冲突。
    claims.accept_change(db, ChangeIdInput(case_id=case_id, change_id=ids[0]),
                         actor="human", entry="local-web")
    assert _codes(lambda: claims.accept_all(db, ClaimAcceptAllInput(
        case_id=case_id, change_ids=ids), actor="human",
        entry="local-web")) == ErrorCode.CONFLICT
    db.rollback()
    assert claims.get_claims(db, case_id).pending_count == 2

    # 完整集合：全部确认。
    snapshot = claims.accept_all(db, ClaimAcceptAllInput(
        case_id=case_id, change_ids=ids[1:]), actor="human", entry="local-web")
    assert snapshot.pending_count == 0
    assert {item.title for item in snapshot.items} == {"候选0", "候选1", "候选2"}


def test_accept_writes_a_reconstructable_human_change_and_audit(db, app_engine):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, 1000, title="本金")
    item = snapshot.items[0]
    snapshot = claims.update_item(db, UpdateClaimItemInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision,
        changes=ClaimItemChanges(amount_cents=2000), request_key="acc-1"),
        actor="agent", entry="mcp")
    pending = snapshot.pending_changes[0]
    original_diff = list(pending.diff[0].after.items())
    seq_before = snapshot.seq

    snapshot = claims.accept_change(db, ChangeIdInput(case_id=case_id, change_id=pending.id),
                                    actor="human", entry="local-web")
    record = next(change for change in snapshot.changes if change.id == snapshot.last_change_id)
    assert record.command == "accept_change" and record.actor == "human"
    assert record.seq == seq_before + 1
    entry = record.diff[0]
    assert entry.before["amount_cents"] == 1000 and entry.after["amount_cents"] == 2000
    assert entry.before_rev == item.revision and entry.after_rev == item.revision + 1
    assert record.entry == "local-web"
    # 原候选提案不被改写。
    accepted = next(change for change in snapshot.changes if change.id == pending.id)
    assert list(accepted.diff[0].after.items()) == original_diff
    assert accepted.review_state == "accepted"

    operations = db.execute(text(
        "SELECT operation, count(*) FROM audit_events WHERE case_id = :case_id "
        "GROUP BY operation"), {"case_id": case_id}).all()
    counts = dict(operations)
    assert counts["claims.accept_change"] == 1
    assert counts["claims.update_item"] == 1  # 暂存时的人工侧无额外写入

    # 另一连接读到的实际状态与修订号一致（缓存/提交都成立）。
    other = Session(bind=app_engine)
    fresh = claims.get_claims(other, case_id)
    other.close()
    assert _find(fresh, "本金").amount_cents == 2000
    assert _find(fresh, "本金").revision == item.revision + 1


# --- 5. Agent 幂等 ----------------------------------------------------------

def test_agent_replay_returns_the_original_change_and_rejects_other_operations(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    first = _item(db, case_id, group_id, 100, actor="agent", request_key="idem-1",
                  title="候选")
    original = first.pending_changes[0].id
    assert first.last_change_id == original

    replay = _item(db, case_id, group_id, 100, actor="agent", request_key="idem-1",
                   title="候选")
    assert replay.last_change_id == original
    assert replay.pending_count == 1 and replay.seq == first.seq
    assert len(replay.changes) == len(first.changes)

    # 同一 request_key 换命令、换来源或换 payload 都不能被当成原操作。
    assert _codes(lambda: _group(db, case_id, "另一操作", actor="agent",
                                 request_key="idem-1")) == ErrorCode.CONFLICT
    assert _codes(lambda: _item(db, case_id, group_id, 200, actor="agent",
                                request_key="idem-1", title="候选")) == ErrorCode.CONFLICT
    assert _codes(lambda: _item(db, case_id, group_id, 100, actor="human",
                                request_key="idem-1", title="候选")) == ErrorCode.CONFLICT
    db.rollback()
    assert claims.get_claims(db, case_id).pending_count == 1


def test_agent_update_candidate_replays_and_detects_payload_change(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, 1000, title="本金")
    item = snapshot.items[0]
    payload = UpdateClaimItemInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision,
        changes=ClaimItemChanges(calc_note="按合同第 3 条"), request_key="upd-1")
    first = claims.update_item(db, payload, actor="agent", entry="mcp")
    original = first.pending_changes[0].id
    second = claims.update_item(db, payload, actor="agent", entry="mcp")
    assert second.pending_count == 1 and second.seq == first.seq
    assert second.last_change_id == original

    changed = UpdateClaimItemInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision,
        changes=ClaimItemChanges(calc_note="按合同第 4 条"), request_key="upd-1")
    assert _codes(lambda: claims.update_item(db, changed, actor="agent",
                                             entry="mcp")) == ErrorCode.CONFLICT


def test_concurrent_first_writes_with_the_same_request_key_yield_one_change(db, app_engine):
    case_id = create_case(db)
    db.commit()
    payload = AddClaimGroupInput(case_id=case_id, title="并发组", request_key="conc-1")
    barrier = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def submit() -> None:
        session = Session(bind=app_engine)
        try:
            barrier.wait(5)
            snapshot = claims.add_group(session, payload, actor="agent", entry="mcp")
            with lock:
                results.append(snapshot)
        except BaseException as exc:  # pragma: no cover - 失败时用于诊断
            with lock:
                results.append(exc)
        finally:
            session.close()

    workers = [threading.Thread(target=submit, daemon=True) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(15)
    errors = [row for row in results if isinstance(row, BaseException)]
    assert errors == [], errors
    snapshots = [row for row in results if not isinstance(row, BaseException)]
    assert len(snapshots) == 2
    assert len({snapshot.last_change_id for snapshot in snapshots}) == 1
    final = claims.get_claims(db, case_id)
    assert final.pending_count == 1 and final.seq == 1


# --- 6. 迁移降级只清理 claims 领域 -----------------------------------------
# 见 tests/test_claims_migration.py::test_downgrade_clears_claims_seq_and_audit_only
