import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { describe, expect, it } from 'vitest';
import ClaimsPrototypePage from './ClaimsPrototypePage';

function renderPage() {
  return render(<MemoryRouter><ClaimsPrototypePage /></MemoryRouter>);
}

describe('ClaimsPrototypePage · 演示控制', () => {
  it('标题、演示入口与基线总计', () => {
    renderPage();
    expect(screen.getByRole('heading', { level: 1, name: /TEST-ONLY 甲公司诉乙公司合同纠纷案/ })).toBeTruthy();
    expect(screen.getByText(/诉请金额原型 · TEST-ONLY 合成案卷/)).toBeTruthy();
    expect(screen.getByRole('button', { name: '模拟 Agent 追加利息项' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '模拟 Agent 修改律师费' })).toBeTruthy();
    expect(screen.getByTestId('total-CNY').textContent).toBe('¥1,402,400.00');
  });

  it('模拟 Agent 修改律师费：待确认、撤回还原', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: '模拟 Agent 修改律师费' }));
    expect(screen.getByRole('status').textContent).toContain('Agent 已提交金额修改');
    const lawyerRow = screen.getAllByTestId('claim-item-row')
      .find((row) => row.textContent?.includes('律师费（一审）'))!;
    expect(within(lawyerRow).getByText('待确认修改')).toBeTruthy();
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥1,402,400.00');
    await userEvent.click(screen.getByRole('button', { name: '撤回「律师费（一审）」' }));
    expect(screen.queryByText('待确认修改')).toBeNull();
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥1,402,400.00');
  });

  it('模拟 Agent 追加利息项：确认后计入并变绿', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: '模拟 Agent 追加利息项' }));
    expect(screen.getAllByTestId('claim-pending-row')).toHaveLength(2);
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥1,402,400.00');
    await userEvent.click(screen.getByRole('button', { name: /确认「逾期利息（Agent 补充/ }));
    expect(screen.getAllByTestId('claim-pending-row')).toHaveLength(1);
    expect(screen.getByTestId('group-subtotal').textContent).toBe('¥1,405,687.67');
    expect(screen.getAllByText('Agent 已确认').length).toBeGreaterThan(0);
  });

  it('重置演示恢复 TEST-ONLY 基线', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: '模拟 Agent 追加利息项' }));
    expect(screen.getAllByTestId('claim-pending-row')).toHaveLength(2);
    await userEvent.click(screen.getByRole('button', { name: '重置演示' }));
    expect(screen.getAllByTestId('claim-pending-row')).toHaveLength(1);
    expect(screen.getAllByTestId('claim-item-row')).toHaveLength(5);
    expect(screen.getByTestId('total-CNY').textContent).toBe('¥1,402,400.00');
  });
});
