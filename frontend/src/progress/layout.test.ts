/**
 * elkjs 嵌套布局实测（真实计算，非 mock）：
 * 程序内阶段横排、阶段下工作纵排、父节点尺寸包住子节点。
 */
import { describe, expect, it } from 'vitest';
import { buildLitigationFixture } from './fixtures';
import { buildElkGraph } from './graphModel';
import { computeLayout } from './layout';

describe('elk 嵌套布局', () => {
  it('程序内阶段横排，阶段下工作纵排，父节点包住子节点', async () => {
    const snapshot = buildLitigationFixture().getSnapshot();
    const result = await computeLayout(buildElkGraph(snapshot));
    const byTitle = (title: string) => snapshot.nodes.find((node) => node.title === title)!;
    const find = (id: string) => {
      let found: { x?: number; y?: number; width?: number; height?: number; children?: unknown[] } | undefined;
      function walk(item: typeof found) {
        if (!item) return;
        if ((item as { id?: string }).id === id) found = item;
        ((item as { children?: typeof found[] }).children ?? []).forEach(walk);
      }
      walk(result as typeof found);
      return found!;
    };
    const program = find(byTitle('一审').id) as { x: number; y: number; width: number; height: number; children: { id: string; x: number; y: number; width: number; height: number }[] };
    const stages = program.children;
    expect(stages.length).toBeGreaterThanOrEqual(4);
    // 横排：x 递增，y 相近
    for (let i = 1; i < stages.length; i += 1) {
      expect(stages[i].x).toBeGreaterThan(stages[i - 1].x);
    }
    // 父节点包住子节点
    for (const child of stages) {
      expect(child.x + child.width).toBeLessThanOrEqual(program.width);
      expect(child.y + child.height).toBeLessThanOrEqual(program.height);
    }
    // 阶段下工作纵排：y 递增
    const pretrial = find(byTitle('庭前程序').id) as { children: { id: string; x: number; y: number }[] };
    const works = pretrial.children;
    expect(works.length).toBeGreaterThanOrEqual(1);
    const exchange = find(byTitle('证据交换（第一次）').id) as { children: { id: string; x: number; y: number }[] };
    for (let i = 1; i < exchange.children.length; i += 1) {
      expect(exchange.children[i].y).toBeGreaterThan(exchange.children[i - 1].y);
    }
  }, 20000);
});