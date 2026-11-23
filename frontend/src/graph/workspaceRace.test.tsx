/**
 * 竞态回归：两案切换时上一案的延迟响应不得覆盖当前案；提交期间轮询刷新不得覆盖提交结果。
 * 用受控 Promise 精确控制响应到达顺序（不连后端）。
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useGraphWorkspace } from './useGraphWorkspace';
import type {
  CommandResult, GraphRules, GraphSnapshot, GraphWorkspaceSource,
} from './types';
import { useClaimsWorkspace } from '../claims/useClaimsWorkspace';
import type {
  ClaimSnapshot, ClaimWorkspaceSource, CommandResult as ClaimCommandResult,
} from '../claims/types';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function graphSnapshot(caseId: string, seq: number, title: string): GraphSnapshot {
  return {
    case_id: caseId, case_title: title, archived: false, seq, seqs: { graph: seq, evidence: 0 },
    entities: [], aliases: [], relations: [], sources: [], graphs: [], nodes: [], edges: [],
    groups: [], snapshots: [], consents: [],
    evidence: { groups: [], items: [], bindings: [] },
    materials: [], segments: [], annotations: [],
    progress_nodes: [], labels: {}, evidence_numbers: {}, pending_changes: [], changes: [],
    last_change_id: null,
  };
}

const rules: GraphRules = {
  entity_kinds: [{ value: 'person', label: '自然人' }],
  relation_kinds: [{ value: 'equity_control', label: '股权控制' }],
  roles: [],
  credibility: [{
    value: 'evidenced', label: '有证据', dash: '', stroke: '#333', width: 2,
    hint: '有出处支撑', badge: '证',
  }],
  snapshot_stages: [], consent_channels: [], agent_commands: [],
  max_path_depth: 6, max_paths: 20,
};

function sourceWith(load: () => Promise<GraphSnapshot>, extra: Partial<GraphWorkspaceSource> = {}) {
  return {
    load, rules: () => rules,
    human: async () => ({ ok: false, code: 'forbidden', message: '未使用' }),
    ...extra,
  } as GraphWorkspaceSource;
}

describe('useGraphWorkspace · 案件切换与刷新竞态', () => {
  it('上一案的延迟响应不写入当前案', async () => {
    const slow = deferred<GraphSnapshot>();
    const fast = deferred<GraphSnapshot>();
    const source = sourceWith(() => slow.promise);
    const { result, rerender } = renderHook(
      ({ caseId, src }: { caseId: string; src: GraphWorkspaceSource }) =>
        useGraphWorkspace({ caseId, source: src }),
      { initialProps: { caseId: 'case-a', src: source } },
    );

    const second = sourceWith(() => fast.promise);
    rerender({ caseId: 'case-b', src: second });
    await act(async () => { fast.resolve(graphSnapshot('case-b', 2, '乙案')); });
    await waitFor(() => { expect(result.current.snapshot?.case_title).toBe('乙案'); });

    // A 案响应迟到：必须被丢弃，界面仍是 B 案。
    await act(async () => { slow.resolve(graphSnapshot('case-a', 9, '甲案')); });
    expect(result.current.snapshot?.case_id).toBe('case-b');
    expect(result.current.snapshot?.case_title).toBe('乙案');
  });

  it('返回数据的 case_id 与当前案不符时拒绝写入并提示', async () => {
    const source = sourceWith(async () => graphSnapshot('case-x', 3, '别的案子'));
    const { result } = renderHook(() => useGraphWorkspace({ caseId: 'case-a', source }));
    await waitFor(() => { expect(result.current.loadError).toContain('不属于当前案件'); });
    expect(result.current.snapshot).toBeNull();
  });

  it('提交进行中不刷新：轮询看到的序号变化不会覆盖提交结果', async () => {
    const submit = deferred<CommandResult>();
    const load = vi.fn(async () => graphSnapshot('case-a', 1, '甲案'));
    const seqs = vi.fn(async () => ({ graph: 1, evidence: 0 }));
    const source = sourceWith(load, {
      seqs,
      human: () => submit.promise,
    });
    const { result } = renderHook(() =>
      useGraphWorkspace({ caseId: 'case-a', source, pollMs: 20 }));
    await waitFor(() => { expect(result.current.snapshot?.seq).toBe(1); });

    let pending!: Promise<CommandResult>;
    act(() => { pending = result.current.run({ type: 'add_entity', kind: 'person', name: '张三' }); });
    // 提交中外部序号已变化，但此时不得发起刷新读取。
    seqs.mockResolvedValue({ graph: 5, evidence: 0 });
    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(load).toHaveBeenCalledTimes(1);

    await act(async () => {
      submit.resolve({ ok: true, snapshot: graphSnapshot('case-a', 6, '甲案') });
      await pending;
    });
    expect(result.current.snapshot?.seq).toBe(6);
  });
});

function claimSnapshot(caseId: string, seq: number, title: string): ClaimSnapshot {
  return {
    case_id: caseId, case_title: title, seq, groups: [], items: [],
    group_subtotals: {}, totals_by_currency: [], pending_changes: [], changes: [],
    pending_count: 0, excluded_group_count: 0, included_group_count: 0,
  };
}

describe('useClaimsWorkspace · 案件切换与刷新竞态', () => {
  it('上一案的延迟响应不写入当前案', async () => {
    const slow = deferred<ClaimSnapshot>();
    const fast = deferred<ClaimSnapshot>();
    const make = (load: () => Promise<ClaimSnapshot>) => ({
      load,
      human: async () => ({ ok: false, code: 'forbidden', message: '未使用' }),
    } as ClaimWorkspaceSource);

    const { result, rerender } = renderHook(
      ({ caseId, src }: { caseId: string; src: ClaimWorkspaceSource }) =>
        useClaimsWorkspace({ caseId, source: src }),
      { initialProps: { caseId: 'case-a', src: make(() => slow.promise) } },
    );
    rerender({ caseId: 'case-b', src: make(() => fast.promise) });
    await act(async () => { fast.resolve(claimSnapshot('case-b', 2, '乙案')); });
    await waitFor(() => { expect(result.current.snapshot?.case_id).toBe('case-b'); });

    await act(async () => { slow.resolve(claimSnapshot('case-a', 7, '甲案')); });
    expect(result.current.snapshot?.case_id).toBe('case-b');
  });

  it('提交进行中跳过轮询刷新', async () => {
    const submit = deferred<ClaimCommandResult>();
    const load = vi.fn(async () => claimSnapshot('case-a', 1, '甲案'));
    const seq = vi.fn(async () => 1);
    const source = {
      load, seq, human: () => submit.promise,
    } as unknown as ClaimWorkspaceSource;
    const { result } = renderHook(() =>
      useClaimsWorkspace({ caseId: 'case-a', source, pollMs: 20 }));
    await waitFor(() => { expect(result.current.snapshot?.seq).toBe(1); });

    let pending!: Promise<ClaimCommandResult>;
    act(() => { pending = result.current.run({ type: 'add_group', title: '新分组', currency: 'CNY' }); });
    seq.mockResolvedValue(9);
    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(load).toHaveBeenCalledTimes(1);

    await act(async () => {
      submit.resolve({ ok: true, snapshot: claimSnapshot('case-a', 2, '甲案') });
      await pending;
    });
    expect(result.current.snapshot?.seq).toBe(2);
  });
});
