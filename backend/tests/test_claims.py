"""诉请金额 P2：整数分口径、分组/金额项命令、CAS、Agent 候选暂存与人工复核、
跨案隔离、归档守卫、审计、独立序号、精确值候选与一致性检查。

真实 PostgreSQL（conftest 重建 legal_workbench_test），全部 TEST-ONLY 合成数据。
金额断言按分，且用 P1 原型的参考场景（1,402,400.00 与二审 42,345.67）核对。
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from legal_workbench import claims, consistency, models, progress
from legal_workbench.errors import ErrorCode, WorkbenchError
from legal_workbench.schemas import (
    AddClaimGroupInput, AddClaimItemInput, ChangeIdInput, ClaimAcceptAllInput,
    ClaimGroupChanges, ClaimGroupRevisionInput, ClaimItemChanges, ClaimItemRevisionInput,
    ReorderClaimGroupInput, ReorderClaimItemInput, UpdateClaimGroupInput,
    UpdateClaimItemInput,
)

from . import factories
from .factories import create_case


def cents(text_amount: str) -> int:
    return int(Decimal(text_amount.replace(",", "")) * 100)


def format_cents(value: int) -> str:
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    return f"{sign}{absolute // 100:,}.{absolute % 100:02d}"


def _group(db, case_id, title="对被告一主诉请", *, currency="CNY", actor="human",
           request_key=None):
    return claims.add_group(db, AddClaimGroupInput(
        case_id=case_id, title=title, currency=currency, request_key=request_key),
        actor=actor, entry="local-web")


def _item(db, case_id, group_id, amount_cents, *, category="principal", title="金额项",
          actor="human", request_key=None, segment_id=None, material_id=None):
    return claims.add_item(db, AddClaimItemInput(
        case_id=case_id, group_id=group_id, title=title, category=category,
        amount_cents=amount_cents, segment_id=segment_id, material_id=material_id,
        request_key=request_key), actor=actor, entry="local-web")


def _find_group(snapshot, title):
    return next(group for group in snapshot.groups if group.title == title)


def _find_item(snapshot, title):
    return next(item for item in snapshot.items if item.title == title)


def _revision(snapshot, group_id):
    return next(group.revision for group in snapshot.groups if group.id == group_id)


def _codes(fn):
    with pytest.raises(WorkbenchError) as exc:
        fn()
    return exc.value.code


def _text_source(db, *, case_id=None, text_body="第一条 合同价款为人民币1,280,000元。",
                 raw="人民币1,280,000元", amount=Decimal("1280000"), currency="CNY",
                 name="TEST-ONLY 购销合同.txt"):
    """Build case → material → parse run/unit → segment → amount exact value.

    运行发布为文件版本的 active run（与真实解析完成后的状态一致）：候选口径只取
    active/current 运行，未发布的运行不会被当作可取用的出处。
    """
    if case_id is None:
        case_id = create_case(db)
    intake_id = factories.create_intake(db, case_id)
    intake_item_id = factories.create_intake_item(
        db, case_id, intake_id, f"虚构/{uuid.uuid4().hex}.txt")
    # 每个合成材料用不同字节，避免同案 sha256 唯一约束冲突。
    object_id = factories.create_stored_object(db, case_id, content=uuid.uuid4().bytes)
    material_id = factories.create_material(db, case_id, intake_item_id, display_name=name)
    file_version_id = factories.create_file_version(db, case_id, material_id, object_id)
    run = models.ParseRun(
        id=uuid.uuid4(), case_id=case_id, file_version_id=file_version_id,
        parser_version="test", config={}, sequence=0, status="complete", coverage={})
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
    db.execute(text("UPDATE file_versions SET active_parse_run_id = :run WHERE id = :version"),
               {"run": run.id, "version": file_version_id})
    return {"case_id": case_id, "material_id": material_id,
            "file_version_id": file_version_id, "run_id": run.id,
            "segment_id": segment.id, "exact_value_id": exact.id, "text": text_body}


# --- integer cents, deduction, per-currency totals ---------------------------

def test_amounts_are_integer_cents_and_deduction_subtracts(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, 1, title="一分钱")
    snapshot = _item(db, case_id, group_id, 30000, category="deduction", title="已付款抵扣")
    assert snapshot.group_subtotals[str(group_id)] == 1 - 30000
    stored = db.execute(text(
        "SELECT amount_cents, pg_typeof(amount_cents)::text FROM claim_items "
        "WHERE case_id = :case_id ORDER BY title"), {"case_id": case_id}).all()
    assert [row[0] for row in stored] == [1, 30000]
    assert all(row[1] == "bigint" for row in stored)
    # 抵扣只存正数，方向由类别决定，不存在双负号。
    assert all(row[0] >= 0 for row in stored)


def test_p1_reference_scenario_toggle_matches_prototype(db):
    """P1 参考场景：1,402,400.00 ＋ 二审 42,345.67，开关后为 1,444,745.67。"""
    case_id = create_case(db, "TEST-ONLY 甲公司诉乙公司合同纠纷案")
    db.commit()
    snapshot = _group(db, case_id, "对被告一主诉请")
    first = snapshot.groups[0]
    for amount, category, title in [
            (cents("1,280,000.00"), "principal", "合同价款（本金）"),
            (cents("86,400.00"), "interest", "逾期利息"),
            (cents("256,000.00"), "liquidated_damages", "违约金"),
            (cents("80,000.00"), "expense", "律师费（一审）"),
            (cents("300,000.00"), "deduction", "已付款抵扣")]:
        snapshot = _item(db, case_id, first.id, amount, category=category, title=title)
    assert snapshot.group_subtotals[str(first.id)] == cents("1,402,400.00")
    assert [entry.total_cents for entry in snapshot.totals_by_currency] == [cents("1,402,400.00")]

    snapshot = _group(db, case_id, "二审变更诉请")
    second = _find_group(snapshot, "二审变更诉请")
    snapshot = _item(db, case_id, second.id, cents("12,345.67"), category="interest",
                     title="一审判决后利息")
    snapshot = _item(db, case_id, second.id, cents("30,000.00"), category="expense",
                     title="二审律师费")
    assert snapshot.group_subtotals[str(second.id)] == cents("42,345.67")

    # 重叠程序默认不计入：手动开关关闭后，全案总计只含一审组。
    snapshot = claims.update_group(db, UpdateClaimGroupInput(
        case_id=case_id, group_id=second.id, expected_revision=second.revision,
        changes=ClaimGroupChanges(include_in_total=False)),
        actor="human", entry="local-web")
    assert [entry.total_cents for entry in snapshot.totals_by_currency] == [cents("1,402,400.00")]
    assert snapshot.excluded_group_count == 1

    # 手动打开开关 → 精确相加，无浮点误差。
    snapshot = claims.update_group(db, UpdateClaimGroupInput(
        case_id=case_id, group_id=second.id,
        expected_revision=_revision(snapshot, second.id),
        changes=ClaimGroupChanges(include_in_total=True)),
        actor="human", entry="local-web")
    assert [entry.total_cents for entry in snapshot.totals_by_currency] == [cents("1,444,745.67")]
    assert format_cents(snapshot.totals_by_currency[0].total_cents) == "1,444,745.67"
    assert snapshot.included_group_count == 2


def test_totals_are_grouped_per_currency_and_excluded_groups_stay_out(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id, "人民币组")
    cny = snapshot.groups[0]
    snapshot = _item(db, case_id, cny.id, 10000)
    snapshot = _group(db, case_id, "美元组", currency="usd")
    usd = _find_group(snapshot, "美元组")
    assert usd.currency == "USD"  # 小写输入归一化为三位大写代码
    snapshot = _item(db, case_id, usd.id, 500)
    totals = {entry.currency: entry.total_cents for entry in snapshot.totals_by_currency}
    assert totals == {"CNY": 10000, "USD": 500}
    # 只显示币种，不换算、不相加。
    assert [entry.currency for entry in snapshot.totals_by_currency] == ["CNY", "USD"]

    snapshot = claims.update_group(db, UpdateClaimGroupInput(
        case_id=case_id, group_id=cny.id, expected_revision=cny.revision,
        changes=ClaimGroupChanges(include_in_total=False)),
        actor="human", entry="local-web")
    assert [entry.currency for entry in snapshot.totals_by_currency] == ["USD"]
    assert _codes(lambda: _group(db, case_id, "坏币种", currency="C1N")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: _group(db, case_id, "坏币种", currency="人民币")) == ErrorCode.INVALID_INPUT


def test_large_amounts_stay_exact_and_overflow_rolls_back(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    big = cents("123,456,789,012.34")
    snapshot = _item(db, case_id, group_id, big, title="结算总额")
    assert _find_item(snapshot, "结算总额").amount_cents == 12345678901234
    assert snapshot.group_subtotals[str(group_id)] == 12345678901234

    # 单项上界：协议层就拒绝超过 JS 安全整数的金额。
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AddClaimItemInput(case_id=case_id, group_id=group_id, title="超界",
                          category="principal", amount_cents=2**53)

    # 合计上界：两项各自合法，但相加超出可精确表示范围 → 整笔回滚。
    half = 2**52
    snapshot = _item(db, case_id, group_id, half, title="大额甲")
    assert _codes(lambda: _item(db, case_id, group_id, half, title="大额乙")) == ErrorCode.INVALID_INPUT
    db.rollback()
    titles = db.execute(text(
        "SELECT title FROM claim_items WHERE case_id = :case_id ORDER BY title"),
        {"case_id": case_id}).scalars().all()
    assert titles == ["大额甲", "结算总额"]
    assert claims.get_claims(db, case_id).group_subtotals[str(group_id)] == big + half


# --- agent candidates are staged until a human accepts ----------------------

def test_agent_item_is_staged_and_not_counted_until_accepted(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, cents("1,402,400.00"))
    before_seq = snapshot.seq

    snapshot = _item(db, case_id, group_id, cents("3,287.67"), category="interest",
                     title="逾期利息（补充）", actor="agent", request_key="agent-1")
    # 暂存：不在明细里，不进小计，只出现在待确认列表。
    assert [item.title for item in snapshot.items] == ["金额项"]
    assert snapshot.group_subtotals[str(group_id)] == cents("1,402,400.00")
    assert snapshot.pending_count == 1
    assert snapshot.seq == before_seq + 1
    pending = snapshot.pending_changes[0]
    assert pending.actor == "agent" and pending.review_state == "pending"
    assert pending.diff[0].after["amount_cents"] == cents("3,287.67")

    snapshot = claims.accept_change(db, ChangeIdInput(case_id=case_id, change_id=pending.id),
                                    actor="human", entry="local-web")
    assert _find_item(snapshot, "逾期利息（补充）").amount_cents == cents("3,287.67")
    assert snapshot.group_subtotals[str(group_id)] == cents("1,405,687.67")
    assert snapshot.pending_count == 0
    accepted = next(change for change in snapshot.changes if change.id == pending.id)
    assert accepted.review_state == "accepted" and accepted.reviewed_at is not None

    operations = db.execute(text(
        "SELECT operation FROM audit_events WHERE case_id = :case_id ORDER BY created_at"),
        {"case_id": case_id}).scalars().all()
    assert "claims.add_item" in operations and "claims.accept_change" in operations


def test_agent_group_candidate_is_staged_then_accepted(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id, "一审")
    snapshot = _group(db, case_id, "二审（Agent 候选）", actor="agent", request_key="g-1")
    assert [group.title for group in snapshot.groups] == ["一审"]
    pending = snapshot.pending_changes[0]
    assert pending.diff[0].target == "group"
    snapshot = claims.accept_change(db, ChangeIdInput(case_id=case_id, change_id=pending.id),
                                    actor="human", entry="local-web")
    assert [group.title for group in snapshot.groups] == ["一审", "二审（Agent 候选）"]
    assert snapshot.groups[1].created_by == "agent"
    assert snapshot.groups[1].updated_by == "human"


def test_revert_discards_candidate_and_is_human_only(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, 1000)
    snapshot = _item(db, case_id, group_id, 2000, actor="agent", request_key="a-2",
                     title="候选")
    pending = snapshot.pending_changes[0]
    assert _codes(lambda: claims.accept_change(
        db, ChangeIdInput(case_id=case_id, change_id=pending.id),
        actor="agent", entry="mcp")) == ErrorCode.FORBIDDEN
    assert _codes(lambda: claims.revert_change(
        db, ChangeIdInput(case_id=case_id, change_id=pending.id),
        actor="agent", entry="mcp")) == ErrorCode.FORBIDDEN

    snapshot = claims.revert_change(db, ChangeIdInput(case_id=case_id, change_id=pending.id),
                                    actor="human", entry="local-web")
    assert snapshot.pending_count == 0
    assert [item.title for item in snapshot.items] == ["金额项"]
    reverted = next(change for change in snapshot.changes if change.id == pending.id)
    assert reverted.review_state == "reverted"
    assert reverted.reverted_by is not None
    assert snapshot.last_change_id == reverted.reverted_by
    # 已撤回的候选不能再确认，也不能重复撤回。
    assert _codes(lambda: claims.accept_change(
        db, ChangeIdInput(case_id=case_id, change_id=pending.id),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: claims.revert_change(
        db, ChangeIdInput(case_id=case_id, change_id=pending.id),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT


def test_agent_requires_request_key_and_replays_idempotently(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    payload = AddClaimItemInput(case_id=case_id, group_id=group_id, title="候选",
                                category="other", amount_cents=100)
    assert _codes(lambda: claims.add_item(db, payload, actor="agent",
                                          entry="mcp")) == ErrorCode.INVALID_INPUT
    first = _item(db, case_id, group_id, 100, actor="agent", request_key="same-key")
    second = _item(db, case_id, group_id, 100, actor="agent", request_key="same-key")
    assert first.pending_count == second.pending_count == 1
    assert second.seq == first.seq  # 重放不新增变更、不推进序号
    # 人工新建同样按 request_key 幂等（业务行已存在时直接返回快照）。
    snapshot = _group(db, case_id, "人工组", request_key="human-key")
    snapshot = _group(db, case_id, "人工组", request_key="human-key")
    assert [group.title for group in snapshot.groups].count("人工组") == 1


def test_agent_cannot_use_human_only_commands(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, 100)
    item = snapshot.items[0]
    assert _codes(lambda: claims.reorder_items(db, ReorderClaimItemInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision,
        direction="up"), actor="agent", entry="mcp")) == ErrorCode.FORBIDDEN
    assert _codes(lambda: claims.remove_item(db, ClaimItemRevisionInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision),
        actor="agent", entry="mcp")) == ErrorCode.FORBIDDEN
    assert _codes(lambda: claims.remove_group(db, ClaimGroupRevisionInput(
        case_id=case_id, group_id=group_id, expected_revision=snapshot.groups[0].revision),
        actor="agent", entry="mcp")) == ErrorCode.FORBIDDEN


def test_accept_all_confirms_only_displayed_ids(db):
    """请求集合必须与当前待确认集合完全一致（逐项验证，缺一项整体冲突）。"""
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    for index in range(3):
        snapshot = _item(db, case_id, group_id, 100 * (index + 1), actor="agent",
                         request_key=f"bulk-{index}", title=f"候选{index}")
    assert snapshot.pending_count == 3
    ids = [change.id for change in snapshot.pending_changes]
    assert _codes(lambda: claims.accept_all(db, ClaimAcceptAllInput(
        case_id=case_id, change_ids=[]), actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: claims.accept_all(db, ClaimAcceptAllInput(
        case_id=case_id), actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: claims.accept_all(db, ClaimAcceptAllInput(
        case_id=case_id, expected_seq=snapshot.seq - 1),
        actor="human", entry="local-web")) == ErrorCode.CONFLICT
    # 少一项：整体冲突，一项都不确认（原先会静默确认匹配到的子集）。
    assert _codes(lambda: claims.accept_all(db, ClaimAcceptAllInput(
        case_id=case_id, change_ids=ids[:2]), actor="human",
        entry="local-web")) == ErrorCode.CONFLICT
    db.rollback()
    assert claims.get_claims(db, case_id).pending_count == 3
    snapshot = claims.accept_all(db, ClaimAcceptAllInput(case_id=case_id, change_ids=ids),
                                 actor="human", entry="local-web")
    assert [item.title for item in snapshot.items] == ["候选0", "候选1", "候选2"]
    assert snapshot.pending_count == 0


# --- CAS, conflicts and atomicity -------------------------------------------

def test_update_item_cas_conflict(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, 100)
    item = snapshot.items[0]
    payload = UpdateClaimItemInput(case_id=case_id, item_id=item.id,
                                   expected_revision=item.revision,
                                   changes=ClaimItemChanges(amount_cents=200))
    claims.update_item(db, payload, actor="human", entry="local-web")
    assert _codes(lambda: claims.update_item(db, payload, actor="human",
                                             entry="local-web")) == ErrorCode.CONFLICT
    assert claims.get_claims(db, case_id).items[0].amount_cents == 200


def test_accept_conflict_is_atomic_and_keeps_candidate_pending(db):
    """行在暂存后被带外修改（CAS 失配）→ 整笔确认回滚，候选仍待确认。"""
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, 1000, title="本金")
    item = snapshot.items[0]
    snapshot = claims.update_item(db, UpdateClaimItemInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision,
        changes=ClaimItemChanges(amount_cents=2000), request_key="stale-1"),
        actor="agent", entry="mcp")
    pending = snapshot.pending_changes[0]
    # 带外写入（模拟旧客户端/直接 SQL）推进修订号，确认必须冲突而非覆盖。
    db.execute(text("UPDATE claim_items SET amount_cents = 1500, revision = revision + 1 "
                    "WHERE id = :id"), {"id": item.id})
    db.commit()
    assert _codes(lambda: claims.accept_change(
        db, ChangeIdInput(case_id=case_id, change_id=pending.id),
        actor="human", entry="local-web")) == ErrorCode.CONFLICT
    db.rollback()
    snapshot = claims.get_claims(db, case_id)
    assert snapshot.items[0].amount_cents == 1500
    assert snapshot.pending_count == 1
    assert snapshot.pending_changes[0].review_state == "pending"


def test_pending_candidate_blocks_human_edit_and_group_removal(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, 1000, title="本金")
    item = snapshot.items[0]
    snapshot = claims.update_item(db, UpdateClaimItemInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision,
        changes=ClaimItemChanges(amount_cents=2000), request_key="pending-1"),
        actor="agent", entry="mcp")
    assert _codes(lambda: claims.update_item(db, UpdateClaimItemInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision,
        changes=ClaimItemChanges(title="改名")), actor="human",
        entry="local-web")) == ErrorCode.CONFLICT
    assert _codes(lambda: claims.remove_item(db, ClaimItemRevisionInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision),
        actor="human", entry="local-web")) == ErrorCode.CONFLICT
    assert _codes(lambda: claims.remove_group(db, ClaimGroupRevisionInput(
        case_id=case_id, group_id=group_id, expected_revision=snapshot.groups[0].revision),
        actor="human", entry="local-web")) == ErrorCode.CONFLICT


def test_accept_add_item_conflicts_when_group_removed(db):
    """暂存后分组被删（带外/旧客户端）→ 确认冲突，不写入孤儿金额项。"""
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id, "临时组")
    group = snapshot.groups[0]
    snapshot = _item(db, case_id, group.id, 500, actor="agent", request_key="g-add")
    pending = snapshot.pending_changes[0]
    db.execute(text("UPDATE claim_groups SET removed_at = now(), revision = revision + 1 "
                    "WHERE id = :id"), {"id": group.id})
    db.commit()
    assert _codes(lambda: claims.accept_change(
        db, ChangeIdInput(case_id=case_id, change_id=pending.id),
        actor="human", entry="local-web")) == ErrorCode.CONFLICT
    db.rollback()
    snapshot = claims.get_claims(db, case_id)
    assert snapshot.items == []
    assert snapshot.pending_count == 1


def test_remove_group_cascades_items_and_restore_item_reorders(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id, "第一组")
    first = snapshot.groups[0]
    snapshot = _item(db, case_id, first.id, 100, title="甲")
    snapshot = _item(db, case_id, first.id, 200, title="乙")
    assert snapshot.group_subtotals[str(first.id)] == 300
    snapshot = claims.remove_item(db, ClaimItemRevisionInput(
        case_id=case_id, item_id=_find_item(snapshot, "甲").id,
        expected_revision=_find_item(snapshot, "甲").revision), actor="human", entry="local-web")
    assert snapshot.group_subtotals[str(first.id)] == 200
    assert _find_item(snapshot, "甲").removed_at is not None
    snapshot = claims.restore_item(db, ClaimItemRevisionInput(
        case_id=case_id, item_id=_find_item(snapshot, "甲").id,
        expected_revision=_find_item(snapshot, "甲").revision), actor="human", entry="local-web")
    assert snapshot.group_subtotals[str(first.id)] == 300
    # 恢复后追加到末尾：顺序为 乙、甲。
    active = [item.title for item in snapshot.items if item.removed_at is None]
    assert active == ["乙", "甲"]

    snapshot = claims.reorder_items(db, ReorderClaimItemInput(
        case_id=case_id, item_id=_find_item(snapshot, "甲").id,
        expected_revision=_find_item(snapshot, "甲").revision, direction="up"),
        actor="human", entry="local-web")
    assert [item.title for item in snapshot.items if item.removed_at is None] == ["甲", "乙"]

    snapshot = claims.remove_group(db, ClaimGroupRevisionInput(
        case_id=case_id, group_id=first.id,
        expected_revision=_revision(snapshot, first.id)), actor="human", entry="local-web")
    assert _find_group(snapshot, "第一组").removed_at is not None
    assert all(item.removed_at is not None for item in snapshot.items)
    assert snapshot.totals_by_currency == []
    # 分组已删，金额项不能单独恢复。
    assert _codes(lambda: claims.restore_item(db, ClaimItemRevisionInput(
        case_id=case_id, item_id=_find_item(snapshot, "甲").id,
        expected_revision=_find_item(snapshot, "甲").revision),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT


def test_reorder_groups_swaps_positions_and_cas(db):
    case_id = create_case(db)
    db.commit()
    _group(db, case_id, "第一")
    snapshot = _group(db, case_id, "第二")
    second = _find_group(snapshot, "第二")
    snapshot = claims.reorder_groups(db, ReorderClaimGroupInput(
        case_id=case_id, group_id=second.id, expected_revision=second.revision,
        direction="up"), actor="human", entry="local-web")
    assert [group.title for group in snapshot.groups] == ["第二", "第一"]
    assert _codes(lambda: claims.reorder_groups(db, ReorderClaimGroupInput(
        case_id=case_id, group_id=second.id, expected_revision=second.revision,
        direction="up"), actor="human", entry="local-web")) == ErrorCode.CONFLICT
    assert _codes(lambda: claims.reorder_groups(db, ReorderClaimGroupInput(
        case_id=case_id, group_id=second.id,
        expected_revision=_revision(snapshot, second.id),
        direction="up"), actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT


# --- case isolation, archive, audit, sequence -------------------------------

def test_cross_case_references_rejected_by_service_and_database(db):
    case_a = create_case(db, "TEST-ONLY A")
    case_b = create_case(db, "TEST-ONLY B")
    db.commit()
    snapshot = _group(db, case_b, "B 组")
    group_b = snapshot.groups[0]
    # 服务层：用 A 案身份操作 B 案分组 → case_mismatch。
    assert _codes(lambda: claims.update_group(db, UpdateClaimGroupInput(
        case_id=case_a, group_id=group_b.id, expected_revision=group_b.revision,
        changes=ClaimGroupChanges(title="越权")), actor="human",
        entry="local-web")) == ErrorCode.CASE_MISMATCH
    assert _codes(lambda: _item(db, case_a, group_b.id, 100)) == ErrorCode.CASE_MISMATCH
    # 数据库：组合外键直接拒绝跨案金额项。
    db.add(models.ClaimItem(
        id=uuid.uuid4(), case_id=case_a, group_id=group_b.id, title="跨案",
        category="principal", amount_cents=1, position=0, created_by="human",
        updated_by="human", revision=0))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    # 跨案出处同样被服务层与数据库两层拒绝。
    source_a = _text_source(db, case_id=case_a)
    source_b = _text_source(db, case_id=case_b, name="TEST-ONLY B 合同.txt")
    db.commit()
    snapshot = _group(db, case_b, "B 组二")
    group_b2 = _find_group(snapshot, "B 组二")
    assert _codes(lambda: _item(db, case_b, group_b2.id, 100,
                                segment_id=source_a["segment_id"])) == ErrorCode.CASE_MISMATCH
    db.add(models.ClaimItem(
        id=uuid.uuid4(), case_id=case_b, group_id=group_b2.id, title="跨案出处",
        category="principal", amount_cents=1, position=0, created_by="human",
        updated_by="human", revision=0, segment_id=source_a["segment_id"]))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    assert source_b["segment_id"] != source_a["segment_id"]


def test_archived_case_is_read_only_for_every_command(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, 100)
    item = snapshot.items[0]
    snapshot = _item(db, case_id, group_id, 200, actor="agent", request_key="arch-1",
                     title="候选")
    pending = snapshot.pending_changes[0]
    db.execute(text("UPDATE cases SET archived_at = now() WHERE id = :id"), {"id": case_id})
    db.commit()

    assert _codes(lambda: _group(db, case_id, "归档后新建")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: claims.update_group(db, UpdateClaimGroupInput(
        case_id=case_id, group_id=group_id, expected_revision=_revision(snapshot, group_id),
        changes=ClaimGroupChanges(title="改名")), actor="human",
        entry="local-web")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: claims.update_item(db, UpdateClaimItemInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision,
        changes=ClaimItemChanges(amount_cents=300)), actor="human",
        entry="local-web")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: claims.remove_item(db, ClaimItemRevisionInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: claims.accept_change(
        db, ChangeIdInput(case_id=case_id, change_id=pending.id),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    assert _codes(lambda: claims.revert_change(
        db, ChangeIdInput(case_id=case_id, change_id=pending.id),
        actor="human", entry="local-web")) == ErrorCode.INVALID_INPUT
    db.rollback()
    assert claims.get_claims(db, case_id).items[0].amount_cents == 100


def test_claim_sequence_is_independent_from_progress(db):
    case_id = create_case(db)
    db.commit()
    progress_before = progress.get_progress(db, case_id).seq
    snapshot = _group(db, case_id)
    snapshot = _item(db, case_id, snapshot.groups[0].id, 100)
    assert snapshot.seq == 2
    assert progress.get_progress(db, case_id).seq == progress_before
    rows = db.execute(text(
        "SELECT domain, seq FROM case_change_seq WHERE case_id = :case_id ORDER BY domain"),
        {"case_id": case_id}).all()
    assert ("claims", 2) in [(row[0], row[1]) for row in rows]
    seqs = progress.get_change_seqs(db, case_id).seqs
    assert seqs["claims"] == 2 and seqs.get("progress", 0) == progress_before


def test_every_write_records_audit_and_change(db):
    case_id = create_case(db)
    db.commit()
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, 100, title="甲")
    snapshot = _item(db, case_id, group_id, 200, title="乙")
    item = _find_item(snapshot, "乙")
    claims.reorder_items(db, ReorderClaimItemInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision,
        direction="up"), actor="human", entry="local-web")
    snapshot = claims.get_claims(db, case_id)
    item = _find_item(snapshot, "乙")
    claims.update_item(db, UpdateClaimItemInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision,
        changes=ClaimItemChanges(calc_note="说明")), actor="human", entry="local-web")
    snapshot = claims.get_claims(db, case_id)
    item = _find_item(snapshot, "乙")
    claims.remove_item(db, ClaimItemRevisionInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision),
        actor="human", entry="local-web")
    snapshot = claims.get_claims(db, case_id)
    item = _find_item(snapshot, "乙")
    claims.restore_item(db, ClaimItemRevisionInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision),
        actor="human", entry="local-web")
    snapshot = claims.get_claims(db, case_id)
    commands = [change.command for change in snapshot.changes]
    assert commands == ["add_group", "add_item", "add_item", "reorder_items",
                        "update_item", "remove_item", "restore_item"]
    assert [change.seq for change in snapshot.changes] == list(range(1, 8))
    operations = db.execute(text(
        "SELECT operation, count(*) FROM audit_events WHERE case_id = :case_id "
        "GROUP BY operation"), {"case_id": case_id}).all()
    counts = dict(operations)
    assert counts["claims.add_group"] == 1
    assert counts["claims.add_item"] == 2
    assert counts["claims.reorder_items"] == 1
    assert counts["claims.update_item"] == 1
    assert counts["claims.remove_item"] == 1
    assert counts["claims.restore_item"] == 1


# --- exact-value candidates ------------------------------------------------

def test_candidates_come_only_from_this_case_exact_amounts(db):
    case_a = create_case(db, "TEST-ONLY 候选 A")
    case_b = create_case(db, "TEST-ONLY 候选 B")
    db.commit()
    source_a = _text_source(db, case_id=case_a)
    _text_source(db, case_id=case_b, raw="人民币99,999元", amount=Decimal("99999"),
                 text_body="B 案材料：人民币99,999元。", name="TEST-ONLY B.txt")
    # 非金额精确值不应出现在候选里。
    db.add(models.ExactValue(
        id=uuid.uuid4(), case_id=case_a, segment_id=source_a["segment_id"],
        kind="identifier", raw_text="（2026）测1号", canonical_value={}, span={}))
    db.flush()
    result = claims.list_amount_candidates(db, case_a)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.segment_id == source_a["segment_id"]
    assert candidate.amount_cents == cents("1,280,000.00")
    assert candidate.currency == "CNY"
    assert candidate.material_id is not None
    assert candidate.page == 3
    assert "1,280,000" in candidate.excerpt
    assert claims.list_amount_candidates(db, case_a, query="99,999").candidates == []
    assert len(claims.list_amount_candidates(db, case_b, query="99,999").candidates) == 1


def test_candidate_keeps_original_amount_after_reparse_and_survives_segment_removal(db):
    case_id = create_case(db)
    db.commit()
    source = _text_source(db, case_id=case_id)
    candidate = claims.list_amount_candidates(db, case_id).candidates[0]
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, candidate.amount_cents,
                     segment_id=candidate.segment_id, title="合同价款")
    item = snapshot.items[0]
    assert item.segment_id == candidate.segment_id

    # 重新解析产生新的运行与片段，原引用与金额保持原值。
    second = _text_source(db, case_id=case_id, text_body="重解析：人民币1,300,000元。",
                          raw="人民币1,300,000元", amount=Decimal("1300000"),
                          name="TEST-ONLY 购销合同（重解析）.txt")
    db.commit()
    # 候选按 active/current 口径：两份材料的当前运行各给出一条。
    assert len(claims.list_amount_candidates(db, case_id).candidates) == 2
    snapshot = claims.get_claims(db, case_id)
    item = _find_item(snapshot, "合同价款")
    assert item.amount_cents == candidate.amount_cents
    assert item.segment_id == candidate.segment_id

    # 引用不可变：删除仍被金额项引用的片段必须由数据库拒绝，出处不会静默消失。
    db.execute(text("DELETE FROM exact_values WHERE segment_id = :sid"),
               {"sid": candidate.segment_id})
    with pytest.raises(IntegrityError) as exc:
        db.execute(text("DELETE FROM segments WHERE id = :sid"), {"sid": candidate.segment_id})
    assert "fk_claim_items_segment" in str(exc.value)
    db.rollback()
    db.expire_all()
    item = _find_item(claims.get_claims(db, case_id), "合同价款")
    assert item.amount_cents == cents("1,280,000.00")
    assert item.segment_id == candidate.segment_id
    assert second["segment_id"] is not None


def test_candidate_with_sub_cent_precision_requires_manual_entry(db):
    case_id = create_case(db)
    db.commit()
    _text_source(db, case_id=case_id, text_body="利息人民币1.234元。", raw="人民币1.234元",
                 amount=Decimal("1.234"), name="TEST-ONLY 精度.txt")
    db.commit()
    candidate = claims.list_amount_candidates(db, case_id).candidates[0]
    assert candidate.amount_cents is None


# --- consistency ------------------------------------------------------------

def test_consistency_is_clean_and_detects_claim_corruption(db, store):
    case_id = create_case(db)
    db.commit()
    source = _text_source(db, case_id=case_id)
    snapshot = _group(db, case_id)
    group_id = snapshot.groups[0].id
    snapshot = _item(db, case_id, group_id, 1000, title="甲", segment_id=source["segment_id"])
    snapshot = _item(db, case_id, group_id, 2000, title="乙")
    db.commit()
    report = consistency.check_consistency(db, store, case_id=case_id)
    claim_issues = [issue for issue in report.issues if issue.kind.startswith("claims_")]
    assert claim_issues == []
    assert report.checked_claim_groups == 1
    assert report.checked_claim_items == 2
    assert report.to_dict()["checked"]["claim_items"] == 2

    # 位置重复：数据库没有唯一约束，一致性检查必须报出。
    db.execute(text("UPDATE claim_items SET position = 0 WHERE case_id = :case_id"),
               {"case_id": case_id})
    db.commit()
    report = consistency.check_consistency(db, store, case_id=case_id)
    assert "claims_duplicate_item_position" in [issue.kind for issue in report.issues]

    # 待确认候选的目标被软删：确认时只能冲突，检查应提前报出。
    db.execute(text("UPDATE claim_items SET position = 1 WHERE title = '乙'"))
    db.commit()
    snapshot = claims.get_claims(db, case_id)
    item = _find_item(snapshot, "乙")
    claims.update_item(db, UpdateClaimItemInput(
        case_id=case_id, item_id=item.id, expected_revision=item.revision,
        changes=ClaimItemChanges(amount_cents=2500), request_key="stale-2"),
        actor="agent", entry="mcp")
    db.execute(text("UPDATE claim_items SET removed_at = now() WHERE id = :id"),
               {"id": item.id})
    db.commit()
    report = consistency.check_consistency(db, store, case_id=case_id)
    assert "claims_stale_pending_change" in [issue.kind for issue in report.issues]
