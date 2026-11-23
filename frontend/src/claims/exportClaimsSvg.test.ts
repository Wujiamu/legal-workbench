import { afterEach, describe, expect, it, vi } from 'vitest';
import { buildClaimsFixture } from './fixtures';
import {
  buildClaimsSvg, claimsExportFileName, downloadTextFile,
} from './exportClaimsSvg';
import type { ClaimItem } from './types';

function makeItem(overrides: Partial<ClaimItem> & { id: string; title: string; amount_cents: number }): ClaimItem {
  return {
    case_id: 'case-1',
    group_id: 'group-1',
    category: 'principal',
    calc_note: 'SECRET_CALC_NOTE_DO_NOT_EXPORT',
    source: {
      segment_id: 'seg-1',
      material_id: 'mat-1',
      material_title: '购销合同.pdf',
      page: 2,
      locator: '第1条',
      excerpt: '原文',
      span: { start: 0, end: 2 },
      raw_text: '100元',
    },
    position: 0,
    origin: 'human',
    review_state: 'none',
    revision: 1,
    removed_at: null,
    ...overrides,
  };
}

describe('buildClaimsSvg · 内部工作态与对外态投影', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('工作态渲染标题、币种、柱条、内联颜色与精确金额，且绝不泄露 calc_note', () => {
    const snapshot = buildClaimsFixture().getSnapshot();
    const group = snapshot.groups[0];
    const items = snapshot.items
      .filter((item) => item.group_id === group.id)
      .map((item, idx) => ({
        ...item,
        calc_note: `INTERNAL_CALC_SECRET_${idx}`,
      }));
    const subtotal = snapshot.group_subtotals[group.id];

    const { svg, width, height } = buildClaimsSvg({
      title: group.title,
      caseTitle: snapshot.case_title,
      currency: group.currency,
      items,
      subtotalCents: subtotal,
      generatedAt: '2026-11-22 10:00',
    });

    expect(svg.startsWith('<svg')).toBe(true);
    expect(svg.endsWith('</svg>')).toBe(true);
    expect(width).toBeGreaterThan(500);
    expect(height).toBeGreaterThan(300);
    expect(svg).not.toContain('NaN');

    expect(svg).toContain(group.title);
    expect(svg).toContain('人民币（CNY）');
    expect(svg).toContain('内部工作态 · 精确到分');
    expect(svg).toContain('导出 2026-11-22 10:00');
    expect(svg).toContain('fill="#2f5d50"');
    expect(svg).toContain('fill="#b3261e"');
    expect(svg).toContain('fill="#173e35"');
    expect(svg).toContain('stroke="#a7b5aa"');
    expect(svg).toContain('stroke="#b9c6bd"');
    expect(svg).toContain('stroke-dasharray="3 3"');
    expect(svg).toContain('¥1,280,000.00');
    expect(svg).toContain('−¥300,000.00');
    expect(svg).toContain('¥1,402,400.00');

    expect(svg).not.toContain('INTERNAL_CALC_SECRET_');
    expect(svg).not.toContain('relative_source');
  });

  it('净小计为负时末柱使用 #8c2f2f，空分组不产生 NaN', () => {
    const negative = buildClaimsSvg({
      title: '净抵扣分组',
      currency: 'CNY',
      items: [
        makeItem({ id: 'i-1', title: '本金', amount_cents: 100 }),
        makeItem({ id: 'i-2', title: '抵扣', category: 'deduction', amount_cents: 250 }),
      ],
      subtotalCents: -150,
    });
    expect(negative.svg).toContain('fill="#8c2f2f"');
    expect(negative.svg).toContain('-¥1.50');
    expect(negative.svg).not.toContain('NaN');

    const empty = buildClaimsSvg({
      title: '空分组',
      currency: 'USD',
      items: [],
      subtotalCents: 0,
    });
    expect(empty.svg).toContain('$0.00');
    expect(empty.svg).not.toContain('NaN');
  });

  it.each([
    ['client', '客户沟通态'],
    ['court', '当庭呈递态'],
  ] as const)('对外态 %s 渲染角标、冻结信息、脚注并过滤未展示对象', (profile, modeLabel) => {
    const itemA = makeItem({
      id: 'claim-a',
      title: '货款本金',
      amount_cents: 50000,
      calc_note: 'SECRET_NOTE_A',
    });
    const itemB = makeItem({
      id: 'claim-b',
      title: '已付抵扣',
      category: 'deduction',
      amount_cents: 10000,
      calc_note: 'SECRET_NOTE_B',
    });
    const fullDigest = 'abcdef1234567890fedcba0987654321abcdef1234567890fedcba0987654321';

    const { svg } = buildClaimsSvg({
      title: '对被告一诉请',
      caseTitle: '甲乙合同案',
      currency: 'CNY',
      items: [itemA, itemB],
      subtotalCents: 40000,
      projection: {
        profile,
        references: [
          { targetId: 'claim-a', index: 1, label: '证据1-1 第2页 购销合同' },
          { targetId: 'claim-a', index: 2, label: '证据1-2 第5页 对账单' },
          { targetId: 'excluded-claim', index: 3, label: 'SECRET_EXCLUDED_REF' },
        ],
        frozenAt: '2026-11-22T08:00:00Z',
        digest: fullDigest,
        notice: '含 1 项人工确认例外',
      },
    });

    expect(svg).toContain(modeLabel);
    expect(svg).toContain('冻结 2026-11-22T08:00:00Z');
    expect(svg).toContain('摘要 abcdef123456');
    expect(svg).not.toContain(fullDigest);
    expect(svg).toContain('含 1 项人工确认例外');
    expect(svg).toContain('[1] [2]');
    expect(svg).toContain('[1] 证据1-1 第2页 购销合同');
    expect(svg).toContain('[2] 证据1-2 第5页 对账单');
    expect(svg).not.toContain('SECRET_EXCLUDED_REF');
    expect(svg).not.toContain('SECRET_NOTE_');
    expect(svg).not.toContain('内部工作态');
    expect(svg).not.toContain('完整编号见引用索引附表');
  });

  it('超过 12 条脚注时只展示前 12 条并附“完整编号见引用索引附表”', () => {
    const items = [makeItem({ id: 'claim-many', title: '多出处本金', amount_cents: 100000 })];
    const references = Array.from({ length: 14 }, (_, idx) => ({
      targetId: 'claim-many',
      index: idx + 1,
      label: `证据1-${idx + 1} 第${idx + 1}页`,
    }));

    const { svg } = buildClaimsSvg({
      title: '多引用诉请',
      currency: 'CNY',
      items,
      subtotalCents: 100000,
      projection: {
        profile: 'court',
        references,
      },
    });

    expect(svg).toContain('[1] 证据1-1 第1页');
    expect(svg).toContain('[12] 证据1-12 第12页');
    expect(svg).not.toContain('[13] 证据1-13');
    expect(svg).toContain('完整编号见引用索引附表');
  });

  it('安全转义 XML 特殊字符并生成可读导出文件名', () => {
    const item = makeItem({
      id: 'c-xml',
      title: 'A&B <违约金>',
      amount_cents: 12345,
      calc_note: 'relative_source_hidden',
    });

    const { svg } = buildClaimsSvg({
      title: '主诉请 <1> & "2"',
      caseTitle: "O'Neil & Co",
      currency: 'CNY',
      items: [item],
      subtotalCents: 12345,
      projection: {
        profile: 'client',
        references: [{ targetId: 'c-xml', index: 1, label: '证据<1> & "附页"' }],
        notice: '注: <检查> & "确认"',
      },
    });

    expect(svg).toContain('主诉请 &lt;1&gt; &amp; &quot;2&quot;');
    expect(svg).toContain('O&apos;Neil &amp; Co');
    expect(svg).toContain('A&amp;B &lt;违约金&gt;');
    expect(svg).toContain('[1] 证据&lt;1&gt; &amp; &quot;附页&quot;');
    expect(svg).toContain('注: &lt;检查&gt; &amp; &quot;确认&quot;');
    expect(svg).not.toContain('relative_source_hidden');

    const date = new Date('2026-11-22T02:00:00Z');
    expect(claimsExportFileName('对被告一/主诉请', 'svg', date)).toBe('对被告一-主诉请-2026-11-22.svg');
    expect(claimsExportFileName('对被告一/主诉请', 'png', date)).toBe('对被告一-主诉请-2026-11-22.png');
    expect(claimsExportFileName('对被告一/主诉请', 'md', date)).toBe('对被告一-主诉请-2026-11-22-引用索引.md');
    expect(claimsExportFileName('对被告一/主诉请', 'csv', date)).toBe('对被告一-主诉请-2026-11-22-引用索引.csv');
    expect(claimsExportFileName('   ', 'md', date)).toBe('诉请金额-2026-11-22-引用索引.md');
  });

  it('downloadTextFile 通过 Blob 与临时锚点触发下载并释放 URL', () => {
    vi.useFakeTimers();
    const createObjectURL = vi.fn(() => 'blob:claims-test');
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, configurable: true });
    Object.defineProperty(URL, 'revokeObjectURL', { value: revokeObjectURL, configurable: true });

    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    downloadTextFile('citations.csv', 'col1,col2\n1,2\n', 'text/csv;charset=utf-8');

    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(click).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1000);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:claims-test');
    vi.useRealTimers();
  });
});
