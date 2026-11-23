/** Material detail: classification, rename, preview/extracted text, working copy. */
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { describeError } from '../api/client';
import { materialsApi, searchApi } from '../api/endpoints';
import type { CreateWorkingCopyOutput, GetOriginalOutput, MaterialSummary }
  from '../api/endpoints';
import { Button, EmptyState, ErrorState, Field, LoadingState, StatusText, inputStyle }
  from '../ui/primitives';
import { palette, radius, spacing } from '../ui/tokens';
import { coverageLabel, statusTone } from '../pages/CaseWorkspacePage';
import { AddAnnotation } from './Annotations';

const STATUS_TEXT: Record<MaterialSummary['status'], string> = {
  pending: '待处理', copying: '复制中', published: '已发布',
  ready: '就绪', failed: '失败', skipped: '已跳过',
};

interface ExtractedSegment {
  source_ref: string;
  locator: Record<string, unknown>;
  text: string;
  method: string;
  status: string;
}

export function MaterialDetail({ caseId, material, archived = false, onChanged }: {
  caseId: string;
  material: MaterialSummary;
  archived?: boolean;
  onChanged: () => void;
}) {
  const [category, setCategory] = useState(material.category);
  const fieldId = useId();
  const [acquisition, setAcquisition] = useState(material.acquisition ?? '');
  const [name, setName] = useState(material.display_name);
  const [revision, setRevision] = useState(material.revision);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [original, setOriginal] = useState<GetOriginalOutput | null>(null);
  const [originalLoading, setOriginalLoading] = useState(true);
  const [segments, setSegments] = useState<ExtractedSegment[] | null>(null);
  const [textMode, setTextMode] = useState(false);
  const [workingCopy, setWorkingCopy] = useState<CreateWorkingCopyOutput | null>(null);
  // Responses may land out of order; only the newest request may write state.
  const originalGeneration = useRef(0);
  const textGeneration = useRef(0);
  const copyGeneration = useRef(0);
  const editGeneration = useRef(0);
  // Which material the current detail is about.  Anything that acts on a file
  // version (copy, register, reparse) must compare against this, not just rely on
  // being started from a click: the click happened on the previous material.
  const scopeMaterial = useRef(material.material_id);

  useEffect(() => {
    setCategory(material.category);
    setName(material.display_name);
    setAcquisition(material.acquisition ?? '');
    setRevision(material.revision);
    setNotice(null);
    setError(null);
    setWorkingCopy(null);
    // A request from the previous material may still be in flight and will never
    // clear its own busy flag (its completion is discarded as stale).  Reset here
    // or the new material opens with every button disabled.
    setBusy(false);
    // The previous material's file must never be shown under the new title:
    // its preview, download link, "create working copy" and "reparse" all point
    // at a different file.  Clear first, then fill in.
    setOriginal(null);
    setSegments(null);
    setOriginalLoading(true);
    // Invalidate every request that belongs to the previous material.  Bumping
    // only the original's generation let a late text response from A land under
    // B's title until B's own text arrived.
    const generation = ++originalGeneration.current;
    textGeneration.current += 1;
    copyGeneration.current += 1;
    editGeneration.current += 1;
    scopeMaterial.current = material.material_id;
    const controller = new AbortController();
    materialsApi.original(caseId, material.material_id, controller.signal)
      .then((value) => {
        if (originalGeneration.current === generation) setOriginal(value);
      })
      .catch(() => {
        // An abort also lands here; only the newest request may clear state.
        if (originalGeneration.current === generation) setOriginal(null);
      })
      .finally(() => {
        if (originalGeneration.current === generation) setOriginalLoading(false);
      });
    return () => controller.abort();
  }, [caseId, material.material_id, material.category, material.display_name, material.revision]);

  const loadText = useCallback(async (fileVersionId: string) => {
    const generation = ++textGeneration.current;
    setError(null);
    try {
      const payload = await searchApi.extractedText(caseId, fileVersionId);
      if (textGeneration.current !== generation) return;  // stale version
      setSegments(payload.segments);
      if (payload.segments.length === 0) {
        setNotice('尚无已发布的解析片段，请先运行 worker 完成解析。');
      }
    } catch (cause) {
      if (textGeneration.current !== generation) return;
      setError(describeError(cause));
    }
  }, [caseId]);

  // Re-fetch segments whenever the visible version changes while the text tab is open,
  // so a rename or re-classification never leaves the pane stuck on "loading".
  useEffect(() => {
    if (!textMode || !original) return;
    void loadText(original.file_version_id);
  }, [textMode, original, loadText]);

  async function saveCategory() {
    const generation = ++editGeneration.current;
    const owner = material.material_id;
    const mine = () => editGeneration.current === generation
      && scopeMaterial.current === owner;
    setBusy(true);
    setError(null);
    try {
      const result = await materialsApi.classify(caseId, owner, category, revision);
      // The list belongs to the case, not to this detail: refresh it even when the
      // user already switched away, or going back shows the old name and revision.
      onChanged();
      // The user may have switched materials: this answer belongs to the old one
      // and must not rewrite the revision, notice or busy flag of the new one.
      if (!mine()) return;
      setRevision(result.revision);
      setNotice('分类已保存。');
    } catch (cause) {
      if (!mine()) return;
      setError(describeError(cause));
    } finally {
      if (mine()) setBusy(false);
    }
  }

  async function saveAcquisition() {
    const generation = ++editGeneration.current;
    const owner = material.material_id;
    const mine = () => editGeneration.current === generation && scopeMaterial.current === owner;
    setBusy(true);
    setError(null);
    try {
      const result = await materialsApi.acquisition(caseId, owner, acquisition, revision);
      onChanged();
      if (!mine()) return;
      setRevision(result.revision);
      setNotice('取得方式已保存。');
    } catch (cause) {
      if (mine()) setError(describeError(cause));
    } finally {
      if (mine()) setBusy(false);
    }
  }

  async function saveName() {
    const generation = ++editGeneration.current;
    const owner = material.material_id;
    const mine = () => editGeneration.current === generation
      && scopeMaterial.current === owner;
    setBusy(true);
    setError(null);
    try {
      const result = await materialsApi.rename(caseId, owner, name, revision);
      // Same as classify: the case's list must learn the new name even after a
      // switch, while this detail's own state stays scoped to its material.
      onChanged();
      if (!mine()) return;
      setRevision(result.revision);
      setNotice('显示名已保存。');
    } catch (cause) {
      if (!mine()) return;
      setError(describeError(cause));
    } finally {
      if (mine()) setBusy(false);
    }
  }

  async function makeCopy() {
    if (!original) return;
    const generation = ++copyGeneration.current;
    const owner = material.material_id;
    setBusy(true);
    setError(null);
    try {
      // Name is derived server-side: appending "（工作副本）" after the
      // extension produced ".docx（工作副本）", which the server turned into .bin.
      const created = await materialsApi.createWorkingCopy(caseId, original.file_version_id);
      // The user may have switched materials while this was in flight; the copy
      // belongs to the old one, so it must not enable "register" on the new one.
      if (copyGeneration.current !== generation || scopeMaterial.current !== owner) return;
      setWorkingCopy(created);
      setNotice(`工作副本已创建：${created.relative_path}`);
    } catch (cause) {
      if (copyGeneration.current !== generation || scopeMaterial.current !== owner) return;
      setError(describeError(cause));
    } finally {
      if (copyGeneration.current === generation && scopeMaterial.current === owner) {
        setBusy(false);
      }
    }
  }

  async function registerCopy() {
    if (!workingCopy) return;
    const generation = copyGeneration.current;
    const owner = material.material_id;
    setBusy(true);
    setError(null);
    try {
      const result = await materialsApi.registerVersion(caseId, workingCopy.working_copy_id, 0);
      if (copyGeneration.current !== generation || scopeMaterial.current !== owner) return;
      setNotice(`已登记新版本，解析任务 ${result.job_id.slice(0, 8)}… 已排队。`);
      setWorkingCopy(null);
      onChanged();
    } catch (cause) {
      if (copyGeneration.current !== generation || scopeMaterial.current !== owner) return;
      setError(describeError(cause));
    } finally {
      if (copyGeneration.current === generation && scopeMaterial.current === owner) {
        setBusy(false);
      }
    }
  }

  async function reparse() {
    if (!original) return;
    const generation = originalGeneration.current;
    const owner = material.material_id;
    setBusy(true);
    setError(null);
    try {
      const result = await materialsApi.reparse(caseId, original.file_version_id);
      if (originalGeneration.current !== generation || scopeMaterial.current !== owner) return;
      setNotice(`已排队重新解析（run ${result.parse_run_id.slice(0, 8)}…）。`);
    } catch (cause) {
      if (originalGeneration.current !== generation || scopeMaterial.current !== owner) return;
      setError(describeError(cause));
    } finally {
      if (originalGeneration.current === generation && scopeMaterial.current === owner) {
        setBusy(false);
      }
    }
  }

  return (
    <article>
      <h2 style={{ fontSize: 16, margin: `0 0 ${spacing.xs}px`, wordBreak: 'break-all' }}>
        {material.display_name}
      </h2>
      <p style={{ fontSize: 13, color: palette.textMuted, margin: `0 0 ${spacing.md}px`,
                  wordBreak: 'break-all' }}>
        导入记录 · {material.source_label} / {material.relative_source}
      </p>

      <p style={{ fontSize: 13, margin: `0 0 ${spacing.lg}px` }}>
        <StatusText tone={statusTone(material.status)}>{STATUS_TEXT[material.status]}</StatusText>
        {' · '}{coverageLabel(material.coverage)}
        {' · '}版本修订 {revision}
        {original ? <> {' · '}原件校验 <StatusText
          tone={original.integrity_status === 'ok' ? 'ok' : 'error'}>
          {original.integrity_status === 'ok' ? '通过' : original.integrity_status}
        </StatusText></> : null}
      </p>

      {error ? <ErrorState message={error} /> : null}
      {notice ? <p style={{ marginTop: spacing.sm }}><StatusText tone="ok">{notice}</StatusText></p> : null}

      <div style={{ display: 'grid', gap: spacing.md, maxWidth: 520, marginTop: spacing.md }}>
        <Field label="取得方式" controlId={`${fieldId}-acquisition`}>
          <div className="material-edit">
            <input id={`${fieldId}-acquisition`} value={acquisition} maxLength={500}
              disabled={busy || archived}
              placeholder="待补充，如委托人提供、从微信导出"
              onChange={(event) => setAcquisition(event.target.value)} style={inputStyle} />
            <Button onClick={() => void saveAcquisition()}
              disabled={busy || archived || acquisition.trim() === (material.acquisition ?? '')}>保存取得方式</Button>
          </div>
        </Field>
        <Field label="分类" controlId={`${fieldId}-category`}>
          <div style={{ display: 'flex', gap: spacing.sm }}>
            <input id={`${fieldId}-category`} value={category} disabled={busy || archived}
                   onChange={(event) => setCategory(event.target.value)}
                   style={inputStyle} />
            <Button onClick={() => void saveCategory()} disabled={busy || archived}>保存</Button>
          </div>
        </Field>
        <Field label="显示名" controlId={`${fieldId}-name`}>
          <div style={{ display: 'flex', gap: spacing.sm }}>
            <input id={`${fieldId}-name`} value={name} disabled={busy || archived}
                   onChange={(event) => setName(event.target.value)}
                   style={inputStyle} />
            <Button onClick={() => void saveName()} disabled={busy || archived}>保存</Button>
          </div>
        </Field>
      </div>

      <div style={{ display: 'flex', gap: spacing.sm, flexWrap: 'wrap', marginTop: spacing.md }}>
        <Button variant="quiet" disabled={!original || busy || archived} onClick={() => void makeCopy()}>
          创建工作副本
        </Button>
        <Button variant="quiet" disabled={!workingCopy || busy || archived} onClick={() => void registerCopy()}>
          登记为新版本
        </Button>
        <Button variant="quiet" disabled={!original || busy || archived} onClick={() => void reparse()}>
          重新解析
        </Button>
        {original ? (
          <a href={`/api/v1/cases/${caseId}/file-versions/${original.file_version_id}/download`}
             style={{ alignSelf: 'center', fontSize: 13, color: palette.accent }}>
            下载原件
          </a>
        ) : null}
      </div>

      {workingCopy ? (
        <p style={{ fontSize: 13, marginTop: spacing.md }}>
          工作副本位于 <code>{workingCopy.relative_path}</code>，编辑后点“登记为新版本”。
        </p>
      ) : null}

      {original ? <AddAnnotation key={original.file_version_id} caseId={caseId} versionId={original.file_version_id}
        pdf={/\.pdf$/i.test(material.relative_source)} archived={archived} /> : null}
      <PreviewPane caseId={caseId} original={original} segments={segments}
                   loading={originalLoading} textMode={textMode} onTextMode={setTextMode} />
    </article>
  );
}

function PreviewPane({ caseId, original, segments, loading, textMode, onTextMode }: {
  caseId: string;
  original: { file_version_id: string } | null;
  segments: ExtractedSegment[] | null;
  loading: boolean;
  textMode: boolean;
  onTextMode: (value: boolean) => void;
}) {
  // While the next material's original is in flight, show a loading state rather
  // than "no original" — otherwise a slow response looks like a missing file.
  if (!original) return loading
    ? <LoadingState label="正在读取原件…" />
    : <EmptyState title="尚无原件版本" />;
  const url = `/api/v1/cases/${caseId}/file-versions/${original.file_version_id}`;
  return (
    <section style={{ marginTop: spacing.lg, border: `1px solid ${palette.border}`,
                      borderRadius: radius.md, background: palette.surface }}>
      <div style={{ display: 'flex', gap: spacing.sm, padding: spacing.sm,
                    borderBottom: `1px solid ${palette.border}` }}>
        <Button variant={textMode ? 'quiet' : 'primary'}
                onClick={() => onTextMode(false)}>原件预览</Button>
        <Button variant={textMode ? 'primary' : 'quiet'}
                onClick={() => onTextMode(true)}>提取文本</Button>
      </div>
      {!textMode ? (
        <iframe title="原件预览" src={`${url}/preview`}
                style={{ width: '100%', height: 480, border: 0 }} />
      ) : segments === null ? (
        <LoadingState label="正在读取提取文本…" />
      ) : segments.length === 0 ? (
        <EmptyState title="没有提取文本" detail="该版本可能尚未解析，或解析未识别到文字。" />
      ) : (
        <div style={{ padding: spacing.md, fontSize: 13, lineHeight: 1.8 }}>
          {segments.map((segment) => (
            <div key={segment.source_ref} style={{ marginBottom: spacing.md }}>
              <p style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{segment.text}</p>
              <p style={{ margin: `${spacing.xs}px 0 0`, fontSize: 11, color: palette.textMuted }}>
                定位 {JSON.stringify(segment.locator)} · 方式 {segment.method} · {segment.status}
              </p>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
