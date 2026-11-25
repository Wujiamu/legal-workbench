import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { describeError } from '../api/client';
import type { Page } from '../api/client';
import { casesApi } from '../api/endpoints';
import type {
  CaseDeadlineFilter, CaseListFilters, CaseOverview, CaseSortBy, CaseSortDir,
  CaseSummary, CaseWorkStatus, DashboardDeadlinesOutput, Deadline,
} from '../api/endpoints';
import { AppBrand, ThemeSwitcher } from '../components/BrandMark';
import { deadlineKind, DeadlineTimeline } from '../components/DeadlineTimeline';
import { WORK_STATUS_OPTIONS, workStatusLabel } from '../ui/caseStatus';
import { Button, EmptyState, ErrorState, LoadingState } from '../ui/primitives';
import { shanghaiToday } from '../ui/time';

const PAGE_SIZE = 50;
type CaseRow = CaseSummary & Partial<Pick<
  CaseOverview,
  'stage' | 'next_action' | 'deadlines' | 'active_stage' | 'active_stages' | 'pending_work'
>>;

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

function defaultSortFor(showArchived: boolean): CaseSortBy {
  return showArchived ? 'created_at' : 'next_due';
}

export function CasesPage() {
  const navigate = useNavigate();
  const dialog = useRef<HTMLDialogElement>(null);
  const composing = useRef(false);
  const [page, setPage] = useState<Page<CaseRow> | null>(null);
  const [offset, setOffset] = useState(0);
  const [showArchived, setShowArchived] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState('');
  const [timeline, setTimeline] = useState<DashboardDeadlinesOutput | null>(null);
  const [timelineError, setTimelineError] = useState('');
  const [createError, setCreateError] = useState('');
  const [title, setTitle] = useState('');
  const [caseNumber, setCaseNumber] = useState('');
  const [plaintiff, setPlaintiff] = useState('');
  const [defendant, setDefendant] = useState('');
  const [newStatus, setNewStatus] = useState<CaseWorkStatus>('normal');
  const [busy, setBusy] = useState(false);
  const [updatingCaseId, setUpdatingCaseId] = useState<string | null>(null);

  // 筛选与排序状态
  const [searchDraft, setSearchDraft] = useState('');
  const [queryText, setQueryText] = useState('');
  const [statusFilter, setStatusFilter] = useState<CaseWorkStatus | ''>('');
  const [deadlineFilter, setDeadlineFilter] = useState<CaseDeadlineFilter>('all');
  const [sortBy, setSortBy] = useState<CaseSortBy>('next_due');
  const [sortDir, setSortDir] = useState<CaseSortDir>('asc');

  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (!composing.current) {
        const trimmed = searchDraft.trim();
        if (trimmed !== queryText) {
          setQueryText(trimmed);
          setOffset(0);
        }
      }
    }, 220);
    return () => window.clearTimeout(timer);
  }, [searchDraft, queryText]);

  const activeDefaultSort = defaultSortFor(showArchived);
  const hasFilters = Boolean(
    queryText || statusFilter || (!showArchived && deadlineFilter !== 'all')
    || sortBy !== activeDefaultSort || sortDir !== 'asc',
  );

  useEffect(() => {
    const controller = new AbortController();
    setError('');
    const filters: CaseListFilters | undefined = hasFilters
      ? {
          q: queryText || undefined,
          work_status: statusFilter || undefined,
          deadline_filter: !showArchived && deadlineFilter !== 'all' ? deadlineFilter : undefined,
          sort_by: sortBy,
          sort_dir: sortDir,
        }
      : undefined;
    const request = showArchived
      ? casesApi.list(offset, PAGE_SIZE, controller.signal, true, filters)
      : casesApi.dashboard(offset, PAGE_SIZE, controller.signal, filters);
    request
      .then((result) => { if (!controller.signal.aborted) setPage(result); })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setError(describeError(cause)); });
    return () => controller.abort();
  }, [offset, attempt, showArchived, hasFilters, queryText, statusFilter, deadlineFilter, sortBy, sortDir]);

  // 时间轴数据独立于案件表分页：接口返回全部未归档案件的期限，切换页码/筛选不重取。
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

  function toggleArchiveView() {
    const nextArchived = !showArchived;
    setShowArchived(nextArchived);
    setDeadlineFilter('all');
    setSortBy(defaultSortFor(nextArchived));
    setSortDir('asc');
    setOffset(0);
  }

  function resetFilters() {
    setSearchDraft('');
    setQueryText('');
    setStatusFilter('');
    setDeadlineFilter('all');
    setSortBy(defaultSortFor(showArchived));
    setSortDir('asc');
    setOffset(0);
  }

  function toggleSortColumn(column: CaseSortBy) {
    if (sortBy === column) {
      setSortDir((dir) => (dir === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortBy(column);
      setSortDir('asc');
    }
    setOffset(0);
  }

  function ariaSortOf(column: CaseSortBy): 'ascending' | 'descending' | 'none' {
    if (sortBy !== column) return 'none';
    return sortDir === 'asc' ? 'ascending' : 'descending';
  }

  function sortIndicator(column: CaseSortBy): string {
    if (sortBy !== column) return '↕';
    return sortDir === 'asc' ? '↑' : '↓';
  }

  async function changeRowStatus(item: CaseRow, nextStatus: CaseWorkStatus) {
    if (item.archived_at || updatingCaseId) return;
    setUpdatingCaseId(item.case_id);
    setError('');
    try {
      const updated = await casesApi.updateMetadata(item.case_id, {
        expected_revision: item.revision,
        case_number: item.case_number ?? null,
        plaintiff: item.plaintiff ?? null,
        defendant: item.defendant ?? null,
        work_status: nextStatus,
      });
      setPage((current) => current
        ? {
            ...current,
            items: current.items.map((row) =>
              row.case_id === item.case_id ? { ...row, ...updated } : row),
          }
        : current);
      if (statusFilter !== '' || sortBy === 'work_status') {
        setAttempt((value) => value + 1);
      }
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setUpdatingCaseId(null);
    }
  }

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (busy || !title.trim()) return;
    setCreateError('');
    setBusy(true);
    try {
      const hasMeta = Boolean(
        caseNumber.trim() || plaintiff.trim() || defendant.trim() || newStatus !== 'normal',
      );
      const created = await casesApi.create(
        title.trim(),
        undefined,
        hasMeta
          ? {
              case_number: caseNumber.trim() || null,
              plaintiff: plaintiff.trim() || null,
              defendant: defendant.trim() || null,
              work_status: newStatus,
            }
          : undefined,
      );
      navigate(`/cases/${created.case_id}?view=overview`);
    } catch (cause) {
      setCreateError(describeError(cause));
      setBusy(false);
    }
  }

  const filterActive = Boolean(queryText || statusFilter || (!showArchived && deadlineFilter !== 'all'));

  return <>
    <header className="app-bar"><div className="app-bar-inner">
      <AppBrand /><span className="app-section">诉讼案件</span>
      <ThemeSwitcher />
    </div></header>
    <main className="dashboard">
      <div className="page-heading">
        <h1>案件工作表</h1>
        <div className="dashboard-actions">
          <button
            type="button"
            className="archive-btn"
            aria-pressed={showArchived}
            onClick={toggleArchiveView}
          >
            {showArchived ? '返回在办案件' : '查看已归档'}
          </button>
          <Button onClick={() => {
            setTitle(''); setCaseNumber(''); setPlaintiff(''); setDefendant('');
            setNewStatus('normal'); setCreateError('');
            dialog.current?.showModal();
          }}>＋ 新建案件</Button>
        </div>
      </div>
      {error ? <ErrorState message={error} onRetry={() => setAttempt((value) => value + 1)} /> : null}
      {showArchived ? null : (
        timelineError
          ? <ErrorState message={timelineError} onRetry={() => setAttempt((value) => value + 1)} />
          : timeline
            ? <DeadlineTimeline items={timeline.items} today={timeline.as_of}
                                caseTotal={timeline.case_total} />
            : <LoadingState label="期限时间轴加载中…" />
      )}
      {page ? <section className="case-register" aria-label={showArchived ? '已归档案件列表' : '案件列表'}>
        <div className="case-toolbar" role="search" aria-label="筛选与排序案件">
          <label className="case-filter-search">
            <span className="sr-only">搜索案件</span>
            <input
              type="search"
              placeholder="搜案名 / 案号 / 原被告 / 下一步工作…"
              value={searchDraft}
              onCompositionStart={() => { composing.current = true; }}
              onCompositionEnd={(event) => {
                composing.current = false;
                const val = event.currentTarget.value.trim();
                if (val !== queryText) {
                  setQueryText(val);
                  setOffset(0);
                }
              }}
              onChange={(event) => setSearchDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !composing.current) {
                  const val = searchDraft.trim();
                  if (val !== queryText) {
                    setQueryText(val);
                    setOffset(0);
                  }
                }
              }}
            />
          </label>
          <label className="case-filter-control">
            <span>办理状态</span>
            <select
              aria-label="按办理状态筛选"
              value={statusFilter}
              onChange={(event) => {
                setStatusFilter(event.target.value as CaseWorkStatus | '');
                setOffset(0);
              }}
            >
              <option value="">全部状态</option>
              {WORK_STATUS_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </label>
          {showArchived ? null : (
            <label className="case-filter-control">
              <span>期限</span>
              <select
                aria-label="按期限筛选"
                value={deadlineFilter}
                onChange={(event) => {
                  setDeadlineFilter(event.target.value as CaseDeadlineFilter);
                  setOffset(0);
                }}
              >
                <option value="all">全部期限</option>
                <option value="overdue">仅看逾期</option>
                <option value="due_soon">7天内及逾期</option>
                <option value="has_open">有未完成期限</option>
              </select>
            </label>
          )}
          <label className="case-filter-control">
            <span>排序</span>
            <select
              aria-label="排序方式"
              value={sortBy}
              onChange={(event) => {
                setSortBy(event.target.value as CaseSortBy);
                setOffset(0);
              }}
            >
              {showArchived ? null : <option value="next_due">最近期限</option>}
              <option value="work_status">办理状态</option>
              <option value="case_number">案号</option>
              <option value="title">案件名称</option>
              <option value="created_at">创建时间</option>
            </select>
          </label>
          <button
            type="button"
            className="case-sort-dir"
            aria-label={sortDir === 'asc' ? '当前升序，点击切换为降序' : '当前降序，点击切换为升序'}
            onClick={() => {
              setSortDir((dir) => (dir === 'asc' ? 'desc' : 'asc'));
              setOffset(0);
            }}
          >
            {sortDir === 'asc' ? '升序 ↑' : '降序 ↓'}
          </button>
          {hasFilters ? (
            <button type="button" className="case-filter-reset" onClick={resetFilters}>
              重置
            </button>
          ) : null}
        </div>
        <div className="table-scroll" tabIndex={0} aria-label="案件工作表">
          <table className="case-table">
            <thead>
              <tr>
                <th scope="col" aria-sort={ariaSortOf('title')}>
                  <button type="button" className="th-sort-btn" onClick={() => toggleSortColumn('title')}>
                    案件 / 案号 <span aria-hidden="true">{sortIndicator('title')}</span>
                  </button>
                </th>
                <th scope="col">原告 / 被告</th>
                <th scope="col" aria-sort={ariaSortOf('work_status')}>
                  <button type="button" className="th-sort-btn" onClick={() => toggleSortColumn('work_status')}>
                    办理状态 <span aria-hidden="true">{sortIndicator('work_status')}</span>
                  </button>
                </th>
                <th scope="col">当前节点</th>
                <th scope="col" aria-sort={showArchived ? 'none' : ariaSortOf('next_due')}>
                  {showArchived ? '最近期限' : (
                    <button type="button" className="th-sort-btn" onClick={() => toggleSortColumn('next_due')}>
                      最近期限 <span aria-hidden="true">{sortIndicator('next_due')}</span>
                    </button>
                  )}
                </th>
                <th scope="col">下一步工作</th>
              </tr>
            </thead>
            <tbody>{page.items.map((item) => {
              const deadlinesList = item.deadlines ?? [];
              const next = deadlinesList.filter((entry) => !entry.completed)
                .sort((a, b) => a.due.localeCompare(b.due))[0];
              // P4：当前节点优先读进度树的所有进行中阶段（并行程序各一个）；旧 stage 仅作过渡回退。
              const activeStages = item.active_stages?.length ? item.active_stages : [];
              const currentNode = activeStages.length > 0
                ? activeStages.join('、')
                : (item.active_stage ?? item.stage);
              const rowStatus = item.work_status ?? 'normal';
              const hasParties = Boolean(item.plaintiff || item.defendant);
              return <tr key={item.case_id}>
                <th scope="row">
                  <Link to={`/cases/${item.case_id}?view=overview`}>{item.title}</Link>
                  {item.case_number ? <small className="case-no">{item.case_number}</small> : null}
                  {item.archived_at ? <small>已归档 {item.archived_at.slice(0, 10)}</small> : null}
                </th>
                <td>
                  {hasParties ? (
                    <div className="party-cell">
                      {item.plaintiff ? <div><span className="party-role">原：</span>{item.plaintiff}</div> : null}
                      {item.defendant ? <div><span className="party-role">被：</span>{item.defendant}</div> : null}
                    </div>
                  ) : <span className="muted">待登记</span>}
                </td>
                <td>
                  {item.archived_at ? (
                    <span className={`status-pill status-${rowStatus}`}>{workStatusLabel(rowStatus)}</span>
                  ) : (
                    <select
                      aria-label={`办理状态 ${item.title}`}
                      className={`status-select status-${rowStatus}`}
                      value={rowStatus}
                      disabled={updatingCaseId !== null}
                      onChange={(event) => void changeRowStatus(item, event.target.value as CaseWorkStatus)}
                    >
                      {WORK_STATUS_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  )}
                </td>
                <td>{currentNode || <span className="muted">{item.archived_at ? '已归档' : '待登记'}</span>}</td>
                <td>{next ? <><DeadlineLabel item={next} today={today} /><div>{next.title}</div></>
                  : <span className="muted">{item.archived_at ? '只读' : deadlinesList.length ? '无未完成期限' : '待登记'}</span>}</td>
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
        {page.items.length === 0 ? (
          filterActive
            ? <EmptyState title="没有符合当前筛选条件的案件" detail="可尝试清空关键词，或点上方「重置」查看全部案件。" />
            : showArchived
              ? <EmptyState title="暂无已归档案件" detail="结案归档后的案件会收录在此，可随时进入取消归档。" />
              : <EmptyState title="还没有案件" detail="新建案件后，可以登记办案节点和期限。" />
        ) : null}
        <footer className="table-footer"><span>共 {page.total} 个{showArchived ? '已归档' : ''}案件</span>{page.total > PAGE_SIZE ? <div>
          <Button variant="quiet" disabled={offset === 0} onClick={() => setOffset(offset - PAGE_SIZE)}>上一页</Button>
          <Button variant="quiet" disabled={offset + PAGE_SIZE >= page.total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</Button>
        </div> : null}</footer>
      </section> : !error ? <LoadingState /> : null}
    </main>
    <dialog ref={dialog} className="work-dialog work-dialog-wide" aria-labelledby="new-case-heading"
            onCancel={(event) => { if (busy) event.preventDefault(); }}>
      <form onSubmit={(event) => void create(event)}>
        <h2 id="new-case-heading">新建案件</h2>
        <p className="muted">案件类型 · 诉讼（案号与当事人均选填，后续可在概览修改）</p>
        <label>案件名称<input autoFocus required value={title} onChange={(event) => setTitle(event.target.value)} /></label>
        <div className="case-dialog-grid">
          <label>案号（选填）
            <input maxLength={120} placeholder="如 (2025)沪0115民初12345号"
              value={caseNumber} onChange={(event) => setCaseNumber(event.target.value)} />
          </label>
          <label>办理状态
            <select value={newStatus} onChange={(event) => setNewStatus(event.target.value as CaseWorkStatus)}>
              {WORK_STATUS_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </label>
          <label>原告 / 申请人（选填）
            <input maxLength={200} placeholder="可留空"
              value={plaintiff} onChange={(event) => setPlaintiff(event.target.value)} />
          </label>
          <label>被告 / 被申请人（选填）
            <input maxLength={200} placeholder="可留空"
              value={defendant} onChange={(event) => setDefendant(event.target.value)} />
          </label>
        </div>
        {createError ? <ErrorState message={createError} /> : null}
        <div className="work-actions">
          <Button variant="quiet" disabled={busy} onClick={() => dialog.current?.close()}>取消</Button>
          <Button type="submit" disabled={busy || !title.trim()}>{busy ? '正在创建…' : '创建案件'}</Button>
        </div>
      </form>
    </dialog>
  </>;
}
