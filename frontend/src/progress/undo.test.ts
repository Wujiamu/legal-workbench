/**
 * 撤销栈（用户反馈第七轮）：按工作区域分栈（节点图 / Agent 确认框），
 * Cmd/Ctrl+Z 只撤销当前焦点区域的最近一步；高亮框标示焦点区域。
 * 原型口径：人工命令的逆操作直接执行；Agent 待确认变更走 revert_change。
 */
import { describe, expect, it } from 'vitest';
import { buildLitigationFixture } from './fixtures';
import { createUndoStacks } from './undo';

describe('撤销栈', () => {
  it('人工新增可撤销：逆操作删除该节点', () => {
    const executor = buildLitigationFixture();
    const stacks = createUndoStacks();
    const before = executor.getSnapshot().nodes.length;
    const result = executor.human({ type: 'add_node', parent_id: null, kind: 'stage', title: '临时阶段' });
    if (!result.ok) throw new Error('add failed');
    const added = result.snapshot.nodes.find((node) => node.title === '临时阶段')!;
    stacks.pushHuman('graph', { type: 'remove_node', node_id: added.id, expected_revision: added.revision });
    const undo = stacks.undo('graph', executor);
    expect(undo.ok).toBe(true);
    expect(executor.getSnapshot().nodes.filter((node) => !node.removed_at)).toHaveLength(before);
  });

  it('两个区域互不干扰：graph 撤销不影响 review 栈', () => {
    const executor = buildLitigationFixture();
    const stacks = createUndoStacks();
    const change = executor.getSnapshot().pending_changes[0];
    stacks.pushReview(change.id);
    const result = executor.human({ type: 'add_node', parent_id: null, kind: 'stage', title: 'X' });
    if (!result.ok) throw new Error('add failed');
    const added = result.snapshot.nodes.find((node) => node.title === 'X')!;
    stacks.pushHuman('graph', { type: 'remove_node', node_id: added.id, expected_revision: added.revision });
    expect(stacks.depth('review')).toBe(1);
    stacks.undo('graph', executor);
    expect(stacks.depth('review')).toBe(1);
    expect(stacks.depth('graph')).toBe(0);
    // review 栈撤销的是 Agent 变更撤回
    const undo = stacks.undo('review', executor);
    expect(undo.ok).toBe(true);
    expect(executor.getSnapshot().changes.find((item) => item.id === change.id)?.review_state).toBe('reverted');
  });

  it('空栈撤销返回不可用提示，不改动数据', () => {
    const executor = buildLitigationFixture();
    const stacks = createUndoStacks();
    const seq = executor.getSnapshot().seq;
    const undo = stacks.undo('graph', executor);
    expect(undo.ok).toBe(false);
    expect(executor.getSnapshot().seq).toBe(seq);
  });

  it('人工修改的逆操作恢复旧值', () => {
    const executor = buildLitigationFixture();
    const stacks = createUndoStacks();
    const filing = executor.getSnapshot().nodes.find((node) => node.title === '立案')!;
    const result = executor.human({
      type: 'update_node', node_id: filing.id, expected_revision: filing.revision,
      changes: { note: '新备注' },
    });
    if (!result.ok) throw new Error('update failed');
    stacks.pushHuman('graph', {
      type: 'update_node', node_id: filing.id,
      expected_revision: result.snapshot.nodes.find((node) => node.id === filing.id)!.revision,
      changes: { note: filing.note },
    });
    stacks.undo('graph', executor);
    expect(executor.getSnapshot().nodes.find((node) => node.id === filing.id)!.note).toBeNull();
  });
});