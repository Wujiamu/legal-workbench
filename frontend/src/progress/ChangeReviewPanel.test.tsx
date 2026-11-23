/**
 * Agent 待确认修改面板：修改前/后对照、确认、撤回（总体计划 6.4）。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ChangeReviewPanel } from './ChangeReviewPanel';
import { buildLitigationFixture } from './fixtures';
import { defaultRules } from './rules';
import type { ProgressCommand } from './types';

function setup() {
  const executor = buildLitigationFixture();
  const onCommand = vi.fn((command: ProgressCommand) => executor.human(command));
  const view = render(
    <ChangeReviewPanel snapshot={executor.getSnapshot()} rules={defaultRules} onCommand={onCommand} />,
  );
  return { executor, onCommand, view };
}

describe('ChangeReviewPanel', () => {
  it('列出待确认变更，显示命令、修改前后状态对照', () => {
    setup();
    const panel = screen.getByRole('region', { name: 'Agent 待确认修改' });
    expect(panel.textContent).toContain('设置当前阶段');
    expect(panel.textContent).toContain('庭审');
    expect(panel.textContent).toContain('未开始 → 进行中');
    expect(panel.textContent).toContain('庭前程序');
    expect(panel.textContent).toContain('进行中 → 已完成');
  });

  it('确认按钮发出 accept_change', () => {
    const { executor, onCommand } = setup();
    const change = executor.getSnapshot().pending_changes[0];
    fireEvent.click(screen.getByRole('button', { name: '确认' }));
    expect(onCommand).toHaveBeenCalledWith({ type: 'accept_change', change_id: change.id });
  });

  it('撤回按钮发出 revert_change', () => {
    const { executor, onCommand } = setup();
    const change = executor.getSnapshot().pending_changes[0];
    fireEvent.click(screen.getByRole('button', { name: '撤回' }));
    expect(onCommand).toHaveBeenCalledWith({ type: 'revert_change', change_id: change.id });
  });

  it('撤回冲突时显示原因，变更仍保留', async () => {
    const { executor, view } = setup();
    const hearing = executor.getSnapshot().nodes.find((node) => node.title === '庭审')!;
    executor.human({
      type: 'update_node', node_id: hearing.id, expected_revision: hearing.revision,
      changes: { title: '庭审（人工改）' },
    });
    view.rerender(
      <ChangeReviewPanel snapshot={executor.getSnapshot()} rules={defaultRules}
        onCommand={(command) => executor.human(command)} />,
    );
    fireEvent.click(screen.getByRole('button', { name: '撤回' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('已被后续修改'));
    expect(screen.getByRole('region', { name: 'Agent 待确认修改' }).textContent).toContain('设置当前阶段');
  });

  it('没有待确认修改时显示空态', () => {
    const { executor, view } = setup();
    const change = executor.getSnapshot().pending_changes[0];
    executor.human({ type: 'accept_change', change_id: change.id });
    view.rerender(
      <ChangeReviewPanel snapshot={executor.getSnapshot()} rules={defaultRules}
        onCommand={(command) => executor.human(command)} />,
    );
    expect(screen.getByRole('region', { name: 'Agent 待确认修改' }).textContent)
      .toContain('没有待确认的 Agent 修改');
  });

  it('新建节点类变更显示标题与种类', () => {
    const executor = buildLitigationFixture();
    executor.agent({
      type: 'add_node', parent_id: null, kind: 'stage', title: 'Agent 建议阶段', request_key: 'k-1',
    });
    render(<ChangeReviewPanel snapshot={executor.getSnapshot()} rules={defaultRules}
      onCommand={(command) => executor.human(command)} />);
    const panel = screen.getByRole('region', { name: 'Agent 待确认修改' });
    expect(panel.textContent).toContain('新建节点');
    expect(panel.textContent).toContain('Agent 建议阶段');
    expect(within(panel).getAllByRole('button', { name: '确认' })).toHaveLength(2);
  });
});