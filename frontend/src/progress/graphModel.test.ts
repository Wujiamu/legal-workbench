/**
 * 图形视图模型：与大纲视图读同一快照，节点集合一致（总体计划第八节）。
 * 包含关系用嵌套（复合节点）表达，非包含关系用独立边表达。
 */
import { describe, expect, it } from 'vitest';
import { buildLitigationFixture } from './fixtures';
import { applyElkLayout, buildElkGraph, buildGraphModel } from './graphModel';
import { defaultRules } from './rules';
import { buildTree, flattenTree } from './tree';

describe('graphModel', () => {
  it('节点集合与大纲视图一致，已删除节点不出现', () => {
    const executor = buildLitigationFixture();
    const work = executor.getSnapshot().nodes.find((node) => node.title === '质证意见')!;
    executor.human({ type: 'remove_node', node_id: work.id, expected_revision: work.revision });
    const snapshot = executor.getSnapshot();
    const model = buildGraphModel(snapshot, defaultRules);
    const outlineIds = flattenTree(buildTree(snapshot.nodes)).map((item) => item.node.id).sort();
    expect(model.nodes.map((node) => node.id).sort()).toEqual(outlineIds);
  });

  it('父节点在数组中先于子节点（RF adoptUserNodes 顺序要求，快照乱序也不重叠）', () => {
    const executor = buildLitigationFixture();
    const snapshot = executor.getSnapshot();
    // 模拟后端按创建序返回：把子节点排到父节点前面
    const shuffled = { ...snapshot, nodes: [...snapshot.nodes].reverse() };
    const model = buildGraphModel(shuffled, defaultRules);
    const index = new Map(model.nodes.map((node, i) => [node.id, i]));
    for (const node of model.nodes) {
      if (node.parentId) {
        expect(index.get(node.parentId)!).toBeLessThan(index.get(node.id)!);
      }
    }
    // 节点集合不因重排丢失
    expect(model.nodes).toHaveLength(snapshot.nodes.filter((n) => n.removed_at === null).length);
  });

  it('包含关系成为父子嵌套，关系成为带标签的虚线边', () => {
    const executor = buildLitigationFixture();
    executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '二审' });
    const nodes = executor.getSnapshot().nodes;
    executor.human({
      type: 'add_relation',
      from_node_id: nodes.find((node) => node.title === '二审')!.id,
      to_node_id: nodes.find((node) => node.title === '一审')!.id,
      relation_type: 'causes',
    });
    const snapshot = executor.getSnapshot();
    const model = buildGraphModel(snapshot, defaultRules);
    const program = model.nodes.find((node) => node.data.node.title === '一审')!;
    const stage = model.nodes.find((node) => node.data.node.title === '庭前程序')!;
    expect(stage.parentId).toBe(program.id);
    const relationEdges = model.edges.filter((edge) => edge.data?.kind === 'relation');
    expect(relationEdges).toHaveLength(1);
    expect(relationEdges[0].label).toBe('引起');
    expect(relationEdges[0].style?.strokeDasharray).toBeTruthy();
  });

  it('elk 输入按程序嵌套阶段，阶段下嵌套工作', () => {
    const snapshot = buildLitigationFixture().getSnapshot();
    const elk = buildElkGraph(snapshot);
    const program = elk.children!.find((child) => child.id === snapshot.nodes.find((node) => node.title === '一审')!.id)!;
    const stageIds = program.children!.map((child) => child.id);
    const pretrial = snapshot.nodes.find((node) => node.title === '庭前程序')!;
    expect(stageIds).toContain(pretrial.id);
    const pretrialElk = program.children!.find((child) => child.id === pretrial.id)!;
    const exchange = snapshot.nodes.find((node) => node.title === '证据交换（第一次）')!;
    expect(pretrialElk.children!.map((child) => child.id)).toContain(exchange.id);
  });

  it('应用 elk 布局结果：坐标、尺寸与父子相对位置', () => {
    const snapshot = buildLitigationFixture().getSnapshot();
    const model = buildGraphModel(snapshot, defaultRules);
    const program = snapshot.nodes.find((node) => node.title === '一审')!;
    const stage = snapshot.nodes.find((node) => node.title === '庭前程序')!;
    const fakeElk = {
      id: 'root', x: 0, y: 0, width: 2000, height: 800,
      children: [
        {
          id: program.id, x: 10, y: 20, width: 900, height: 400,
          children: [{ id: stage.id, x: 30, y: 40, width: 200, height: 60 }],
        },
      ],
    };
    const laid = applyElkLayout(model, fakeElk);
    const laidProgram = laid.nodes.find((node) => node.id === program.id)!;
    const laidStage = laid.nodes.find((node) => node.id === stage.id)!;
    expect(laidProgram.position).toEqual({ x: 10, y: 20 });
    expect(laidProgram.style).toMatchObject({ width: 900, height: 400 });
    expect(laidStage.position).toEqual({ x: 30, y: 40 }); // 相对父节点
    expect(laidStage.parentId).toBe(program.id);
  });
});
describe('graphModel · 收起子树', () => {
  it('收起节点的子孙不出现，自身成为带子项数的紧凑卡片', () => {
    const snapshot = buildLitigationFixture().getSnapshot();
    const program = snapshot.nodes.find((node) => node.title === '一审')!;
    const model = buildGraphModel(snapshot, defaultRules, new Set([program.id]));
    expect(model.nodes.some((node) => node.id === program.id)).toBe(true);
    expect(model.nodes.some((node) => node.data.node.title === '立案')).toBe(false);
    expect(model.nodes.some((node) => node.data.node.title === '证据交换（第一次）')).toBe(false);
    const card = model.nodes.find((node) => node.id === program.id)!;
    expect(card.type).toBe('progress');
    expect(card.data.collapsed).toBe(true);
    expect(card.data.childCount).toBe(4);
    expect(card.parentId).toBeUndefined();
  });

  it('指向被收起子孙的关系边改挂到收起的祖先节点', () => {
    const executor = buildLitigationFixture();
    executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '二审' });
    const snapshot0 = executor.getSnapshot();
    const hearing = snapshot0.nodes.find((node) => node.title === '庭审')!;
    const second = snapshot0.nodes.find((node) => node.title === '二审')!;
    executor.human({ type: 'add_relation', from_node_id: second.id, to_node_id: hearing.id, relation_type: 'causes' });
    const snapshot = executor.getSnapshot();
    const program = snapshot.nodes.find((node) => node.title === '一审')!;
    const model = buildGraphModel(snapshot, defaultRules, new Set([program.id]));
    const edge = model.edges.find((item) => item.data?.kind === 'relation')!;
    expect(edge.target).toBe(program.id);
    expect(model.nodes.some((node) => node.id === edge.target)).toBe(true);
  });

  it('elk 输入把收起节点当叶子，关系边端点同样改挂', () => {
    const snapshot = buildLitigationFixture().getSnapshot();
    const program = snapshot.nodes.find((node) => node.title === '一审')!;
    const elk = buildElkGraph(snapshot, new Set([program.id]));
    const elkProgram = elk.children!.find((child) => child.id === program.id)!;
    expect(elkProgram.children).toBeUndefined();
    expect(elkProgram.width).toBeGreaterThan(0);
    const ids = new Set<string>();
    (function walk(item: { id: string; children?: { id: string }[] }) {
      ids.add(item.id);
      (item.children ?? []).forEach(walk);
    })(elk as { id: string; children?: { id: string }[] });
    for (const edge of elk.edges ?? []) {
      for (const endpoint of [...edge.sources, ...edge.targets]) {
        expect(ids.has(endpoint)).toBe(true);
      }
    }
  });

  it('审查 08：删除带关系的节点后，布局边不引用已删除端点，剩余节点仍可布局', () => {
    const executor = buildLitigationFixture();
    const snap0 = executor.getSnapshot();
    const hearing = snap0.nodes.find((node) => node.title === '庭审')!;
    const judgment = snap0.nodes.find((node) => node.title === '裁判')!;
    executor.human({ type: 'add_relation', from_node_id: hearing.id, to_node_id: judgment.id, relation_type: 'causes' });
    const withRelation = executor.getSnapshot();
    const hearingNow = withRelation.nodes.find((node) => node.id === hearing.id)!;
    executor.human({ type: 'remove_node', node_id: hearing.id, expected_revision: hearingNow.revision });
    const snapshot = executor.getSnapshot();
    // 库中关系保留（供恢复），但渲染与布局都不再引用已删除端点
    expect(snapshot.relations.filter((relation) => relation.removed_at === null)).toHaveLength(1);
    const model = buildGraphModel(snapshot, defaultRules);
    expect(model.edges.filter((edge) => edge.data?.kind === 'relation')).toHaveLength(0);
    const elk = buildElkGraph(snapshot);
    const ids = new Set<string>();
    (function walk(item: { id: string; children?: { id: string }[] }) {
      ids.add(item.id);
      (item.children ?? []).forEach(walk);
    })(elk as { id: string; children?: { id: string }[] });
    expect(ids.has(hearing.id)).toBe(false);
    for (const edge of elk.edges ?? []) {
      for (const endpoint of [...edge.sources, ...edge.targets]) {
        expect(ids.has(endpoint)).toBe(true);
      }
    }
    // 剩余有效节点仍在布局输入中
    expect(ids.has(judgment.id)).toBe(true);
  });
});
