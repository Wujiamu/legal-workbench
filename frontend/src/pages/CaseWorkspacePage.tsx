/** Case workspace: categories, material list, and a detail pane with tabs. */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Link, useParams } from 'react-router';
import { describeError } from '../api/client';
import { casesApi, materialsApi, searchApi } from '../api/endpoints';
import type { CaseOverview, CorpusCoverageMatrixOut, Coverage, MaterialSummary } from '../api/endpoints';
import { BrandMark, ThemeSwitcher } from '../components/BrandMark';
import { CaseNavBar, isWorkspaceTab } from '../components/CaseNavBar';
import type { WorkspaceTab } from '../components/CaseNavBar';
import { CaseOverviewPanel } from '../components/CaseOverview';
import { AnnotationPanel } from '../components/Annotations';
import { COVERAGE_STATUSES, CoverageMatrix } from '../components/CoverageMatrix';
import type { CoverageMatrixFilter } from '../components/CoverageMatrix';
import { MaterialDetail } from '../components/MaterialDetail';
import { UploadPanel } from '../components/UploadPanel';
import { AnalysisPanel, SearchPage } from '../components/SearchPanel';
import { Button, EmptyState, ErrorState, LoadingState, StatusText } from '../ui/primitives';
import { formatTimestamp } from '../ui/time';
import { palette, radius, spacing, fontStack } from '../ui/tokens';

const PAGE_SIZE = 50;
type Tab = WorkspaceTab;

function resolveTab(params: URLSearchParams): Tab {
  if (params.get('ref')) return 'search';
  const view = params.get('view');
  return isWorkspaceTab(view) ? view : 'material';
}

export function coverageLabel(coverage: Coverage): string {
  const parts: string[] = [];
  if (coverage.complete) parts.push(`完整 ${coverage.complete}`);
  if (coverage.partial) parts.push(`部分 ${coverage.partial}`);
  if (coverage.failed) parts.push(`失败 ${coverage.failed}`);
  if (coverage.pending) parts.push(`待处理 ${coverage.pending}`);
  if (coverage.unsupported) parts.push(`不支持 ${coverage.unsupported}`);
  return parts.length ? parts.join(' · ') : '尚未解析';
}

export function statusTone(status: MaterialSummary['status']): 'ok' | 'warn' | 'error' | 'muted' {
  if (status === 'ready') return 'ok';
  if (status === 'failed') return 'error';
  if (status === 'skipped') return 'muted';
  return 'warn';
}

const STATUS_TEXT: Record<MaterialSummary['status'], string> = {
  pending: '待处理',
  copying: '复制中',
  published: '已发布',
  ready: '就绪',
  failed: '失败',
  skipped: '已跳过',
};

export function CaseWorkspacePage() {
  const { caseId = '' } = useParams();
  return <CaseWorkspace key={caseId} caseId={caseId} />;
}

function CaseWorkspace({ caseId }: { caseId: string }) {
  const [materials, setMaterials] = useState<MaterialSummary[] | null>(null);
  const [total, setTotal] = useState(0);
  const [caseTotal, setCaseTotal] = useState(0);
  const [categories, setCategories] = useState<string[]>([]);
  const [offset, setOffset] = useState(0);
  const [category, setCategory] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string | null>(null);
  const [matrix, setMatrix] = useState<CorpusCoverageMatrixOut | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [index, setIndex] = useState<Coverage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showUpload, setShowUpload] = useState(false);
  // A `?ref=` link points at a source excerpt; opening the page must land on the
  // search tab, otherwise the parameter is honoured only after a manual tab click.
  const [searchParams, setSearchParams] = useSearchParams();
  const [tab, setTab] = useState<Tab>(() => resolveTab(searchParams));
  const [visitedSearch, setVisitedSearch] = useState(tab === 'search');
  useEffect(() => { if (tab === 'search') setVisitedSearch(true); }, [tab]);
  useEffect(() => {
    const view = searchParams.get('view');
    if (!searchParams.get('ref') && isWorkspaceTab(view)) {
      setTab((current) => (current === view ? current : view));
    }
  }, [searchParams]);
  const [overview, setOverview] = useState<CaseOverview | null>(null);
  const [overviewError, setOverviewError] = useState('');
  const [reloadOverview, setReloadOverview] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setOverviewError('');
    casesApi.overview(caseId, controller.signal)
      .then((item) => { if (!controller.signal.aborted) setOverview(item); })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setOverviewError(describeError(cause));
      });
    return () => controller.abort();
  }, [caseId, reloadOverview]);

  const verticalPositions = useRef<Partial<Record<Tab, number>>>({});
  const previousTab = useRef(tab);
  useLayoutEffect(() => {
    if (previousTab.current !== tab) window.scrollTo(0, verticalPositions.current[tab] ?? 0);
    previousTab.current = tab;
  }, [tab]);

  function switchTab(next: Tab) {
    if (next === tab) return;
    verticalPositions.current[tab] = window.scrollY;
    setTab(next);
    setSearchParams((current) => {
      const nextParams = new URLSearchParams(current);
      nextParams.set('view', next);
      if (next !== 'search') nextParams.delete('ref');
      return nextParams;
    }, { replace: true });
  }

  const [focus, setFocus] = useState<{ analysisId: string; versionId: string } | null>(null);

  const [refreshCount, setRefreshCount] = useState(0);
  const refresh = useCallback(() => setRefreshCount((count) => count + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    setError(null);
    materialsApi.list(
      caseId,
      {
        category: category ?? undefined,
        offset: statusFilter ? 0 : offset,
        limit: statusFilter ? 200 : PAGE_SIZE,
      },
      signal,
    )
      .then((page) => {
        if (signal.aborted) return;
        setMaterials(page.items);
        setTotal(page.total);
        // Case-wide values, so the sidebar does not shrink to the current page.
        setCaseTotal(page.case_total);
        setCategories(page.categories);
      })
      .catch((cause: unknown) => {
        if (!signal.aborted) setError(describeError(cause));
      });
    searchApi.indexStatus(caseId, signal)
      .then((status) => { if (!signal.aborted) setIndex(status); })
      .catch(() => { if (!signal.aborted) setIndex(null); });
    return () => controller.abort();
  }, [caseId, category, statusFilter, offset, refreshCount]);

  const displayedMaterials = (() => {
    if (!materials || !statusFilter || !matrix) return materials;
    const ids = category !== null
      ? matrix.cells.find((cell) => cell.category === category && cell.status === statusFilter)?.material_ids ?? []
      : matrix.materials.filter((item) => item.status === statusFilter).map((item) => item.material_id);
    const idSet = new Set(ids);
    return materials.filter((item) => idSet.has(item.material_id));
  })();

  const current = displayedMaterials?.find((item) => item.material_id === selected) ?? null;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const pageIndex = Math.floor(offset / PAGE_SIZE);
  const archived = Boolean(overview?.archived_at);

  useEffect(() => {
    if (archived) setShowUpload(false);
  }, [archived]);

  // Changing the filter resets to the first page; keeping the old offset would
  // land on an empty page whose size no longer matches the new filter.
  const pickCategory = useCallback((next: string | null) => {
    setCategory(next);
    setStatusFilter(null);
    setOffset(0);
    setSelected(null);
  }, []);

  const selectMatrixCell = useCallback((filter: CoverageMatrixFilter) => {
    setCategory(filter.category);
    setStatusFilter(filter.status);
    setOffset(0);
    setSelected(null);
  }, []);

  return (
    <div style={{ minHeight: '100vh', background: palette.background, fontFamily: fontStack,
                  color: palette.text, display: 'flex', flexDirection: 'column' }}>
      <header className="app-bar"><div className="app-bar-inner">
        <BrandMark size={24} />
        <Link to="/" className="back-link">← 案件工作表</Link>
        <h1 title={overview?.title}>{overview?.title ?? '案件工作区'}</h1>
        <ThemeSwitcher />
      </div></header>
      <CaseNavBar
        caseId={caseId}
        active={tab}
        onSwitchWorkspaceTab={switchTab}
        rightSlot={
          <Button variant="quiet" disabled={archived} onClick={() => { if (!archived) setShowUpload((value) => !value); }}>
            {showUpload && !archived ? '收起导入' : '导入材料'}
          </Button>
        }
      />

      {archived ? (
        <p role="status" className="graph-archived" style={{ margin: '10px 16px 0' }}>
          本案已归档：材料、概览、分析与标注只读；查看、检索与导出仍可用。如需修改请前往
          <Link to={`/cases/${caseId}/progress`}>办案进度</Link>
          或
          <Link to={`/cases/${caseId}/graph`}>关系图</Link>
          取消归档。
        </p>
      ) : null}

      {error && tab === 'material' ? <div style={{ padding: spacing.md }}><ErrorState message={error} onRetry={refresh} /></div> : null}

      {showUpload && !archived ? (
        <div style={{ padding: spacing.md, background: palette.surface,
                      borderBottom: `1px solid ${palette.border}` }}>
          <UploadPanel caseId={caseId} onUploaded={refresh} />
        </div>
      ) : null}

      <div className="workspace">
        <aside className="pane-sidebar" hidden={tab !== 'material'}>
          <p style={{ fontSize: 12, color: palette.textMuted, margin: `0 0 ${spacing.sm}px` }}>分类</p>
          <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
            <li>
              <button onClick={() => pickCategory(null)}
                      aria-current={category === null ? 'true' : undefined}
                      style={categoryButtonStyle(category === null)}>全部（{caseTotal}）</button>
            </li>
            {categories.map((name) => (
              <li key={name}>
                <button onClick={() => pickCategory(name)}
                        aria-current={category === name ? 'true' : undefined}
                        style={categoryButtonStyle(category === name)}>{name}</button>
              </li>
            ))}
          </ul>
        </aside>

        <section className="pane-list" hidden={tab !== 'material'}>
          {statusFilter ? (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                          gap: spacing.sm, padding: `${spacing.sm}px ${spacing.lg}px`,
                          borderBottom: `1px solid ${palette.border}`,
                          background: 'var(--warm)', fontSize: 12 }}>
              <span>
                格子筛选：{category ?? '全部分类'} ·{' '}
                {COVERAGE_STATUSES.find((item) => item.key === statusFilter)?.label ?? statusFilter}
              </span>
              <Button variant="quiet" onClick={() => {
                setStatusFilter(null);
                setCategory(null);
                setOffset(0);
              }}>
                清除格子筛选
              </Button>
            </div>
          ) : null}
          {displayedMaterials === null ? <LoadingState /> : displayedMaterials.length === 0 ? (
            <EmptyState
              title={statusFilter ? '该状态下没有匹配材料' : '该分类下没有材料'}
              detail={statusFilter ? '可点上方“清除格子筛选”查看全部材料。' : '点右上角“导入材料”上传文件。'}
            />
          ) : (
            <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
              {displayedMaterials.map((item) => (
                <li key={item.material_id} style={{ borderBottom: `1px solid ${palette.border}` }}>
                  <button onClick={() => { setSelected(item.material_id); switchTab('material'); }}
                          aria-current={selected === item.material_id && tab === 'material'
                            ? 'true' : undefined}
                          style={{ display: 'block', width: '100%', textAlign: 'left', border: 0,
                                   background: selected === item.material_id && tab === 'material'
                                     ? palette.accentSoft : 'transparent',
                                   padding: `${spacing.md}px ${spacing.lg}px`, font: 'inherit',
                                   cursor: 'pointer', color: palette.text }}>
                    <span style={{ display: 'block', fontSize: 14, wordBreak: 'break-all' }}>
                      {item.display_name}
                    </span>
                    <span style={{ display: 'block', fontSize: 12, color: palette.textMuted,
                                   marginTop: spacing.xs }}>
                      <StatusText tone={statusTone(item.status)}>{STATUS_TEXT[item.status]}</StatusText>
                      {' · '}{coverageLabel(item.coverage)}
                    </span>
                    <span style={{ display: 'block', fontSize: 12, color: palette.textMuted,
                                   marginTop: 2 }}>
                      {formatTimestamp(item.created_at)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {/* Real pagination: a case can hold far more than one page. */}
          {statusFilter ? (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                          gap: spacing.sm, padding: `${spacing.sm}px ${spacing.lg}px`,
                          borderTop: `1px solid ${palette.border}`, fontSize: 12,
                          color: palette.textMuted }}>
              <span>筛选命中 {displayedMaterials?.length ?? 0} 份</span>
            </div>
          ) : (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                          gap: spacing.sm, padding: `${spacing.sm}px ${spacing.lg}px`,
                          borderTop: `1px solid ${palette.border}`, fontSize: 12,
                          color: palette.textMuted }}>
              <span>
                {total === 0 ? '0 份' : `${offset + 1}–${Math.min(offset + PAGE_SIZE, total)} / ${total} 份`}
              </span>
              <span style={{ display: 'flex', gap: spacing.xs, alignItems: 'center' }}>
                {/* Functional updates: two clicks in one batch must step twice,
                    not collapse into one because both read the same offset. */}
                <Button variant="quiet" disabled={pageIndex === 0}
                        onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}>上一页</Button>
                <span>第 {pageIndex + 1} / {pageCount} 页</span>
                <Button variant="quiet" disabled={offset + PAGE_SIZE >= total}
                        onClick={() => setOffset((value) => value + PAGE_SIZE)}>下一页</Button>
              </span>
            </div>
          )}
        </section>

        <section className="pane-detail">
          <div hidden={tab !== 'search'}>
            {(tab === 'search' || visitedSearch) ? <SearchPage caseId={caseId} sourceRevision={refreshCount}
              archived={archived}
              onOpenAnalysis={(analysisId, versionId) => {
                setFocus({ analysisId, versionId }); switchTab('analysis');
              }} /> : null}
          </div>
          {tab === 'analysis' ? <AnalysisPanel caseId={caseId} focus={focus} archived={archived} onFocusConsumed={() => setFocus(null)} /> : null}
          {tab === 'overview' ? (overviewError
            ? <ErrorState message={overviewError} onRetry={() => setReloadOverview((value) => value + 1)} />
            : overview ? <CaseOverviewPanel item={overview} archived={archived} onSaved={setOverview}
              onReload={() => setReloadOverview((value) => value + 1)} /> : <LoadingState />) : null}
          {tab === 'annotations' ? <AnnotationPanel caseId={caseId} archived={archived} /> : null}
          {tab === 'material' ? <>
            {index ? <p className="muted workspace-coverage">索引覆盖 · {coverageLabel(index)}</p> : null}
            <CoverageMatrix
              caseId={caseId}
              refreshKey={refreshCount}
              activeFilter={statusFilter || category ? { category, status: statusFilter } : null}
              onSelectCell={selectMatrixCell}
              onMatrixLoaded={setMatrix}
            />
            {current ? <MaterialDetail caseId={caseId} material={current} archived={archived} onChanged={refresh} />
              : <EmptyState title="选择一份材料" detail="左侧列表中选择材料查看详情。" />}
          </> : null}
        </section>
      </div>
    </div>
  );
}

function categoryButtonStyle(active: boolean): React.CSSProperties {
  return {
    display: 'block',
    width: '100%',
    textAlign: 'left',
    border: 0,
    borderRadius: radius.sm,
    background: active ? palette.accentSoft : 'transparent',
    color: palette.text,
    font: 'inherit',
    fontSize: 13,
    padding: `${spacing.sm}px ${spacing.md}px`,
    cursor: 'pointer',
    wordBreak: 'break-all',
  };
}
