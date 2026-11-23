/**
 * 快照保真与证据守卫回归（P1 内存执行器）。
 * 复现两条失败链：
 * 1) Agent 待确认出处被当作有效出处，人工升级「有证据」后冻结出 sources=[] 的实线；
 * 2) 连续两条 Agent 改标签的待确认被叠加时，快照取到「待确认A」而不是原始「董事」。
 * 同时固定：人工改动与旧待确认重叠、复核顺序、撤回依赖、分图命名/别名/出处排除待确认。
 */
import { describe, expect, it } from 'vitest';
import { buildGraphFixture } from './fixtures';
import type { CommandResult, GraphExecutor, GraphSnapshot, SnapshotPayload } from './types';
import { pendingChangesInGraph } from './model';

function must(result: CommandResult): Extract<CommandResult, { ok: true }> {
  if (!result.ok) throw new Error(`命令失败：${result.message}`);
  return result;
}

function graphId(exec: GraphExecutor, title: string): string {
  const graph = exec.getSnapshot().graphs.find((item) => item.title === title
    && item.removed_at === null);
  if (!graph) throw new Error(`缺少关系图：${title}`);
  return graph.id;
}

function entityOf(exec: GraphExecutor, name: string) {
  const entity = exec.getSnapshot().entities.find((item) => item.name.includes(name)
    && item.removed_at === null);
  if (!entity) throw new Error(`缺少主体：${name}`);
  return entity;
}

function relationOf(exec: GraphExecutor, label: string) {
  const relation = exec.getSnapshot().relations.find((item) => item.label === label
    && item.removed_at === null);
  if (!relation) throw new Error(`缺少关系：${label}`);
  return relation;
}

function frozen(exec: GraphExecutor, stage: string): SnapshotPayload {
  const snapshot = exec.getSnapshot().snapshots.find((item) => item.stage_label === stage);
  if (!snapshot) throw new Error(`缺少快照：${stage}`);
  return snapshot.payload;
}

function pendingOf(exec: GraphExecutor, command: string, targetId?: string) {
  const change = exec.getSnapshot().pending_changes.find((item) => item.command === command
    && (targetId === undefined
      || item.diff.some((entry) => {
        const record = (entry.after ?? entry.before) as Record<string, unknown> | null;
        return entry.id === targetId || record?.relation_id === targetId
          || record?.entity_id === targetId;
      })));
  if (!change) throw new Error(`缺少待确认变更：${command}`);
  return change;
}

/** 不变量：冻结载荷里的「有证据」边必须带至少一条真实出处。 */
function expectEvidencedEdgesHaveSources(payload: SnapshotPayload): void {
  for (const edge of payload.edges) {
    if (edge.credibility !== 'evidenced') continue;
    expect(edge.sources.length, `实线 ${edge.label ?? edge.kind} 缺少出处`).toBeGreaterThan(0);
    for (const source of edge.sources) expect(source.label).not.toContain('已失效');
  }
}

function wsOf(exec: GraphExecutor): GraphSnapshot {
  return exec.getSnapshot();
}

describe('失败链 1：Agent 待确认出处不得被当作有效证据', () => {
  it('人工先升级「有证据」被拒；确认出处后升级，快照实线带真实出处', () => {
    const exec = buildGraphFixture();
    const equityId = graphId(exec, '股权与实控关系');
    const office = relationOf(exec, '董事');

    must(exec.agent({
      type: 'add_source', relation_id: office.id, ref: { segment_id: 'seg-board-2' },
      proves: '股东会决议同意提供保证', request_key: 'agent-source-office',
    }));

    const refused = exec.human({
      type: 'update_relation', relation_id: office.id,
      expected_revision: relationOf(exec, '董事').revision,
      changes: { credibility: 'evidenced' },
    });
    expect(refused.ok).toBe(false);
    if (!refused.ok) expect(refused.message).toContain('待确认');

    // 待确认出处不计入有效出处：即便强行冻结（已知悉待确认），也不能出现无出处的实线。
    must(exec.human({
      type: 'create_snapshot', graph_id: equityId, stage_label: '庭前',
      acknowledge_pending: true,
    }));
    const beforeAccept = frozen(exec, '庭前');
    const officeEdge = beforeAccept.edges.find((edge) => edge.relation_id === office.id)!;
    expect(officeEdge.credibility).toBe('suspected');
    expect(officeEdge.sources).toHaveLength(0);
    expectEvidencedEdgesHaveSources(beforeAccept);

    // 确认该出处后才允许升级；冻结的实线必须带已确认出处。
    must(exec.human({
      type: 'accept_change', change_id: pendingOf(exec, 'add_source', office.id).id,
    }));
    must(exec.human({
      type: 'update_relation', relation_id: office.id,
      expected_revision: relationOf(exec, '董事').revision,
      changes: { credibility: 'evidenced' },
    }));
    must(exec.human({
      type: 'create_snapshot', graph_id: equityId, stage_label: '庭后',
      acknowledge_pending: true,
    }));
    const afterAccept = frozen(exec, '庭后');
    const evidencedOffice = afterAccept.edges.find((edge) => edge.relation_id === office.id)!;
    expect(evidencedOffice.credibility).toBe('evidenced');
    expect(evidencedOffice.sources).toHaveLength(1);
    expect(evidencedOffice.sources[0].label).toBe('证据2-2 第2页');
    expectEvidencedEdgesHaveSources(afterAccept);
  });

  it('Agent 同批提案（先补出处再升级）仍可提交，但须按顺序确认', () => {
    const exec = buildGraphFixture();
    const office = relationOf(exec, '董事');
    must(exec.agent({
      type: 'add_source', relation_id: office.id, ref: { segment_id: 'seg-board-2' },
      proves: '股东会决议同意提供保证', request_key: 'agent-source-office',
    }));
    must(exec.agent({
      type: 'update_relation', relation_id: office.id,
      expected_revision: relationOf(exec, '董事').revision,
      changes: { credibility: 'evidenced' },
    }));
    const upgrade = pendingOf(exec, 'update_relation', office.id);
    const sourceChange = pendingOf(exec, 'add_source', office.id);

    const early = exec.human({ type: 'accept_change', change_id: upgrade.id });
    expect(early.ok).toBe(false);
    if (!early.ok) expect(early.message).toContain('出处');

    must(exec.human({ type: 'accept_change', change_id: sourceChange.id }));
    must(exec.human({ type: 'accept_change', change_id: upgrade.id }));
    expect(relationOf(exec, '董事').credibility).toBe('evidenced');
  });

  it('撤回待确认出处前，必须先撤回依赖它的「有证据」升级', () => {
    const exec = buildGraphFixture();
    const office = relationOf(exec, '董事');
    must(exec.agent({
      type: 'add_source', relation_id: office.id, ref: { segment_id: 'seg-board-2' },
      proves: '股东会决议同意提供保证', request_key: 'agent-source-office',
    }));
    must(exec.agent({
      type: 'update_relation', relation_id: office.id,
      expected_revision: relationOf(exec, '董事').revision,
      changes: { credibility: 'evidenced' },
    }));
    const sourceChange = pendingOf(exec, 'add_source', office.id);
    const upgrade = pendingOf(exec, 'update_relation', office.id);

    const blocked = exec.human({ type: 'revert_change', change_id: sourceChange.id });
    expect(blocked.ok).toBe(false);
    if (!blocked.ok) expect(blocked.message).toContain('有证据');

    must(exec.human({ type: 'revert_change', change_id: upgrade.id }));
    must(exec.human({ type: 'revert_change', change_id: sourceChange.id }));
    expect(relationOf(exec, '董事').credibility).toBe('suspected');
    expect(wsOf(exec).sources.some((source) => source.relation_id === office.id
      && source.removed_at === null)).toBe(false);
  });
});

describe('失败链 2：叠加待确认不得污染冻结载荷', () => {
  it('连续两次 Agent 改标签，快照保留原始「董事」；复核须按顺序', () => {
    const exec = buildGraphFixture();
    const equityId = graphId(exec, '股权与实控关系');
    const office = relationOf(exec, '董事');

    must(exec.agent({
      type: 'update_relation', relation_id: office.id,
      expected_revision: office.revision, changes: { label: '待确认A' },
    }));
    const first = pendingOf(exec, 'update_relation', office.id);
    must(exec.agent({
      type: 'update_relation', relation_id: office.id,
      expected_revision: relationOf(exec, '待确认A').revision,
      changes: { label: '待确认B' },
    }));
    const second = exec.getSnapshot().pending_changes
      .filter((change) => change.command === 'update_relation'
        && change.diff.some((entry) => entry.target === 'relation' && entry.id === office.id))
      .pop()!;
    expect(second.id).not.toBe(first.id);
    expect(relationOf(exec, '待确认B')).toBeTruthy();

    must(exec.human({
      type: 'create_snapshot', graph_id: equityId, stage_label: '庭前',
      acknowledge_pending: true,
    }));
    const payload = frozen(exec, '庭前');
    const edge = payload.edges.find((item) => item.relation_id === office.id)!;
    expect(edge.label).toBe('董事');
    expectEvidencedEdgesHaveSources(payload);

    // 超前确认第二条被拒：必须按写入顺序复核。
    const outOfOrder = exec.human({ type: 'accept_change', change_id: second.id });
    expect(outOfOrder.ok).toBe(false);
    if (!outOfOrder.ok) expect(outOfOrder.message).toContain('顺序');

    must(exec.human({ type: 'accept_change', change_id: first.id }));
    must(exec.human({ type: 'accept_change', change_id: second.id }));
    expect(relationOf(exec, '待确认B')).toBeTruthy();
    must(exec.human({
      type: 'create_snapshot', graph_id: equityId, stage_label: '庭后',
      acknowledge_pending: true,
    }));
    expect(frozen(exec, '庭后').edges.find((item) => item.relation_id === office.id)!.label)
      .toBe('待确认B');
  });

  it('撤回叠加待确认须倒序，撤完恢复原始值', () => {
    const exec = buildGraphFixture();
    const office = relationOf(exec, '董事');
    must(exec.agent({
      type: 'update_relation', relation_id: office.id,
      expected_revision: office.revision, changes: { label: '待确认A' },
    }));
    const first = pendingOf(exec, 'update_relation', office.id);
    must(exec.agent({
      type: 'update_relation', relation_id: office.id,
      expected_revision: relationOf(exec, '待确认A').revision,
      changes: { label: '待确认B' },
    }));
    const second = exec.getSnapshot().pending_changes
      .filter((change) => change.command === 'update_relation'
        && change.diff.some((entry) => entry.target === 'relation' && entry.id === office.id))
      .pop()!;

    const early = exec.human({ type: 'revert_change', change_id: first.id });
    expect(early.ok).toBe(false);
    if (!early.ok) expect(early.message).toContain('顺序');

    must(exec.human({ type: 'revert_change', change_id: second.id }));
    expect(relationOf(exec, '待确认A')).toBeTruthy();
    must(exec.human({ type: 'revert_change', change_id: first.id }));
    expect(relationOf(exec, '董事')).toBeTruthy();
  });

  it('人工改写与旧待确认重叠：快照取人工值，过期提案不能确认、只能撤回', () => {
    const exec = buildGraphFixture();
    const equityId = graphId(exec, '股权与实控关系');
    const office = relationOf(exec, '董事');
    must(exec.agent({
      type: 'update_relation', relation_id: office.id,
      expected_revision: office.revision, changes: { label: '待确认A' },
    }));
    const stale = pendingOf(exec, 'update_relation', office.id);

    must(exec.human({
      type: 'update_relation', relation_id: office.id,
      expected_revision: relationOf(exec, '待确认A').revision,
      changes: { label: '人工定稿' },
    }));
    must(exec.human({
      type: 'create_snapshot', graph_id: equityId, stage_label: '庭前',
      acknowledge_pending: true,
    }));
    expect(frozen(exec, '庭前').edges.find((item) => item.relation_id === office.id)!.label)
      .toBe('人工定稿');

    const accept = exec.human({ type: 'accept_change', change_id: stale.id });
    expect(accept.ok).toBe(false);
    if (!accept.ok) expect(accept.message).toContain('人工');

    const discarded = must(exec.human({ type: 'revert_change', change_id: stale.id }));
    expect(discarded.notice).toContain('人工');
    expect(relationOf(exec, '人工定稿')).toBeTruthy();
  });
});

describe('冻结载荷涉及的其他对象同样排除待确认', () => {
  it('Agent 重命名分图、新增别名、补出处都不进入快照，且计入冻结前提示', () => {
    const exec = buildGraphFixture();
    const equityId = graphId(exec, '股权与实控关系');
    const equity = wsOf(exec).graphs.find((item) => item.id === equityId)!;
    const zhang = entityOf(exec, '张三');
    const office = relationOf(exec, '董事');

    must(exec.agent({
      type: 'update_graph', graph_id: equityId, expected_revision: equity.revision,
      changes: { title: 'Agent 拟改：股权结构' },
    }));
    must(exec.agent({ type: 'add_alias', entity_id: zhang.id, alias: 'Agent 别名·张三' }));
    must(exec.agent({
      type: 'add_source', relation_id: office.id, ref: { segment_id: 'seg-board-2' },
      proves: '股东会决议同意提供保证', request_key: 'agent-source-office',
    }));

    const before = pendingChangesInGraph(wsOf(exec), equityId);
    expect(before.some((change) => change.command === 'update_graph')).toBe(true);
    expect(before.some((change) => change.command === 'add_alias')).toBe(true);
    expect(before.some((change) => change.command === 'add_source')).toBe(true);

    must(exec.human({
      type: 'create_snapshot', graph_id: equityId, stage_label: '庭前',
      acknowledge_pending: true,
    }));
    const payload = frozen(exec, '庭前');
    expect(payload.graph.title).toBe('股权与实控关系');
    const zhangNode = payload.nodes.find((node) => node.entity_id === zhang.id)!;
    expect(zhangNode.aliases).not.toContain('Agent 别名·张三');
    const officeEdge = payload.edges.find((edge) => edge.relation_id === office.id)!;
    expect(officeEdge.sources).toHaveLength(0);
    expectEvidencedEdgesHaveSources(payload);
  });

  it('多条出处时合法操作不被阻止：可继续补出处、删其中一条、冻结仍为实线', () => {
    const exec = buildGraphFixture();
    const equityId = graphId(exec, '股权与实控关系');
    const equityZhang = relationOf(exec, '持股 60%');
    expect(equityZhang.credibility).toBe('evidenced');

    must(exec.human({
      type: 'add_source', relation_id: equityZhang.id, ref: { material_id: 'mat-registry' },
      proves: '工商档案整册',
    }));
    const sources = wsOf(exec).sources.filter((source) => source.relation_id === equityZhang.id
      && source.removed_at === null);
    expect(sources).toHaveLength(2);

    must(exec.agent({
      type: 'add_source', relation_id: equityZhang.id, ref: { segment_id: 'seg-bank-6' },
      proves: 'Agent 补充的往来款片段', request_key: 'agent-source-extra',
    }));
    must(exec.human({ type: 'remove_source', source_id: sources[0].id }));
    expect(relationOf(exec, '持股 60%').credibility).toBe('evidenced');

    must(exec.human({
      type: 'create_snapshot', graph_id: equityId, stage_label: '庭前',
      acknowledge_pending: true,
    }));
    const payload = frozen(exec, '庭前');
    const edge = payload.edges.find((item) => item.relation_id === equityZhang.id)!;
    expect(edge.credibility).toBe('evidenced');
    expect(edge.sources).toHaveLength(1);
    expectEvidencedEdgesHaveSources(payload);
  });

  it('夹具旧快照与新建快照都满足「实线必有出处」', () => {
    const exec = buildGraphFixture();
    for (const snapshot of wsOf(exec).snapshots) {
      expectEvidencedEdgesHaveSources(snapshot.payload);
    }
  });

  it('Agent 新建分图在确认前不能冻结，确认后按自身名称冻结', () => {
    const exec = buildGraphFixture();
    const created = must(exec.agent({
      type: 'create_graph', title: 'Agent 拟建：担保链', request_key: 'agent-graph-1',
    })).snapshot;
    const agentGraph = created.graphs.find((graph) => graph.title === 'Agent 拟建：担保链')!;

    const refused = exec.human({
      type: 'create_snapshot', graph_id: agentGraph.id, stage_label: '庭前',
    });
    expect(refused.ok).toBe(false);
    if (!refused.ok) expect(refused.message).toContain('待确认');

    must(exec.human({ type: 'accept_change', change_id: pendingOf(exec, 'create_graph').id }));
    must(exec.human({
      type: 'create_snapshot', graph_id: agentGraph.id, stage_label: '庭前',
    }));
    const payload = frozen(exec, '庭前');
    expect(payload.graph.title).toBe('Agent 拟建：担保链');
    expect(payload.nodes).toHaveLength(0);
  });

  it('Agent 待确认新增的主体/关系上不能直接做人工写入', () => {
    const exec = buildGraphFixture();
    const ding = entityOf(exec, '丁物流');
    const blockedEntity = exec.human({
      type: 'update_entity', entity_id: ding.id, expected_revision: ding.revision,
      changes: { note: '人工补注' },
    });
    expect(blockedEntity.ok).toBe(false);
    if (!blockedEntity.ok) expect(blockedEntity.message).toContain('待确认');

    const dingRelation = wsOf(exec).relations.find((relation) =>
      relation.label === '运输合同相对方')!;
    const blockedSource = exec.human({
      type: 'add_source', relation_id: dingRelation.id, ref: { segment_id: 'seg-bank-4' },
    });
    expect(blockedSource.ok).toBe(false);
    if (!blockedSource.ok) expect(blockedSource.message).toContain('待确认');
  });
});
