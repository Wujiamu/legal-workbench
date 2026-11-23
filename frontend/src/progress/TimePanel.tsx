/**
 * 工时面板（用户反馈第七轮：计时器+快捷时长+日视图+按标题汇总）。
 * P1 原型内存口径；归档后只读由页面守卫。
 */
import { useEffect, useMemo, useState } from 'react';
import type { TimeLog } from './timeLog';
import type { ProgressNode, ProgressSnapshot } from './types';

const QUICK_HOURS = [0.25, 0.5, 1, 2];

function today(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function TimePanel({ log, snapshot, readOnly, onChanged }: {
  log: TimeLog;
  snapshot: ProgressSnapshot;
  readOnly: boolean;
  onChanged: () => void;
}) {
  const [tab, setTab] = useState<'timer' | 'day' | 'summary'>('timer');
  const [date, setDate] = useState(today());
  const [tick, setTick] = useState(0);
  const [error, setError] = useState('');
  const [manualNode, setManualNode] = useState('');
  const running = log.running();
  const nodes = useMemo(() => snapshot.nodes.filter((node) => node.removed_at === null), [snapshot]);

  // 运行中计时每秒刷新显示
  useEffect(() => {
    if (running.length === 0) return;
    const timer = setInterval(() => setTick((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [running.length]);

  function guard<T>(result: { ok: boolean; message?: string } & T): boolean {
    if (!result.ok) {
      setError(result.message ?? '操作失败');
      return false;
    }
    setError('');
    onChanged();
    return true;
  }

  function elapsedLabel(startedAt: string): string {
    void tick;
    const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(startedAt)) / 60000));
    return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
  }

  const day = log.day(date);
  const summary = log.summaryByTitle();
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
              <select id="time-node" value={manualNode} disabled={readOnly}
                onChange={(event) => setManualNode(event.target.value)}>
                <option value="">选择节点…</option>
                {nodes.map((node) => <option key={node.id} value={node.id}>{node.title}</option>)}
              </select>
            </label>
            <button type="button" disabled={readOnly || !manualNode}
              onClick={() => {
                const node = nodes.find((item) => item.id === manualNode);
                if (node) guard(log.start(node.id, node.title));
              }}>开始计时</button>
          </div>
          {running.length > 0 ? (
            <ul className="time-running">
              {running.map((entry) => (
                <li key={entry.id}>
                  <strong>{entry.node_title}</strong>
                  <span className="muted">已计 {elapsedLabel(entry.started_at!)}</span>
                  <div className="work-actions">
                    {QUICK_HOURS.map((hours) => (
                      <button key={hours} type="button" disabled={readOnly}
                        onClick={() => guard(log.stop(entry.id, undefined, { hours }))}>{hours}h</button>
                    ))}
                    <button type="button" disabled={readOnly}
                      onClick={() => guard(log.stop(entry.id))}>停止（按实际）</button>
                  </div>
                </li>
              ))}
            </ul>
          ) : <p className="muted">没有进行中的计时。</p>}
          <div className="time-manual">
            <strong>补录</strong>
            <div className="work-actions">
              {QUICK_HOURS.map((hours) => (
                <button key={hours} type="button" disabled={readOnly || !manualNode}
                  onClick={() => {
                    const node = nodes.find((item) => item.id === manualNode);
                    if (node) guard(log.add(node.id, node.title, hours, date));
                  }}>{hours}h</button>
              ))}
            </div>
          </div>
        </>
      ) : null}
      {tab === 'day' ? (
        <>
          <label htmlFor="time-date">日期
            <input id="time-date" type="date" value={date} onChange={(event) => setDate(event.target.value)} />
          </label>
          <p>合计 <strong>{day.totalHours}h</strong></p>
          {day.entries.length === 0 ? <p className="muted">当日无记录。</p> : (
            <table className="time-table">
              <thead><tr><th>节点</th><th>时长</th><th>来源</th><th></th></tr></thead>
              <tbody>
                {day.entries.map((entry) => (
                  <tr key={entry.id}>
                    <td>{entry.node_title}</td>
                    <td>{entry.hours}h</td>
                    <td>{entry.source === 'timer' ? '计时' : '补录'}</td>
                    <td><button type="button" disabled={readOnly}
                      onClick={() => guard(log.remove(entry.id))}>删除</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      ) : null}
      {tab === 'summary' ? (
        <>
          <p>总计 <strong>{totalAll}h</strong>（billable hour 表雏形：按节点标题聚合）</p>
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