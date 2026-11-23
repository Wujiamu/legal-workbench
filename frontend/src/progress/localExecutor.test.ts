/**
 * P1 内存执行器：与后端 progress 服务同一套命令语义（总体计划 6.2）。
 * 测试先行的垂直切片；P2 后端落地后这些断言同样适用于 HTTP 入口。
 */
import { describe, expect, it } from 'vitest';
import { createLocalExecutor } from './localExecutor';
import { defaultRules } from './rules';
import type { ProgressCommand } from './types';

function setup() {
  return createLocalExecutor({ caseId: 'case-1', rules: defaultRules });
}

function addRoot(executor: ReturnType<typeof setup>, title: string, kind = 'stage') {
  return executor.human({ type: 'add_node', parent_id: null, kind, title });
}

describe('进度执行器 · 基础命令', () => {
  it('新案快照为空，进度序号为 0', () => {
    const executor = setup();
    const snapshot = executor.getSnapshot();
    expect(snapshot.nodes).toEqual([]);
    expect(snapshot.seq).toBe(0);
    expect(executor.getSeqs()).toEqual({ progress: 0 });
  });

  it('人工添加根阶段：快照含节点、序号 +1、变更记录为 none、审计一条', () => {
    const executor = setup();
    const result = addRoot(executor, '诉前准备');
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.snapshot.seq).toBe(1);
    expect(result.snapshot.nodes).toHaveLength(1);
    const node = result.snapshot.nodes[0];
    expect(node).toMatchObject({
      case_id: 'case-1', parent_id: null, kind: 'stage', state: 'not_started',
      title: '诉前准备', created_by: 'human', revision: 0, removed_at: null,
    });
    expect(result.snapshot.pending_changes).toEqual([]);
    const audit = executor.auditLog();
    expect(audit).toHaveLength(1);
    expect(audit[0]).toMatchObject({ operation: 'progress.add_node', actor: 'human' });
    expect(audit[0].target_ids).toEqual([node.id]);
  });

  it('包含规则拒绝把程序放进阶段，且不产生任何副作用', () => {
    const executor = setup();
    const stage = addRoot(executor, '庭前程序');
    expect(stage.ok).toBe(true);
    if (!stage.ok) return;
    const stageId = stage.snapshot.nodes[0].id;
    const before = executor.getSnapshot();
    const result = executor.human({
      type: 'add_node', parent_id: stageId, kind: 'program', title: '管辖异议',
    });
    expect(result).toMatchObject({ ok: false, code: 'validation' });
    expect(executor.getSnapshot()).toEqual(before);
    expect(executor.auditLog()).toHaveLength(1);
  });

  it('种类与状态必须来自规则配置', () => {
    const executor = setup();
    expect(executor.human({ type: 'add_node', parent_id: null, kind: 'bogus', title: 'x' }))
      .toMatchObject({ ok: false, code: 'validation' });
    expect(executor.human({
      type: 'add_node', parent_id: null, kind: 'stage', title: 'x', state: 'bogus',
    })).toMatchObject({ ok: false, code: 'validation' });
  });

  it('标题非空且不超过 200 字', () => {
    const executor = setup();
    expect(executor.human({ type: 'add_node', parent_id: null, kind: 'stage', title: '  ' }))
      .toMatchObject({ ok: false, code: 'validation' });
    expect(executor.human({
      type: 'add_node', parent_id: null, kind: 'stage', title: '字'.repeat(201),
    })).toMatchObject({ ok: false, code: 'validation' });
  });

  it('同级 position 连续，指定位置插入会顺延其余节点', () => {
    const executor = setup();
    addRoot(executor, '甲');
    addRoot(executor, '乙');
    const result = executor.human({
      type: 'add_node', parent_id: null, kind: 'stage', title: '丙', position: 1,
    });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const titles = result.snapshot.nodes
      .slice().sort((a, b) => a.position - b.position).map((node) => node.title);
    expect(titles).toEqual(['甲', '丙', '乙']);
    expect(result.snapshot.nodes.map((node) => node.position).sort()).toEqual([0, 1, 2]);
  });
});

describe('进度执行器 · 修订号 CAS', () => {
  function seedStage(executor: ReturnType<typeof setup>) {
    const result = addRoot(executor, '庭前程序');
    if (!result.ok) throw new Error('seed failed');
    return result.snapshot.nodes[0];
  }

  it('陈旧修订号更新被拒（409 语义），节点不变', () => {
    const executor = setup();
    const node = seedStage(executor);
    const first = executor.human({
      type: 'update_node', node_id: node.id, expected_revision: 0,
      changes: { title: '庭前程序（二）' },
    });
    expect(first.ok).toBe(true);
    const stale = executor.human({
      type: 'update_node', node_id: node.id, expected_revision: 0,
      changes: { title: '覆盖' },
    });
    expect(stale).toMatchObject({ ok: false, code: 'conflict' });
    expect(executor.getSnapshot().nodes[0].title).toBe('庭前程序（二）');
  });

  it('更新成功：修订号 +1、updated_by 记录、变更 diff 含修改前后', () => {
    const executor = setup();
    const node = seedStage(executor);
    const result = executor.human({
      type: 'update_node', node_id: node.id, expected_revision: 0,
      changes: { state: 'in_progress' },
    });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const updated = result.snapshot.nodes[0];
    expect(updated.revision).toBe(1);
    expect(updated.state).toBe('in_progress');
    const change = result.snapshot.pending_changes; // 人工变更不待确认
    expect(change).toEqual([]);
    const audit = executor.auditLog();
    expect(audit[audit.length - 1].operation).toBe('progress.update_node');
  });
});

describe('进度执行器 · Agent 待确认与撤回（K5）', () => {
  function seedTwoStages(executor: ReturnType<typeof setup>) {
    const program = executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '一审' });
    if (!program.ok) throw new Error('seed failed');
    const programId = program.snapshot.nodes[0].id;
    const a = executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '庭前程序' });
    const b = executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '庭审' });
    if (!a.ok || !b.ok) throw new Error('seed failed');
    const nodes = b.snapshot.nodes;
    return {
      programId,
      stageA: nodes.find((node) => node.title === '庭前程序')!,
      stageB: nodes.find((node) => node.title === '庭审')!,
    };
  }

  it('Agent 改为进行中：变更待确认，节点生效但标记 pending', () => {
    const executor = setup();
    const { stageB } = seedTwoStages(executor);
    const result = executor.agent({
      type: 'set_active_stage', stage_id: stageB.id, expected_revision: stageB.revision,
    });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.snapshot.pending_changes).toHaveLength(1);
    const change = result.snapshot.pending_changes[0];
    expect(change).toMatchObject({ command: 'set_active_stage', actor: 'agent', review_state: 'pending' });
    const active = result.snapshot.nodes.find((node) => node.id === stageB.id)!;
    expect(active.state).toBe('in_progress');
  });

  it('set_active_stage 连带调整同程序原进行中阶段，记为一条变更', () => {
    const executor = setup();
    const { stageA, stageB } = seedTwoStages(executor);
    const first = executor.human({
      type: 'set_active_stage', stage_id: stageA.id, expected_revision: stageA.revision,
    });
    expect(first.ok).toBe(true);
    const result = executor.agent({
      type: 'set_active_stage', stage_id: stageB.id, expected_revision: stageB.revision,
    });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const a = result.snapshot.nodes.find((node) => node.id === stageA.id)!;
    const b = result.snapshot.nodes.find((node) => node.id === stageB.id)!;
    expect(a.state).toBe('completed');
    expect(b.state).toBe('in_progress');
    expect(result.snapshot.pending_changes).toHaveLength(1);
    expect(result.snapshot.pending_changes[0].diff.length).toBe(2);
  });

  it('人工确认后不再待确认', () => {
    const executor = setup();
    const { stageB } = seedTwoStages(executor);
    const changed = executor.agent({
      type: 'set_active_stage', stage_id: stageB.id, expected_revision: stageB.revision,
    });
    if (!changed.ok) throw new Error('agent failed');
    const changeId = changed.snapshot.pending_changes[0].id;
    const result = executor.human({ type: 'accept_change', change_id: changeId });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.snapshot.pending_changes).toEqual([]);
  });

  it('撤回恢复修改前状态，含连带调整的阶段；撤回本身是一条新变更', () => {
    const executor = setup();
    const { stageA, stageB } = seedTwoStages(executor);
    executor.human({ type: 'set_active_stage', stage_id: stageA.id, expected_revision: stageA.revision });
    const changed = executor.agent({
      type: 'set_active_stage', stage_id: stageB.id, expected_revision: stageB.revision,
    });
    if (!changed.ok) throw new Error('agent failed');
    const changeId = changed.snapshot.pending_changes[0].id;
    const result = executor.human({ type: 'revert_change', change_id: changeId });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const a = result.snapshot.nodes.find((node) => node.id === stageA.id)!;
    const b = result.snapshot.nodes.find((node) => node.id === stageB.id)!;
    expect(a.state).toBe('in_progress');
    expect(b.state).toBe('not_started');
    expect(result.snapshot.pending_changes).toEqual([]);
    const audit = executor.auditLog();
    expect(audit[audit.length - 1].operation).toBe('progress.revert_change');
  });

  it('节点已被后续修改时撤回被拒，提示冲突', () => {
    const executor = setup();
    const { stageB } = seedTwoStages(executor);
    const changed = executor.agent({
      type: 'update_node', node_id: stageB.id, expected_revision: stageB.revision,
      changes: { note: 'Agent 备注' },
    });
    if (!changed.ok) throw new Error('agent failed');
    const changeId = changed.snapshot.pending_changes[0].id;
    executor.human({
      type: 'update_node', node_id: stageB.id, expected_revision: stageB.revision + 1,
      changes: { title: '庭审（人工改）' },
    });
    const result = executor.human({ type: 'revert_change', change_id: changeId });
    expect(result).toMatchObject({ ok: false, code: 'conflict' });
    expect(executor.getSnapshot().nodes.find((node) => node.id === stageB.id)!.title)
      .toBe('庭审（人工改）');
  });

  it('Agent 不能确认或撤回，也不能自报 actor 绕过', () => {
    const executor = setup();
    const { stageB } = seedTwoStages(executor);
    const changed = executor.agent({
      type: 'update_node', node_id: stageB.id, expected_revision: stageB.revision,
      changes: { note: 'x' },
    });
    if (!changed.ok) throw new Error('agent failed');
    const changeId = changed.snapshot.pending_changes[0].id;
    expect(executor.agent({ type: 'accept_change', change_id: changeId }))
      .toMatchObject({ ok: false, code: 'validation' });
    expect(executor.agent({ type: 'revert_change', change_id: changeId }))
      .toMatchObject({ ok: false, code: 'validation' });
    expect(executor.getSnapshot().pending_changes).toHaveLength(1);
  });

  it('Agent 新建节点必须带 request_key，重复键只建一个节点', () => {
    const executor = setup();
    expect(executor.agent({ type: 'add_node', parent_id: null, kind: 'work', title: '无键' }))
      .toMatchObject({ ok: false, code: 'validation' });
    const command: ProgressCommand = {
      type: 'add_node', parent_id: null, kind: 'stage', title: '证据整理', request_key: 'req-1',
    };
    const first = executor.agent(command);
    const second = executor.agent(command);
    expect(first.ok).toBe(true);
    expect(second.ok).toBe(true);
    if (!first.ok || !second.ok) return;
    expect(second.snapshot.nodes).toHaveLength(1);
    expect(second.snapshot.seq).toBe(first.snapshot.seq);
    expect(second.snapshot.pending_changes).toHaveLength(1);
  });
});
describe('进度执行器 · 移动与包含规则', () => {
  function seedTree(executor: ReturnType<typeof setup>) {
    const program = executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '一审' });
    if (!program.ok) throw new Error('seed');
    const programId = program.snapshot.nodes[0].id;
    const stage = executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '庭前程序' });
    if (!stage.ok) throw new Error('seed');
    const stageId = stage.snapshot.nodes.find((node) => node.title === '庭前程序')!.id;
    const work = executor.human({ type: 'add_node', parent_id: stageId, kind: 'work', title: '证据整理' });
    if (!work.ok) throw new Error('seed');
    const workId = work.snapshot.nodes.find((node) => node.title === '证据整理')!.id;
    return { programId, stageId, workId };
  }

  it('移动到自身子孙下被拒（成环）', () => {
    const executor = setup();
    const { programId, stageId, workId } = seedTree(executor);
    const program = executor.getSnapshot().nodes.find((node) => node.id === programId)!;
    const result = executor.human({
      type: 'move_node', node_id: programId, expected_revision: program.revision,
      new_parent_id: workId,
    });
    expect(result).toMatchObject({ ok: false, code: 'validation' });
    expect(executor.getSnapshot().nodes.find((node) => node.id === programId)!.parent_id).toBeNull();
    void stageId;
  });

  it('违反包含规则的移动被拒', () => {
    const executor = setup();
    const { stageId, workId } = seedTree(executor);
    const work = executor.getSnapshot().nodes.find((node) => node.id === workId)!;
    // work 不能直接放在程序下？规则允许 program→work；改用 stage→program 方向：
    const stage = executor.getSnapshot().nodes.find((node) => node.id === stageId)!;
    const result = executor.human({
      type: 'move_node', node_id: stageId, expected_revision: stage.revision,
      new_parent_id: workId,
    });
    expect(result).toMatchObject({ ok: false, code: 'validation' });
    void work;
  });

  it('合法移动：父节点变化、两侧位置连续、修订号 +1', () => {
    const executor = setup();
    const { programId, stageId, workId } = seedTree(executor);
    const root2 = executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '二审' });
    if (!root2.ok) throw new Error('seed');
    const program2Id = root2.snapshot.nodes.find((node) => node.title === '二审')!.id;
    const work = executor.getSnapshot().nodes.find((node) => node.id === workId)!;
    const result = executor.human({
      type: 'move_node', node_id: workId, expected_revision: work.revision,
      new_parent_id: program2Id,
    });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const moved = result.snapshot.nodes.find((node) => node.id === workId)!;
    expect(moved.parent_id).toBe(program2Id);
    expect(moved.revision).toBe(work.revision + 1);
    const oldSiblings = result.snapshot.nodes.filter((node) => node.parent_id === stageId && node.removed_at === null);
    expect(oldSiblings).toHaveLength(0);
    void programId;
  });
});

describe('进度执行器 · 模板片段（K6）', () => {
  it('插入二审片段：程序与内部阶段一次建成，template_ref 带版本', () => {
    const executor = setup();
    const result = executor.human({ type: 'insert_fragment', parent_id: null, fragment_key: 'second-instance' });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.snapshot.nodes).toHaveLength(4);
    const program = result.snapshot.nodes.find((node) => node.kind === 'program')!;
    expect(program.title).toBe('二审');
    expect(program.template_ref).toBe('second-instance@1');
    const stages = result.snapshot.nodes.filter((node) => node.parent_id === program.id);
    expect(stages.map((node) => node.title)).toEqual(['上诉', '二审审理', '二审裁判']);
    expect(result.snapshot.seq).toBe(1); // 一条命令一条变更
  });

  it('片段位置违反包含规则时被拒', () => {
    const executor = setup();
    const stage = addRoot(executor, '庭前程序');
    if (!stage.ok) throw new Error('seed');
    const result = executor.human({
      type: 'insert_fragment', parent_id: stage.snapshot.nodes[0].id, fragment_key: 'second-instance',
    });
    expect(result).toMatchObject({ ok: false, code: 'validation' });
    expect(executor.getSnapshot().nodes).toHaveLength(1);
  });

  it('Agent 插入片段整体待确认，撤回后整组移除', () => {
    const executor = setup();
    const result = executor.agent({ type: 'insert_fragment', parent_id: null, fragment_key: 'second-instance' });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.snapshot.pending_changes).toHaveLength(1);
    const changeId = result.snapshot.pending_changes[0].id;
    const reverted = executor.human({ type: 'revert_change', change_id: changeId });
    expect(reverted.ok).toBe(true);
    if (!reverted.ok) return;
    expect(reverted.snapshot.nodes.every((node) => node.removed_at !== null)).toBe(true);
  });
});

describe('进度执行器 · 软删除与恢复（K11）', () => {
  it('删除是软删除：快照仍含节点但 removed_at 非空', () => {
    const executor = setup();
    const program = executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '一审' });
    if (!program.ok) throw new Error('seed');
    const added = executor.human({
      type: 'add_node', parent_id: program.snapshot.nodes[0].id, kind: 'work', title: '临时工作',
    });
    if (!added.ok) throw new Error('seed');
    const node = added.snapshot.nodes.find((item) => item.title === '临时工作')!;
    const result = executor.human({ type: 'remove_node', node_id: node.id, expected_revision: node.revision });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const removed = result.snapshot.nodes.find((item) => item.id === node.id)!;
    expect(removed.removed_at).not.toBeNull();
    const restored = executor.human({ type: 'restore_node', node_id: node.id, expected_revision: removed.revision });
    expect(restored.ok).toBe(true);
    if (!restored.ok) return;
    expect(restored.snapshot.nodes.find((item) => item.id === node.id)!.removed_at).toBeNull();
  });

  it('有未删除子节点的节点不能删除', () => {
    const executor = setup();
    const program = executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '一审' });
    if (!program.ok) throw new Error('seed');
    const programId = program.snapshot.nodes[0].id;
    executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '立案' });
    const current = executor.getSnapshot().nodes.find((node) => node.id === programId)!;
    expect(executor.human({ type: 'remove_node', node_id: programId, expected_revision: current.revision }))
      .toMatchObject({ ok: false, code: 'validation' });
  });
});

describe('进度执行器 · 关系与业务关联（K4/K7）', () => {
  function seedWithCatalog() {
    return createLocalExecutor({
      caseId: 'case-1', rules: defaultRules,
      catalog: {
        materials: [{ id: 'mat-1', case_id: 'case-1', title: 'TEST-ONLY 合同.pdf' }],
        analyses: [{ id: 'ana-1', case_id: 'case-1', title: 'TEST-ONLY 侵权比对' }],
        annotations: [{ id: 'ann-1', case_id: 'case-1', title: 'TEST-ONLY 标注' }],
      },
    });
  }

  it('引起关系建立成功，自环与未知类型被拒', () => {
    const executor = seedWithCatalog();
    addRoot(executor, '一审', 'program');
    addRoot(executor, '无效程序', 'program');
    const [a, b] = executor.getSnapshot().nodes;
    const ok = executor.human({ type: 'add_relation', from_node_id: b.id, to_node_id: a.id, relation_type: 'causes' });
    expect(ok.ok).toBe(true);
    if (!ok.ok) return;
    expect(ok.snapshot.relations).toHaveLength(1);
    expect(executor.human({ type: 'add_relation', from_node_id: a.id, to_node_id: a.id, relation_type: 'causes' }))
      .toMatchObject({ ok: false, code: 'validation' });
    expect(executor.human({ type: 'add_relation', from_node_id: a.id, to_node_id: b.id, relation_type: 'bogus' }))
      .toMatchObject({ ok: false, code: 'validation' });
  });

  function seedWorkNode(executor: ReturnType<typeof seedWithCatalog>) {
    const program = executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '一审' });
    if (!program.ok) throw new Error('seed');
    const added = executor.human({
      type: 'add_node', parent_id: program.snapshot.nodes[0].id, kind: 'work', title: '证据整理',
    });
    if (!added.ok) throw new Error('seed');
    return added.snapshot.nodes.find((node) => node.title === '证据整理')!;
  }

  it('关联材料带显示名；重复关联被拒；跨案目标被拒', () => {
    const executor = seedWithCatalog();
    const node = seedWorkNode(executor);
    const linked = executor.human({ type: 'link', node_id: node.id, target: { material_id: 'mat-1' } });
    expect(linked.ok).toBe(true);
    if (!linked.ok) return;
    expect(linked.snapshot.links[0]).toMatchObject({ material_id: 'mat-1', display_name: 'TEST-ONLY 合同.pdf' });
    expect(executor.human({ type: 'link', node_id: node.id, target: { material_id: 'mat-1' } }))
      .toMatchObject({ ok: false, code: 'validation' });
    const other = createLocalExecutor({
      caseId: 'case-1', rules: defaultRules,
      catalog: { materials: [{ id: 'mat-x', case_id: 'case-2', title: '他案材料' }] },
    });
    const otherNode = seedWorkNode(other);
    expect(other.human({ type: 'link', node_id: otherNode.id, target: { material_id: 'mat-x' } }))
      .toMatchObject({ ok: false, code: 'case_mismatch' });
  });

  it('软删除节点保留关联，恢复后仍可见', () => {
    const executor = seedWithCatalog();
    const node = seedWorkNode(executor);
    executor.human({ type: 'link', node_id: node.id, target: { analysis_id: 'ana-1' } });
    const current = executor.getSnapshot().nodes.find((item) => item.id === node.id)!;
    const removed = executor.human({ type: 'remove_node', node_id: node.id, expected_revision: current.revision });
    expect(removed.ok).toBe(true);
    if (!removed.ok) return;
    expect(removed.snapshot.links).toHaveLength(1);
  });
});

describe('进度执行器 · 扩展性（规则配置驱动）', () => {
  it('注入新种类与四层包含规则后，不改代码即可建出四层节点', () => {
    const rules = {
      ...defaultRules,
      kinds: {
        ...defaultRules.kinds,
        preservation: {
          label: '保全',
          states: [{ value: 'pending', label: '未完成' }, { value: 'done', label: '已完成' }],
          default_state: 'pending',
        },
      },
      containment: { ...defaultRules.containment, work: ['work', 'preservation'], preservation: ['work'] },
    };
    const executor = createLocalExecutor({ caseId: 'case-1', rules });
    const chain: [string, string][] = [
      ['program', '一审'], ['stage', '庭前程序'], ['work', '证据整理'], ['preservation', '证据保全'],
    ];
    let parentId: string | null = null;
    for (const [kind, title] of chain) {
      const result = executor.human({ type: 'add_node', parent_id: parentId, kind, title });
      expect(result.ok).toBe(true);
      if (!result.ok) return;
      parentId = result.snapshot.nodes.find((node) => node.title === title)!.id;
    }
    const snapshot = executor.getSnapshot();
    expect(snapshot.nodes).toHaveLength(4);
    const leaf = snapshot.nodes.find((node) => node.kind === 'preservation')!;
    expect(leaf.title).toBe('证据保全');
    // 深度不由代码限定：preservation→work 允许第五层，preservation→preservation 被规则拒绝
    expect(executor.human({ type: 'add_node', parent_id: parentId, kind: 'work', title: '保全跟进' }).ok)
      .toBe(true);
    expect(executor.human({ type: 'add_node', parent_id: parentId, kind: 'preservation', title: '越界' }))
      .toMatchObject({ ok: false, code: 'validation' });
  });
});

describe('进度执行器 · 关系与关联的撤回', () => {
  function seedTwoPrograms(executor: ReturnType<typeof setup>) {
    addRoot(executor, '一审', 'program');
    addRoot(executor, '无效程序', 'program');
    const [a, b] = executor.getSnapshot().nodes;
    return { a, b };
  }

  it('Agent 建立的关系待确认，撤回后关系软删除', () => {
    const executor = setup();
    const { a, b } = seedTwoPrograms(executor);
    const changed = executor.agent({ type: 'add_relation', from_node_id: b.id, to_node_id: a.id, relation_type: 'causes' });
    expect(changed.ok).toBe(true);
    if (!changed.ok) return;
    expect(changed.snapshot.pending_changes).toHaveLength(1);
    const result = executor.human({ type: 'revert_change', change_id: changed.snapshot.pending_changes[0].id });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.snapshot.relations[0].removed_at).not.toBeNull();
    expect(result.snapshot.pending_changes).toEqual([]);
  });

  it('Agent 建立的关联待确认，撤回后关联移除', () => {
    const executor = createLocalExecutor({
      caseId: 'case-1', rules: defaultRules,
      catalog: { materials: [{ id: 'mat-1', case_id: 'case-1', title: 'TEST-ONLY 材料' }] },
    });
    const { a } = seedTwoPrograms(executor);
    const changed = executor.agent({ type: 'link', node_id: a.id, target: { material_id: 'mat-1' } });
    expect(changed.ok).toBe(true);
    if (!changed.ok) return;
    const result = executor.human({ type: 'revert_change', change_id: changed.snapshot.pending_changes[0].id });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.snapshot.links).toEqual([]);
  });
});

describe('进度执行器 · 一键确认与撤销栈口径', () => {
  it('accept_all 只确认 pending，人工变更不受影响，序号 +1', () => {
    const executor = setup();
    const a = addRoot(executor, '甲');
    if (!a.ok) throw new Error('seed');
    const node = a.snapshot.nodes[0];
    executor.agent({ type: 'update_node', node_id: node.id, expected_revision: node.revision, changes: { note: 'x' } });
    executor.agent({ type: 'add_node', parent_id: null, kind: 'stage', title: '乙', request_key: 'k2' });
    const before = executor.getSnapshot().seq;
    // 省略 IDs 时必须携带匹配的 expected_seq（审查末节）。
    const result = executor.human({ type: 'accept_all', expected_seq: before });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.snapshot.pending_changes).toEqual([]);
    expect(result.snapshot.seq).toBe(before + 1);
    const audit = executor.auditLog();
    expect(audit[audit.length - 1].operation).toBe('progress.accept_all');
  });

  it('审查末节：显式提交空集合一律拒绝，序号不变', () => {
    const executor = setup();
    executor.agent({ type: 'add_node', parent_id: null, kind: 'stage', title: '甲', request_key: 'a' });
    const before = executor.getSnapshot().seq;
    expect(executor.human({ type: 'accept_all', change_ids: [] }))
      .toMatchObject({ ok: false, code: 'validation' });
    expect(executor.getSnapshot().seq).toBe(before);
    expect(executor.getSnapshot().pending_changes).toHaveLength(1);
  });

  it('审查末节：省略 IDs 且无 expected_seq 拒绝，有待审项也不确认', () => {
    const executor = setup();
    executor.agent({ type: 'add_node', parent_id: null, kind: 'stage', title: '甲', request_key: 'a' });
    const before = executor.getSnapshot().seq;
    expect(executor.human({ type: 'accept_all' }))
      .toMatchObject({ ok: false, code: 'validation' });
    expect(executor.getSnapshot().seq).toBe(before);
    expect(executor.getSnapshot().pending_changes).toHaveLength(1);
  });

  it('Agent 不能执行 accept_all', () => {
    const executor = setup();
    expect(executor.agent({ type: 'accept_all' })).toMatchObject({ ok: false, code: 'validation' });
  });

  it('无待确认时 accept_all 拒绝且序号不变', () => {
    const executor = setup();
    const before = executor.getSnapshot().seq;
    expect(executor.human({ type: 'accept_all' })).toMatchObject({ ok: false, code: 'validation' });
    expect(executor.getSnapshot().seq).toBe(before);
  });

  it('审查 04：accept_all 只确认已展示条目，其间新增的待审修改不被一并确认', () => {
    const executor = setup();
    executor.agent({ type: 'add_node', parent_id: null, kind: 'stage', title: '甲', request_key: 'a' });
    const shownIds = executor.getSnapshot().pending_changes.map((change) => change.id);
    // 打开确认框后、点确认前，Agent 又写入一条
    executor.agent({ type: 'add_node', parent_id: null, kind: 'stage', title: '乙', request_key: 'b' });
    const newChange = executor.getSnapshot().pending_changes.find((change) => !shownIds.includes(change.id))!;
    const result = executor.human({ type: 'accept_all', change_ids: shownIds });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.snapshot.pending_changes.map((change) => change.id)).toEqual([newChange.id]);
  });

  it('审查 04：expected_seq 前进时整批冲突', () => {
    const executor = setup();
    executor.agent({ type: 'add_node', parent_id: null, kind: 'stage', title: '甲', request_key: 'a' });
    const staleSeq = executor.getSnapshot().seq;
    executor.agent({ type: 'add_node', parent_id: null, kind: 'stage', title: '乙', request_key: 'b' });
    expect(executor.human({ type: 'accept_all', expected_seq: staleSeq }))
      .toMatchObject({ ok: false, code: 'conflict' });
  });
});

describe('进度执行器 · 审查整改回归（2026-11-19）', () => {
  it('审查 07：普通编辑把阶段设为进行中，联动同程序原进行中阶段', () => {
    const executor = setup();
    const program = executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '一审' });
    if (!program.ok) throw new Error('seed');
    const programId = program.snapshot.nodes[0].id;
    executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '庭前程序', state: 'in_progress' });
    const added = executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '庭审' });
    if (!added.ok) throw new Error('seed');
    const hearing = added.snapshot.nodes.find((node) => node.title === '庭审')!;
    const result = executor.human({
      type: 'update_node', node_id: hearing.id, expected_revision: hearing.revision,
      changes: { state: 'in_progress' },
    });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.snapshot.nodes.find((node) => node.title === '庭前程序')!.state).toBe('completed');
    expect(result.snapshot.nodes.filter((node) => node.kind === 'stage' && node.state === 'in_progress')).toHaveLength(1);
  });

  it('审查 07：新增进行中阶段也联动原阶段', () => {
    const executor = setup();
    const program = executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '一审' });
    if (!program.ok) throw new Error('seed');
    const programId = program.snapshot.nodes[0].id;
    executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '庭前程序', state: 'in_progress' });
    const result = executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '庭审', state: 'in_progress' });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.snapshot.nodes.find((node) => node.title === '庭前程序')!.state).toBe('completed');
    expect(result.snapshot.nodes.find((node) => node.title === '庭审')!.state).toBe('in_progress');
  });

  it('审查 05：撤回新建节点时其下已有后续子节点则整笔拒绝', () => {
    const executor = setup();
    const created = executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '二审' });
    if (!created.ok) throw new Error('seed');
    const program = created.snapshot.nodes[0];
    const createChange = created.snapshot.changes.find((change) => change.command === 'add_node')!;
    executor.human({ type: 'add_node', parent_id: program.id, kind: 'stage', title: '立案' });
    expect(executor.human({ type: 'revert_change', change_id: createChange.id }))
      .toMatchObject({ ok: false, code: 'conflict' });
    expect(executor.getSnapshot().nodes.filter((node) => node.removed_at === null).map((node) => node.title))
      .toEqual(expect.arrayContaining(['二审', '立案']));
  });

  it('审查 05：移动记录全部受影响同级，撤回后顺序整体还原', () => {
    const executor = setup();
    const program = executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '一审' });
    if (!program.ok) throw new Error('seed');
    const programId = program.snapshot.nodes[0].id;
    executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '甲' });
    executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '乙' });
    executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '丙' });
    const snap0 = executor.getSnapshot();
    const jia = snap0.nodes.find((node) => node.title === '甲')!;
    const bing = snap0.nodes.find((node) => node.title === '丙')!;
    const moved = executor.human({
      type: 'move_node', node_id: bing.id, expected_revision: bing.revision,
      new_parent_id: programId, before_node_id: jia.id,
    });
    expect(moved.ok).toBe(true);
    if (!moved.ok) return;
    const order = (snapshot: typeof snap0) => snapshot.nodes
      .filter((node) => node.parent_id === programId && node.removed_at === null)
      .sort((a, b) => a.position - b.position).map((node) => node.title);
    expect(order(moved.snapshot)).toEqual(['丙', '甲', '乙']);
    const moveChange = moved.snapshot.changes.find((change) => change.command === 'move_node')!;
    const reverted = executor.human({ type: 'revert_change', change_id: moveChange.id });
    expect(reverted.ok).toBe(true);
    if (!reverted.ok) return;
    expect(order(reverted.snapshot)).toEqual(['甲', '乙', '丙']);
  });

  it('审查 06：移除关系后重建同类有效关系，再撤回该移除应冲突', () => {
    const executor = setup();
    addRoot(executor, '一审', 'program');
    addRoot(executor, '二审', 'program');
    const [a, b] = executor.getSnapshot().nodes;
    const added = executor.human({ type: 'add_relation', from_node_id: a.id, to_node_id: b.id, relation_type: 'causes' });
    if (!added.ok) throw new Error('seed');
    const relationId = added.snapshot.relations[0].id;
    const removed = executor.human({ type: 'remove_relation', relation_id: relationId });
    if (!removed.ok) throw new Error('seed');
    const removeChange = removed.snapshot.changes.find((change) => change.command === 'remove_relation')!;
    executor.human({ type: 'add_relation', from_node_id: a.id, to_node_id: b.id, relation_type: 'causes' });
    expect(executor.human({ type: 'revert_change', change_id: removeChange.id }))
      .toMatchObject({ ok: false, code: 'conflict' });
    expect(executor.getSnapshot().relations.filter((relation) => relation.removed_at === null)).toHaveLength(1);
  });

  it('审查 14：片段插入支持 before_node_id，后续同级让位且记入变更', () => {
    const executor = setup();
    const added = executor.human({ type: 'add_node', parent_id: null, kind: 'stage', title: '甲' });
    if (!added.ok) throw new Error('seed');
    const jia = added.snapshot.nodes[0];
    executor.human({ type: 'add_node', parent_id: null, kind: 'stage', title: '乙' });
    const result = executor.human({
      type: 'insert_fragment', parent_id: null, fragment_key: 'second-instance', before_node_id: jia.id,
    });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const roots = result.snapshot.nodes.filter((node) => node.parent_id === null)
      .sort((a, b) => a.position - b.position).map((node) => node.title);
    expect(roots.indexOf('二审')).toBeLessThan(roots.indexOf('甲'));
    const fragChange = result.snapshot.changes.find((change) => change.command === 'insert_fragment')!;
    expect(fragChange.diff.some((entry) => entry.id === jia.id && entry.before !== null)).toBe(true);
  });
});

describe('进度执行器 · 原型与后端同口径（审查 R4-1/R4-2）', () => {
  it('R4-1：撤回 remove_node 还原旧 position，让位同级入 reverse_diff，二次撤回还原', () => {
    const executor = setup();
    const program = executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '一审' });
    if (!program.ok) throw new Error('seed');
    const programId = program.snapshot.nodes[0].id;
    executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '甲' });
    executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '乙' });
    const snap0 = executor.getSnapshot();
    const jia = snap0.nodes.find((node) => node.title === '甲')!;
    const yi = snap0.nodes.find((node) => node.title === '乙')!;
    // 删除甲（position 0）
    const removed = executor.human({ type: 'remove_node', node_id: jia.id, expected_revision: jia.revision });
    if (!removed.ok) throw new Error('remove');
    const removeChange = removed.snapshot.changes.find((change) => change.command === 'remove_node')!;
    // 乙移到 position 0（压缩）
    const yiNow = removed.snapshot.nodes.find((node) => node.id === yi.id)!;
    executor.human({ type: 'move_node', node_id: yi.id, expected_revision: yiNow.revision, new_parent_id: programId });
    // 撤回删除甲 → 甲回 0、乙让位到 1，位置唯一，让位者入 reverse_diff
    const reverted = executor.human({ type: 'revert_change', change_id: removeChange.id });
    expect(reverted.ok).toBe(true);
    if (!reverted.ok) return;
    const kids = reverted.snapshot.nodes.filter((node) => node.parent_id === programId && node.removed_at === null)
      .sort((a, b) => a.position - b.position);
    expect(kids.map((node) => node.title)).toEqual(['甲', '乙']);
    expect(kids.map((node) => node.position)).toEqual([0, 1]);
    const revertChange = reverted.snapshot.changes.find((change) => change.command === 'revert_change')!;
    const revertIds = new Set(revertChange.diff.map((entry) => entry.id));
    expect(revertIds.has(yi.id)).toBe(true); // 让位者入 diff
    expect(revertIds.has(jia.id)).toBe(true);
    // R5-2/复核残留：断言让位条目 before/after/rev 数值（乙 position 0→1，revision+1）
    const yiEntry = revertChange.diff.find((entry) => entry.id === yi.id)!;
    expect((yiEntry.before as unknown as { position: number }).position).toBe(0);
    expect((yiEntry.after as unknown as { position: number }).position).toBe(1);
    expect(yiEntry.after_rev).toBe((yiEntry.before_rev ?? 0) + 1);
    // 二次撤回（撤回该撤回）→ 甲重新软删、乙位置还原
    const reReverted = executor.human({ type: 'revert_change', change_id: revertChange.id });
    expect(reReverted.ok).toBe(true);
    if (!reReverted.ok) return;
    const kids2 = reReverted.snapshot.nodes.filter((node) => node.parent_id === programId && node.removed_at === null)
      .sort((a, b) => a.position - b.position);
    expect(kids2.map((node) => node.title)).toEqual(['乙']);
    expect(kids2[0].position).toBe(0);
  });

  it('R4-1：「撤回的撤回」软删遇后续子节点整笔拒绝', () => {
    const executor = setup();
    const program = executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '一审' });
    if (!program.ok) throw new Error('seed');
    const programId = program.snapshot.nodes[0].id;
    const added = executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '庭审' });
    if (!added.ok) throw new Error('seed');
    const hearing = added.snapshot.nodes.find((node) => node.title === '庭审')!;
    const removed = executor.human({ type: 'remove_node', node_id: hearing.id, expected_revision: hearing.revision });
    if (!removed.ok) throw new Error('remove');
    const removeChange = removed.snapshot.changes.find((change) => change.command === 'remove_node')!;
    const reverted = executor.human({ type: 'revert_change', change_id: removeChange.id });
    if (!reverted.ok) throw new Error('revert');
    const revertChange = reverted.snapshot.changes.find((change) => change.command === 'revert_change')!;
    const hearingNow = reverted.snapshot.nodes.find((node) => node.id === hearing.id)!;
    executor.human({ type: 'add_node', parent_id: hearingNow.id, kind: 'work', title: '出庭准备' });
    expect(executor.human({ type: 'revert_change', change_id: revertChange.id }))
      .toMatchObject({ ok: false, code: 'conflict' });
  });

  it('R4-1：撤回移除关系/关联时端点已软删则拒绝', () => {
    const executor = createLocalExecutor({
      caseId: 'case-1', rules: defaultRules,
      catalog: { materials: [{ id: 'mat-1', case_id: 'case-1', title: 'TEST-ONLY 材料' }] },
    });
    addRoot(executor, '一审', 'program');
    addRoot(executor, '二审', 'program');
    const [a, b] = executor.getSnapshot().nodes;
    const rel = executor.human({ type: 'add_relation', from_node_id: a.id, to_node_id: b.id, relation_type: 'causes' });
    if (!rel.ok) throw new Error('seed');
    const relationId = rel.snapshot.relations[0].id;
    const rmRel = executor.human({ type: 'remove_relation', relation_id: relationId });
    if (!rmRel.ok) throw new Error('seed');
    const rmRelChange = rmRel.snapshot.changes.find((change) => change.command === 'remove_relation')!;
    const bNow = rmRel.snapshot.nodes.find((node) => node.id === b.id)!;
    executor.human({ type: 'remove_node', node_id: b.id, expected_revision: bNow.revision });
    expect(executor.human({ type: 'revert_change', change_id: rmRelChange.id }))
      .toMatchObject({ ok: false, code: 'conflict' });
    // 关联同理
    const link = executor.human({ type: 'link', node_id: a.id, target: { material_id: 'mat-1' } });
    if (!link.ok) throw new Error('seed');
    const linkId = link.snapshot.links[0].id;
    const unlink = executor.human({ type: 'unlink', link_id: linkId });
    if (!unlink.ok) throw new Error('seed');
    const unlinkChange = unlink.snapshot.changes.find((change) => change.command === 'unlink')!;
    const aNow = unlink.snapshot.nodes.find((node) => node.id === a.id)!;
    executor.human({ type: 'remove_node', node_id: a.id, expected_revision: aNow.revision });
    expect(executor.human({ type: 'revert_change', change_id: unlinkChange.id }))
      .toMatchObject({ ok: false, code: 'conflict' });
  });

  it('R4-1：add_node 让位同级入 diff，撤回新建整体还原位置', () => {
    const executor = setup();
    executor.human({ type: 'add_node', parent_id: null, kind: 'stage', title: '甲' });
    const added = executor.human({ type: 'add_node', parent_id: null, kind: 'stage', title: '乙' });
    if (!added.ok) throw new Error('seed');
    const jia = added.snapshot.nodes.find((node) => node.title === '甲')!;
    const inserted = executor.human({ type: 'add_node', parent_id: null, kind: 'stage', title: '丙', position: 0 });
    if (!inserted.ok) throw new Error('seed');
    const addChange = inserted.snapshot.changes.find((change) => change.command === 'add_node'
      && (change.diff[0].after as { title: string }).title === '丙')!;
    expect(addChange.diff.some((entry) => entry.id === jia.id && entry.before !== null)).toBe(true);
    const reverted = executor.human({ type: 'revert_change', change_id: addChange.id });
    expect(reverted.ok).toBe(true);
    if (!reverted.ok) return;
    const roots = reverted.snapshot.nodes.filter((node) => node.parent_id === null && node.removed_at === null)
      .sort((a, b) => a.position - b.position);
    expect(roots.map((node) => node.title)).toEqual(['甲', '乙']);
    expect(roots.map((node) => node.position)).toEqual([0, 1]);
  });

  it('R4-2：撤回删除恢复进行中阶段，期间另一阶段已进行中，不自联动抵消', () => {
    const executor = setup();
    const program = executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '一审' });
    if (!program.ok) throw new Error('seed');
    const programId = program.snapshot.nodes[0].id;
    const a = executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '庭前程序', state: 'in_progress' });
    if (!a.ok) throw new Error('seed');
    const pretrial = a.snapshot.nodes.find((node) => node.title === '庭前程序')!;
    const b = executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '庭审' });
    if (!b.ok) throw new Error('seed');
    const hearing = b.snapshot.nodes.find((node) => node.title === '庭审')!;
    // 删除进行中的庭前程序
    const removed = executor.human({ type: 'remove_node', node_id: pretrial.id, expected_revision: pretrial.revision });
    if (!removed.ok) throw new Error('remove');
    const removeChange = removed.snapshot.changes.find((change) => change.command === 'remove_node')!;
    // 期间把庭审设为进行中（不触碰被删节点 revision）
    const hearingNow = removed.snapshot.nodes.find((node) => node.id === hearing.id)!;
    executor.human({ type: 'set_active_stage', stage_id: hearing.id, expected_revision: hearingNow.revision });
    // 撤回删除庭前程序 → 庭前程序恢复 in_progress，庭审联动 completed，恰好一个进行中
    const reverted = executor.human({ type: 'revert_change', change_id: removeChange.id });
    expect(reverted.ok).toBe(true);
    if (!reverted.ok) return;
    const stages = reverted.snapshot.nodes.filter((node) => node.kind === 'stage' && node.removed_at === null);
    const active = stages.filter((node) => node.state === 'in_progress');
    expect(active).toHaveLength(1);
    expect(active[0].title).toBe('庭前程序'); // 恢复者保持进行中，不被自联动抵消
    expect(stages.find((node) => node.title === '庭审')!.state).toBe('completed');
  });

  it('R5-2：普通编辑（update_node）撤回还原字段，不误联动其他进行中阶段', () => {
    const executor = setup();
    const program = executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '一审' });
    if (!program.ok) throw new Error('seed');
    const programId = program.snapshot.nodes[0].id;
    const a = executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '立案', state: 'in_progress' });
    if (!a.ok) throw new Error('seed');
    const filing = a.snapshot.nodes.find((node) => node.title === '立案')!;
    const b = executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '庭审' });
    if (!b.ok) throw new Error('seed');
    const hearing = b.snapshot.nodes.find((node) => node.title === '庭审')!;
    // Agent 编辑庭审备注（pending）
    const edited = executor.agent({ type: 'update_node', node_id: hearing.id, expected_revision: hearing.revision, changes: { note: 'TEST-ONLY 待开庭' } });
    if (!edited.ok) throw new Error('edit');
    const editChange = edited.snapshot.changes.find((change) => change.command === 'update_node')!;
    // 人工撤回该编辑 → 备注还原为 null，立案保持 in_progress 不被误联动
    const reverted = executor.human({ type: 'revert_change', change_id: editChange.id });
    expect(reverted.ok).toBe(true);
    if (!reverted.ok) return;
    const hearingNow = reverted.snapshot.nodes.find((node) => node.id === hearing.id)!;
    expect(hearingNow.note).toBeNull();
    const filingNow = reverted.snapshot.nodes.find((node) => node.id === filing.id)!;
    expect(filingNow.state).toBe('in_progress'); // 不被误置 completed
    const stages = reverted.snapshot.nodes.filter((node) => node.kind === 'stage' && node.removed_at === null);
    expect(stages.filter((node) => node.state === 'in_progress')).toHaveLength(1);
  });

  it('R4-2 边界：让位同级原本进行中，既让位又被联动，恰好一个进行中', () => {
    const executor = setup();
    const program = executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '一审' });
    if (!program.ok) throw new Error('seed');
    const programId = program.snapshot.nodes[0].id;
    // 庭前程序 position 0 进行中、庭审 position 1
    const a = executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '庭前程序', state: 'in_progress' });
    if (!a.ok) throw new Error('seed');
    const pretrial = a.snapshot.nodes.find((node) => node.title === '庭前程序')!;
    const b = executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '庭审' });
    if (!b.ok) throw new Error('seed');
    const hearing = b.snapshot.nodes.find((node) => node.title === '庭审')!;
    // 删除进行中的庭前程序（position 0）→ 庭审压缩到 0
    const removed = executor.human({ type: 'remove_node', node_id: pretrial.id, expected_revision: pretrial.revision });
    if (!removed.ok) throw new Error('remove');
    const removeChange = removed.snapshot.changes.find((change) => change.command === 'remove_node')!;
    // 期间把庭审设为进行中（此时庭审 position 0、in_progress）
    const hearingNow = removed.snapshot.nodes.find((node) => node.id === hearing.id)!;
    executor.human({ type: 'set_active_stage', stage_id: hearing.id, expected_revision: hearingNow.revision });
    // 新增裁判插到 position 0 → 庭审让位到 1，仍 in_progress
    const c = executor.human({ type: 'add_node', parent_id: programId, kind: 'stage', title: '裁判', position: 0 });
    if (!c.ok) throw new Error('seed');
    const judgment = c.snapshot.nodes.find((node) => node.title === '裁判')!;
    expect(judgment.position).toBe(0);
    // 撤回删除庭前程序 → 庭前程序回 0 恢复 in_progress；庭审让位（1→2）且被联动 completed
    const reverted = executor.human({ type: 'revert_change', change_id: removeChange.id });
    expect(reverted.ok).toBe(true);
    if (!reverted.ok) return;
    const stages = reverted.snapshot.nodes.filter((node) => node.kind === 'stage' && node.removed_at === null);
    const active = stages.filter((node) => node.state === 'in_progress');
    expect(active).toHaveLength(1);
    expect(active[0].title).toBe('庭前程序'); // 恢复者保持进行中
    expect(stages.find((node) => node.title === '庭审')!.state).toBe('completed'); // 让位者也被联动
    // 庭审 reverse_diff 含两条（让位 position＋联动 state）
    const revertChange = reverted.snapshot.changes.find((change) => change.command === 'revert_change')!;
    const hearingEntries = revertChange.diff.filter((entry) => entry.id === hearing.id);
    expect(hearingEntries.length).toBeGreaterThanOrEqual(2);
  });
});
