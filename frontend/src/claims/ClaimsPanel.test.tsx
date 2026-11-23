import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useRef, useState } from 'react';
import { describe, expect, it } from 'vitest';
import { ClaimsPanel } from './ClaimsPanel';
import { buildClaimsFixture } from './fixtures';
import type { ClaimCommand, ClaimExecutor, ClaimSnapshot } from './types';

function Harness({ agentUpdateLawyer = false }: { agentUpdateLawyer?: boolean }) {
  const executorRef = useRef<ClaimExecutor | null>(null);
  if (!executorRef.current) {
    const executor = buildClaimsFixture();
    if (agentUpdateLawyer) {
      const lawyer = executor.getSnapshot().items.find((item) => item.title === '律师费（一审）')!;
      const result = executor.agent({ type: 'update_item', item_id: lawyer.id, amount: '88000.00' });
      if (!result.ok) throw new Error(result.message);
    }
    executorRef.current = executor;
  }
  const [snapshot, setSnapshot] = useState<ClaimSnapshot>(() => executorRef.current!.getSnapshot());
  function command(next: ClaimCommand) {
    const result = executorRef.current!.human(next);
    if (result.ok && result.snapshot) setSnapshot(result.snapshot);
    return result;
  }
  return <ClaimsPanel snapshot={snapshot} onCommand={command} />;
}

function itemTitles(): string[] {
  return screen.getAllByTestId('claim-item-row')
    .map((row) => row.querySelector('.claims-item-title')?.textContent ?? '');
}

describe('ClaimsPanel · 基线', () => {
  it('显示两组、全案总计、组内小计与 Agent 待确认项', () => {
    render(<Harness />);
    expect(screen.getByRole('heading', { name: '对被告一主诉请' })).toBeTruthy();
    expect(screen.getByTestId('total-CNY').textContent).toBe('¥1,402,400.00');
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥1,402,400.00');
    expect(screen.getAllByTestId('claim-item-row')).toHaveLength(5);
    expect(screen.getAllByTestId('claim-pending-row')).toHaveLength(1);
    expect(screen.getByText(/含 1 条 Agent 待确认修改/)).toBeTruthy();
    expect(screen.getByText(/另有 1 组未计入全案总计/)).toBeTruthy();
  });

  it('瀑布图末柱与明细小计一致', () => {
    const { container } = render(<Harness />);
    expect(container.querySelector('[data-role="total"]')!.getAttribute('data-to')).toBe('140240000');
    expect(container.querySelectorAll('[data-role="bar"]')).toHaveLength(5);
    expect(container.querySelector('[data-kind="deduction"]')).toBeTruthy();
  });
});

describe('ClaimsPanel · 计入全案总计', () => {
  it('勾选二审组后总计增加，取消后恢复', async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole('button', { name: /^二审变更诉请/ }));
    const checkbox = screen.getByRole('checkbox', { name: '计入全案总计' }) as HTMLInputElement;
    expect(checkbox.checked).toBe(false);
    await userEvent.click(checkbox);
    expect(screen.getByTestId('total-CNY').textContent).toBe('¥1,444,745.67');
    await userEvent.click(checkbox);
    expect(screen.getByTestId('total-CNY').textContent).toBe('¥1,402,400.00');
  });
});

describe('ClaimsPanel · 金额项增改删排', () => {
  it('新增金额项：字符串换算为整数分并更新小计', async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole('button', { name: '＋ 添加金额项' }));
    expect(screen.getByRole('heading', { name: '添加金额项' })).toBeTruthy();
    await userEvent.type(screen.getByLabelText('名称'), '差旅费');
    await userEvent.selectOptions(screen.getByLabelText('类别'), 'expense');
    await userEvent.type(screen.getByLabelText('金额（元）'), '1,000.50');
    expect(screen.getByTestId('amount-cents').textContent).toContain('¥1,000.50');
    expect(screen.getByTestId('amount-cents').textContent).toContain('100,050 分');
    await userEvent.click(screen.getByRole('button', { name: '添加' }));
    expect(screen.getAllByTestId('claim-item-row')).toHaveLength(6);
    expect(itemTitles()).toContain('差旅费');
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥1,403,400.50');
  });

  it('金额非法时显示错误且不新增', async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole('button', { name: '＋ 添加金额项' }));
    await userEvent.type(screen.getByLabelText('名称'), '非法金额');
    await userEvent.type(screen.getByLabelText('金额（元）'), '12.345');
    expect(screen.getByText(/最多两位小数/)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '添加' }));
    expect(screen.getByRole('heading', { name: '添加金额项' })).toBeTruthy();
    expect(screen.getAllByTestId('claim-item-row')).toHaveLength(5);
  });

  it('中文输入法组词期间回车被拦截，不提交表单', async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole('button', { name: '＋ 添加金额项' }));
    const amount = screen.getByLabelText('金额（元）');
    await userEvent.type(amount, '100');
    fireEvent.compositionStart(amount);
    const composingEnter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    Object.defineProperty(composingEnter, 'isComposing', { value: true });
    amount.dispatchEvent(composingEnter);
    expect(composingEnter.defaultPrevented).toBe(true);
    expect(screen.getByRole('heading', { name: '添加金额项' })).toBeTruthy();
    fireEvent.compositionEnd(amount);
    await new Promise((resolve) => setTimeout(resolve, 80));
    const plainEnter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    amount.dispatchEvent(plainEnter);
    expect(plainEnter.defaultPrevented).toBe(false);
    expect(screen.getAllByTestId('claim-item-row')).toHaveLength(5);
  });

  it('编辑金额项更新金额与小计', async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole('button', { name: '编辑「律师费（一审）」' }));
    const amount = screen.getByLabelText('金额（元）');
    await userEvent.clear(amount);
    await userEvent.type(amount, '90000');
    await userEvent.click(screen.getByRole('button', { name: '保存' }));
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥1,412,400.00');
  });

  it('组内上下移动按真实 DOM 顺序生效', async () => {
    render(<Harness />);
    expect(itemTitles()[0]).toBe('合同价款（本金）');
    expect(itemTitles()[1]).toBe('逾期利息（暂计至 2026-10-31）');
    await userEvent.click(screen.getByRole('button', { name: '下移「合同价款（本金）」' }));
    expect(itemTitles()[0]).toBe('逾期利息（暂计至 2026-10-31）');
    expect(itemTitles()[1]).toBe('合同价款（本金）');
    await userEvent.click(screen.getByRole('button', { name: '上移「合同价款（本金）」' }));
    expect(itemTitles()[0]).toBe('合同价款（本金）');
  });

  it('删除金额项需确认，确认后小计减少', async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole('button', { name: '删除「律师费（一审）」' }));
    expect(screen.getByRole('heading', { name: '删除金额项？' })).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '确认删除' }));
    expect(screen.getAllByTestId('claim-item-row')).toHaveLength(4);
    expect(itemTitles()).not.toContain('律师费（一审）');
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥1,322,400.00');
  });
});

describe('ClaimsPanel · 精确值与模拟原文', () => {
  it('选取高额精确值带入金额与出处，可打开模拟原文', async () => {
    const { container } = render(<Harness />);
    await userEvent.click(screen.getByRole('button', { name: '＋ 添加金额项' }));
    await userEvent.type(screen.getByLabelText('名称'), '结算金额（高额）');
    await userEvent.type(screen.getByLabelText('搜索本案精确值'), '结算');
    await userEvent.click(screen.getByRole('button', { name: /123,456,789,012\.34元/ }));
    const amount = screen.getByLabelText('金额（元）') as HTMLInputElement;
    expect(amount.value).toBe('123456789012.34');
    expect(screen.getByTestId('amount-cents').textContent).toContain('12,345,678,901,234 分');
    await userEvent.click(screen.getByRole('button', { name: '添加' }));
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥123,458,191,412.34');
    await userEvent.click(screen.getByRole('button', { name: 'TEST-ONLY 结算单.pdf · 第 2 页' }));
    expect(screen.getByRole('heading', { name: '模拟原文' })).toBeTruthy();
    expect(container.querySelector('mark')?.textContent).toBe('123,456,789,012.34元');
    await userEvent.click(screen.getByRole('button', { name: '关闭' }));
    expect(screen.queryByRole('heading', { name: '模拟原文' })).toBeNull();
  });

  it('选取人民币精确值带入合同出处', async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole('button', { name: '＋ 添加金额项' }));
    await userEvent.type(screen.getByLabelText('名称'), '合同价款副本');
    await userEvent.type(screen.getByLabelText('搜索本案精确值'), '合同价款');
    await userEvent.click(screen.getByRole('button', { name: /人民币1,280,000元/ }));
    expect((screen.getByLabelText('金额（元）') as HTMLInputElement).value).toBe('1280000.00');
    expect(document.querySelector('.claims-chosen-source')?.textContent).toContain('第 3 页 · 第 1 条');
    await userEvent.click(screen.getByRole('button', { name: '添加' }));
    expect(screen.getAllByTestId('claim-item-row')).toHaveLength(6);
  });
});

describe('ClaimsPanel · Agent 待确认', () => {
  it('撤回待确认新增项：明细与小计保持基线', async () => {
    render(<Harness />);
    expect(screen.getAllByTestId('claim-pending-row')).toHaveLength(1);
    await userEvent.click(screen.getByRole('button', { name: '撤回「逾期利息（补充至 2026-11-20）」' }));
    expect(screen.queryAllByTestId('claim-pending-row')).toHaveLength(0);
    expect(screen.getByText('没有待确认的 Agent 修改。')).toBeTruthy();
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥1,402,400.00');
  });

  it('确认待确认新增项：入账并显示绿色已确认徽标', async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole('button', { name: '确认「逾期利息（补充至 2026-11-20）」' }));
    expect(screen.queryAllByTestId('claim-pending-row')).toHaveLength(0);
    expect(screen.getAllByTestId('claim-item-row')).toHaveLength(6);
    expect(screen.getByText('Agent 已确认')).toBeTruthy();
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥1,405,687.67');
  });

  it('Agent 修改待确认：确认前原值不变，撤回后还原', async () => {
    render(<Harness agentUpdateLawyer />);
    const lawyerRow = screen.getAllByTestId('claim-item-row')
      .find((row) => row.textContent?.includes('律师费（一审）'))!;
    expect(within(lawyerRow).getByText('待确认修改')).toBeTruthy();
    expect(lawyerRow.textContent).toContain('¥80,000.00');
    expect(screen.getByText(/¥80,000.00 → ¥88,000.00/)).toBeTruthy();
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥1,402,400.00');
    await userEvent.click(screen.getByRole('button', { name: '撤回「律师费（一审）」' }));
    expect(screen.queryByText('待确认修改')).toBeNull();
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥1,402,400.00');
    // 基线自带的 1 条 Agent 待确认新增项仍在
    expect(screen.getAllByTestId('claim-review-item')).toHaveLength(1);
  });

  it('Agent 修改待确认：确认后按新值计入', async () => {
    render(<Harness agentUpdateLawyer />);
    await userEvent.click(screen.getByRole('button', { name: '确认「律师费（一审）」' }));
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥1,410,400.00');
    expect(screen.getByText('Agent 已确认')).toBeTruthy();
  });
});

describe('ClaimsPanel · 低值、溢出与金额校验', () => {
  function seqText(): string {
    return document.querySelector('.claims-seq')?.textContent ?? '';
  }

  async function createGroup(title: string) {
    await userEvent.click(screen.getByRole('button', { name: '＋ 新建分组' }));
    await userEvent.type(screen.getByLabelText('分组名称'), title);
    await userEvent.click(screen.getByRole('button', { name: '创建分组' }));
  }

  async function openItemDialog() {
    await userEvent.click(screen.getByRole('button', { name: '＋ 添加金额项' }));
  }

  it('新建只含 0.01 元的分组：瀑布图与明细都正常', async () => {
    const { container } = render(<Harness />);
    await createGroup('一分钱分组');
    await openItemDialog();
    await userEvent.type(screen.getByLabelText('名称'), '一分钱');
    await userEvent.type(screen.getByLabelText('金额（元）'), '0.01');
    expect(screen.getByTestId('amount-cents').textContent).toContain('1 分');
    await userEvent.click(screen.getByRole('button', { name: '添加' }));

    expect(screen.getAllByTestId('claim-item-row')).toHaveLength(1);
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥0.01');
    const total = container.querySelector('[data-role="total"]')!;
    expect(total.getAttribute('data-to')).toBe('1');
    const ticks = [...container.querySelectorAll('.wf-axis')].map((node) => node.textContent);
    expect(ticks).toEqual(['0.02', '0', '-0.01']);
  });

  it('本金 90 万亿元再加 0.01：受控失败，明细、小计与 seq 保持写前状态', async () => {
    render(<Harness />);
    await createGroup('上界分组');
    // 上界本金不并入已有的人民币总计，才能单独构造组内小计溢出
    await userEvent.click(screen.getByRole('checkbox', { name: '计入全案总计' }));
    await openItemDialog();
    await userEvent.type(screen.getByLabelText('名称'), '本金');
    await userEvent.type(screen.getByLabelText('金额（元）'), '90071992547409.91');
    await userEvent.click(screen.getByRole('button', { name: '添加' }));
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥90,071,992,547,409.91');
    const seqBefore = seqText();

    await openItemDialog();
    await userEvent.type(screen.getByLabelText('名称'), '利息');
    await userEvent.type(screen.getByLabelText('金额（元）'), '0.01');
    await userEvent.click(screen.getByRole('button', { name: '添加' }));

    const alerts = screen.getAllByRole('alert').map((node) => node.textContent ?? '');
    expect(alerts.some((text) => text.includes('超出可精确表示的范围')
      && text.includes('本次修改未保存'))).toBe(true);
    // 对话框保留输入，方便改金额重试
    expect(screen.getByRole('heading', { name: '添加金额项' })).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '取消' }));

    expect(screen.getAllByTestId('claim-item-row')).toHaveLength(1);
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥90,071,992,547,409.91');
    expect(seqText()).toBe(seqBefore);
    // 页面仍可用：继续添加一分钱到另一分组不会受影响
    await createGroup('一分钱分组');
    await openItemDialog();
    await userEvent.type(screen.getByLabelText('名称'), '一分钱');
    await userEvent.type(screen.getByLabelText('金额（元）'), '0.01');
    await userEvent.click(screen.getByRole('button', { name: '添加' }));
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥0.01');
  });

  it('千分位位置错误或币种标识在中间时拒绝提交', async () => {
    render(<Harness />);
    await openItemDialog();
    await userEvent.type(screen.getByLabelText('名称'), '非法分隔符');
    const amount = screen.getByLabelText('金额（元）');
    await userEvent.type(amount, '1,2');
    expect(document.querySelector('.field-hint.claims-invalid')?.textContent)
      .toContain('千分位只能每三位分隔');
    await userEvent.click(screen.getByRole('button', { name: '添加' }));
    expect(screen.getAllByTestId('claim-item-row')).toHaveLength(5);

    await userEvent.clear(amount);
    await userEvent.type(amount, '1USD2');
    expect(document.querySelector('.field-hint.claims-invalid')?.textContent)
      .toContain('千分位只能每三位分隔');
    await userEvent.click(screen.getByRole('button', { name: '添加' }));
    expect(screen.getAllByTestId('claim-item-row')).toHaveLength(5);
  });

  it('人民币分组内手填美元金额：明确提示币种冲突，不静默改币种', async () => {
    render(<Harness />);
    await openItemDialog();
    await userEvent.type(screen.getByLabelText('名称'), '美元利息');
    await userEvent.type(screen.getByLabelText('金额（元）'), '美元100');
    const conflict = screen.getByTestId('amount-currency-conflict');
    expect(conflict.textContent).toContain('美元');
    expect(conflict.textContent).toContain('不会自动改币种');
    expect(screen.queryByTestId('amount-cents')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: '添加' }));
    expect(screen.getByRole('alert').textContent).toContain('不会自动改币种');
    expect(screen.getAllByTestId('claim-item-row')).toHaveLength(5);
  });

  it('精确值候选按组币种筛选，外币候选不会带入人民币分组', async () => {
    render(<Harness />);
    await openItemDialog();
    await userEvent.type(screen.getByLabelText('搜索本案精确值'), '美元');
    expect(screen.getByTestId('candidates-hidden').textContent).toContain('已隐藏 1 条');
    expect(screen.queryByRole('button', { name: /美元12,345\.67元/ })).toBeNull();
    expect(screen.getByText('没有与本组币种一致的匹配精确值。')).toBeTruthy();
  });
});

describe('ClaimsPanel · 分组增改删排', () => {
  it('新建、重命名、调序、删除分组', async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole('button', { name: '＋ 新建分组' }));
    expect(screen.getByRole('heading', { name: '新建诉请分组' })).toBeTruthy();
    await userEvent.type(screen.getByLabelText('分组名称'), '对被告二诉请');
    await userEvent.click(screen.getByRole('button', { name: '创建分组' }));
    expect(document.querySelectorAll('.claims-group-title')).toHaveLength(3);
    expect(screen.getByRole('heading', { name: '对被告二诉请' })).toBeTruthy();
    expect(screen.getByText(/组内小计 ¥0\.00/)).toBeTruthy();

    await userEvent.click(screen.getByRole('button', { name: '编辑分组' }));
    const title = screen.getByLabelText('分组名称');
    await userEvent.clear(title);
    await userEvent.type(title, '对被告二连带诉请');
    await userEvent.click(screen.getByRole('button', { name: '保存' }));
    expect(screen.getByRole('heading', { name: '对被告二连带诉请' })).toBeTruthy();

    await userEvent.click(screen.getByRole('button', { name: '上移分组「对被告二连带诉请」' }));
    const titles = [...document.querySelectorAll('.claims-group-title')].map((node) => node.textContent);
    expect(titles[1]).toBe('对被告二连带诉请');

    await userEvent.click(screen.getByRole('button', { name: '删除分组' }));
    expect(screen.getByRole('heading', { name: '删除诉请分组？' })).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '确认删除' }));
    expect(document.querySelectorAll('.claims-group-title')).toHaveLength(2);
    expect(screen.getByRole('heading', { name: '对被告一主诉请' })).toBeTruthy();
  });
});
