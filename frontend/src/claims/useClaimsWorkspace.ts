/**
 * 诉请金额工作区状态钩子：原型（同步内存）与真实案件（异步 HTTP）共用。
 *
 * 与关系图同一套竞态守卫：案件切换后到达的响应直接丢弃；
 * 快照 case_id 不匹配或 seq 回退的响应不写入界面，避免旧响应覆盖当前分组/金额项。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { describeError } from '../api/client';
import type {
  ClaimCommand, ClaimSnapshot, ClaimWorkspaceSource, CommandResult, MaybePromise,
} from './types';

const DEFAULT_POLL_MS = 10_000;

export interface UseClaimsWorkspaceOptions {
  caseId: string;
  source: ClaimWorkspaceSource;
  pollMs?: number;
}

export interface ClaimsWorkspaceState {
  snapshot: ClaimSnapshot | null;
  loading: boolean;
  loadError: string;
  run(command: ClaimCommand): Promise<CommandResult>;
  runAgent(command: ClaimCommand): Promise<CommandResult>;
  reload(): Promise<void>;
}

function isPromise<T>(value: MaybePromise<T>): value is Promise<T> {
  return typeof (value as Promise<T>)?.then === 'function';
}

function messageOf(cause: unknown): string {
  return describeError(cause) || '无法连接本地服务。';
}

export function useClaimsWorkspace({
  caseId, source, pollMs = DEFAULT_POLL_MS,
}: UseClaimsWorkspaceOptions): ClaimsWorkspaceState {
  const bootRef = useRef<{ snapshot: ClaimSnapshot | null; pending: Promise<ClaimSnapshot> | null } | null>(null);
  if (bootRef.current === null) {
    const loaded = source.load();
    bootRef.current = {
      snapshot: isPromise(loaded) || loaded.case_id !== caseId ? null : loaded,
      pending: isPromise(loaded) ? loaded : null,
    };
  }
  const boot = bootRef.current;

  const [snapshot, setSnapshot] = useState<ClaimSnapshot | null>(boot.snapshot);
  const [loading, setLoading] = useState(boot.snapshot === null);
  const [loadError, setLoadError] = useState('');

  const caseRef = useRef(caseId);
  caseRef.current = caseId;
  const seqRef = useRef(boot.snapshot?.seq ?? -1);
  const tokenRef = useRef(0);
  const mounted = useRef(true);
  const booted = useRef(false);
  /** 进行中的写命令：提交期间不刷新，避免轮询覆盖正在提交的结果。 */
  const inFlight = useRef(0);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const accept = useCallback((next: ClaimSnapshot, origin: string): 'applied' | 'stale' | 'mismatch' => {
    if (!mounted.current || caseRef.current !== origin) return 'mismatch';
    if (next.case_id !== origin) return 'mismatch';
    if (seqRef.current >= 0 && next.seq < seqRef.current) return 'stale';
    seqRef.current = next.seq;
    setSnapshot(next);
    return 'applied';
  }, []);

  const reload = useCallback(async (): Promise<void> => {
    if (inFlight.current > 0) return;
    const my = ++tokenRef.current;
    const origin = caseId;
    try {
      const next = await source.load();
      if (my !== tokenRef.current || caseRef.current !== origin) return;
      const state = accept(next, origin);
      if (state === 'mismatch') {
        if (mounted.current) setLoadError('返回的数据不属于当前案件，已忽略。');
        return;
      }
      if (state === 'applied') setLoadError('');
    } catch (cause) {
      if (my === tokenRef.current && caseRef.current === origin && mounted.current) {
        setLoadError(messageOf(cause));
      }
    }
  }, [caseId, source, accept]);

  useEffect(() => {
    if (!booted.current) {
      booted.current = true;
      if (boot.pending) {
        const my = ++tokenRef.current;
        const origin = caseId;
        boot.pending.then((next) => {
          if (my !== tokenRef.current || caseRef.current !== origin) return;
          if (accept(next, origin) === 'mismatch' && mounted.current) {
            setLoadError('返回的数据不属于当前案件，已忽略。');
          }
        }).catch((cause: unknown) => {
          if (my === tokenRef.current && mounted.current) setLoadError(messageOf(cause));
        }).finally(() => {
          if (mounted.current && my === tokenRef.current) setLoading(false);
        });
      } else {
        setLoading(false);
      }
      return;
    }
    seqRef.current = -1;
    setSnapshot(null);
    setLoadError('');
    setLoading(true);
    void reload().finally(() => { if (mounted.current) setLoading(false); });
  }, [reload, boot, caseId, accept]);

  const run = useCallback(async (command: ClaimCommand): Promise<CommandResult> => {
    const origin = caseId;
    let result: CommandResult;
    inFlight.current += 1;
    try {
      result = await source.human(command);
    } catch (cause) {
      return { ok: false, code: 'validation', message: messageOf(cause) };
    } finally {
      inFlight.current -= 1;
    }
    if (caseRef.current !== origin) {
      return { ok: false, code: 'conflict', message: '案件已切换，本次操作结果已忽略。' };
    }
    if (result.ok && result.snapshot) {
      const state = accept(result.snapshot, origin);
      if (state === 'mismatch') {
        return { ok: false, code: 'conflict', message: '返回的数据不属于当前案件，已忽略。' };
      }
    }
    return result;
  }, [caseId, source, accept]);

  const runAgent = useCallback(async (command: ClaimCommand): Promise<CommandResult> => {
    if (!source.agent) {
      return { ok: false, code: 'forbidden', message: '当前入口不提供 Agent 写入。' };
    }
    const origin = caseId;
    let result: CommandResult;
    inFlight.current += 1;
    try {
      result = await source.agent(command);
    } catch (cause) {
      return { ok: false, code: 'validation', message: messageOf(cause) };
    } finally {
      inFlight.current -= 1;
    }
    if (caseRef.current !== origin) {
      return { ok: false, code: 'conflict', message: '案件已切换，本次操作结果已忽略。' };
    }
    if (result.ok && result.snapshot) {
      const state = accept(result.snapshot, origin);
      if (state === 'mismatch') {
        return { ok: false, code: 'conflict', message: '返回的数据不属于当前案件，已忽略。' };
      }
    }
    return result;
  }, [caseId, source, accept]);

  useEffect(() => {
    if (!source.seq) return;
    const check = () => {
      if (inFlight.current > 0) return;
      Promise.resolve(source.seq!()).then((value) => {
        if (caseRef.current !== caseId) return;
        if (value !== seqRef.current) void reload();
      }).catch(() => { /* 静默，下次再试 */ });
    };
    const timer = window.setInterval(check, pollMs);
    const onVisible = () => { if (document.visibilityState === 'visible') check(); };
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('focus', check);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('focus', check);
    };
  }, [caseId, source, pollMs, reload]);

  return { snapshot, loading, loadError, run, runAgent, reload };
}
