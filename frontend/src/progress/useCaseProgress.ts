/**
 * 快照、命令、变化检测与冲突处理（总体计划 6.4）。
 * 命令后以服务端快照为准；轮询案件变化序号（默认 10 秒）与回到前台时检查；
 * 迟到响应按请求序号丢弃；冲突时重新读取并把结果交回组件保留草稿。
 * 执行器接口同步/异步两用：原型为内存执行器，P4 换 HTTP 不改本钩子。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  CommandResult, ProgressCommand, ProgressExecutor, ProgressRules, ProgressSnapshot,
} from './types';

export interface UseCaseProgressOptions {
  pollMs?: number;
}

export interface UseCaseProgress {
  snapshot: ProgressSnapshot | null;
  rules: ProgressRules;
  notice: string;
  conflict: string;
  execute(command: ProgressCommand): CommandResult;
  refresh(): void;
  dismissNotice(): void;
  dismissConflict(): void;
}

function isPromise(value: unknown): value is Promise<ProgressSnapshot> {
  return typeof (value as Promise<ProgressSnapshot>)?.then === 'function';
}

export function useCaseProgress(executor: ProgressExecutor,
                                { pollMs = 10000 }: UseCaseProgressOptions = {}): UseCaseProgress {
  const [rules] = useState<ProgressRules>(() => executor.getRules());
  const initialRef = useRef<ProgressSnapshot | Promise<ProgressSnapshot> | undefined>(undefined);
  const [snapshot, setSnapshot] = useState<ProgressSnapshot | null>(() => {
    const initial = executor.getSnapshot() as ProgressSnapshot | Promise<ProgressSnapshot>;
    initialRef.current = initial;
    return isPromise(initial) ? null : initial;
  });
  const [notice, setNotice] = useState('');
  const [conflict, setConflict] = useState('');
  const token = useRef(0);
  const seqRef = useRef(snapshot?.seq ?? -1);

  const sync = useCallback(() => {
    const my = ++token.current;
    const incoming = executor.getSnapshot() as ProgressSnapshot | Promise<ProgressSnapshot>;
    Promise.resolve(incoming).then((next) => {
      if (my !== token.current) return; // 迟到响应丢弃
      if (seqRef.current >= 0 && next.seq !== seqRef.current) {
        setNotice('进度数据已更新');
      }
      seqRef.current = next.seq;
      setSnapshot(next);
    }).catch(() => {
      if (my === token.current) setNotice('进度数据读取失败，请稍后重试');
    });
  }, [executor]);

  const applyResult = useCallback((result: CommandResult) => {
    if (result.ok) {
      token.current += 1; // 命令返回即最新，作废在途读取
      seqRef.current = result.snapshot.seq;
      setSnapshot(result.snapshot);
      setConflict('');
    } else if (result.code === 'conflict') {
      setConflict(result.message);
      sync(); // 重新读取服务端状态；草稿由组件保留
    }
  }, [sync]);

  const execute = useCallback((command: ProgressCommand): CommandResult => {
    const result = executor.human(command);
    applyResult(result);
    return result;
  }, [executor, applyResult]);

  useEffect(() => {
    const initial = initialRef.current;
    initialRef.current = undefined;
    if (initial !== undefined && isPromise(initial)) {
      const my = ++token.current;
      initial.then((next) => {
        if (my !== token.current) return;
        seqRef.current = next.seq;
        setSnapshot(next);
      }).catch(() => {
        if (my === token.current) setNotice('进度数据读取失败，请稍后重试');
      });
    } else if (snapshot === null) {
      sync();
    }
    const check = () => {
      const seqs = executor.getSeqs();
      if (seqs.progress !== seqRef.current) sync();
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [executor, pollMs, sync]);

  return {
    snapshot, rules, notice, conflict, execute, refresh: sync,
    dismissNotice: () => setNotice(''),
    dismissConflict: () => setConflict(''),
  };
}