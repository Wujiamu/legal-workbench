import { describe, expect, it } from 'vitest';
import { buildClaimsFixture, FIXTURE_CASE_ID } from './fixtures';
import { createClaimExecutor } from './localExecutor';
import type { ClaimExecutor, ClaimSnapshot } from './types';

function setup(): { executor: ClaimExecutor; snapshot: ClaimSnapshot } {
  const executor = buildClaimsFixture();
  return { executor, snapshot: executor.getSnapshot() };
}

function groupId(snapshot: ClaimSnapshot, title: string): string {
  const group = snapshot.groups.find((entry) => entry.title === title);
  if (!group) throw new Error(`缺少分组：${title}`);
  return group.id;
}

describe('基线（TEST-ONLY 两组）', () => {
  it('两组、组内小计、全案总计与默认不计入的二审组', () => {
    const { snapshot } = setup();
    expect(snapshot.case_id).toBe(FIXTURE_CASE_ID);
    expect(snapshot.groups.map((group) => group.title)).toEqual(['对被告一主诉请', '二审变更诉请']);
    const first = groupId(snapshot, '对被告一主诉请');
    const second = groupId(snapshot, '二审变更诉请');
    expect(snapshot.group_subtotals[first]).toBe(140240000);
    expect(snapshot.group_subtotals[second]).toBe(4234567);
    expect(snapshot.totals_by_currency).toEqual([
      { currency: 'CNY', total_cents: 140240000, included_group_ids: [first] },
    ]);
    expect(snapshot.pending_count).toBe(1);
    expect(snapshot.excluded_group_count).toBe(1);
    expect(snapshot.groups.find((group) => group.id === second)?.include_in_total).toBe(false);
  });

  it('Agent 待确认项暂存，不进入明细与小计', () => {
    const { snapshot } = setup();
    const first = groupId(snapshot, '对被告一主诉请');
    expect(snapshot.items.filter((item) => item.group_id === first)).toHaveLength(5);
    expect(snapshot.pending_changes).toHaveLength(1);
    expect(snapshot.pending_changes[0].after.title).toBe('逾期利息（补充至 2026-11-20）');
    expect(snapshot.pending_changes[0].after.amount_cents).toBe(328767);
    expect(snapshot.items.some((item) => item.title === '逾期利息（补充至 2026-11-20）')).toBe(false);
  });
});

describe('分组增改删排', () => {
  it('新建、重命名、调序、删除分组', () => {
    const { executor, snapshot } = setup();
    const created = executor.human({ type: 'add_group', title: '对被告二诉请', currency: 'CNY', note: '连带责任' });
    expect(created.ok).toBe(true);
    expect(created.snapshot!.groups).toHaveLength(3);
    const id = groupId(created.snapshot!, '对被告二诉请');

    const renamed = executor.human({ type: 'update_group', group_id: id, title: '对被告二连带诉请' });
    expect(renamed.ok).toBe(true);
    expect(renamed.snapshot!.groups.map((group) => group.title)).toContain('对被告二连带诉请');

    const moved = executor.human({ type: 'reorder_groups', group_id: id, direction: 'up' });
    expect(moved.ok).toBe(true);
    expect(moved.snapshot!.groups.map((group) => group.title)[1]).toBe('对被告二连带诉请');

    const removed = executor.human({ type: 'remove_group', group_id: id });
    expect(removed.ok).toBe(true);
    expect(removed.snapshot!.groups.map((group) => group.title)).toEqual(['对被告一主诉请', '二审变更诉请']);
    expect(executor.human({ type: 'add_item', group_id: id, title: 'x', category: 'other', amount: '1' }).ok).toBe(false);
    expect(snapshot.groups).toHaveLength(2);
  });

  it('首尾调序被拒', () => {
    const { executor, snapshot } = setup();
    const first = snapshot.groups[0];
    const last = snapshot.groups[1];
    expect(executor.human({ type: 'reorder_groups', group_id: first.id, direction: 'up' }).ok).toBe(false);
    expect(executor.human({ type: 'reorder_groups', group_id: last.id, direction: 'down' }).ok).toBe(false);
  });
});

describe('金额项增改删排与整数分', () => {
  it('新增本金与抵扣，方向正确', () => {
    const { executor, snapshot } = setup();
    const group = snapshot.groups[0];
    const before = snapshot.group_subtotals[group.id];
    const added = executor.human({
      type: 'add_item', group_id: group.id, title: '补充本金', category: 'principal', amount: '100.00',
    });
    expect(added.ok).toBe(true);
    expect(added.snapshot!.group_subtotals[group.id]).toBe(before + 10000);

    const deducted = executor.human({
      type: 'add_item', group_id: group.id, title: '补充抵扣', category: 'deduction', amount: '30.00',
    });
    expect(deducted.snapshot!.group_subtotals[group.id]).toBe(before + 10000 - 3000);
  });

  it('高额精度逐分累加', () => {
    const { executor, snapshot } = setup();
    const group = snapshot.groups[0];
    const before = snapshot.group_subtotals[group.id];
    const high = executor.human({
      type: 'add_item', group_id: group.id, title: '高额结算', category: 'other',
      amount: '123,456,789,012.34',
    });
    expect(high.ok).toBe(true);
    expect(high.snapshot!.group_subtotals[group.id]).toBe(before + 12345678901234);
    const penny = executor.human({
      type: 'add_item', group_id: group.id, title: '一分钱', category: 'other', amount: '0.01',
    });
    expect(penny.snapshot!.group_subtotals[group.id]).toBe(before + 12345678901234 + 1);
  });

  it('修改金额项后小计更新；删除后移除', () => {
    const { executor, snapshot } = setup();
    const group = snapshot.groups[0];
    const lawyer = snapshot.items.find((item) => item.title === '律师费（一审）')!;
    const updated = executor.human({ type: 'update_item', item_id: lawyer.id, amount: '90000' });
    expect(updated.ok).toBe(true);
    expect(updated.snapshot!.group_subtotals[group.id]).toBe(140240000 + 1000000);
    const removed = executor.human({ type: 'remove_item', item_id: lawyer.id });
    expect(removed.ok).toBe(true);
    expect(removed.snapshot!.items.some((item) => item.id === lawyer.id)).toBe(false);
    expect(removed.snapshot!.group_subtotals[group.id]).toBe(140240000 - 8000000);
  });

  it('组内调序改变真实顺序', () => {
    const { executor, snapshot } = setup();
    const group = snapshot.groups[0];
    const titles = () => executor.getSnapshot().items
      .filter((item) => item.group_id === group.id).map((item) => item.title);
    const firstTitle = titles()[0];
    const secondTitle = titles()[1];
    const moved = executor.human({
      type: 'reorder_items',
      item_id: snapshot.items.find((item) => item.title === secondTitle)!.id,
      direction: 'up',
    });
    expect(moved.ok).toBe(true);
    expect(titles().slice(0, 2)).toEqual([secondTitle, firstTitle]);
    expect(executor.human({
      type: 'reorder_items',
      item_id: snapshot.items.find((item) => item.title === firstTitle)!.id,
      direction: 'up',
    }).ok).toBe(true);
  });
});

describe('按币种全案总计与计入开关', () => {
  it('勾选二审组后总计增加；取消后恢复', () => {
    const { executor, snapshot } = setup();
    const second = snapshot.groups[1];
    const included = executor.human({
      type: 'update_group', group_id: second.id, include_in_total: true,
    });
    expect(included.ok).toBe(true);
    expect(included.snapshot!.totals_by_currency[0].total_cents).toBe(144474567);
    expect(included.snapshot!.excluded_group_count).toBe(0);
    const excluded = executor.human({
      type: 'update_group', group_id: second.id, include_in_total: false,
    });
    expect(excluded.snapshot!.totals_by_currency[0].total_cents).toBe(140240000);
  });

  it('多币种分别汇总，不混加', () => {
    const { executor, snapshot } = setup();
    const second = snapshot.groups[1];
    const changed = executor.human({
      type: 'update_group', group_id: second.id, currency: 'USD', include_in_total: true,
    });
    expect(changed.ok).toBe(true);
    expect(changed.snapshot!.totals_by_currency).toEqual([
      { currency: 'CNY', total_cents: 140240000, included_group_ids: [snapshot.groups[0].id] },
      { currency: 'USD', total_cents: 4234567, included_group_ids: [second.id] },
    ]);
  });
});

describe('Agent 待确认与撤回', () => {
  it('Agent 提交新增：待确认、确认后入账并变绿', () => {
    const { executor, snapshot } = setup();
    const group = snapshot.groups[0];
    const submitted = executor.agent({
      type: 'add_item', group_id: group.id, title: 'Agent 新利息', category: 'interest', amount: '1,000.00',
    });
    expect(submitted.ok).toBe(true);
    expect(submitted.snapshot!.pending_count).toBe(2);
    expect(submitted.snapshot!.group_subtotals[group.id]).toBe(140240000);
    const change = submitted.snapshot!.pending_changes.find((entry) => entry.after.title === 'Agent 新利息')!;
    expect(change.after.amount_cents).toBe(100000);

    const accepted = executor.human({ type: 'accept_change', change_id: change.id });
    expect(accepted.ok).toBe(true);
    expect(accepted.snapshot!.pending_count).toBe(1);
    expect(accepted.snapshot!.group_subtotals[group.id]).toBe(140340000);
    const item = accepted.snapshot!.items.find((entry) => entry.title === 'Agent 新利息')!;
    expect(item.review_state).toBe('accepted');
    expect(item.origin).toBe('agent');
  });

  it('Agent 提交新增后撤回：明细与小计不变', () => {
    const { executor, snapshot } = setup();
    const group = snapshot.groups[0];
    const submitted = executor.agent({
      type: 'add_item', group_id: group.id, title: 'Agent 临时项', category: 'other', amount: '9.99',
    });
    const change = submitted.snapshot!.pending_changes.find((entry) => entry.after.title === 'Agent 临时项')!;
    const reverted = executor.human({ type: 'revert_change', change_id: change.id });
    expect(reverted.ok).toBe(true);
    expect(reverted.snapshot!.pending_count).toBe(1);
    expect(reverted.snapshot!.items.some((item) => item.title === 'Agent 临时项')).toBe(false);
    expect(reverted.snapshot!.group_subtotals[group.id]).toBe(140240000);
    expect(reverted.snapshot!.changes.some((entry) => entry.review_state === 'reverted')).toBe(true);
  });

  it('Agent 修改：确认前原值不变，确认后生效，撤回则还原', () => {
    const { executor, snapshot } = setup();
    const group = snapshot.groups[0];
    const lawyer = snapshot.items.find((item) => item.title === '律师费（一审）')!;
    const submitted = executor.agent({ type: 'update_item', item_id: lawyer.id, amount: '88000.00' });
    expect(submitted.ok).toBe(true);
    expect(submitted.snapshot!.items.find((item) => item.id === lawyer.id)!.amount_cents).toBe(8000000);
    expect(submitted.snapshot!.group_subtotals[group.id]).toBe(140240000);
    const change = submitted.snapshot!.pending_changes.find((entry) => entry.command === 'update_item')!;
    expect(change.before!.amount_cents).toBe(8000000);
    expect(change.after.amount_cents).toBe(8800000);

    const accepted = executor.human({ type: 'accept_change', change_id: change.id });
    expect(accepted.snapshot!.items.find((item) => item.id === lawyer.id)!.amount_cents).toBe(8800000);
    expect(accepted.snapshot!.group_subtotals[group.id]).toBe(141040000);

    const fresh = setup();
    const freshLawyer = fresh.snapshot.items.find((item) => item.title === '律师费（一审）')!;
    const pending = fresh.executor.agent({ type: 'update_item', item_id: freshLawyer.id, amount: '88000.00' });
    const pendingChange = pending.snapshot!.pending_changes.find((entry) => entry.command === 'update_item')!;
    const reverted = fresh.executor.human({ type: 'revert_change', change_id: pendingChange.id });
    expect(reverted.snapshot!.items.find((item) => item.id === freshLawyer.id)!.amount_cents).toBe(8000000);
    expect(reverted.snapshot!.group_subtotals[fresh.snapshot.groups[0].id]).toBe(140240000);
  });

  it('确认与撤回仅限人工；Agent 不能改分组', () => {
    const { executor, snapshot } = setup();
    const change = snapshot.pending_changes[0];
    expect(executor.agent({ type: 'accept_change', change_id: change.id })).toMatchObject({
      ok: false, code: 'forbidden',
    });
    expect(executor.agent({ type: 'revert_change', change_id: change.id })).toMatchObject({
      ok: false, code: 'forbidden',
    });
    expect(executor.agent({ type: 'add_group', title: 'x', currency: 'CNY' })).toMatchObject({
      ok: false, code: 'forbidden',
    });
  });

  it('同一金额项的重复待确认修改被拒；人工编辑需先处理待确认', () => {
    const { executor, snapshot } = setup();
    const lawyer = snapshot.items.find((item) => item.title === '律师费（一审）')!;
    expect(executor.agent({ type: 'update_item', item_id: lawyer.id, amount: '88000' }).ok).toBe(true);
    expect(executor.agent({ type: 'update_item', item_id: lawyer.id, amount: '99000' })).toMatchObject({
      ok: false, code: 'conflict',
    });
    expect(executor.human({ type: 'update_item', item_id: lawyer.id, amount: '99000' })).toMatchObject({
      ok: false, code: 'conflict',
    });
    expect(executor.human({ type: 'remove_item', item_id: lawyer.id })).toMatchObject({
      ok: false, code: 'conflict',
    });
  });
});

describe('校验与隔离', () => {
  it('名称、金额、说明长度与未知对象', () => {
    const { executor, snapshot } = setup();
    const group = snapshot.groups[0];
    expect(executor.human({
      type: 'add_item', group_id: group.id, title: '  ', category: 'other', amount: '1',
    }).ok).toBe(false);
    expect(executor.human({
      type: 'add_item', group_id: group.id, title: 'x', category: 'other', amount: '12.345',
    })).toMatchObject({ ok: false, code: 'validation' });
    expect(executor.human({
      type: 'add_item', group_id: group.id, title: 'x', category: 'other', amount: '1',
      calc_note: 'x'.repeat(2001),
    }).ok).toBe(false);
    expect(executor.human({
      type: 'add_item', group_id: group.id, title: 'x'.repeat(201), category: 'other', amount: '1',
    }).ok).toBe(false);
    expect(executor.human({
      type: 'add_item', group_id: 'missing', title: 'x', category: 'other', amount: '1',
    })).toMatchObject({ ok: false, code: 'not_found' });
    expect(executor.human({ type: 'update_item', item_id: 'missing', amount: '1' }))
      .toMatchObject({ ok: false, code: 'not_found' });
  });

  it('快照与内部状态隔离，修改返回值不影响执行器', () => {
    const { executor } = setup();
    const snapshot = executor.getSnapshot();
    snapshot.items[0].amount_cents = 1;
    snapshot.groups[0].title = '被篡改';
    const fresh = executor.getSnapshot();
    expect(fresh.items[0].amount_cents).not.toBe(1);
    expect(fresh.groups[0].title).not.toBe('被篡改');
  });
});

describe('整数分溢出：受控失败并保留写前状态', () => {
  const MAX_CENTS = 9007199254740991; // 90071992547409.91 元，安全整数分上界

  function upperBoundExecutor(): { executor: ClaimExecutor; groupId: string } {
    const executor = createClaimExecutor({ caseId: 'case-overflow', caseTitle: 'TEST-ONLY 溢出' });
    const created = executor.human({ type: 'add_group', title: '上界分组', currency: 'CNY' });
    expect(created.ok).toBe(true);
    const groupId = created.snapshot!.groups[0].id;
    const principal = executor.human({
      type: 'add_item', group_id: groupId, title: '本金', category: 'principal',
      amount: '90071992547409.91',
    });
    expect(principal.ok).toBe(true);
    expect(principal.snapshot!.group_subtotals[groupId]).toBe(MAX_CENTS);
    return { executor, groupId };
  }

  it('本金 90 万亿元再加 0.01：返回校验失败，明细、seq、总计不变且快照仍可用', () => {
    const { executor, groupId } = upperBoundExecutor();
    const before = executor.getSnapshot();

    const overflow = executor.human({
      type: 'add_item', group_id: groupId, title: '利息', category: 'interest', amount: '0.01',
    });
    expect(overflow.ok).toBe(false);
    expect(overflow.code).toBe('validation');
    expect(overflow.message).toContain('超出可精确表示的范围');
    expect(overflow.message).toContain('本次修改未保存');
    expect(overflow.snapshot).toBeUndefined();

    const after = executor.getSnapshot();
    expect(after.items.map((item) => item.title)).toEqual(['本金']);
    expect(after.group_subtotals[groupId]).toBe(MAX_CENTS);
    expect(after.seq).toBe(before.seq);
    expect(after.changes).toHaveLength(before.changes.length);
    expect(after.pending_count).toBe(before.pending_count);
    expect(after.totals_by_currency).toEqual(before.totals_by_currency);

    // 后续正常命令仍可用，说明状态没有被半写入：把上界分组移出总计后，新分组的一分钱可正常计入
    expect(executor.human({
      type: 'update_group', group_id: groupId, include_in_total: false,
    }).ok).toBe(true);
    const pennyInNewGroup = executor.human({ type: 'add_group', title: '一分钱分组', currency: 'CNY' });
    const newGroupId = pennyInNewGroup.snapshot!.groups[1].id;
    const added = executor.human({
      type: 'add_item', group_id: newGroupId, title: '一分钱', category: 'principal', amount: '0.01',
    });
    expect(added.ok).toBe(true);
    expect(added.snapshot!.group_subtotals[newGroupId]).toBe(1);
    expect(added.snapshot!.group_subtotals[groupId]).toBe(MAX_CENTS);
  });

  it('Agent 待确认项确认后越界：确认受控失败，待确认、seq 与明细不变', () => {
    const { executor, groupId } = upperBoundExecutor();
    // 待确认项不计入小计，暂存本身不会越界
    const staged = executor.agent({
      type: 'add_item', group_id: groupId, title: 'Agent 利息', category: 'interest', amount: '0.01',
    });
    expect(staged.ok).toBe(true);
    expect(staged.snapshot!.pending_count).toBe(1);
    expect(staged.snapshot!.group_subtotals[groupId]).toBe(MAX_CENTS);

    const changeId = staged.snapshot!.pending_changes[0].id;
    const before = executor.getSnapshot();
    const accepted = executor.human({ type: 'accept_change', change_id: changeId });
    expect(accepted).toMatchObject({ ok: false, code: 'validation' });
    expect(accepted.message).toContain('本次修改未保存');

    const after = executor.getSnapshot();
    expect(after.pending_changes).toEqual(before.pending_changes);
    expect(after.pending_count).toBe(1);
    expect(after.items).toHaveLength(1);
    expect(after.group_subtotals[groupId]).toBe(MAX_CENTS);
    expect(after.seq).toBe(before.seq);
  });

  it('调整类别导致小计越界：回滚类别、小计与 seq', () => {
    const executor = createClaimExecutor({ caseId: 'case-category', caseTitle: 'TEST-ONLY 类别' });
    const groupId = executor.human({ type: 'add_group', title: '类别分组', currency: 'CNY' })
      .snapshot!.groups[0].id;
    expect(executor.human({
      type: 'add_item', group_id: groupId, title: '一分钱', category: 'principal', amount: '0.01',
    }).ok).toBe(true);
    const deduction = executor.human({
      type: 'add_item', group_id: groupId, title: '抵扣', category: 'deduction',
      amount: '90071992547409.91',
    });
    expect(deduction.ok).toBe(true);
    expect(deduction.snapshot!.group_subtotals[groupId]).toBe(1 - MAX_CENTS);
    const deductionId = deduction.snapshot!.items.find((item) => item.title === '抵扣')!.id;
    const before = executor.getSnapshot();

    const changed = executor.human({
      type: 'update_item', item_id: deductionId, category: 'other',
    });
    expect(changed).toMatchObject({ ok: false, code: 'validation' });
    const after = executor.getSnapshot();
    expect(after.items.find((item) => item.id === deductionId)!.category).toBe('deduction');
    expect(after.group_subtotals[groupId]).toBe(1 - MAX_CENTS);
    expect(after.seq).toBe(before.seq);
  });

  it('切换币种或计入开关导致币种总计越界：受控失败并保留原币种与开关', () => {
    const executor = createClaimExecutor({ caseId: 'case-currency', caseTitle: 'TEST-ONLY 币种' });
    const cny = executor.human({ type: 'add_group', title: '人民币组', currency: 'CNY' })
      .snapshot!.groups[0].id;
    const usd = executor.human({ type: 'add_group', title: '美元组', currency: 'USD' })
      .snapshot!.groups[1].id;
    expect(executor.human({
      type: 'add_item', group_id: cny, title: '人民币本金', category: 'principal',
      amount: '90071992547409.91',
    }).ok).toBe(true);
    expect(executor.human({
      type: 'add_item', group_id: usd, title: '美元本金', category: 'principal',
      amount: '90071992547409.91',
    }).ok).toBe(true);
    const before = executor.getSnapshot();

    const switched = executor.human({ type: 'update_group', group_id: usd, currency: 'CNY' });
    expect(switched).toMatchObject({ ok: false, code: 'validation' });
    const afterSwitch = executor.getSnapshot();
    expect(afterSwitch.groups.find((group) => group.id === usd)!.currency).toBe('USD');
    expect(afterSwitch.totals_by_currency).toEqual(before.totals_by_currency);
    expect(afterSwitch.seq).toBe(before.seq);

    // 二审组未计入时切开关同样可能越界
    expect(executor.human({
      type: 'update_group', group_id: usd, include_in_total: false,
    }).ok).toBe(true);
    const excluded = executor.getSnapshot();
    const toggled = executor.human({ type: 'update_group', group_id: usd, include_in_total: true });
    expect(toggled.ok).toBe(true); // 美元组计入的是 USD 总计，不与人民币相加
    const usdTotal = toggled.snapshot!.totals_by_currency.find((entry) => entry.currency === 'USD')!;
    expect(usdTotal.total_cents).toBe(MAX_CENTS);

    // 再建一个同币种分组并试图计入，人民币总计会越界
    const extra = executor.human({ type: 'add_group', title: '人民币二组', currency: 'CNY' })
      .snapshot!.groups.find((group) => group.title === '人民币二组')!.id;
    expect(executor.human({
      type: 'update_group', group_id: extra, include_in_total: false,
    }).ok).toBe(true);
    expect(executor.human({
      type: 'add_item', group_id: extra, title: '人民币本金二', category: 'principal',
      amount: '90071992547409.91',
    }).ok).toBe(true);
    const beforeToggle = executor.getSnapshot();
    const overflow = executor.human({ type: 'update_group', group_id: extra, include_in_total: true });
    expect(overflow).toMatchObject({ ok: false, code: 'validation' });
    const afterToggle = executor.getSnapshot();
    expect(afterToggle.groups.find((group) => group.id === extra)!.include_in_total).toBe(false);
    expect(afterToggle.totals_by_currency).toEqual(beforeToggle.totals_by_currency);
    expect(afterToggle.seq).toBe(beforeToggle.seq);
    expect(excluded.seq).toBeGreaterThan(0);
  });
});

describe('手填金额的币种标识', () => {
  it('组币种为人民币时，明确标注美元的金额被拒，不静默改币种', () => {
    const { executor, snapshot } = setup();
    const group = snapshot.groups[0];
    const before = snapshot.items.length;
    const result = executor.human({
      type: 'add_item', group_id: group.id, title: '美元利息', category: 'interest', amount: '美元100',
    });
    expect(result).toMatchObject({ ok: false, code: 'validation' });
    expect(result.message).toContain('不会自动改币种');
    const after = executor.getSnapshot();
    expect(after.items).toHaveLength(before);
    expect(after.groups[0].currency).toBe('CNY');
  });

  it('编辑金额项时同样拒绝外币标志，原金额不变', () => {
    const { executor, snapshot } = setup();
    const lawyer = snapshot.items.find((item) => item.title === '律师费（一审）')!;
    const result = executor.human({ type: 'update_item', item_id: lawyer.id, amount: '$90000' });
    expect(result).toMatchObject({ ok: false, code: 'validation' });
    expect(executor.getSnapshot().items.find((item) => item.id === lawyer.id)!.amount_cents)
      .toBe(8000000);
  });

  it('Agent 提交外币金额也被拒', () => {
    const { executor, snapshot } = setup();
    const result = executor.agent({
      type: 'add_item', group_id: snapshot.groups[0].id, title: 'Agent 美元项',
      category: 'other', amount: 'USD 100',
    });
    expect(result).toMatchObject({ ok: false, code: 'validation' });
    expect(executor.getSnapshot().pending_count).toBe(snapshot.pending_count);
  });

  it('组币种为美元时接受美元金额', () => {
    const executor = createClaimExecutor({ caseId: 'case-usd', caseTitle: 'TEST-ONLY 美元' });
    const groupId = executor.human({ type: 'add_group', title: '美元组', currency: 'USD' })
      .snapshot!.groups[0].id;
    const result = executor.human({
      type: 'add_item', group_id: groupId, title: '美元本金', category: 'principal', amount: '美元12,345.67元',
    });
    expect(result.ok).toBe(true);
    expect(result.snapshot!.items[0].amount_cents).toBe(1234567);
    expect(result.snapshot!.group_subtotals[groupId]).toBe(1234567);
  });
});
