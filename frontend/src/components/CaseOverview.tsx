import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { describeError } from '../api/client';
import { casesApi } from '../api/endpoints';
import type { CaseOverview, CaseWorkStatus, Deadline } from '../api/endpoints';
import { WORK_STATUS_OPTIONS } from '../ui/caseStatus';
import { Button, ErrorState } from '../ui/primitives';

export function CaseOverviewPanel({ item, archived = false, onSaved, onReload }: {
  item: CaseOverview;
  archived?: boolean;
  onSaved: (item: CaseOverview) => void;
  onReload: () => void;
}) {
  const [caseNumber, setCaseNumber] = useState(item.case_number ?? '');
  const [plaintiff, setPlaintiff] = useState(item.plaintiff ?? '');
  const [defendant, setDefendant] = useState(item.defendant ?? '');
  const [workStatus, setWorkStatus] = useState<CaseWorkStatus>(item.work_status ?? 'normal');
  const [metaBusy, setMetaBusy] = useState(false);
  const [metaError, setMetaError] = useState('');
  const [metaSaved, setMetaSaved] = useState(false);

  const [deadlines, setDeadlines] = useState(item.deadlines);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const readOnly = archived || Boolean(item.archived_at);
  const syncedRef = useRef({
    caseId: item.case_id,
    deadlinesKey: JSON.stringify(item.deadlines),
  });

  const metaChanged = (
    caseNumber.trim() !== (item.case_number ?? '')
    || plaintiff.trim() !== (item.plaintiff ?? '')
    || defendant.trim() !== (item.defendant ?? '')
    || workStatus !== (item.work_status ?? 'normal')
  );
  // P4：当前节点/下一步改由进度树驱动，概览只编辑期限；保存时保留旧 stage/next_action 原值。
  const changed = deadlines !== item.deadlines;

  useEffect(() => {
    setCaseNumber(item.case_number ?? '');
    setPlaintiff(item.plaintiff ?? '');
    setDefendant(item.defendant ?? '');
    setWorkStatus(item.work_status ?? 'normal');
  }, [item.case_id, item.case_number, item.plaintiff, item.defendant, item.work_status]);

  useEffect(() => {
    const nextKey = JSON.stringify(item.deadlines);
    const caseChanged = syncedRef.current.caseId !== item.case_id;
    const serverDeadlinesChanged = syncedRef.current.deadlinesKey !== nextKey;
    if (caseChanged) {
      setMetaSaved(false);
      setSaved(false);
      setMetaError('');
      setError('');
    }
    if (caseChanged || serverDeadlinesChanged) {
      syncedRef.current = { caseId: item.case_id, deadlinesKey: nextKey };
      setDeadlines(item.deadlines);
    }
  }, [item.case_id, item.deadlines]);

  async function saveMeta(event: React.FormEvent) {
    event.preventDefault();
    if (metaBusy || readOnly || !metaChanged) return;
    setMetaBusy(true);
    setMetaError('');
    try {
      const result = await casesApi.updateMetadata(item.case_id, {
        expected_revision: item.revision,
        case_number: caseNumber.trim() || null,
        plaintiff: plaintiff.trim() || null,
        defendant: defendant.trim() || null,
        work_status: workStatus,
      });
      onSaved(result);
      setMetaSaved(true);
    } catch (cause) {
      setMetaError(describeError(cause));
    } finally {
      setMetaBusy(false);
    }
  }

  function updateDeadline(index: number, change: Partial<Deadline>) {
    if (readOnly) return;
    setDeadlines((items) => items.map((entry, position) => position === index ? { ...entry, ...change } : entry));
    setSaved(false);
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (busy || readOnly) return;
    setBusy(true);
    setError('');
    try {
      const result = await casesApi.schedule(item.case_id, {
        stage: item.stage ?? '', next_action: item.next_action ?? '',
        deadlines, expected_revision: item.revision,
      });
      onSaved(result);
      setDeadlines(result.deadlines);
      setSaved(true);
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  return <section className="overview" aria-labelledby="progress-title">
    <h2 id="progress-title">案件概览与办案进度</h2>
    {/* P4：当前节点来自进度树（所有进行中阶段），编辑入口在进度页；旧 stage/next_action 仅作过渡回退。 */}
    <div className="progress-summary">
      <p>当前节点：<strong>{(item.active_stages?.length ? item.active_stages.join('、') : (item.active_stage ?? item.stage)) || '待登记'}</strong>
        {(item.pending_work?.length ?? 0) > 0
          ? <span className="muted"> · 未完成工作：{item.pending_work!.slice(0, 5).join('、')}{item.pending_work!.length > 5 ? '…' : ''}</span>
          : null}</p>
      <Link to={`/cases/${item.case_id}/progress`}>打开办案进度图 →</Link>
    </div>
    <form className="overview-meta" onSubmit={(event) => void saveMeta(event)}>
      <fieldset disabled={metaBusy || readOnly} className="schedule case-meta-card">
        <h3>案件基本信息</h3>
        <div className="case-meta-grid">
          <label>案号
            <input maxLength={120} placeholder="未立案可留空，自动装卷时带入封面"
              value={caseNumber}
              onChange={(event) => { setCaseNumber(event.target.value); setMetaSaved(false); }} />
          </label>
          <label>办理状态
            <select value={workStatus}
              onChange={(event) => { setWorkStatus(event.target.value as CaseWorkStatus); setMetaSaved(false); }}>
              {WORK_STATUS_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </label>
          <label>原告 / 申请人
            <input maxLength={200} placeholder="自动装卷时带入封面与目录"
              value={plaintiff}
              onChange={(event) => { setPlaintiff(event.target.value); setMetaSaved(false); }} />
          </label>
          <label>被告 / 被申请人
            <input maxLength={200} placeholder="自动装卷时带入封面与目录"
              value={defendant}
              onChange={(event) => { setDefendant(event.target.value); setMetaSaved(false); }} />
          </label>
        </div>
        <div className="work-actions">
          <Button type="submit" disabled={readOnly || !metaChanged || metaBusy}>
            {metaBusy ? '保存中…' : '保存基本信息'}
          </Button>
          {metaSaved ? <span role="status">基本信息已保存</span> : null}
        </div>
      </fieldset>
      {metaError ? <ErrorState message={metaError} onRetry={onReload} /> : null}
    </form>
    <form onSubmit={(event) => void save(event)}>
      <fieldset disabled={busy || readOnly} className="schedule">
        <h3>期限与计划</h3>
        {deadlines.length ? <ol className="schedule-list">{deadlines.map((entry, index) =>
          <li key={index}>
            <label>事项<input required maxLength={200} value={entry.title}
              onChange={(event) => updateDeadline(index, { title: event.target.value })} /></label>
            <label>期限类型<select value={entry.kind} onChange={(event) =>
              updateDeadline(index, { kind: event.target.value as Deadline['kind'] })}>
              <option value="external">外部期限</option><option value="internal">内部计划</option>
            </select></label>
            <label>到期日<input type="date" required value={entry.due}
              onChange={(event) => updateDeadline(index, { due: event.target.value })} /></label>
            <label className="completed-label"><input type="checkbox" checked={entry.completed}
              onChange={(event) => updateDeadline(index, { completed: event.target.checked })} />已完成</label>
            <Button variant="quiet" disabled={readOnly} onClick={() => {
              setDeadlines((items) => items.filter((_, position) => index !== position)); setSaved(false);
            }}>移除</Button>
          </li>)}</ol> : null}
        <div className="work-actions schedule-actions">
          <Button variant="quiet" disabled={readOnly || deadlines.length >= 200} onClick={() => {
            setDeadlines((items) => [...items, { title: '', kind: 'external', due: '', completed: false }]);
            setSaved(false);
          }}>＋ 登记期限</Button>
          <Button type="submit" disabled={readOnly || !changed || deadlines.some((entry) => !entry.title.trim())}>
            {busy ? '保存中…' : '保存办理信息'}
          </Button>
          {saved ? <span role="status">已保存</span> : null}
        </div>
      </fieldset>
      {error ? <ErrorState message={error} onRetry={onReload} /> : null}
    </form>
  </section>;
}
