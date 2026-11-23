/**
 * P1 内存执行器回归：四档状态与出处不变量、一案多图、证据编号与待确认隔离、
 * 快照冻结/漂移/恢复/对比、客户同意追加、Agent 权限与幂等、归档只读、撤销。
 */
import { describe, expect, it } from 'vitest';
import { buildGraphFixture } from './fixtures';
import type { CommandResult, GraphExecutor, GraphSnapshot } from './types';
import {
  buildCanvasModel, buildSnapshotPayload, diffPayloads, pendingChangesInGraph,
} from './model';

function must(result: CommandResult): Extract<CommandResult, { ok: true }> {
  if (!result.ok) throw new Error(`命令失败：${result.message}`);
  return result;
}

function graphId(exec: GraphExecutor, title: string): string {
  const graph = exec.getSnapshot().graphs.find((item) => item.title === title && item.removed_at === null);
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

function active(ws: GraphSnapshot) {
  return {
    entities: ws.entities.filter((item) => item.removed_at === null),
    relations: ws.relations.filter((item) => item.removed_at === null),
    sources: ws.sources.filter((item) => item.removed_at === null),
  };
}

describe('夹具：一案两图共享事实 + 待确认 + 旧快照', () => {
  it('两图、主体池、四档状态、旧快照与 4 条待确认变更齐备', () => {
    const exec = buildGraphFixture();
    const ws = exec.getSnapshot();
    expect(ws.graphs.filter((graph) => graph.removed_at === null)).toHaveLength(2);
    expect(active(ws).entities).toHaveLength(7);
    expect(active(ws).relations).toHaveLength(9);
    expect(ws.pending_changes).toHaveLength(4);
    expect(ws.snapshots).toHaveLength(1);
    expect(ws.consents).toHaveLength(1);
    const equity = buildCanvasModel(ws, graphId(exec, '股权与实控关系'));
    expect(equity.nodes.map((node) => node.name)).toContain('TEST-ONLY 张三');
    // 担保关系：Agent 已把状态改为有证据，但仍是待确认（黄标），工作稿即时生效。
    const guarantee = equity.edges.find((edge) => edge.label === '货款保证')!;
    expect(guarantee.credibility).toBe('evidenced');
    expect(guarantee.pending).toBe(true);
    expect(equity.edges.some((edge) => edge.credibility === 'suspected')).toBe(true);
  });
});

describe('「有证据」必须有出处，删除最后出处必须显式降级', () => {
  it('新建关系直接设为有证据被拒；无出处升级被拒；补出处后可升级', () => {
    const exec = buildGraphFixture();
    const rejected = exec.human({
      type: 'add_relation', from_entity_id: entityOf(exec, '张三').id,
      to_entity_id: entityOf(exec, '甲公司').id, kind: 'contract',
      label: '新合同', credibility: 'evidenced',
    });
    expect(rejected.ok).toBe(false);
    if (!rejected.ok) expect(rejected.message).toContain('有证据');

    const office = relationOf(exec, '董事');
    const upgrade = exec.human({
      type: 'update_relation', relation_id: office.id,
      expected_revision: office.revision, changes: { credibility: 'evidenced' },
    });
    expect(upgrade.ok).toBe(false);
    if (!upgrade.ok) expect(upgrade.message).toContain('出处');

    must(exec.human({
      type: 'add_source', relation_id: office.id,
      ref: { segment_id: 'seg-board-2' }, proves: '股东会决议同意担保',
    }));
    must(exec.human({
      type: 'update_relation', relation_id: office.id,
      expected_revision: office.revision, changes: { credibility: 'evidenced' },
    }));
    expect(relationOf(exec, '董事').credibility).toBe('evidenced');
  });

  it('删除「有证据」关系最后一条出处：未选降级状态被拒，选了才原子降级', () => {
    const exec = buildGraphFixture();
    const office = relationOf(exec, '董事');
    must(exec.human({
      type: 'add_source', relation_id: office.id,
      ref: { segment_id: 'seg-board-2' }, proves: '股东会决议同意担保',
    }));
    must(exec.human({
      type: 'update_relation', relation_id: office.id,
      expected_revision: office.revision, changes: { credibility: 'evidenced' },
    }));
    const source = exec.getSnapshot().sources.find((item) => item.relation_id === office.id
      && item.removed_at === null)!;
    const refused = exec.human({ type: 'remove_source', source_id: source.id });
    expect(refused.ok).toBe(false);
    if (!refused.ok) {
      expect(refused.message).toContain('降级');
      expect(refused.message).toContain('不会静默降级');
    }
    const downgraded = must(exec.human({
      type: 'remove_source', source_id: source.id, downgrade_to: 'suspected',
    }));
    expect(downgraded.notice).toContain('降级');
    expect(relationOf(exec, '董事').credibility).toBe('suspected');
  });
});

describe('一案多图：共享事实、独立成员', () => {
  it('在任一图改状态，另一图同步；从本图移除不影响案件池与其他图', () => {
    const exec = buildGraphFixture();
    const equityId = graphId(exec, '股权与实控关系');
    const flowId = graphId(exec, '履约与资金往来');
    const guarantee = relationOf(exec, '货款保证');
    must(exec.human({
      type: 'update_relation', relation_id: guarantee.id,
      expected_revision: guarantee.revision, changes: { label: '货款保证（同步验证）' },
    }));
    let ws = exec.getSnapshot();
    expect(buildCanvasModel(ws, equityId).edges.some((edge) => edge.label === '货款保证（同步验证）')).toBe(true);
    expect(buildCanvasModel(ws, flowId).edges.some((edge) => edge.label === '货款保证（同步验证）')).toBe(true);

    const yi = entityOf(exec, '乙公司');
    must(exec.human({ type: 'remove_from_graph', graph_id: equityId, entity_id: yi.id }));
    ws = exec.getSnapshot();
    expect(buildCanvasModel(ws, equityId).nodes.some((node) => node.entity_id === yi.id)).toBe(false);
    // 连到乙公司的图内边一并移除，但案件级关系仍在，另一图仍有成员。
    expect(buildCanvasModel(ws, equityId).edges.some((edge) => edge.label === '货款保证（同步验证）')).toBe(false);
    expect(buildCanvasModel(ws, flowId).edges.some((edge) => edge.label === '货款保证（同步验证）')).toBe(true);
    expect(ws.relations.some((item) => item.id === guarantee.id && item.removed_at === null)).toBe(true);
    expect(ws.entities.some((item) => item.id === yi.id && item.removed_at === null)).toBe(true);
    expect(exec.getImpact({ entity_id: yi.id }).graphs.map((graph) => graph.title))
      .toEqual(['履约与资金往来']);
  });

  it('删除全案主体前能列出受影响图；软删除后两图都隐藏，恢复后回来', () => {
    const exec = buildGraphFixture();
    const yi = entityOf(exec, '乙公司');
    expect(exec.getImpact({ entity_id: yi.id }).graphs).toHaveLength(2);
    const removed = must(exec.human({
      type: 'remove_entity', entity_id: yi.id, expected_revision: yi.revision,
    }));
    expect(removed.notice).toContain('影响 2 张图');
    const ws = exec.getSnapshot();
    expect(buildCanvasModel(ws, graphId(exec, '股权与实控关系')).nodes.some((node) => node.entity_id === yi.id)).toBe(false);
    must(exec.human({
      type: 'restore_entity', entity_id: yi.id,
      expected_revision: exec.getSnapshot().entities.find((item) => item.id === yi.id)!.revision,
    }));
    expect(buildCanvasModel(exec.getSnapshot(), graphId(exec, '股权与实控关系'))
      .nodes.some((node) => node.entity_id === yi.id)).toBe(true);
  });
});

describe('快照：排除待确认、冻结时编号、不可变与编号漂移', () => {
  it('有待确认时先提示数量；继续冻结只含已确认状态并冻结当时编号', () => {
    const exec = buildGraphFixture();
    const equityId = graphId(exec, '股权与实控关系');
    const ws = exec.getSnapshot();
    expect(pendingChangesInGraph(ws, equityId)).toHaveLength(2);
    const refused = exec.human({
      type: 'create_snapshot', graph_id: equityId, stage_label: '庭前',
    });
    expect(refused.ok).toBe(false);
    if (!refused.ok) {
      expect(refused.message).toContain('2 条');
      expect(refused.message).toContain('待确认');
    }
    const created = must(exec.human({
      type: 'create_snapshot', graph_id: equityId, stage_label: '庭前',
      note: '庭前定稿', acknowledge_pending: true,
    }));
    expect(created.notice).toContain('不含 2 条待确认变更');
    const after = exec.getSnapshot();
    const snapshot = after.snapshots.find((item) => item.stage_label === '庭前')!;
    expect(snapshot.excluded_pending_count).toBe(2);
    const guarantee = snapshot.payload.edges.find((edge) => edge.label === '货款保证')!;
    expect(guarantee.credibility).toBe('suspected');
    expect(guarantee.sources).toHaveLength(0);
    expect(snapshot.payload.nodes.some((node) => node.name.includes('丁物流'))).toBe(false);
    // 编号漂移用夹具中「重排前」冻结的 v1（方案沟通）验证：冻结时 证据1-2，工作稿现为 证据1-1。
    const oldSnapshot = after.snapshots.find((item) => item.stage_label === '方案沟通')!;
    const oldContract = oldSnapshot.payload.edges.find((edge) => edge.label === '持股 45%')!;
    expect(oldContract.sources[0].label).toBe('证据1-2 第2页');
    expect(after.labels[oldContract.sources[0].source_id].text).toBe('证据1-1 第2页');
    // 新冻结的快照按当前编号冻结，不与现行目录漂移。
    const currentContract = snapshot.payload.edges.find((edge) => edge.label === '持股 45%')!;
    expect(currentContract.sources[0].label).toBe('证据1-1 第2页');
  });

  it('冻结后继续改工作稿与目录，快照正文不变', () => {
    const exec = buildGraphFixture();
    const equityId = graphId(exec, '股权与实控关系');
    must(exec.human({
      type: 'create_snapshot', graph_id: equityId, stage_label: '庭前', acknowledge_pending: true,
    }));
    const frozen = exec.getSnapshot().snapshots.find((item) => item.stage_label === '庭前')!;
    const frozenText = JSON.stringify(frozen.payload);
    const guarantee = relationOf(exec, '货款保证');
    must(exec.human({
      type: 'update_relation', relation_id: guarantee.id,
      expected_revision: guarantee.revision, changes: { label: '担保（庭后补证）' },
    }));
    const reordered = must(exec.human({
      type: 'reorder_evidence_groups',
      group_ids: [...exec.getSnapshot().evidence.groups
        .filter((group) => group.removed_at === null)
        .sort((a, b) => b.position - a.position)
        .map((group) => group.id)],
    }));
    expect(reordered.notice).toContain('编号已重算');
    const after = exec.getSnapshot();
    expect(JSON.stringify(after.snapshots.find((item) => item.id === frozen.id)!.payload)).toBe(frozenText);
  });
});

describe('快照恢复与撤销', () => {
  it('有待确认变更时拒绝恢复；复核后可恢复，恢复可撤销', () => {
    const exec = buildGraphFixture();
    const equityId = graphId(exec, '股权与实控关系');
    const snapshot = exec.getSnapshot().snapshots[0];
    const blocked = exec.human({ type: 'restore_snapshot', snapshot_id: snapshot.id });
    expect(blocked.ok).toBe(false);
    if (!blocked.ok) expect(blocked.message).toContain('待确认');

    const ws = exec.getSnapshot();
    must(exec.human({ type: 'accept_all', expected_seq: ws.seq }));

    const zhangNode = exec.getSnapshot().nodes.find((node) => node.graph_id === equityId
      && node.entity_id === entityOf(exec, '张三').id)!;
    must(exec.human({
      type: 'save_layout', graph_id: equityId,
      nodes: [{ node_id: zhangNode.id, x: 1000, y: 1000 }],
    }));
    const equityZhang = relationOf(exec, '持股 60%');
    must(exec.human({
      type: 'update_relation', relation_id: equityZhang.id,
      expected_revision: equityZhang.revision, changes: { credibility: 'suspected' },
    }));
    const guarantee = relationOf(exec, '货款保证');
    expect(guarantee.credibility).toBe('evidenced');
    expect(active(exec.getSnapshot()).sources.some((source) => source.relation_id === guarantee.id)).toBe(true);

    const restored = must(exec.human({ type: 'restore_snapshot', snapshot_id: snapshot.id }));
    expect(restored.notice).toContain('已恢复快照 v1');
    const after = exec.getSnapshot();
    expect(after.nodes.find((node) => node.id === zhangNode.id)!.x).toBe(90);
    expect(after.relations.find((item) => item.id === equityZhang.id)!.credibility).toBe('evidenced');
    expect(after.relations.find((item) => item.id === guarantee.id)!.credibility).toBe('suspected');
    // 冻结后才补的出处被覆盖移除（快照覆盖语义）。
    expect(active(after).sources.some((source) => source.relation_id === guarantee.id)).toBe(false);

    const undone = must(exec.undo());
    expect(undone.notice).toContain('已撤销');
    const back = exec.getSnapshot();
    expect(back.nodes.find((node) => node.id === zhangNode.id)!.x).toBe(1000);
    expect(back.relations.find((item) => item.id === guarantee.id)!.credibility).toBe('evidenced');
    expect(active(back).sources.some((source) => source.relation_id === guarantee.id)).toBe(true);
  });
});

describe('客户同意：仅挂快照、追加留痕、不可修改', () => {
  it('确认后可追加更正与撤回；撤回必须指向原记录', () => {
    const exec = buildGraphFixture();
    const snapshot = exec.getSnapshot().snapshots[0];
    const original = exec.getSnapshot().consents[0];
    const base = {
      type: 'add_snapshot_consent' as const, snapshot_id: snapshot.id,
      consenter: '客户 王总', consented_at: '2026-11-20T10:00', channel: 'email',
      source_detail: '邮件回复确认', note: '客户部分同意',
    };
    const noParent = exec.human({ ...base, action: 'revoke' });
    expect(noParent.ok).toBe(false);
    const badChannel = exec.human({ ...base, action: 'confirm', channel: 'bogus' });
    expect(badChannel.ok).toBe(false);

    const amended = must(exec.human({
      ...base, action: 'amend', parent_consent_id: original.id,
      note: '客户部分同意：补充确认资金部分',
    }));
    expect(amended.notice).toContain('更正');
    const amendRecord = exec.getSnapshot().consents.find((item) => item.action === 'amend')!;
    must(exec.human({
      ...base, action: 'revoke', parent_consent_id: amendRecord.id,
      source_detail: '客户电话撤回',
    }));
    const consents = exec.getSnapshot().consents;
    expect(consents).toHaveLength(3);
    expect(consents[0].action).toBe('confirm');
    expect(consents[0].note).toContain('客户部分同意');
    expect(consents[2].parent_consent_id).toBe(amendRecord.id);
    // 已撤回记录不能再被更正。
    const again = exec.human({
      ...base, action: 'amend', parent_consent_id: consents[2].id,
    });
    expect(again.ok).toBe(false);
  });
});

describe('Agent 权限、幂等与归档只读', () => {
  it('Agent 不能冻结/恢复/同意/确认/撤回/改布局', () => {
    const exec = buildGraphFixture();
    const ws = exec.getSnapshot();
    const snapshot = ws.snapshots[0];
    const commands = [
      { type: 'create_snapshot' as const, graph_id: ws.graphs[0].id, stage_label: '庭前' },
      { type: 'restore_snapshot' as const, snapshot_id: snapshot.id },
      {
        type: 'add_snapshot_consent' as const, snapshot_id: snapshot.id, action: 'confirm' as const,
        consenter: '客户', consented_at: '2026-11-20T10:00', channel: 'email',
        source_detail: '邮件',
      },
      { type: 'accept_change' as const, change_id: ws.pending_changes[0].id },
      { type: 'revert_change' as const, change_id: ws.pending_changes[0].id },
      { type: 'save_layout' as const, graph_id: ws.graphs[0].id, nodes: [] },
      { type: 'remove_entity' as const, entity_id: entityOf(exec, '张三').id, expected_revision: 0 },
    ];
    for (const command of commands) {
      const result = exec.agent(command);
      expect(result.ok).toBe(false);
      if (!result.ok) expect(result.message).toContain('Agent 无权');
    }
  });

  it('重复 request_key 只创建一次；归档后全部写命令被拒、读取正常', () => {
    const exec = buildGraphFixture();
    const before = active(exec.getSnapshot()).entities.length;
    const command = {
      type: 'add_entity' as const, kind: 'person', name: 'TEST-ONLY 赵六',
      roles: ['witness'], request_key: 'idem-zhao',
    };
    must(exec.agent(command));
    const seqAfterFirst = exec.getSeqs().graph;
    must(exec.agent(command));
    expect(active(exec.getSnapshot()).entities.length).toBe(before + 1);
    expect(exec.getSeqs().graph).toBe(seqAfterFirst);

    exec.setArchived(true);
    expect(exec.getSnapshot().archived).toBe(true);
    const blocked = exec.human({
      type: 'update_entity', entity_id: entityOf(exec, '张三').id, expected_revision: 0,
      changes: { note: '归档后不应写入' },
    });
    expect(blocked.ok).toBe(false);
    if (!blocked.ok) expect(blocked.code).toBe('read_only');
    expect(exec.getSnapshot().entities.length).toBeGreaterThan(0);
    exec.setArchived(false);
  });
});

describe('证据目录：调序重算、外部导入待确认隔离', () => {
  it('分组调序后编号按 证据{组}-{序} 重算', () => {
    const exec = buildGraphFixture();
    const ws = exec.getSnapshot();
    const groups = ws.evidence.groups.filter((group) => group.removed_at === null)
      .sort((a, b) => a.position - b.position);
    const contract = ws.evidence.items.find((item) => item.title.includes('股权转让协议'))!;
    const registry = ws.evidence.items.find((item) => item.title.includes('公司章程'))!;
    const bank = ws.evidence.items.find((item) => item.title.includes('银行流水'))!;
    expect(ws.evidence_numbers[contract.id]).toBe('证据1-1');
    expect(ws.evidence_numbers[registry.id]).toBe('证据1-2');
    must(exec.human({
      type: 'reorder_evidence_groups', group_ids: [groups[1].id, groups[0].id, groups[2].id],
    }));
    const after = exec.getSnapshot();
    expect(after.evidence_numbers[bank.id]).toBe('证据1-1');
    expect(after.evidence_numbers[contract.id]).toBe('证据2-1');
    expect(after.evidence_numbers[registry.id]).toBe('证据2-2');
  });

  it('外部导入的条目与绑定在人工确认前不参与编号；确认后出处立即带编号', () => {
    const exec = buildGraphFixture();
    const agency = relationOf(exec, '代理签约');
    const agencySource = exec.getSnapshot().sources.find((source) => source.relation_id === agency.id
      && source.removed_at === null)!;
    expect(exec.getSnapshot().labels[agencySource.id].text)
      .toBe('TEST-ONLY 补充协议.pdf（未编号）');

    must(exec.agent({ type: 'import_evidence_catalog', request_key: 'import-demo-1' }));
    let ws = exec.getSnapshot();
    const imported = ws.evidence.items.filter((item) => item.review_state === 'pending');
    expect(imported).toHaveLength(2);
    for (const item of imported) expect(ws.evidence_numbers[item.id]).toBeUndefined();
    expect(ws.labels[agencySource.id].text).toContain('未编号');

    const supplement = imported.find((item) => item.title.includes('补充协议'))!;
    must(exec.human({
      type: 'confirm_evidence_item', item_id: supplement.id, expected_revision: supplement.revision,
    }));
    ws = exec.getSnapshot();
    // 条目确认后即占编号；但绑定未确认前，出处仍显示「未编号」。
    expect(ws.evidence_numbers[supplement.id]).toBe('证据4-1');
    expect(ws.labels[agencySource.id].text).toContain('未编号');
    const binding = ws.evidence.bindings.find((item) => item.item_id === supplement.id
      && item.removed_at === null)!;
    must(exec.human({ type: 'confirm_evidence_binding', binding_id: binding.id }));
    ws = exec.getSnapshot();
    expect(ws.evidence_numbers[supplement.id]).toBe('证据4-1');
    expect(ws.labels[agencySource.id].text).toBe('证据4-1 第1页');

    // 第二条导入（微信第 2 页）确认后，同一页命中两个目录条目，按目录顺序全部列出。
    const chatItem = ws.evidence.items.find((item) => item.review_state === 'pending')!;
    must(exec.human({
      type: 'confirm_evidence_item', item_id: chatItem.id, expected_revision: chatItem.revision,
    }));
    const chatBinding = exec.getSnapshot().evidence.bindings.find((item) =>
      item.item_id === chatItem.id && item.removed_at === null)!;
    must(exec.human({ type: 'confirm_evidence_binding', binding_id: chatBinding.id }));
    ws = exec.getSnapshot();
    const guaranteeSource = ws.sources.find((source) => source.relation_id
      === relationOf(exec, '货款保证').id && source.removed_at === null)!;
    expect(ws.labels[guaranteeSource.id].text).toContain('证据3-1 第2页');
    expect(ws.labels[guaranteeSource.id].text).toContain('证据4-2 第2页');
  });

  it('导入草稿可整体确认或撤回；已人工确认的条目不被整体撤回', () => {
    const exec = buildGraphFixture();
    must(exec.agent({ type: 'import_evidence_catalog', request_key: 'import-demo-2' }));
    const change = exec.getSnapshot().pending_changes.find((item) =>
      item.command === 'import_evidence_catalog')!;
    must(exec.human({ type: 'accept_evidence_change', change_id: change.id }));
    const afterAccept = exec.getSnapshot();
    const confirmed = afterAccept.evidence.items.filter((item) => item.source_kind === 'imported');
    expect(confirmed.every((item) => item.review_state === 'confirmed')).toBe(true);
    expect(confirmed.every((item) => Boolean(afterAccept.evidence_numbers[item.id]))).toBe(true);

    must(exec.agent({ type: 'import_evidence_catalog', request_key: 'import-demo-3' }));
    const second = exec.getSnapshot().pending_changes.find((item) =>
      item.command === 'import_evidence_catalog')!;
    must(exec.human({ type: 'revert_evidence_change', change_id: second.id }));
    const afterRevert = exec.getSnapshot();
    expect(afterRevert.evidence.items.filter((item) => item.source_kind === 'imported'
      && item.review_state === 'pending' && item.removed_at === null)).toHaveLength(0);
  });
});

describe('撤销与快照对比', () => {
  it('人工写命令可撤销；有更新的 Agent 待确认时拒绝撤销', () => {
    const exec = buildGraphFixture();
    const office = relationOf(exec, '董事');
    must(exec.human({
      type: 'update_relation', relation_id: office.id,
      expected_revision: office.revision, changes: { label: '董事（改）' },
    }));
    expect(relationOf(exec, '董事（改）')).toBeTruthy();
    must(exec.undo());
    expect(relationOf(exec, '董事')).toBeTruthy();

    must(exec.human({
      type: 'update_relation', relation_id: office.id,
      expected_revision: relationOf(exec, '董事').revision, changes: { note: '撤销前' },
    }));
    must(exec.agent({
      type: 'add_entity', kind: 'person', name: 'TEST-ONLY 钱七',
      roles: ['witness'], request_key: 'idem-qian',
    }));
    const blocked = exec.undo();
    expect(blocked.ok).toBe(false);
    if (!blocked.ok) expect(blocked.message).toContain('待确认');
  });

  it('快照 vs 当前工作稿：标出状态变化、新增关系与布局移动', () => {
    const exec = buildGraphFixture();
    const equityId = graphId(exec, '股权与实控关系');
    const snapshot = exec.getSnapshot().snapshots[0];
    const zhangNode = exec.getSnapshot().nodes.find((node) => node.graph_id === equityId
      && node.entity_id === entityOf(exec, '张三').id)!;
    must(exec.human({
      type: 'save_layout', graph_id: equityId,
      nodes: [{ node_id: zhangNode.id, x: 120, y: 180 }],
    }));
    const ws = exec.getSnapshot();
    const current = buildSnapshotPayload(ws, equityId, { excludePending: false });
    const diff = diffPayloads(snapshot.payload, current);
    expect(diff.movedNodes.some((node) => node.name.includes('张三'))).toBe(true);
    expect(diff.changedEdges.some((edge) => edge.label === '货款保证'
      && edge.fields.some((field) => field.field === '可信状态'))).toBe(true);
    expect(diff.totalChanges).toBeGreaterThan(0);
  });
});
