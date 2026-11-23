import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent } from 'react';
import { ApiError, describeError } from '../api/client';
import type { Schemas } from '../api/endpoints';
import { Modal } from '../claims/Modal';

export type ChartExportProfile = 'working' | 'client' | 'court';
export type ChartExportFormat = 'svg' | 'png' | 'md' | 'csv';
export type ChartExportIssue = Schemas['ChartIssue'];
export type ApprovedIssue = Schemas['ChartWaiver'];

export interface ChartExportSettings {
  profile: ChartExportProfile;
  snapshotId: string;
  excludedSuspected: string[];
}

export interface ChartExportPreview {
  issues: readonly ChartExportIssue[];
}

interface Props<T extends ChartExportPreview> {
  title: string;
  snapshots?: readonly { id: string; label: string }[];
  initialSnapshotId?: string;
  readOnly: boolean;
  allowWorkingLabels?: boolean;
  onClose: () => void;
  onFreeze?: () => Promise<string>;
  onLocate?: (targetId: string) => void;
  preflight: (settings: ChartExportSettings, signal: AbortSignal) => Promise<T>;
  download: (
    format: ChartExportFormat,
    settings: ChartExportSettings,
    preview: T | null,
    approved: ApprovedIssue[],
    showWorkingLabels: boolean,
    signal?: AbortSignal,
  ) => Promise<void>;
}

const MODES: ReadonlyArray<{ value: ChartExportProfile; label: string }> = [
  { value: 'working', label: '内部工作态' },
  { value: 'client', label: '客户沟通态' },
  { value: 'court', label: '当庭呈递态' },
];

function issueKey(issue: Pick<ChartExportIssue, 'code' | 'target_id'>): string {
  return `${issue.code}:${issue.target_id}`;
}

function formatActionError(failure: unknown): string {
  if (failure instanceof ApiError) return describeError(failure);
  if (failure instanceof DOMException && failure.name === 'AbortError') return '';
  if (failure instanceof Error && failure.message) return failure.message;
  return describeError(failure);
}

export function ChartExportDialog<T extends ChartExportPreview>({
  title,
  snapshots,
  initialSnapshotId = '',
  readOnly,
  allowWorkingLabels = true,
  onClose,
  onFreeze,
  onLocate,
  preflight,
  download,
}: Props<T>) {
  const titleId = useId();
  const [profile, setProfile] = useState<ChartExportProfile>('working');
  const [snapshotId, setSnapshotId] = useState(initialSnapshotId);
  const [excludedSuspected, setExcludedSuspected] = useState<string[]>([]);
  const [excludedLabels, setExcludedLabels] = useState<Record<string, string>>({});
  const [pending, setPending] = useState(false);
  const [showWorkingLabels, setShowWorkingLabels] = useState(false);
  const [reasonByIssue, setReasonByIssue] = useState<Record<string, string>>({});
  const [checked, setChecked] = useState<{ selection: string; preview: T } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const activeRef = useRef(true);
  const downloadAbortRef = useRef<AbortController | null>(null);
  const preflightRef = useRef(preflight);
  preflightRef.current = preflight;
  const composingRef = useRef(false);
  const compositionEndedAtRef = useRef(0);

  useEffect(() => {
    activeRef.current = true;
    return () => {
      activeRef.current = false;
      downloadAbortRef.current?.abort();
    };
  }, []);

  const requiresSnapshot = snapshots !== undefined && profile === 'court' && !snapshotId;
  const selection = `${profile}:${snapshotId}:${[...excludedSuspected].sort().join(',')}`;
  const preview = checked?.selection === selection ? checked.preview : null;

  useEffect(() => {
    setChecked(null);
    setError('');
    if (profile === 'working' || requiresSnapshot) {
      setPending(false);
      return;
    }
    const controller = new AbortController();
    setPending(true);
    void preflightRef.current({ profile, snapshotId, excludedSuspected }, controller.signal).then(
      (result) => {
        if (!controller.signal.aborted) {
          setChecked({ selection, preview: result });
        }
      },
      (failure: unknown) => {
        if (!controller.signal.aborted) {
          const message = describeError(failure);
          if (message) setError(message);
        }
      },
    ).finally(() => {
      if (!controller.signal.aborted) {
        setPending(false);
      }
    });
    return () => controller.abort();
  }, [excludedSuspected, profile, requiresSnapshot, selection, snapshotId]);

  const blockers = preview?.issues.filter((issue) => issue.severity === 'blocker') ?? [];
  const approved: ApprovedIssue[] = blockers.flatMap((issue) => {
    const key = issueKey(issue);
    const reason = reasonByIssue[key]?.trim();
    return issue.can_override && reason ? [{ code: issue.code, target_id: issue.target_id, reason }] : [];
  });
  const canDownload = !busy && !pending && (
    profile === 'working'
    || (preview !== null && blockers.length === approved.length)
  );

  function handleProfileChange(next: ChartExportProfile) {
    setProfile(next);
    setExcludedSuspected([]);
    setExcludedLabels({});
    setReasonByIssue({});
  }

  function handleSnapshotChange(next: string) {
    setSnapshotId(next);
    setExcludedSuspected([]);
    setExcludedLabels({});
    setReasonByIssue({});
  }

  function toggleExcludeSuspected(issue: ChartExportIssue, exclude: boolean) {
    const key = issueKey(issue);
    const missingKey = `missing_source:${issue.target_id}`;
    if (exclude) {
      setExcludedSuspected((current) => (
        current.includes(issue.target_id) ? current : [...current, issue.target_id]
      ));
      setExcludedLabels((current) => ({ ...current, [issue.target_id]: issue.message }));
      setReasonByIssue((current) => {
        if (!(key in current) && !(missingKey in current)) return current;
        const next = { ...current };
        delete next[key];
        delete next[missingKey];
        return next;
      });
    } else {
      restoreSuspected(issue.target_id);
    }
  }

  function restoreSuspected(targetId: string) {
    setExcludedSuspected((current) => current.filter((id) => id !== targetId));
    setExcludedLabels((current) => {
      if (!(targetId in current)) return current;
      const next = { ...current };
      delete next[targetId];
      return next;
    });
  }

  function toggleWaiver(issue: ChartExportIssue, enabled: boolean) {
    const key = issueKey(issue);
    setReasonByIssue((current) => {
      const next = { ...current };
      if (enabled) next[key] = current[key] ?? '';
      else delete next[key];
      return next;
    });
    if (enabled && issue.code === 'suspected_in_court') {
      restoreSuspected(issue.target_id);
    }
  }

  function guardComposition(event: ReactKeyboardEvent<HTMLFormElement>) {
    if (event.key !== 'Enter') return;
    const native = event.nativeEvent as globalThis.KeyboardEvent & { isComposing?: boolean };
    const isIme = composingRef.current
      || Date.now() - compositionEndedAtRef.current < 60
      || Boolean(native.isComposing)
      || native.keyCode === 229
      || event.keyCode === 229;
    if (isIme) {
      event.preventDefault();
    }
  }

  async function freeze() {
    if (!onFreeze || busy || readOnly) return;
    setBusy(true);
    setError('');
    try {
      const newSnapshotId = await onFreeze();
      if (!activeRef.current) return;
      setSnapshotId(newSnapshotId);
      setExcludedSuspected([]);
      setExcludedLabels({});
      setReasonByIssue({});
    } catch (failure) {
      if (activeRef.current) setError(formatActionError(failure));
    } finally {
      if (activeRef.current) setBusy(false);
    }
  }

  async function save(format: ChartExportFormat) {
    if (!canDownload) return;
    downloadAbortRef.current?.abort();
    const controller = new AbortController();
    downloadAbortRef.current = controller;
    setBusy(true);
    setError('');
    try {
      await download(
        format,
        { profile, snapshotId, excludedSuspected },
        preview,
        approved,
        showWorkingLabels,
        controller.signal,
      );
    } catch (failure) {
      if (activeRef.current && !controller.signal.aborted) setError(formatActionError(failure));
    } finally {
      if (downloadAbortRef.current === controller) downloadAbortRef.current = null;
      if (activeRef.current) setBusy(false);
    }
  }

  return (
    <Modal labelledBy={titleId} onClose={() => { if (!busy) onClose(); }} className="work-dialog graph-dialog">
      <form
        onSubmit={(event) => event.preventDefault()}
        onCompositionStart={() => { composingRef.current = true; }}
        onCompositionEnd={() => {
          composingRef.current = false;
          compositionEndedAtRef.current = Date.now();
        }}
        onKeyDown={guardComposition}
      >
        <h2 id={titleId}>导出{title}</h2>
        <label>
          导出模式
          <select
            value={profile}
            disabled={busy}
            onChange={(event) => handleProfileChange(event.target.value as ChartExportProfile)}
          >
            {MODES.map(({ value, label }) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        {profile === 'working' && allowWorkingLabels && (
          <label className="claims-check graph-checkbox">
            <input
              type="checkbox"
              checked={showWorkingLabels}
              disabled={busy}
              onChange={(event) => setShowWorkingLabels(event.target.checked)}
            />
            显示证据编号
          </label>
        )}
        {profile !== 'working' && snapshots !== undefined && (
          <label>
            图表版本
            <select
              value={snapshotId}
              disabled={busy}
              onChange={(event) => handleSnapshotChange(event.target.value)}
            >
              <option value="">{profile === 'court' ? '请选择冻结快照' : '当前图'}</option>
              {snapshots.map(({ id, label }) => <option key={id} value={id}>{label}</option>)}
              {snapshotId && !snapshots.some(({ id }) => id === snapshotId) && (
                <option value={snapshotId}>当前冻结快照</option>
              )}
            </select>
          </label>
        )}
        {requiresSnapshot && (
          <p role="status">当庭呈递态须先选择或冻结关系图快照。</p>
        )}
        {requiresSnapshot && onFreeze && (
          <button type="button" disabled={busy || readOnly} onClick={() => void freeze()}>
            冻结当前图
          </button>
        )}
        {profile !== 'working' && pending && <p role="status">正在核对引用…</p>}
        {profile !== 'working' && preview?.issues.length === 0 && <p role="status">引用预检通过</p>}
        {profile !== 'working' && preview && preview.issues.length > 0 && (
          <ul aria-label="预检问题">
            {preview.issues.map((issue) => {
              const key = issueKey(issue);
              const isSuspectedCourt = profile === 'court' && issue.code === 'suspected_in_court';
              const isExcluded = excludedSuspected.includes(issue.target_id);
              const isWaived = key in reasonByIssue;
              return (
                <li key={key}>
                  <p>
                    <strong>{issue.severity === 'blocker' ? '阻断' : '提示'}</strong> · {issue.message}
                  </p>
                  {issue.target_id && onLocate && (
                    <button type="button" onClick={() => onLocate(issue.target_id)}>定位</button>
                  )}
                  {isSuspectedCourt && (
                    <label className="claims-check graph-checkbox">
                      <input
                        type="checkbox"
                        checked={isExcluded}
                        disabled={busy}
                        onChange={(event) => toggleExcludeSuspected(issue, event.target.checked)}
                      />
                      从当庭导出排除此待查关系
                    </label>
                  )}
                  {issue.severity === 'blocker' && issue.can_override && (
                    <div>
                      <label className="claims-check graph-checkbox">
                        <input
                          type="checkbox"
                          checked={isWaived}
                          disabled={busy}
                          onChange={(event) => toggleWaiver(issue, event.target.checked)}
                        />
                        {isSuspectedCourt ? '确认此项例外（标为待举证主张）' : '确认此项例外'}
                      </label>
                      {isWaived && (
                        <>
                          <textarea
                            aria-label="说明例外原因"
                            value={reasonByIssue[key]}
                            maxLength={500}
                            disabled={busy}
                            onChange={(event) => setReasonByIssue((reasons) => ({
                              ...reasons,
                              [key]: event.target.value,
                            }))}
                          />
                          {!reasonByIssue[key].trim() && (
                            <span className="field-hint">请说明例外原因后方可导出</span>
                          )}
                        </>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {profile === 'court' && excludedSuspected.length > 0 && (
          <section aria-label="已排除待查关系">
            <p className="field-hint">已从当庭导出排除的待查关系（{excludedSuspected.length} 项）</p>
            <ul>
              {excludedSuspected.map((targetId) => (
                <li key={targetId}>
                  <span>{excludedLabels[targetId] ?? '已排除的待查项'}</span>
                  {onLocate && (
                    <button type="button" onClick={() => onLocate(targetId)}>定位</button>
                  )}
                  <button type="button" disabled={busy} onClick={() => restoreSuspected(targetId)}>
                    恢复纳入此待查关系
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
        {error && <p role="alert">{error}</p>}
        <div className="work-actions">
          <button type="button" disabled={busy} onClick={() => { if (!busy) onClose(); }}>关闭</button>
          <button type="button" disabled={!canDownload} onClick={() => void save('svg')}>下载 SVG</button>
          <button type="button" disabled={!canDownload} onClick={() => void save('png')}>下载 PNG</button>
          {profile !== 'working' && (
            <>
              <button type="button" disabled={!canDownload} onClick={() => void save('md')}>
                引用索引 Markdown
              </button>
              <button type="button" disabled={!canDownload} onClick={() => void save('csv')}>
                引用索引 CSV
              </button>
            </>
          )}
        </div>
      </form>
    </Modal>
  );
}
