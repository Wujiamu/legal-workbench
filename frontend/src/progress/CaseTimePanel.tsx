/**
 * 工时面板（P2 持久化）：计时/补录/日视图/按标题汇总，全部走 timeApi。
 * 由案件进度页的「工时」按钮弹出；归档后只读。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { describeError } from '../api/client';
import { timeApi } from '../api/endpoints';
import type { TimeDayOutput, TimeEntryOut } from '../api/endpoints';
import type { ProgressNode } from './types';

const QUICK_HOURS = [0.25, 0.5, 1, 2];

export function localToday(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

type SummaryRow = TimeDayOutput['summary'][number];

export function CaseTimePanel({ caseId, nodes, readOnly }: {
  caseId: string;
  nodes: ProgressNode[];
  readOnly: boolean;
}) {
  const [tab, setTab] = useState<'timer' | 'day' | 'summary'>('timer');
  const [date, setDate] = useState(localToday);
  const [day, setDay] = useState<TimeDayOutput | null>(null);
  const [running, setRunning] = useState<TimeEntryOut[]>([]);
  const [summary, setSummary] = useState<SummaryRow[]>([]);
  const [nodeId, setNodeId] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);

  const options = useMemo(
    () => nodes.filter((node) => node.removed_at === null), [nodes]);

  const loadDay = useCallback(async (which: string) => {
    const out = await timeApi.day(caseId, which);
    // 运行中的计时永远属于今天，且只认 timer（补录条目 stopped_at 也为空）；
    // 切到别的日期时不动 running。
    if (which === localToday()) {
      setRunning(out.entries.filter(
        (entry) => entry.source === 'timer' && entry.stopped_at === null));
    }
    setSummary(out.summary);
    return out;
  }, [caseId]);

  useEffect(() => {
    let alive = true;
    loadDay(date).then((out) => { if (alive) { setDay(out); setError(''); } })
      .catch((cause: unknown) => { if (alive) setError(describeError(cause)); });
    return () => { alive = false; };
  }, [date, loadDay]);

  useEffect(() => {
    if (running.length === 0) return;
    const timer = setInterval(() => setTick((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [running.length]);

  async function act(work: () => Promise<unknown>) {
    if (readOnly) return;
    setBusy(true);
    setError('');
    try {
      await work();
      setDay(await loadDay(date));
      if (date !== localToday()) await loadDay(localToday());
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  function selectedTitle(): string {
    return options.find((node) => node.id === nodeId)?.title ?? '';
  }

  function elapsedLabel(startedAt: string): string {
    void tick;
    const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(startedAt)) / 60000));
    return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
  }

  const totalAll = summary.reduce((sum, row) => sum + row.hours, 0);

  return (
    <section className="progress-time" aria-label="工时记录">
      <header className="progress-review-head">
        <h2>工时</h2>
        <div className="progress-view-switch" role="group" aria-label="工时视图">
          <button type="button" aria-pressed={tab === 'timer'} onClick={() => setTab('timer')}>计时</button>
          <button type="button" aria-pressed={tab === 'day'} onClick={() => setTab('day')}>日视图</button>
          <button type="button" aria-pressed={tab === 'summary'} onClick={() => setTab('summary')}>按标题汇总</button>
        </div>
      </header>
      {readOnly ? <p className="muted">案件已归档，工时只读。</p> : null}
      {error ? <p role="alert" className="progress-error">错误：{error}</p> : null}
      {tab === 'timer' ? (
        <>
          <div className="time-start-row">
            <label htmlFor="time-node">节点
              <select id="time-node" value={nodeId} disabled={readOnly}
                onChange={(event) => setNodeId(event.target.value)}>
                <option value="">选择节点…</option>
                {options.map((node) => <option key={node.id} value={node.id}>{node.title}</option>)}
              </select>
            </label>
            <button type="button" disabled={readOnly || busy || !nodeId}
              onClick={() => void act(() => timeApi.start(caseId, {
                node_id: nodeId, node_title: selectedTitle() }))}>开始计时</button>
          </div>
          {running.length > 0 ? (
            <ul className="time-running">
              {running.map((entry) => (
                <li key={entry.id}>
                  <strong>{entry.node_title}</strong>
                  <span className="muted">已计 {elapsedLabel(entry.started_at ?? new Date().toISOString())}</span>
                  <div className="work-actions">
                    <button type="button" disabled={readOnly || busy}
                      onClick={() => void act(() => timeApi.stop(caseId, entry.id))}>停止（按实际）</button>
                  </div>
                </li>
              ))}
            </ul>
          ) : <p className="muted">没有进行中的计时。</p>}
          <div className="time-manual">
            <strong>补录</strong>
            <div className="work-actions">
              {QUICK_HOURS.map((hours) => (
                <button key={hours} type="button" disabled={readOnly || busy || !nodeId}
                  onClick={() => void act(() => timeApi.add(caseId, {
                    node_id: nodeId, node_title: selectedTitle(), hours, work_date: date }))}>
                  {hours}h
                </button>
              ))}
            </div>
          </div>
        </>
      ) : null}
      {tab === 'day' ? (
        <>
          <label htmlFor="time-date">日期
            <input id="time-date" type="date" value={date}
              onChange={(event) => setDate(event.target.value)} />
          </label>
          <p>合计 <strong>{day?.total_hours ?? 0}h</strong></p>
          {!day || day.entries.length === 0 ? <p className="muted">当日无记录。</p> : (
            <table className="time-table">
              <thead><tr><th>节点</th><th>时长</th><th>来源</th><th>操作</th></tr></thead>
              <tbody>
                {day.entries.map((entry) => (
                  <tr key={entry.id}>
                    <td>{entry.node_title}</td>
                    <td>{entry.hours}h</td>
                    <td>{entry.source === 'timer' ? '计时' : '补录'}</td>
                    <td><button type="button" disabled={readOnly || busy}
                      onClick={() => void act(() => timeApi.remove(caseId, entry.id))}>删除</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      ) : null}
      {tab === 'summary' ? (
        <>
          <p>总计 <strong>{totalAll}h</strong>（按节点标题聚合）</p>
          {summary.length === 0 ? <p className="muted">暂无工时。</p> : (
            <table className="time-table">
              <thead><tr><th>标题</th><th>时长</th><th>条数</th></tr></thead>
              <tbody>
                {summary.map((row) => (
                  <tr key={row.title}><td>{row.title}</td><td>{row.hours}h</td><td>{row.entries}</td></tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      ) : null}
    </section>
  );
}
