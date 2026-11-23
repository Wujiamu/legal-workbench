import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { describeError } from '../api/client';
import type { Page } from '../api/client';
import { casesApi } from '../api/endpoints';
import type { CaseOverview, DashboardDeadlinesOutput, Deadline } from '../api/endpoints';
import { deadlineKind, DeadlineTimeline } from '../components/DeadlineTimeline';
import { Button, EmptyState, ErrorState, LoadingState } from '../ui/primitives';
import { shanghaiToday } from '../ui/time';

const PAGE_SIZE = 50;

export { deadlineKind };

export function DeadlineLabel({ item, today = shanghaiToday() }: {
  item: Deadline; today?: string;
}) {
  const overdue = item.due < today && !item.completed;
  return <span className="deadline-value">
    <time dateTime={item.due} className={overdue ? 'overdue' : undefined}>
      {item.due.replaceAll('-', '.')}
    </time>
    <small>{overdue ? '已逾期 · ' : ''}{deadlineKind(item.kind)}</small>
  </span>;
}

export function CasesPage() {
  const navigate = useNavigate();
  const dialog = useRef<HTMLDialogElement>(null);
  const [page, setPage] = useState<Page<CaseOverview> | null>(null);
  const [offset, setOffset] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState('');
  const [timeline, setTimeline] = useState<DashboardDeadlinesOutput | null>(null);
  const [timelineError, setTimelineError] = useState('');
  const [createError, setCreateError] = useState('');
  const [title, setTitle] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setError('');
    casesApi.dashboard(offset, PAGE_SIZE, controller.signal)
      .then((result) => { if (!controller.signal.aborted) setPage(result); })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setError(describeError(cause)); });
    return () => controller.abort();
  }, [offset, attempt]);

  // 时间轴数据独立于案件表分页：接口返回全部未归档案件的期限，切换页码不重取。
  useEffect(() => {
    const controller = new AbortController();
    setTimelineError('');
    casesApi.deadlines(controller.signal)
      .then((result) => { if (!controller.signal.aborted) setTimeline(result); })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setTimelineError(describeError(cause));
      });
    return () => controller.abort();
  }, [attempt]);

  const today = timeline?.as_of ?? shanghaiToday();

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (busy || !title.trim()) return;
    setCreateError('');
    setBusy(true);
    try {
      const created = await casesApi.create(title.trim());
      navigate(`/cases/${created.case_id}?view=overview`);
    } catch (cause) {
      setCreateError(describeError(cause));
      setBusy(false);
    }
  }

  return <>
    <header className="app-bar"><div className="app-bar-inner">
      <span className="app-brand">法律工作台</span><span className="app-section">诉讼案件</span>
    </div></header>
    <main className="dashboard">
      <div className="page-heading">
        <h1>案件工作表</h1>
        <div className="dashboard-actions">
          <Link className="prototype-entry" to="/prototype/progress">办案进度原型 · 演示数据</Link>
          <Link className="prototype-entry" to="/prototype/graph">主体关系图原型 · 演示数据</Link>
          <Link className="prototype-entry" to="/prototype/claims">诉请金额原型 · 演示数据</Link>
          <Button onClick={() => {
            setTitle(''); setCreateError(''); dialog.current?.showModal();
          }}>＋ 新建案件</Button>
        </div>
      </div>
      {error ? <ErrorState message={error} onRetry={() => setAttempt((value) => value + 1)} /> : null}
      {timelineError
        ? <ErrorState message={timelineError} onRetry={() => setAttempt((value) => value + 1)} />
        : timeline
          ? <DeadlineTimeline items={timeline.items} today={timeline.as_of}
                              caseTotal={timeline.case_total} />
          : <LoadingState label="期限时间轴加载中…" />}
      {page ? <section className="case-register" aria-label="案件列表">
        <div className="table-scroll" tabIndex={0} aria-label="案件工作表">
          <table className="case-table">
            <thead><tr><th scope="col">案件</th><th scope="col">当前节点</th>
              <th scope="col">最近期限</th><th scope="col">下一步工作</th></tr></thead>
            <tbody>{page.items.map((item) => {
              const next = item.deadlines.filter((entry) => !entry.completed)
                .sort((a, b) => a.due.localeCompare(b.due))[0];
              // P4：当前节点优先读进度树的所有进行中阶段（并行程序各一个）；旧 stage 仅作过渡回退。
              const activeStages = item.active_stages?.length ? item.active_stages : [];
              const currentNode = activeStages.length > 0
                ? activeStages.join('、')
                : (item.active_stage ?? item.stage);
              return <tr key={item.case_id}>
                <th scope="row"><Link to={`/cases/${item.case_id}?view=overview`}>{item.title}</Link></th>
                <td>{currentNode || <span className="muted">待登记</span>}</td>
                <td>{next ? <><DeadlineLabel item={next} today={today} /><div>{next.title}</div></>
                  : <span className="muted">{item.deadlines.length ? '无未完成期限' : '待登记'}</span>}</td>
                <td>
                  <Link to={`/cases/${item.case_id}/progress`}>办案进度</Link>
                  {(item.pending_work?.length ?? 0) > 0
                    ? <div className="muted">{item.pending_work!.slice(0, 3).join('、')}{item.pending_work!.length > 3 ? '…' : ''}</div>
                    : item.next_action ? <div className="muted">{item.next_action}</div> : null}
                </td>
              </tr>;
            })}</tbody>
          </table>
        </div>
        {page.items.length === 0 ? <EmptyState title="还没有案件" detail="新建案件后，可以登记办案节点和期限。" /> : null}
        <footer className="table-footer"><span>共 {page.total} 个案件</span>{page.total > PAGE_SIZE ? <div>
          <Button variant="quiet" disabled={offset === 0} onClick={() => setOffset(offset - PAGE_SIZE)}>上一页</Button>
          <Button variant="quiet" disabled={offset + PAGE_SIZE >= page.total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</Button>
        </div> : null}</footer>
      </section> : !error ? <LoadingState /> : null}
    </main>
    <dialog ref={dialog} className="work-dialog" aria-labelledby="new-case-heading"
            onCancel={(event) => { if (busy) event.preventDefault(); }}>
      <form onSubmit={(event) => void create(event)}>
        <h2 id="new-case-heading">新建案件</h2>
        <p className="muted">案件类型 · 诉讼</p>
        <label>案件名称<input autoFocus required value={title} onChange={(event) => setTitle(event.target.value)} /></label>
        {createError ? <ErrorState message={createError} /> : null}
        <div className="work-actions">
          <Button variant="quiet" disabled={busy} onClick={() => dialog.current?.close()}>取消</Button>
          <Button type="submit" disabled={busy || !title.trim()}>{busy ? '正在创建…' : '创建案件'}</Button>
        </div>
      </form>
    </dialog>
  </>;
}
