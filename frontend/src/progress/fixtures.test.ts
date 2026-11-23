/**
 * 合成案卷口径：通用诉讼框架，不含专利侵权等专门案件类型的节点。
 * 并行程序能力由模型与片段支持，演示时按需插入（如二审），不预置在默认案卷。
 */
import { describe, expect, it } from 'vitest';
import { buildLitigationFixture, FIXTURE_CASE_TITLE } from './fixtures';

describe('合成案卷口径', () => {
  it('默认案卷是通用诉讼：无无效程序等专门类型节点', () => {
    const snapshot = buildLitigationFixture().getSnapshot();
    const titles = snapshot.nodes.map((node) => node.title);
    expect(titles).not.toContain('无效程序');
    expect(titles).not.toContain('无效宣告请求');
    expect(titles).not.toContain('口头审理');
    expect(titles).not.toContain('无效决定');
    expect(titles).not.toContain('结案归档');
    const roots = snapshot.nodes.filter((node) => node.parent_id === null).map((node) => node.title);
    expect(roots).toEqual(['诉前准备', '一审']);
    expect(FIXTURE_CASE_TITLE).not.toContain('专利');
    expect(snapshot.relations).toEqual([]);
  });

  it('保留三层嵌套与 Agent 待确认演示场景', () => {
    const snapshot = buildLitigationFixture().getSnapshot();
    const exchange = snapshot.nodes.find((node) => node.title === '证据交换（第一次）')!;
    const works = snapshot.nodes.filter((node) => node.parent_id === exchange.id);
    expect(works.map((node) => node.title)).toEqual(['补充证据清单', '质证意见']);
    expect(snapshot.pending_changes).toHaveLength(1);
    expect(snapshot.links.length).toBeGreaterThanOrEqual(2);
  });
});