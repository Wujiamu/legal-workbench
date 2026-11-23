/**
 * useCaseProgress：快照、命令、变化检测、冲突处理（总体计划 6.4）。
 * 原型接内存执行器；P4 换 HTTP 时保持同一接口。
 */
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { buildLitigationFixture } from './fixtures';
import { createLocalExecutor } from './localExecutor';
import { defaultRules } from './rules';
import type { ProgressExecutor, ProgressSnapshot } from './types';
import { useCaseProgress } from './useCaseProgress';

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

describe('useCaseProgress', () => {
  it('初始读取快照与规则', () => {
    const executor = buildLitigationFixture();
    const { result } = renderHook(() => useCaseProgress(executor));
    expect(result.current.snapshot!.nodes.length).toBeGreaterThan(5);
    expect(result.current.rules.kinds.program.label).toBe('程序');
    expect(result.current.snapshot!.pending_changes).toHaveLength(1);
  });

  it('命令成功后以返回快照替换，序号同步', () => {
    const executor = buildLitigationFixture();
    const { result } = renderHook(() => useCaseProgress(executor));
    const before = result.current.snapshot!.seq;
    act(() => {
      const outcome = result.current.execute({
        type: 'add_node', parent_id: null, kind: 'stage', title: '执行准备',
      });
      expect(outcome.ok).toBe(true);
    });
    expect(result.current.snapshot!.seq).toBe(before + 1);
    expect(result.current.snapshot!.nodes.some((node) => node.title === '执行准备')).toBe(true);
  });

  it('冲突时返回冲突并重新读取服务端状态，不抛出', () => {
    const executor = createLocalExecutor({ caseId: 'case-1', rules: defaultRules });
    executor.human({ type: 'add_node', parent_id: null, kind: 'stage', title: '庭前程序' });
    const node = executor.getSnapshot().nodes[0];
    const { result } = renderHook(() => useCaseProgress(executor));
    act(() => {
      result.current.execute({
        type: 'update_node', node_id: node.id, expected_revision: 0, changes: { title: '第一次改' },
      });
    });
    let outcome: ReturnType<typeof result.current.execute> | undefined;
    act(() => {
      outcome = result.current.execute({
        type: 'update_node', node_id: node.id, expected_revision: 0, changes: { title: '陈旧改' },
      });
    });
    expect(outcome).toMatchObject({ ok: false, code: 'conflict' });
    expect(result.current.conflict).not.toBe('');
    expect(result.current.snapshot!.nodes[0].title).toBe('第一次改');
  });

  it('轮询发现序号变化后刷新并提示', async () => {
    const executor = buildLitigationFixture();
    const { result } = renderHook(() => useCaseProgress(executor, { pollMs: 10000 }));
    const before = result.current.snapshot!.seq;
    const stage = executor.getSnapshot().nodes.find((node) => node.title === '裁判')!;
    act(() => {
      executor.agent({ type: 'update_node', node_id: stage.id, expected_revision: stage.revision, changes: { note: 'Agent 备注' } });
    });
    expect(result.current.snapshot!.seq).toBe(before); // 尚未轮询
    await act(async () => { vi.advanceTimersByTime(10000); });
    expect(result.current.snapshot!.seq).toBe(before + 1);
    expect(result.current.notice).not.toBe('');
  });

  it('回到前台立即检查变化', async () => {
    const executor = buildLitigationFixture();
    const { result } = renderHook(() => useCaseProgress(executor, { pollMs: 60000 }));
    const before = result.current.snapshot!.seq;
    const stage = executor.getSnapshot().nodes.find((node) => node.title === '裁判')!;
    executor.agent({ type: 'update_node', node_id: stage.id, expected_revision: stage.revision, changes: { note: 'x' } });
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(result.current.snapshot!.seq).toBe(before + 1);
  });

  it('迟到的旧响应不覆盖新快照', async () => {
    const real = buildLitigationFixture();
    const gates: ((value: ProgressSnapshot) => void)[] = [];
    const slow = {
      ...real,
      getSnapshot: () => new Promise<ProgressSnapshot>((resolve) => { gates.push(resolve); }),
    } as unknown as ProgressExecutor;
    const { result } = renderHook(() => useCaseProgress(slow));
    // 初始快照来自延迟 Promise，先放行
    await act(async () => { gates.shift()!(real.getSnapshot()); });
    act(() => { real.human({ type: 'add_node', parent_id: null, kind: 'stage', title: '甲阶段' }); });
    const snapshotB = real.getSnapshot();
    act(() => { result.current.refresh(); });
    act(() => { real.human({ type: 'add_node', parent_id: null, kind: 'stage', title: '乙阶段' }); });
    const snapshotC = real.getSnapshot();
    act(() => { result.current.refresh(); });
    // 新请求先返回，旧请求后返回
    await act(async () => { gates[1]!(snapshotC); });
    await act(async () => { gates[0]!(snapshotB); });
    expect(result.current.snapshot!.seq).toBe(snapshotC.seq);
    expect(result.current.snapshot!.nodes.some((node) => node.title === '乙阶段')).toBe(true);
  });
});