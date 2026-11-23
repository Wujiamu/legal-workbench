/**
 * 原型页默认收起口径：深层分组默认收起，主线阶段可见；
 * 收起状态在两种视图间共享，可一键展开/收起。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import ProgressPage from './ProgressPage';

describe('ProgressPage · 收起默认与工具', () => {
  it('深层分组默认收起，程序内主线阶段可见', () => {
    render(<ProgressPage />);
    expect(screen.getByRole('treeitem', { name: '立案' })).toBeTruthy();
    expect(screen.queryByRole('treeitem', { name: '证据交换（第一次）' })).toBeNull();
    const pretrial = screen.getByRole('treeitem', { name: '庭前程序' });
    expect(pretrial.getAttribute('aria-expanded')).toBe('false');
    const program = screen.getByRole('treeitem', { name: '一审' });
    expect(program.getAttribute('aria-expanded')).toBe('true');
  });

  it('展开后子树出现；收起全部/展开全部作用于整棵树', () => {
    render(<ProgressPage />);
    const pretrial = screen.getByRole('treeitem', { name: '庭前程序' });
    fireEvent.click(pretrial.querySelector('.node-btn .node-collapse')!);
    expect(screen.getByRole('treeitem', { name: '证据交换（第一次）' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '收起全部' }));
    expect(screen.queryByRole('treeitem', { name: '立案' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '展开全部' }));
    expect(screen.getByRole('treeitem', { name: '证据交换（第一次）' })).toBeTruthy();
  });
});
describe('ProgressPage · 结案归档（案件级操作，树外按钮）', () => {
  it('树中没有结案归档节点，归档按钮在树外', () => {
    render(<ProgressPage />);
    expect(screen.queryByRole('treeitem', { name: '结案归档' })).toBeNull();
    expect(screen.getByRole('button', { name: '结案归档' })).toBeTruthy();
  });

  it('确认归档后进度只读；取消归档恢复可写', async () => {
    render(<ProgressPage />);
    fireEvent.click(screen.getByRole('button', { name: '结案归档' }));
    fireEvent.click(screen.getByRole('button', { name: '确认归档' }));
    expect(screen.getByText(/本案已归档/)).toBeTruthy();
    const judgment = screen.getByRole('treeitem', { name: '裁判' }).querySelector('.node-btn') as HTMLElement;
    fireEvent.contextMenu(judgment, { clientX: 10, clientY: 10 });
    fireEvent.click(screen.getByRole('menuitem', { name: '设为进行中' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('已归档'));
    expect(judgment.textContent).toContain('未开始');
    fireEvent.click(screen.getByRole('button', { name: '取消归档' }));
    fireEvent.click(screen.getByRole('button', { name: '确认取消归档' }));
    expect(screen.queryByText(/本案已归档/)).toBeNull();
    fireEvent.contextMenu(judgment, { clientX: 10, clientY: 10 });
    fireEvent.click(screen.getByRole('menuitem', { name: '设为进行中' }));
    await waitFor(() => expect(judgment.textContent).toContain('进行中'));
  });
});

describe('ProgressPage · 一键确认与撤销（用户反馈第七轮）', () => {
  it('一键全部确认需二次确认对话框；确认后徽标变绿、待确认清空', async () => {
    render(<ProgressPage />);
    await screen.findByRole('heading', { level: 1, name: /TEST-ONLY/ });
    // 初始有 1 条 Agent 待确认（庭审黄徽标）
    expect(document.querySelector('.agent-badge.agent-pending')).not.toBeNull();
    await userEvent.click(screen.getByRole('button', { name: '一键全部确认' }));
    // 二次确认对话框出现，尚未执行
    expect(screen.getByText('确认全部 Agent 修改？')).toBeTruthy();
    expect(document.querySelector('.agent-badge.agent-pending')).not.toBeNull();
    await userEvent.click(screen.getByRole('button', { name: '返回逐条审核' }));
    expect(document.querySelector('.agent-badge.agent-pending')).not.toBeNull();
    await userEvent.click(screen.getByRole('button', { name: '一键全部确认' }));
    await userEvent.click(screen.getByRole('button', { name: '我已审核，全部确认' }));
    expect(document.querySelector('.agent-badge.agent-pending')).toBeNull();
    expect(document.querySelector('.agent-badge.agent-accepted')).not.toBeNull();
    expect(screen.getByText('没有待确认的 Agent 修改。')).toBeTruthy();
  });

  it('⌘Z 撤销高亮区域：节点图新增可撤销，确认框撤回互不干扰', async () => {
    render(<ProgressPage />);
    await screen.findByRole('heading', { level: 1, name: /TEST-ONLY/ });
    // 在节点图区域新建一个阶段（根级添加菜单）
    const before = document.querySelectorAll('[role=treeitem]').length;
    await userEvent.click(screen.getByRole('button', { name: '＋ 添加' }));
    await userEvent.click(screen.getByRole('menuitem', { name: '阶段' }));
    await userEvent.type(screen.getByLabelText('标题'), '临时阶段');
    await userEvent.click(screen.getByRole('button', { name: '创建' }));
    expect(document.querySelectorAll('[role=treeitem]').length).toBe(before + 1);
    // 焦点在节点图区域（点击视图空白处），⌘Z 撤销新增
    fireEvent.mouseDown(screen.getByLabelText('进度视图'));
    fireEvent.keyDown(window, { key: 'z', metaKey: true });
    await screen.findByRole('button', { name: '临时阶段' }).then(() => {}).catch(() => {});
    // refresh 是异步微任务，等快照落地
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.querySelectorAll('[role=treeitem]').length).toBe(before);
    // 确认框区域：先确认一条，再切焦点到确认框 ⌘Z 撤回
    await userEvent.click(screen.getByRole('button', { name: '确认' }));
    expect(document.querySelector('.agent-badge.agent-accepted')).not.toBeNull();
    const review = screen.getByLabelText('Agent 待确认修改');
    fireEvent.mouseDown(review);
    await new Promise((resolve) => setTimeout(resolve, 0));
    fireEvent.keyDown(window, { key: 'z', metaKey: true });
    await new Promise((resolve) => setTimeout(resolve, 0));
    // 撤回后 Agent 修改被还原：徽标消失、待确认条目清空（回到人工口径）
    expect(document.querySelector('.agent-badge')).toBeNull();
    expect(screen.getByText('没有待确认的 Agent 修改。')).toBeTruthy();
  });
});

describe('ProgressPage · 工时弹窗（用户反馈第十轮）', () => {
  it('工时默认不常驻，点「工时」按钮才弹出对话框，可关闭', async () => {
    render(<ProgressPage />);
    await screen.findByRole('heading', { level: 1, name: /TEST-ONLY/ });
    // 默认侧栏没有工时面板
    expect(document.querySelector('.progress-time')).toBeNull();
    expect(document.querySelector('.time-dialog')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /^工时/ }));
    const dialog = document.querySelector('.time-dialog')!;
    expect(dialog).toBeTruthy();
    expect(dialog.querySelector('.progress-time')).not.toBeNull();
    await userEvent.click(screen.getByRole('button', { name: '关闭' }));
    expect(document.querySelector('.progress-time')).toBeNull();
  });
});
