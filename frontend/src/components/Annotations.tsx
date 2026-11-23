import { useEffect, useState } from 'react';
import type { Page } from '../api/client';
import { describeError } from '../api/client';
import { analysisApi, annotationsApi } from '../api/endpoints';
import type { Annotation } from '../api/endpoints';
import { Button, EmptyState, ErrorState, LoadingState } from '../ui/primitives';
import { SendToGraphDialog } from './SendToGraphDialog';
import { SourcePreview } from './SourcePreview';

const PAGE_SIZE = 30;

export function AnnotationPanel({ caseId, archived = false }: { caseId: string; archived?: boolean }) {
  const [query, setQuery] = useState('');
  const [term, setTerm] = useState('');
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<Page<Annotation> | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setPage(null);
    setError('');
    annotationsApi.list(caseId, term, offset, PAGE_SIZE, undefined, controller.signal)
      .then((result) => { if (!controller.signal.aborted) setPage(result); })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setError(describeError(cause)); });
    return () => controller.abort();
  }, [caseId, term, offset, attempt]);

  const active = page?.items.find((item) => item.annotation_id === selected);
  return <section className="overview" aria-labelledby="annotation-title">
    <h2 id="annotation-title">标注复核</h2>
    <form className="annotation-search" onSubmit={(event) => {
      event.preventDefault(); setTerm(query.trim()); setOffset(0); setSelected(null);
    }}>
      <label>检索标注<input value={query} maxLength={500} onChange={(event) => setQuery(event.target.value)} /></label>
      <Button type="submit">检索</Button>
    </form>
    {error ? <ErrorState message={error} onRetry={() => setAttempt((value) => value + 1)} />
      : page ? <div className="search-columns">
        <div><ul className="annotation-list">{page.items.map((item) => <li key={item.annotation_id}>
          <button type="button" aria-current={item.annotation_id === selected}
            onClick={() => setSelected(item.annotation_id)}>
            <strong>{item.display_name}</strong>
            <span className="muted">{annotationLabel(item)}</span>
            <span className="annotation-excerpt">{item.reviewed_text ?? item.body}</span>
          </button>
        </li>)}</ul>
          {page.items.length === 0 ? <EmptyState title="没有标注" detail="可从材料详情添加人工标注，或由本地 Agent 登记。" /> : null}
          {page.total > PAGE_SIZE ? <div className="work-actions">
            <Button variant="quiet" disabled={offset === 0} onClick={() => setOffset(offset - PAGE_SIZE)}>上一页</Button>
            <Button variant="quiet" disabled={offset + PAGE_SIZE >= page.total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</Button>
          </div> : null}
        </div>
        {active ? <AnnotationDetail key={active.annotation_id} item={active} archived={archived} onReviewed={(updated) => {
          setPage((current) => current && ({ ...current, items: current.items.map((item) =>
            item.annotation_id === updated.annotation_id ? updated : item) }));
        }} /> : null}
      </div> : <LoadingState />}
  </section>;
}

function annotationLabel(item: Annotation) {
  const kind = item.kind === 'transcription' ? '转录' : '内容判断';
  if (item.author === 'human') return `人工标注 · ${kind}`;
  const review = item.reviewed_text === null ? '待人工复核'
    : item.reviewed_text === item.body ? '人工已确认' : '人工已修订';
  return `Agent 标注 · ${kind} · ${review}`;
}

export function AnnotationDetail({ item, archived = false, onReviewed }: {
  item: Annotation;
  archived?: boolean;
  onReviewed: (item: Annotation) => void;
}) {
  const [text, setText] = useState(item.body);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [note, setNote] = useState(false);
  const [savingNote, setSavingNote] = useState(false);
  const [sendOpen, setSendOpen] = useState(false);
  const [sentNotice, setSentNotice] = useState(false);
  async function review(event: React.FormEvent) {
    event.preventDefault();
    if (!text.trim() || busy || savingNote) return;
    setBusy(true); setError('');
    try { onReviewed(await annotationsApi.review(item, text)); }
    catch (cause) { setError(describeError(cause)); }
    finally { setBusy(false); }
  }
  async function saveDraft() {
    if (savingNote || note) return;
    setSavingNote(true);
    setError('');
    try {
      await analysisApi.save(item.case_id, {
        title: `待复核工作笔记 · ${item.display_name}`,
        content: `Agent 标注（待人工复核）\n文件版本 ${item.file_version_id}\n`
          + `标注编号 ${item.annotation_id}${item.page ? ` · 第 ${item.page} 页` : ''}\n\n${item.body}`,
        statement_kind: 'agent_analysis',
        source_refs: [],
      });
      setNote(true);
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setSavingNote(false);
    }
  }
  return <article className="source-card">
    <h3>{item.display_name}</h3>
    <p className="muted">{annotationLabel(item)}</p>
    <div className="work-actions">
      <SourcePreview key={item.annotation_id} caseId={item.case_id} versionId={item.file_version_id}
        name={item.display_name} page={item.page ?? undefined} original={false}
        annotationId={item.annotation_id} archived={archived}
        excerpt={item.reviewed_text ?? item.body} />
      <Button variant="quiet" disabled={archived} onClick={() => setSendOpen(true)}>
        送入关系图
      </Button>
    </div>
    {sentNotice ? <p role="status">已送入关系图</p> : null}
    {sendOpen ? (
      <SendToGraphDialog
        caseId={item.case_id}
        annotationId={item.annotation_id}
        excerpt={item.reviewed_text ?? item.body}
        onClose={() => setSendOpen(false)}
        onCreated={() => setSentNotice(true)}
      />
    ) : null}
    <h4>{item.author === 'agent' ? 'Agent 原标注' : '人工标注'}</h4>
    <p className="source-text">{item.body}</p>
    {item.reviewed_text !== null && item.reviewed_text !== item.body ? <>
      <h4>人工修订</h4><p className="source-text">{item.reviewed_text}</p>
    </> : null}
    {item.author === 'agent' && item.reviewed_text === null ? <form onSubmit={(event) => {
      if (archived) { event.preventDefault(); return; }
      void review(event);
    }}>
      <label>核对原件后的内容<textarea value={text} maxLength={20000} rows={4} disabled={busy || archived}
        onChange={(event) => setText(event.target.value)} /></label>
      <div className="work-actions">
        <Button type="submit" disabled={archived || !text.trim() || busy || savingNote}>
          {busy ? '正在保存…' : text.trim() === item.body ? '确认标注' : '保存人工修订'}
        </Button>
        <Button variant="quiet" disabled={archived || busy || savingNote || note} onClick={() => { if (!archived) void saveDraft(); }}>
          {savingNote ? '正在暂存…' : '暂存为工作笔记'}
        </Button>
      </div>
      {note ? <p role="status">已在「分析」中保存工作笔记。标注仍待复核。</p> : null}
    </form> : null}
    {error ? <ErrorState message={error} /> : null}
  </article>;
}

export function AddAnnotation({ caseId, versionId, pdf, archived = false }: {
  caseId: string; versionId: string; pdf: boolean; archived?: boolean;
}) {
  const [page, setPage] = useState('');
  const [kind, setKind] = useState<Annotation['kind']>('transcription');
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (archived || !body.trim() || busy) return;
    setBusy(true); setMessage(''); setError('');
    try {
      await annotationsApi.create(caseId, { file_version_id: versionId, kind, body, page: page ? Number(page) : null });
      setBody(''); setMessage('已保存人工标注');
    } catch (cause) { setError(describeError(cause)); }
    finally { setBusy(false); }
  }
  return <details className="annotation-add">
    <summary>添加人工标注</summary>
    <form onSubmit={(event) => void save(event)}>
      <label>标注类型<select value={kind} disabled={busy || archived} onChange={(event) => setKind(event.target.value as Annotation['kind'])}>
        <option value="transcription">转录</option><option value="interpretation">内容判断</option>
      </select></label>
      {pdf ? <label>PDF 页码（选填）<input type="number" min="1" max="2147483647" step="1"
        disabled={busy || archived}
        value={page} onChange={(event) => setPage(event.target.value)} /></label> : null}
      <label>标注内容<textarea value={body} maxLength={20000} rows={3} disabled={busy || archived}
        onChange={(event) => setBody(event.target.value)} /></label>
      <Button type="submit" disabled={archived || !body.trim() || busy}>保存人工标注</Button>
      {message ? <p role="status">{message}</p> : null}
      {error ? <ErrorState message={error} /> : null}
    </form>
  </details>;
}
