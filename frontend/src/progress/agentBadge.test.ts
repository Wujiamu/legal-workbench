/**
 * Agent 徽标口径（用户反馈第七轮）：节点后小框只写「Agent」，
 * 认可状态用颜色：待确认黄、已确认绿；被撤回的修改不再显示。
 */
import { describe, expect, it } from 'vitest';
import { buildLitigationFixture } from './fixtures';
import { agentBadgeState } from './tree';

describe('Agent 徽标状态', () => {
  it('待确认变更触及的节点为 pending（黄）', () => {
    const snapshot = buildLitigationFixture().getSnapshot();
    const hearing = snapshot.nodes.find((node) => node.title === '庭审')!;
    expect(agentBadgeState(snapshot, hearing.id, snapshot.changes)).toBe('pending');
  });

  it('确认后为 accepted（绿）', () => {
    const executor = buildLitigationFixture();
    const change = executor.getSnapshot().pending_changes[0];
    executor.human({ type: 'accept_change', change_id: change.id });
    const snapshot = executor.getSnapshot();
    const hearing = snapshot.nodes.find((node) => node.title === '庭审')!;
    expect(agentBadgeState(snapshot, hearing.id, snapshot.changes)).toBe('accepted');
  });

  it('撤回后无徽标；从未被 Agent 触及的节点无徽标', () => {
    const executor = buildLitigationFixture();
    const change = executor.getSnapshot().pending_changes[0];
    executor.human({ type: 'revert_change', change_id: change.id });
    const snapshot = executor.getSnapshot();
    const hearing = snapshot.nodes.find((node) => node.title === '庭审')!;
    expect(agentBadgeState(snapshot, hearing.id, snapshot.changes)).toBeNull();
    const filing = snapshot.nodes.find((node) => node.title === '立案')!;
    expect(agentBadgeState(snapshot, filing.id, snapshot.changes)).toBeNull();
  });

  it('同一节点既有已确认又有待确认时优先 pending', () => {
    const executor = buildLitigationFixture();
    const change = executor.getSnapshot().pending_changes[0];
    executor.human({ type: 'accept_change', change_id: change.id });
    const hearing = executor.getSnapshot().nodes.find((node) => node.title === '庭审')!;
    executor.agent({ type: 'update_node', node_id: hearing.id, expected_revision: hearing.revision, changes: { note: 'again' } });
    const final = executor.getSnapshot();
    expect(agentBadgeState(final, hearing.id, final.changes)).toBe('pending');
  });
});