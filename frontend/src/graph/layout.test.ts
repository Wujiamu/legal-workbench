/** elkjs 自动排版：可见节点全覆盖、不重叠、分组包围盒与收起分组排除。 */
import { describe, expect, it } from 'vitest';
import { buildGraphFixture } from './fixtures';
import { NODE_HEIGHT, NODE_WIDTH, autoLayout } from './layout';
import { buildCanvasModel } from './model';

function equityModel() {
  const executor = buildGraphFixture();
  const ws = executor.getSnapshot();
  const graph = ws.graphs.find((item) => item.title === '股权与实控关系')!;
  return { executor, ws, graph, model: buildCanvasModel(ws, graph.id) };
}

describe('autoLayout', () => {
  it('stress 为每个可见节点给出有限坐标且节点矩形不重叠', async () => {
    const { model } = equityModel();
    const result = await autoLayout(model, 'stress');
    expect(result.nodes.size).toBe(model.nodes.length);
    for (const position of result.nodes.values()) {
      expect(position.x).toBeGreaterThanOrEqual(0);
      expect(position.y).toBeGreaterThanOrEqual(0);
      expect(Number.isFinite(position.x)).toBe(true);
      expect(Number.isFinite(position.y)).toBe(true);
    }
    const points = [...result.nodes.values()];
    for (let i = 0; i < points.length; i += 1) {
      for (let j = i + 1; j < points.length; j += 1) {
        const overlapX = Math.abs(points[i].x - points[j].x) < NODE_WIDTH;
        const overlapY = Math.abs(points[i].y - points[j].y) < NODE_HEIGHT;
        expect(overlapX && overlapY).toBe(false);
      }
    }
  }, 20000);

  it('layered 沿水平方向分层，并重算分组包围盒', async () => {
    const { model } = equityModel();
    const result = await autoLayout(model, 'layered');
    expect(result.nodes.size).toBe(model.nodes.length);
    const xs = [...new Set([...result.nodes.values()].map((point) => point.x))];
    expect(xs.length).toBeGreaterThan(1);
    expect(result.groups.size).toBeGreaterThan(0);
    for (const rect of result.groups.values()) {
      expect(rect.x).toBeGreaterThanOrEqual(0);
      expect(rect.y).toBeGreaterThanOrEqual(0);
      expect(rect.width).toBeGreaterThan(NODE_WIDTH);
      expect(rect.height).toBeGreaterThan(NODE_HEIGHT);
    }
  }, 20000);

  it('收起分组不参与排版，也不生成该组包围盒', async () => {
    const { executor, graph, model } = equityModel();
    const group = model.groups[0];
    const collapsed = executor.human({
      type: 'upsert_graph_group', graph_id: graph.id, group_id: group.id,
      title: group.title, color: group.color, collapsed: true,
    });
    if (!collapsed.ok) throw new Error(collapsed.message);
    const nextModel = buildCanvasModel(collapsed.snapshot, graph.id);
    const hidden = nextModel.nodes.filter((node) => node.group_id === group.id);
    expect(hidden.length).toBeGreaterThan(0);
    const result = await autoLayout(nextModel, 'stress');
    expect(result.nodes.size).toBe(nextModel.nodes.length - hidden.length);
    for (const node of hidden) expect(result.nodes.has(node.id)).toBe(false);
    expect(result.groups.has(group.id)).toBe(false);
  }, 20000);
});
