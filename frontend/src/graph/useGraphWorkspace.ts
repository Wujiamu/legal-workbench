/**
 * 关系图工作区状态钩子：原型（同步内存）与真实案件（异步 HTTP）共用。
 *
 * 守卫顺序（与 CaseProgressPage 的 P4 口径一致）：
 * 1. 响应回来时案件已切换 → 丢弃，不写入任何状态；
 * 2. 快照 case_id 与请求案件不一致 → 丢弃并提示，绝不跨案覆盖；
 * 3. 快照序号低于当前已显示序号 → 视为迟到响应，丢弃（弹窗可正常关闭）；
 * 4. 只有通过以上检查的响应才替换快照，避免旧响应把选中对象改回旧版本。
 *
 * 同步数据源（P1 内存执行器）在首次渲染即拿到快照，不出现加载闪烁；
 * 异步数据源（P4 HTTP）返回 Promise，由同一套守卫处理。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { describeError } from '../api/client';
import type {
  CommandResult, GraphCommand, GraphRules, GraphSnapshot, GraphWorkspaceSource, MaybePromise,
} from './types';

const DEFAULT_POLL_MS = 10_000;

export interface UseGraphWorkspaceOptions {
  caseId: string;
  source: GraphWorkspaceSource;
  /** 变化序号轮询间隔；数据源未提供 seqs 时不轮询。 */
  pollMs?: number;
}

export interface GraphWorkspaceState {
  snapshot: GraphSnapshot | null;
  rules: GraphRules | null;
  loading: boolean;
  loadError: string;
  notice: string;
  error: string;
  setNotice(value: string): void;
  setError(value: string): void;
  dismissNotice(): void;
  dismissError(): void;
  run(command: GraphCommand): Promise<CommandResult>;
  runAgent(command: GraphCommand): Promise<CommandResult>;
  undo(): Promise<CommandResult>;
  impact(target: { entity_id?: string; relation_id?: string }):
    Promise<{ id: string; title: string }[] | null>;
  archive(archived: boolean): Promise<void>;
  reload(): Promise<void>;
}

type AcceptResult = 'applied' | 'stale' | 'mismatch';

interface Boot {
  snapshot: GraphSnapshot | null;
  rules: GraphRules | null;
  snapshotPromise: Promise<GraphSnapshot> | null;
  rulesPromise: Promise<GraphRules> | null;
}

function isPromise<T>(value: MaybePromise<T>): value is Promise<T> {
  return typeof (value as Promise<T>)?.then === 'function';
}

function messageOf(cause: unknown): string {
  return describeError(cause) || '无法连接本地服务。';
}

function bootFrom(source: GraphWorkspaceSource, caseId: string): Boot {
  const loaded = source.load();
  const loadedRules = source.rules();
  const snapshot = isPromise(loaded) || loaded.case_id !== caseId ? null : loaded;
  return {
    snapshot,
    rules: isPromise(loadedRules) ? null : loadedRules,
    snapshotPromise: isPromise(loaded) ? loaded : null,
    rulesPromise: isPromise(loadedRules) ? loadedRules : null,
  };
}

export function useGraphWorkspace({
  caseId, source, pollMs = DEFAULT_POLL_MS,
}: UseGraphWorkspaceOptions): GraphWorkspaceState {
  const bootRef = useRef<Boot | null>(null);
  if (bootRef.current === null) bootRef.current = bootFrom(source, caseId);
  const boot = bootRef.current;

  const [snapshot, setSnapshot] = useState<GraphSnapshot | null>(boot.snapshot);
  const [rules, setRules] = useState<GraphRules | null>(boot.rules);
  const [loading, setLoading] = useState(boot.snapshot === null);
  const [loadError, setLoadError] = useState('');
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');

  // 渲染期同步更新，保证 Promise 回调看到的是最新案件。
  const caseRef = useRef(caseId);
  caseRef.current = caseId;
  const seqRef = useRef(boot.snapshot?.seq ?? -1);
  const domainSeqRef = useRef(boot.snapshot?.seqs ?? null);
  const loadToken = useRef(0);
  const mounted = useRef(true);
  const booted = useRef(false);
  /** 进行中的写命令数：提交期间不接受轮询刷新，避免覆盖正在提交的结果。 */
  const inFlight = useRef(0);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const accept = useCallback((next: GraphSnapshot, origin: string): AcceptResult => {
    if (!mounted.current || caseRef.current !== origin) return 'mismatch';
    if (next.case_id !== origin) return 'mismatch';
    if (seqRef.current >= 0 && next.seq < seqRef.current) return 'stale';
    seqRef.current = next.seq;
    domainSeqRef.current = next.seqs;
    setSnapshot(next);
    return 'applied';
  }, []);

  const loadRules = useCallback(async (): Promise<void> => {
    try {
      const value = await source.rules();
      if (mounted.current && caseRef.current === caseId) setRules(value);
    } catch (cause) {
      if (mounted.current && caseRef.current === caseId) setLoadError(messageOf(cause));
    }
  }, [caseId, source]);

  const reload = useCallback(async (): Promise<void> => {
    // 正在提交写命令时不刷新：轮询的旧读取不能覆盖即将返回的提交结果。
    if (inFlight.current > 0) return;
    const my = ++loadToken.current;
    const origin = caseId;
    try {
      const next = await source.load();
      if (my !== loadToken.current || caseRef.current !== origin) return;
      const state = accept(next, origin);
      if (state === 'mismatch') {
        if (mounted.current) setLoadError('返回的数据不属于当前案件，已忽略。');
        return;
      }
      if (state === 'stale') return;
      setLoadError('');
    } catch (cause) {
      if (my === loadToken.current && caseRef.current === origin && mounted.current) {
        setLoadError(messageOf(cause));
      }
    }
  }, [caseId, source, accept]);

  // 首次挂载：同步快照直接可用；异步快照等 Promise；之后数据源或案件变化才重置。
  useEffect(() => {
    if (!booted.current) {
      booted.current = true;
      const my = ++loadToken.current;
      const origin = caseId;
      if (boot.snapshotPromise) {
        boot.snapshotPromise.then((next) => {
          if (my !== loadToken.current || caseRef.current !== origin) return;
          if (accept(next, origin) === 'mismatch' && mounted.current) {
            setLoadError('返回的数据不属于当前案件，已忽略。');
          }
        }).catch((cause: unknown) => {
          if (my === loadToken.current && mounted.current) setLoadError(messageOf(cause));
        }).finally(() => {
          if (mounted.current && my === loadToken.current) setLoading(false);
        });
      } else {
        setLoading(false);
      }
      if (boot.rulesPromise) {
        boot.rulesPromise.then((value) => {
          if (mounted.current && caseRef.current === origin) setRules(value);
        }).catch((cause: unknown) => {
          if (mounted.current && caseRef.current === origin) setLoadError(messageOf(cause));
        });
      }
      return;
    }
    // 数据源或案件变化：清空旧状态并重新读取。
    seqRef.current = -1;
    setSnapshot(null);
    setRules(null);
    setLoadError('');
    setNotice('');
    setError('');
    setLoading(true);
    void reload().finally(() => { if (mounted.current) setLoading(false); });
    void loadRules();
  }, [reload, loadRules, boot, caseId, accept]);

  const run = useCallback(async (command: GraphCommand): Promise<CommandResult> => {
    const origin = caseId;
    let result: CommandResult;
    inFlight.current += 1;
    try {
      result = await source.human(command);
    } catch (cause) {
      const message = messageOf(cause);
      if (caseRef.current === origin) setError(message);
      return { ok: false, code: 'validation', message };
    } finally {
      inFlight.current -= 1;
    }
    if (caseRef.current !== origin) {
      return { ok: false, code: 'case_mismatch', message: '案件已切换，本次操作结果已忽略。' };
    }
    if (!result.ok) {
      setError(result.message);
      return result;
    }
    const state = accept(result.snapshot, origin);
    if (state === 'mismatch') {
      const message = '返回的数据不属于当前案件，已忽略。';
      setError(message);
      return { ok: false, code: 'case_mismatch', message };
    }
    setNotice(result.notice ?? '');
    setError('');
    return result;
  }, [caseId, source, accept]);

  const runAgent = useCallback(async (command: GraphCommand): Promise<CommandResult> => {
    if (!source.agent) {
      return { ok: false, code: 'read_only', message: '当前入口不提供 Agent 写入。' };
    }
    const origin = caseId;
    let result: CommandResult;
    inFlight.current += 1;
    try {
      result = await source.agent(command);
    } catch (cause) {
      const message = messageOf(cause);
      if (caseRef.current === origin) setError(message);
      return { ok: false, code: 'validation', message };
    } finally {
      inFlight.current -= 1;
    }
    if (caseRef.current !== origin) {
      return { ok: false, code: 'case_mismatch', message: '案件已切换，本次操作结果已忽略。' };
    }
    if (!result.ok) {
      setError(result.message);
      return result;
    }
    const state = accept(result.snapshot, origin);
    if (state === 'mismatch') {
      const message = '返回的数据不属于当前案件，已忽略。';
      setError(message);
      return { ok: false, code: 'case_mismatch', message };
    }
    setNotice(result.notice ?? '');
    setError('');
    return result;
  }, [caseId, source, accept]);

  const undo = useCallback(async (): Promise<CommandResult> => {
    if (!source.undo) {
      return { ok: false, code: 'read_only', message: '当前入口不支持撤销。' };
    }
    const origin = caseId;
    inFlight.current += 1;
    try {
      const result = await source.undo();
      if (caseRef.current !== origin) {
        return { ok: false, code: 'case_mismatch', message: '案件已切换，本次操作结果已忽略。' };
      }
      if (result.ok) {
        if (accept(result.snapshot, origin) === 'mismatch') {
          return { ok: false, code: 'case_mismatch', message: '返回的数据不属于当前案件，已忽略。' };
        }
        setNotice(result.notice ?? '');
        setError('');
      } else {
        setError(result.message);
      }
      return result;
    } catch (cause) {
      const message = messageOf(cause);
      if (caseRef.current === origin) setError(message);
      return { ok: false, code: 'validation', message };
    } finally {
      inFlight.current = Math.max(0, inFlight.current - 1);
    }
  }, [caseId, source, accept]);

  const impact = useCallback(async (
    target: { entity_id?: string; relation_id?: string },
  ): Promise<{ id: string; title: string }[] | null> => {
    if (!source.impact) return null;
    try {
      const result = await source.impact(target);
      if (caseRef.current !== caseId) return null;
      return result.graphs;
    } catch {
      return null;
    }
  }, [caseId, source]);

  const archive = useCallback(async (archived: boolean): Promise<void> => {
    if (!source.archive) throw new Error('当前入口不支持归档操作。');
    const origin = caseId;
    await source.archive(archived);
    if (caseRef.current !== origin) return;
    await reload();
  }, [caseId, source, reload]);

  useEffect(() => {
    if (!source.seqs) return;
    const check = () => {
      // 提交期间不比较序号：避免把提交中的序号差当成外部变更触发刷新。
      if (inFlight.current > 0) return;
      Promise.resolve(source.seqs!()).then((seqs) => {
        if (caseRef.current !== caseId) return;
        const known = domainSeqRef.current;
        // 证据目录的写命令只推进 evidence 序号，图与目录任一变化都要重新读取。
        if (!known || seqs.graph !== known.graph || seqs.evidence !== known.evidence) {
          void reload();
        }
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

  return {
    snapshot, rules, loading, loadError, notice, error,
    setNotice, setError,
    dismissNotice: () => setNotice(''),
    dismissError: () => setError(''),
    run, runAgent, undo, impact, archive, reload,
  };
}
