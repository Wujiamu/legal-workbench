/**
 * 工时域（用户反馈第七轮：计时器+快捷时长+日视图全做）。
 * 独立于 progress 命令集；引用进度节点；归档后不可补录（页面层守卫）。
 */
import { describe, expect, it } from 'vitest';
import { buildLitigationFixture } from './fixtures';
import { createTimeLog } from './timeLog';

function setup() {
  const executor = buildLitigationFixture();
  const work = executor.getSnapshot().nodes.find((node) => node.title === '补充证据清单')!;
  const log = createTimeLog({ caseId: 'case-demo' });
  return { executor, work, log };
}

describe('工时 · 计时器', () => {
  it('开始计时生成运行中条目，停止按经过时间入账（0.25h 粒度向上取整）', () => {
    const { work, log } = setup();
    let now = Date.parse('2026-11-16T09:00:00Z');
    const started = log.start(work.id, work.title, () => now);
    expect(started.ok).toBe(true);
    if (!started.ok) return;
    expect(log.running()).toHaveLength(1);
    now += 40 * 60 * 1000; // 40 分钟
    const stopped = log.stop(started.entry.id, () => now);
    expect(stopped.ok).toBe(true);
    if (!stopped.ok) return;
    expect(stopped.entry.hours).toBe(0.75);
    expect(stopped.entry.node_title).toBe('补充证据清单');
    expect(log.running()).toHaveLength(0);
    expect(log.entries()).toHaveLength(1);
  });

  it('不足 15 分钟按 0.25h 计；同一节点重复开始被拒', () => {
    const { work, log } = setup();
    let now = Date.parse('2026-11-16T09:00:00Z');
    const started = log.start(work.id, work.title, () => now);
    if (!started.ok) throw new Error('start failed');
    expect(log.start(work.id, work.title, () => now)).toMatchObject({ ok: false });
    now += 3 * 60 * 1000;
    const stopped = log.stop(started.entry.id, () => now);
    if (!stopped.ok) throw new Error('stop failed');
    expect(stopped.entry.hours).toBe(0.25);
  });

  it('停止时可用快捷时长覆盖实际经过时间', () => {
    const { work, log } = setup();
    const now = () => Date.parse('2026-11-16T09:00:00Z');
    const started = log.start(work.id, work.title, now);
    if (!started.ok) throw new Error('start failed');
    const stopped = log.stop(started.entry.id, now, { hours: 2 });
    if (!stopped.ok) throw new Error('stop failed');
    expect(stopped.entry.hours).toBe(2);
  });
});

describe('工时 · 手动补录与日视图', () => {
  it('快捷时长补录生成条目；非法时长被拒', () => {
    const { work, log } = setup();
    const added = log.add(work.id, work.title, 0.5, '2026-11-16');
    expect(added.ok).toBe(true);
    expect(log.add(work.id, work.title, 0, '2026-11-16')).toMatchObject({ ok: false });
    expect(log.add(work.id, work.title, -1, '2026-11-16')).toMatchObject({ ok: false });
    expect(log.add(work.id, work.title, 25, '2026-11-16')).toMatchObject({ ok: false });
  });

  it('日视图按日期过滤并合计；billable 汇总按标题聚合', () => {
    const { executor, work, log } = setup();
    const hearing = executor.getSnapshot().nodes.find((node) => node.title === '庭审')!;
    log.add(work.id, work.title, 0.5, '2026-11-16');
    log.add(work.id, work.title, 1, '2026-11-16');
    log.add(hearing.id, hearing.title, 2, '2026-11-15');
    const day = log.day('2026-11-16');
    expect(day.entries).toHaveLength(2);
    expect(day.totalHours).toBe(1.5);
    const summary = log.summaryByTitle();
    expect(summary.find((row) => row.title === '补充证据清单')!.hours).toBe(1.5);
    expect(summary.find((row) => row.title === '庭审')!.hours).toBe(2);
  });

  it('删除条目', () => {
    const { work, log } = setup();
    const added = log.add(work.id, work.title, 0.5, '2026-11-16');
    if (!added.ok) throw new Error('add failed');
    expect(log.remove(added.entry.id).ok).toBe(true);
    expect(log.entries()).toHaveLength(0);
  });
});