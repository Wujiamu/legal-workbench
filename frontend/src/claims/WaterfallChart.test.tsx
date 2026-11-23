import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { buildClaimsFixture } from './fixtures';
import { waterfallColumns, waterfallScale, WaterfallChart } from './WaterfallChart';
import type { ClaimCategory, ClaimItem } from './types';

function claimItem(id: string, amountCents: number, category: ClaimCategory = 'principal'): ClaimItem {
  return {
    id, case_id: 'case', group_id: 'group', title: id, category,
    amount_cents: amountCents, calc_note: '', source: null, position: 0,
    origin: 'human', review_state: 'none', revision: 0, removed_at: null,
  };
}

/** 刻度标签只能是 0 或两位小数的分/万元写法，不能出现浮点尾巴。 */
const TICK_LABEL = /^(0|-?[\d,]+\.\d{2}(万)?)$/;

function ticks(container: HTMLElement): string[] {
  return [...container.querySelectorAll('.wf-axis')].map((node) => node.textContent ?? '');
}

describe('瀑布图几何与明细同源', () => {
  const snapshot = buildClaimsFixture().getSnapshot();
  const group = snapshot.groups[0];
  const items = snapshot.items.filter((item) => item.group_id === group.id);
  const subtotal = snapshot.group_subtotals[group.id];

  it('累计列首尾与抵扣方向正确', () => {
    const columns = waterfallColumns(items, subtotal);
    expect(columns).toHaveLength(items.length + 1);
    expect(columns[0].from).toBe(0);
    expect(columns[0].to).toBe(128000000);
    const deduction = columns.find((column) => column.category === 'deduction')!;
    expect(deduction.from - deduction.to).toBe(30000000);
    const total = columns[columns.length - 1];
    expect(total.kind).toBe('total');
    expect(total.from).toBe(0);
    expect(total.to).toBe(140240000);
    expect(total.to).toBe(subtotal);
  });

  it('柱高与起止值成比例，抵扣柱向下', () => {
    const columns = waterfallColumns(items, subtotal);
    const scale = waterfallScale(columns);
    const deduction = columns.find((column) => column.category === 'deduction')!;
    const yFrom = scale.y(deduction.from);
    const yTo = scale.y(deduction.to);
    expect(yTo).toBeGreaterThan(yFrom);
    const positive = columns[0];
    expect(scale.y(positive.to)).toBeLessThan(scale.y(positive.from));
    // 0 以上为正、0 以下为负，baseline 在中间
    expect(scale.y(0)).toBeGreaterThan(scale.y(scale.max));
    expect(scale.y(0)).toBeLessThan(scale.y(scale.min));
  });

  it('渲染柱条、末柱与无障碍描述', () => {
    const { container } = render(
      <WaterfallChart items={items} subtotalCents={subtotal} currency="CNY" groupTitle={group.title} />,
    );
    const bars = container.querySelectorAll('[data-role="bar"]');
    expect(bars).toHaveLength(items.length);
    const total = container.querySelector('[data-role="total"]')!;
    expect(total.getAttribute('data-to')).toBe('140240000');
    expect(total.getAttribute('data-from')).toBe('0');
    const deductionBar = container.querySelector('[data-kind="deduction"]')!;
    expect(Number(deductionBar.getAttribute('data-to')))
      .toBeLessThan(Number(deductionBar.getAttribute('data-from')));
    const svg = container.querySelector('svg')!;
    expect(svg.getAttribute('aria-label')).toContain('组内瀑布图');
    expect(svg.getAttribute('aria-label')).toContain('小计');
    expect(container.textContent).toContain('组内小计');
    expect(container.textContent).toContain('¥1,402,400.00');
  });
});

describe('低值与边界刻度（刻度取整到分，不把浮点分值交给整数分格式化）', () => {
  it('只含 0.01 元的分组正常渲染，刻度为整数分', () => {
    const items = [claimItem('一分钱', 1)];
    const columns = waterfallColumns(items, 1);
    const scale = waterfallScale(columns);
    expect(Number.isSafeInteger(scale.max)).toBe(true);
    expect(Number.isSafeInteger(scale.min)).toBe(true);
    expect(scale.max).toBe(2);
    expect(scale.min).toBe(-1);

    const { container } = render(
      <WaterfallChart items={items} subtotalCents={1} currency="CNY" groupTitle="一分钱分组" />,
    );
    expect(ticks(container)).toEqual(['0.02', '0', '-0.01']);
    ticks(container).forEach((label) => expect(label).toMatch(TICK_LABEL));
    expect(container.querySelector('[data-role="total"]')!.getAttribute('data-to')).toBe('1');
    expect(container.textContent).toContain('¥0.01');
  });

  it('零金额分组不抛错，刻度为 1 元区间', () => {
    const { container } = render(
      <WaterfallChart items={[]} subtotalCents={0} currency="CNY" groupTitle="空分组" />,
    );
    expect(ticks(container)).toEqual(['1.00', '0', '-1.00']);
    expect(container.querySelector('[data-role="total"]')!.getAttribute('data-to')).toBe('0');
    expect(container.textContent).toContain('¥0.00');
  });

  it('低值净扣除：小计为负、末柱向下、刻度到分', () => {
    const items = [
      claimItem('一分钱本金', 1),
      claimItem('两分钱抵扣', 2, 'deduction'),
    ];
    const columns = waterfallColumns(items, -1);
    const scale = waterfallScale(columns);
    expect(Number.isSafeInteger(scale.max)).toBe(true);
    expect(Number.isSafeInteger(scale.min)).toBe(true);
    const { container } = render(
      <WaterfallChart items={items} subtotalCents={-1} currency="CNY" groupTitle="低值净扣除" />,
    );
    expect(ticks(container)).toEqual(['0.02', '0', '-0.02']);
    const total = container.querySelector('[data-role="total"]')!;
    expect(total.getAttribute('data-to')).toBe('-1');
    expect(total.getAttribute('class')).toContain('wf-negative');
    expect(container.textContent).toContain('-¥0.01');
  });

  it('安全整数分上界（约 90 万亿元）仍可渲染', () => {
    const max = Number.MAX_SAFE_INTEGER;
    const items = [claimItem('上界本金', max)];
    const columns = waterfallColumns(items, max);
    const scale = waterfallScale(columns);
    expect(scale.max).toBe(max);
    expect(Number.isSafeInteger(scale.min)).toBe(true);

    const { container } = render(
      <WaterfallChart items={items} subtotalCents={max} currency="CNY" groupTitle="上界分组" />,
    );
    expect(container.querySelector('[data-role="total"]')!.getAttribute('data-to')).toBe(String(max));
    expect(ticks(container)).toHaveLength(3);
    ticks(container).forEach((label) => expect(label).toMatch(TICK_LABEL));
    expect(container.textContent).toContain('¥90,071,992,547,409.91');
  });
});
