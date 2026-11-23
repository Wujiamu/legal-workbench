/** Case search: four modes, highlighted hits, jump to source, save as analysis. */
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { describeError } from '../api/client';
import { analysisApi, searchApi } from '../api/endpoints';
import type { AnalysisSummary, AnalysisVersionOutput, ReadSourceOutput, SearchHit,
  SearchOutput }
  from '../api/endpoints';
import { codePointToUtf16 } from '../api/endpoints';
import { Button, EmptyState, ErrorState, Field, LoadingState, inputStyle }
  from '../ui/primitives';
import { formatTimestamp } from '../ui/time';
import { palette, radius, spacing } from '../ui/tokens';
import { coverageLabel } from '../pages/CaseWorkspacePage';
import { SendToGraphDialog } from './SendToGraphDialog';
import { SourcePreview } from './SourcePreview';
import { SaveAnalysisForm } from './SaveAnalysisForm';
import { useSearchParams } from 'react-router';

type Mode = 'literal' | 'identifier' | 'amount' | 'date';
type Scope = 'originals' | 'working' | 'analysis';

const MODE_LABEL: Record<Mode, string> = {
  literal: '字面', identifier: '编号', amount: '金额', date: '日期',
};
const SCOPE_LABEL: Record<Scope, string> = {
  originals: '原件', working: '工作版本', analysis: '分析成果',
};

/** Split context into (before, match, after) using code-point offsets, safely. */
export function splitMatch(hit: SearchHit): { before: string; match: string; after: string } {
  const start = codePointToUtf16(hit.context, hit.match_start);
  const end = codePointToUtf16(hit.context, hit.match_end);
  return {
    before: hit.context.slice(0, start),
    match: hit.context.slice(start, end),
    after: hit.context.slice(end),
  };
}

export function SearchPage({ caseId, onOpenAnalysis, sourceRevision, archived = false }: {
  caseId: string;
  onOpenAnalysis: (analysisId: string, versionId: string) => void;
  sourceRevision?: number;
  archived?: boolean;
}) {
  const searchId = useId();
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<Mode>('literal');
  const [scope, setScope] = useState<Scope>('originals');
  const [currency, setCurrency] = useState('CNY');
  const [result, setResult] = useState<SearchOutput | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<SearchHit | null>(null);
  const controller = useRef<AbortController | null>(null);
  // The chosen source is part of the URL, not only of component state: a reload
  // or a shared link has to reopen the same excerpt, and copying a reference
  // has to produce something the other side can open.
  const [searchParams, setSearchParams] = useSearchParams();
  const deepLinkedRef = searchParams.get('ref');
  const activeRef = deepLinkedRef;

  const run = useCallback(async (offset = 0) => {
    if (!query.trim()) return;
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setBusy(true);
    setError(null);
    try {
      const payload = await searchApi.search(caseId, {
        query: query.trim(), mode, scope,
        currency: mode === 'amount' ? currency : undefined,
        offset, limit: 20,
      }, next.signal);
      if (!next.signal.aborted) setResult(payload);
    } catch (cause) {
      if (!next.signal.aborted) setError(describeError(cause));
    } finally {
      if (!next.signal.aborted) setBusy(false);
    }
  }, [caseId, query, mode, scope, currency]);

  useEffect(() => () => controller.current?.abort(), []);

  return (
    <section>
      <search aria-label="案内检索">
        <form className="search-form" method="post" onSubmit={(event) => {
          event.preventDefault();
          if (!busy) void run();
        }}>
          <div className="search-form-query">
            <Field label="检索词" controlId={`${searchId}-query`}>
              <input id={`${searchId}-query`} name="query" value={query} onChange={(event) => setQuery(event.target.value)}
                     placeholder="例如：合同 / HT-001 / 100 / 2024-01-01" style={inputStyle} />
            </Field>
          </div>
          <div>
            <Field label="模式" controlId={`${searchId}-mode`}>
              <select id={`${searchId}-mode`} name="mode" value={mode} onChange={(event) => setMode(event.target.value as Mode)}
                      style={inputStyle}>
                {(Object.keys(MODE_LABEL) as Mode[]).map((value) => (
                  <option key={value} value={value}>{MODE_LABEL[value]}</option>
                ))}
              </select>
            </Field>
          </div>
          <div>
            <Field label="范围" controlId={`${searchId}-scope`}>
              <select id={`${searchId}-scope`} name="scope" value={scope} onChange={(event) => setScope(event.target.value as Scope)}
                      style={inputStyle}>
                {(Object.keys(SCOPE_LABEL) as Scope[]).map((value) => (
                  <option key={value} value={value}>{SCOPE_LABEL[value]}</option>
                ))}
              </select>
            </Field>
          </div>
          {mode === 'amount' ? (
            <div className="search-form-currency">
              <Field label="币种" controlId={`${searchId}-currency`}>
                <input id={`${searchId}-currency`} name="currency" value={currency} maxLength={3}
                       onChange={(event) => setCurrency(event.target.value.toUpperCase())}
                       style={inputStyle} />
              </Field>
            </div>
          ) : null}
          <div className="search-form-submit">
            <Button type="submit" disabled={busy || !query.trim()}>
              {busy ? '检索中…' : '检索'}
            </Button>
          </div>
        </form>
      </search>

      {error ? <ErrorState message={error} onRetry={() => void run()} /> : null}

      <div className="search-columns">
      {result ? (
        <div style={{ marginTop: spacing.md }}>
          <p style={{ fontSize: 13, color: palette.textMuted, margin: `0 0 ${spacing.sm}px` }}>
            共 {result.total} 条 · 模式 {MODE_LABEL[result.mode_used as Mode] ?? result.mode_used}
            {' · '}索引覆盖 {coverageLabel(result.coverage)}
          </p>
          {result.hits.length === 0 ? (
            <EmptyState title="没有命中" detail="换一个检索词或切换模式试试。" />
          ) : (
            <div style={{ display: 'grid', gap: spacing.sm }}>
              {result.hits.map((hit, position) => (
                <HitRow key={`${hit.source?.source_ref ?? hit.analysis_version_id}-${position}`}
                        hit={hit}
                        selected={selected === hit
                          || (hit.source?.source_ref != null
                              && hit.source.source_ref === activeRef)}
                        onSelect={() => {
                          setSelected(hit);
                          if (hit.source?.source_ref) {
                            setSearchParams({ ref: hit.source.source_ref }, { replace: true });
                            if (window.matchMedia?.('(max-width: 900px)').matches) {
                              window.requestAnimationFrame(() =>
                                document.getElementById('active-source')?.scrollIntoView({ block: 'start' }));
                            }
                          }
                        }}
                        onOpenAnalysis={onOpenAnalysis} />
              ))}
            </div>
          )}
          {result.total > result.limit ? (
            <div style={{ display: 'flex', gap: spacing.sm, marginTop: spacing.md }}>
              <Button variant="quiet" disabled={result.offset === 0}
                      onClick={() => void run(Math.max(0, result.offset - result.limit))}>
                上一页
              </Button>
              <Button variant="quiet" disabled={result.offset + result.limit >= result.total}
                      onClick={() => void run(result.offset + result.limit)}>下一页</Button>
            </div>
          ) : null}
        </div>
      ) : null}

      {activeRef ? (
        <SourcePanel caseId={caseId} sourceRef={activeRef} sourceRevision={sourceRevision}
                     archived={archived} />
      ) : null}
      </div>
    </section>
  );
}

function HitRow({ hit, selected, onSelect, onOpenAnalysis }: {
  hit: SearchHit;
  selected: boolean;
  onSelect: () => void;
  onOpenAnalysis: (analysisId: string, versionId: string) => void;
}) {
  const parts = useMemo(() => splitMatch(hit), [hit]);
  return (
    <div style={{ border: `1px solid ${selected ? palette.accent : palette.border}`,
                  borderRadius: radius.md, padding: spacing.md, background: palette.surface }}>
      <div style={{ fontSize: 12, color: palette.textMuted, marginBottom: spacing.xs }}>
        {/* The label is text, not colour: analysis hits must be unmistakable. */}
        {hit.source_type === 'analysis' ? '分析成果' : SCOPE_LABEL[hit.source_type]}
        {hit.amount ? ` · ${hit.amount} ${hit.currency ?? ''}` : null}
        {hit.source ? ` · ${hit.source.method === 'ocr' ? 'OCR' : hit.source.locator.type === 'pdf' ? '文字层提取' : '原始文本提取'}` : null}
      </div>
      <p style={{ margin: 0, fontSize: 14, lineHeight: 1.7, whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word' }}>
        {parts.before}
        <mark style={{ background: palette.accentSoft, color: palette.text, fontWeight: 600 }}>
          {parts.match}
        </mark>
        {parts.after}
      </p>
      <div style={{ display: 'flex', gap: spacing.sm, marginTop: spacing.sm, flexWrap: 'wrap' }}>
        {hit.source ? (
          <Button variant="quiet" onClick={onSelect}>查看上下文</Button>
        ) : null}
        {hit.analysis_id && hit.analysis_version_id ? (
          <Button variant="quiet"
                  onClick={() => onOpenAnalysis(hit.analysis_id!, hit.analysis_version_id!)}>
            打开分析版本
          </Button>
        ) : null}
      </div>
    </div>
  );
}

export function SourcePanel({ caseId, sourceRef, sourceRevision, archived = false }: {
  caseId: string;
  sourceRef: string | null;
  sourceRevision?: number;
  method?: string;
  fileVersionId?: string;
  archived?: boolean;
}) {
  // Bind the body and actions to the current ref before rendering a response.
  const [copyFeedback, setCopyFeedback] = useState<{ ref: string; message: string } | null>(null);
  const [sendFeedback, setSendFeedback] = useState<{ ref: string; message: string } | null>(null);
  const [sendOpen, setSendOpen] = useState(false);
  const [state, setState] = useState<{
    sourceRef: string | null;
    payload: ReadSourceOutput | null;
    error: string | null;
  }>({ sourceRef: null, payload: null, error: null });

  useEffect(() => {
    if (sourceRef === null) return;
    const controller = new AbortController();
    // Clear immediately: nothing from the previous selection may survive into
    // the new one, not even for a frame.
    setCopyFeedback(null);
    setSendFeedback(null);
    setSendOpen(false);
    setState((previous) => previous.sourceRef === sourceRef
      ? previous : { sourceRef, payload: null, error: null });
    searchApi.readSource(caseId, sourceRef, 200, controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        setState((prev) => (prev.sourceRef === sourceRef
          ? { sourceRef, payload: next, error: null }
          : prev));
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setState((prev) => (prev.sourceRef === sourceRef
          ? { sourceRef, payload: null, error: describeError(cause) }
          : prev));
      });
    return () => controller.abort();
  }, [caseId, sourceRef, sourceRevision]);

  if (sourceRef === null) return null;
  // Anything shown must belong to the current hit.
  const payload = state.sourceRef === sourceRef ? state.payload : null;
  const ready = payload !== null;
  const error = state.sourceRef === sourceRef ? state.error : null;
  const page = payload?.locator.type === 'pdf' && typeof payload.locator.page === 'number'
    ? payload.locator.page : undefined;
  const name = payload?.display_name ?? '来源文件';
  return (
    <section className="source-card" id="active-source">
      <h3>{payload ? name : '来源片段'}</h3>
      <span className="sr-only" data-testid="current-ref">{sourceRef}</span>
      {error ? <ErrorState message={error} /> : null}
      {!ready ? (!error ? <LoadingState /> : null) : (
        <>
          {state.payload!.context_before ? (
            <p style={{ margin: 0, fontSize: 13, color: palette.textMuted, whiteSpace: 'pre-wrap' }}>
              {state.payload!.context_before}
            </p>
          ) : null}
          <p style={{ margin: `${spacing.xs}px 0`, fontSize: 14, lineHeight: 1.8,
                      whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{state.payload!.text}</p>
          {state.payload!.context_after ? (
            <p style={{ margin: 0, fontSize: 13, color: palette.textMuted, whiteSpace: 'pre-wrap' }}>
              {state.payload!.context_after}
            </p>
          ) : null}
          <p className="muted">
            {payload.is_original !== false ? '原件' : '工作版本'}
            {page ? ` · 第 ${page} 页` : ''}
            {' · '}{payload.method === 'ocr' ? 'OCR' : page ? '文字层提取' : '原始文本提取'}
          </p>
          <p className="muted">
            {payload.is_original !== false ? '取得方式' : '对应原件的取得方式'} · {payload.acquisition || '待补充'}
          </p>
          {payload.source_label ? <p className="muted">
            {payload.is_original !== false ? '导入来源' : '对应原件的导入记录'} · {payload.source_label}
            {payload.relative_source ? ` · ${payload.relative_source}` : ''}
          </p> : null}
          <details className="source-metadata">
            <summary>来源与版本详情</summary>
            <p>定位 <span data-testid="current-locator">{JSON.stringify(payload.locator)}</span></p>
            <p>文件版本 {payload.file_version_id}</p>
            <p>来源引用 {sourceRef}</p>
          </details>
          <div className="work-actions">
            <SourcePreview key={sourceRef} caseId={caseId} versionId={payload.file_version_id}
              name={name} page={page} raster={payload.locator.type === 'image'}
              frame={payload.locator.type === 'image' && typeof payload.locator.frame === 'number'
                ? payload.locator.frame : undefined} original={payload.is_original !== false}
              segmentId={sourceRef} archived={archived} excerpt={payload.text} />
          {/* Copy and save only exist once the body for *this* ref is on screen. */}
            <Button variant="quiet" onClick={async () => {
              const url = new URL(window.location.href);
              url.searchParams.set('ref', sourceRef);
              try {
                await navigator.clipboard.writeText(url.toString());
                setCopyFeedback({ ref: sourceRef, message: '已复制来源链接' });
              } catch {
                setCopyFeedback({ ref: sourceRef, message: '无法复制，可从地址栏复制链接。' });
              }
            }}>复制来源链接</Button>
            <Button variant="quiet" disabled={archived} onClick={() => setSendOpen(true)}>
              送入关系图
            </Button>
          </div>
          {copyFeedback?.ref === sourceRef ? <p role="status">{copyFeedback.message}</p> : null}
          {sendFeedback?.ref === sourceRef ? <p role="status">{sendFeedback.message}</p> : null}
          {sendOpen ? (
            <SendToGraphDialog
              caseId={caseId}
              segmentId={sourceRef}
              excerpt={payload.text}
              onClose={() => setSendOpen(false)}
              onCreated={() => setSendFeedback({ ref: sourceRef, message: '已送入关系图' })}
            />
          ) : null}
          <details className="source-metadata">
            <summary>另存为分析</summary>
            <SaveAnalysisForm key={sourceRef} caseId={caseId} sourceRef={sourceRef} archived={archived} />
          </details>
        </>
      )}
    </section>
  );
}

export function AnalysisPanel({ caseId, focus, archived = false, onFocusConsumed }: {
  caseId: string;
  focus: { analysisId: string; versionId: string } | null;
  archived?: boolean;
  onFocusConsumed: () => void;
}) {
  const [items, setItems] = useState<AnalysisSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [content, setContent] = useState<AnalysisVersionOutput | null>(null);

  const load = useCallback((signal?: AbortSignal) => {
    analysisApi.list(caseId, 0, 50, signal)
      .then((page) => setItems(page.items))
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === 'AbortError') return;
        setError(describeError(cause));
      });
  }, [caseId]);

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    if (!focus) return;
    analysisApi.version(caseId, focus.analysisId, focus.versionId)
      .then((payload) => setContent({ ...payload, analysis_id: focus.analysisId,
                                      version_id: focus.versionId }))
      .catch((cause: unknown) => setError(describeError(cause)));
    onFocusConsumed();
  }, [caseId, focus, onFocusConsumed]);

  async function open(analysisId: string, versionId: string) {
    try {
      const payload = await analysisApi.version(caseId, analysisId, versionId);
      setContent({ ...payload, analysis_id: analysisId, version_id: versionId });
    } catch (cause) {
      setError(describeError(cause));
    }
  }

  return (
    <section style={{ marginTop: spacing.lg }}>
      <h3 style={{ fontSize: 13, color: palette.textMuted, margin: `0 0 ${spacing.sm}px` }}>
        分析成果
      </h3>
      {error ? <ErrorState message={error} onRetry={() => load()} /> : null}
      {items === null ? <LoadingState /> : items.length === 0 ? (
        <EmptyState title="还没有分析成果" detail="检索结果可另存为分析，或由 Agent 通过 MCP 写入。" />
      ) : (
        <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
          {items.map((item) => (
            <li key={item.analysis_id} style={{ borderBottom: `1px solid ${palette.border}` }}>
              <button onClick={() => void open(item.analysis_id, item.current_version_id)}
                      style={{ display: 'block', width: '100%', textAlign: 'left', border: 0,
                               background: 'transparent', padding: `${spacing.sm}px 0`,
                               font: 'inherit', cursor: 'pointer', color: palette.text }}>
                <span style={{ fontSize: 14 }}>{item.title}</span>
                <span style={{ fontSize: 12, color: palette.textMuted, marginLeft: spacing.sm }}>
                  审核 {item.review_status === 'approved' ? '已通过'
                    : item.review_status === 'rejected' ? '已驳回' : '待审核'}
                  {' · '}{formatTimestamp(item.created_at)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {content ? (
        <article style={{ marginTop: spacing.md, border: `1px solid ${palette.border}`,
                          borderRadius: radius.md, padding: spacing.md, background: palette.surface }}>
          <h4 style={{ margin: `0 0 ${spacing.xs}px`, fontSize: 15 }}>{content.title}</h4>
          <p style={{ fontSize: 12, color: palette.textMuted, margin: `0 0 ${spacing.sm}px` }}>
            版本 {content.version_id.slice(0, 8)}… · 审核 {content.review_status} ·
            来源引用 {content.source_refs.length} 条
          </p>
          {/* Rendered as text, never as HTML: Markdown is stored, not executed. */}
          <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', font: 'inherit',
                        fontSize: 13, lineHeight: 1.8, margin: 0 }}>{content.content}</pre>
          <div style={{ display: 'flex', gap: spacing.sm, marginTop: spacing.md }}>
            <Button variant="quiet" disabled={archived} onClick={() => void review(content, 'approved')}>
              标记通过
            </Button>
            <Button variant="quiet" disabled={archived} onClick={() => void review(content, 'rejected')}>
              标记驳回
            </Button>
          </div>
        </article>
      ) : null}
    </section>
  );

  async function review(target: typeof content, status: 'approved' | 'rejected') {
    if (!target || archived) return;
    try {
      const revision = items?.find((item) => item.analysis_id === target.analysis_id)?.revision ?? 0;
      await analysisApi.review(caseId, target.analysis_id, target.version_id, revision, status);
      setContent({ ...target, review_status: status });
      load();
    } catch (cause) {
      setError(describeError(cause));
    }
  }
}
