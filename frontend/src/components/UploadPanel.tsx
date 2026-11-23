/** Batch upload from the browser: real relative paths, per-file outcome, retry. */
import { useEffect, useId, useRef, useState } from 'react';
import { describeError } from '../api/client';
import { materialsApi } from '../api/endpoints';
import type { IntakeItem, JobStatusOutput } from '../api/endpoints';
import { Button, Field, StatusText, inputStyle } from '../ui/primitives';
import { palette, radius, spacing } from '../ui/tokens';

const MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024;
// The running batch has to survive a reload or a collapsed panel: keeping it in
// component state alone meant the job kept running with nothing tracking it.
const TRACK_KEY = (caseId: string) => `lwb.intake.${caseId}`;
const ITEM_PAGE = 200;

function remember(caseId: string, intakeId: string, jobId: string) {
  try {
    window.sessionStorage.setItem(TRACK_KEY(caseId), JSON.stringify({ intakeId, jobId }));
  } catch {
    // A blocked storage API must not break uploading.
  }
}

function forget(caseId: string) {
  try {
    window.sessionStorage.removeItem(TRACK_KEY(caseId));
  } catch {
    // ignored
  }
}

function recall(caseId: string): { intakeId: string; jobId: string } | null {
  try {
    const raw = window.sessionStorage.getItem(TRACK_KEY(caseId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { intakeId?: string; jobId?: string };
    return parsed.intakeId && parsed.jobId
      ? { intakeId: parsed.intakeId, jobId: parsed.jobId } : null;
  } catch {
    return null;
  }
}

export interface UploadOutcome {
  intakeId: string;
  jobId: string;
  succeeded: number;
  failed: number;
}

const STATUS_TEXT: Record<IntakeItem['status'], string> = {
  pending: '待处理',
  copying: '复制中',
  published: '已发布',
  ready: '就绪',
  failed: '失败',
  skipped: '已跳过',
};

export function UploadPanel({ caseId, onUploaded }: {
  caseId: string;
  onUploaded: (outcome: UploadOutcome) => void;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const sourceId = useId();
  const [label, setLabel] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [items, setItems] = useState<IntakeItem[] | null>(null);
  const [job, setJob] = useState<JobStatusOutput | null>(null);
  const [intakeId, setIntakeId] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const requestKey = useRef<string>(crypto.randomUUID());

  // Resume tracking after a reload: the job is still running server-side.
  useEffect(() => {
    const saved = recall(caseId);
    if (!saved) return;
    setIntakeId(saved.intakeId);
    setJobId(saved.jobId);
    void refresh(saved.intakeId, saved.jobId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseId]);
  // onUploaded must fire once per batch *when the job reaches a terminal state*,
  // not when the upload POST returns: at that moment the worker has not created
  // any material yet, so the parent's reload would render an empty list and the
  // user would read "upload failed" while the files were fine.
  const notifiedJob = useRef<string | null>(null);

  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
  const overLimit = totalBytes > MAX_UPLOAD_BYTES;

  function addFiles(list: FileList | null) {
    if (!list) return;
    // A new selection is a new batch: a fresh request key is what distinguishes it.
    requestKey.current = crypto.randomUUID();
    setFiles(Array.from(list));
    setItems(null);
    setJob(null);
    setError(null);
  }

  async function upload() {
    if (files.length === 0 || overLimit) return;
    setBusy(true);
    setError(null);
    try {
      const form = new FormData();
      form.set('source_label', label.trim() || '浏览器上传');
      form.set('request_key', requestKey.current);
      for (const file of files) {
        const relative = file.webkitRelativePath || file.name;
        form.append('relative_paths', relative);
        form.append('files', file, relative);
      }
      const created = await materialsApi.upload(caseId, form);
      setIntakeId(created.intake_id);
      setJobId(created.job_id);
      remember(caseId, created.intake_id, created.job_id);
      notifiedJob.current = null;
      await refresh(created.intake_id, created.job_id);
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  async function refresh(nextIntake = intakeId, nextJob = jobId) {
    if (!nextIntake || !nextJob) return;
    try {
      const status = await materialsApi.job(caseId, nextJob);
      // Counts must cover the whole batch.  Reading one page of 200 made a
      // 500-file import report "就绪 200" and hide every later failure.
      const all: IntakeItem[] = [];
      for (let offset = 0; ; offset += ITEM_PAGE) {
        const page = await materialsApi.intakeItems(caseId, nextIntake, offset, ITEM_PAGE);
        all.push(...page.items);
        if (page.items.length < ITEM_PAGE) break;
      }
      setItems(all);
      setJob(status);
      const done = status.status === 'succeeded' || status.status === 'partial'
        || status.status === 'failed';
      if (done && notifiedJob.current !== nextJob) {
        notifiedJob.current = nextJob;
        onUploaded({
          intakeId: nextIntake,
          jobId: nextJob,
          succeeded: all.filter((item) => item.status === 'ready').length,
          failed: all.filter((item) => item.status === 'failed').length,
        });
      }
    } catch (cause) {
      setError(describeError(cause));
    }
  }

  // Poll while the job runs.  Without this the panel showed "已完成（3/3）· 就绪 3"
  // next to an empty material list until the user reloaded the page by hand.
  useEffect(() => {
    if (!intakeId || !jobId) return;
    if (job && (job.status === 'succeeded' || job.status === 'partial'
                || job.status === 'failed')) return;
    const timer = window.setTimeout(() => void refresh(intakeId, jobId), 1500);
    return () => window.clearTimeout(timer);
  }, [intakeId, jobId, job]);

  async function retry() {
    if (!jobId) return;
    setBusy(true);
    try {
      await materialsApi.retry(caseId, jobId);
      // Retry keeps the same job id, so the "already notified" marker has to be
      // cleared and polling restarted: otherwise a successful retry never told
      // the material list to reload.
      notifiedJob.current = null;
      setJob(null);
      await refresh();
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  const ready = items?.filter((item) => item.status === 'ready').length ?? 0;
  const failed = items?.filter((item) => item.status === 'failed').length ?? 0;
  const skipped = items?.filter((item) => item.status === 'skipped').length ?? 0;

  return (
    <section style={{ border: `1px solid ${palette.border}`, borderRadius: radius.md,
                      padding: spacing.lg, marginBottom: spacing.lg, background: palette.surface }}>
      <Field label="来源说明" controlId={sourceId}
             hint="填写本批材料的导入说明。单份材料的取得方式可在材料详情登记。">
        <input id={sourceId} aria-describedby={`${sourceId}-hint`} value={label} onChange={(event) => setLabel(event.target.value)}
               placeholder="例如：委托人提供的合同材料" style={inputStyle} />
      </Field>

      <div style={{ display: 'flex', gap: spacing.sm, alignItems: 'center', flexWrap: 'wrap' }}>
        <input ref={inputRef} type="file" multiple hidden
               onChange={(event) => addFiles(event.target.files)} />
        <Button variant="quiet" onClick={() => inputRef.current?.click()}>选择文件</Button>
        <Button variant="quiet" disabled={busy} onClick={() => void upload()}>
          {busy ? '上传中…' : `上传 ${files.length} 个文件`}
        </Button>
        {files.length > 0 ? (
          <span style={{ fontSize: 12, color: palette.textMuted }}>
            合计 {(totalBytes / 1024 / 1024).toFixed(1)} MB
          </span>
        ) : null}
      </div>

      {overLimit ? (
        <p style={{ marginTop: spacing.sm }}>
          <StatusText tone="error">
            单批总大小上限为 {(MAX_UPLOAD_BYTES / 1024 / 1024 / 1024).toFixed(0)} GB，
            当前为 {(totalBytes / 1024 / 1024 / 1024).toFixed(2)} GB，请分批上传。
          </StatusText>
        </p>
      ) : null}

      {error ? <p style={{ marginTop: spacing.sm }}><StatusText tone="error">{error}</StatusText></p> : null}

      {job ? (
        <p style={{ marginTop: spacing.md, fontSize: 13 }}>
          任务 {job.status === 'succeeded' ? '已完成' : job.status === 'partial' ? '部分完成'
            : job.status === 'failed' ? '失败' : '进行中'}
          （{job.completed_units}/{job.total_units}）· 就绪 {ready} · 失败 {failed} · 跳过 {skipped}
          {failed > 0 ? (
            <span style={{ marginLeft: spacing.sm }}>
              <Button variant="quiet" onClick={() => void retry()}>重试失败项</Button>
            </span>
          ) : null}
          <span style={{ marginLeft: spacing.sm }}>
            <Button variant="quiet" onClick={() => void refresh()}>刷新状态</Button>
          </span>
        </p>
      ) : null}

      {items && items.length > 0 ? (
        <ul style={{ listStyle: 'none', margin: `${spacing.sm}px 0 0`, padding: 0,
                     maxHeight: 220, overflowY: 'auto', fontSize: 13 }}>
          {items.map((item) => (
            <li key={item.intake_item_id} style={{ padding: `${spacing.xs}px 0`,
                                                   borderTop: `1px solid ${palette.border}` }}>
              <span style={{ wordBreak: 'break-all' }}>{item.relative_source}</span>
              {' · '}
              <StatusText tone={item.status === 'ready' ? 'ok'
                : item.status === 'failed' ? 'error'
                : item.status === 'skipped' ? 'muted' : 'warn'}>
                {STATUS_TEXT[item.status]}
              </StatusText>
              {item.error_message ? (
                <span style={{ color: palette.danger }}> — {item.error_message}</span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
