/**
 * 送入关系图弹窗：从原文片段预览、检索详情或标注复核一键创建关系并挂载出处。
 *
 * 后端约束：graph.addRelation 拒绝在尚无出处时直接传入 credibility="evidenced"，
 * 因此选「有证据」时分三步提交：addRelation(suspected) -> addSource -> updateRelation(evidenced)。
 */
import { useEffect, useId, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { describeError } from '../api/client';
import { graphApi, graphAssistApi } from '../api/endpoints';
import type { GraphWorkspaceOut, Schemas, SegmentHintsOut } from '../api/endpoints';
import { guardComposition, useCompositionGuard } from '../graph/GraphDialogs';
import { mapRules } from '../graph/httpExecutor';
import type { Credibility, GraphRules } from '../graph/types';

export interface SendToGraphDialogProps {
  caseId: string;
  segmentId?: string;
  annotationId?: string;
  excerpt?: string;
  onClose: () => void;
  onCreated?: (ws: GraphWorkspaceOut) => void;
}

function extractAmountDefault(hints: SegmentHintsOut | null): { amount: string; currency: string } {
  const match = hints?.fragment.exact_values.find((item) => item.kind === 'amount');
  if (!match) return { amount: '', currency: 'CNY' };
  const rawCanonical = match.canonical_value?.value;
  const fallback = typeof rawCanonical === 'string' || typeof rawCanonical === 'number'
    ? String(rawCanonical)
    : '';
  return {
    amount: match.amount ?? fallback,
    currency: match.currency ?? 'CNY',
  };
}

function extractDateDefault(hints: SegmentHintsOut | null): string {
  const match = hints?.fragment.exact_values.find((item) => item.kind === 'date');
  if (!match) return '';
  const candidate = match.canonical_value?.date ?? match.canonical_value?.iso;
  if (typeof candidate === 'string' && /^\d{4}-\d{2}-\d{2}/.test(candidate)) {
    return candidate.slice(0, 10);
  }
  const isoMatch = /(\d{4})-(\d{1,2})-(\d{1,2})/.exec(match.raw_text);
  if (isoMatch) {
    return `${isoMatch[1]}-${isoMatch[2].padStart(2, '0')}-${isoMatch[3].padStart(2, '0')}`;
  }
  const cnMatch = /(\d{4})年(\d{1,2})月(\d{1,2})日/.exec(match.raw_text);
  if (cnMatch) {
    return `${cnMatch[1]}-${cnMatch[2].padStart(2, '0')}-${cnMatch[3].padStart(2, '0')}`;
  }
  return '';
}

function compactSnippet(text: string | undefined | null): string {
  if (!text) return '';
  return text.trim().replace(/\s+/g, ' ').slice(0, 120);
}

function findCreatedRelationId(
  createdWs: GraphWorkspaceOut,
  previousRelationIds: Set<string>,
  fromEntityId: string,
  toEntityId: string,
  kind: string,
): string | null {
  if (createdWs.last_change_id) {
    const change = createdWs.changes.find((item) => item.id === createdWs.last_change_id);
    const diffEntry = change?.diff.find((entry) => entry.target === 'relation' && entry.after);
    if (diffEntry) return diffEntry.id;
  }
  const added = createdWs.relations.find((item) => item.removed_at === null
    && !previousRelationIds.has(item.id)
    && item.from_entity_id === fromEntityId
    && item.to_entity_id === toEntityId
    && item.kind === kind);
  if (added) return added.id;
  const matched = createdWs.relations.find((item) => item.removed_at === null
    && item.from_entity_id === fromEntityId
    && item.to_entity_id === toEntityId
    && item.kind === kind);
  return matched?.id ?? null;
}

export function SendToGraphDialog({
  caseId, segmentId, annotationId, excerpt, onClose, onCreated,
}: SendToGraphDialogProps) {
  const baseId = useId();
  const titleId = `${baseId}-title`;
  const dialogRef = useRef<HTMLDialogElement>(null);
  const fromSelectRef = useRef<HTMLSelectElement>(null);
  const mountedRef = useRef(true);
  const excerptRef = useRef(excerpt);
  excerptRef.current = excerpt;
  const imeGuard = useCompositionGuard();
  const createdStateRef = useRef<{
    relationId: string;
    fromEntityId: string;
    toEntityId: string;
    kind: string;
    sourceAdded: boolean;
    latestWs: GraphWorkspaceOut;
  } | null>(null);

  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [rules, setRules] = useState<GraphRules | null>(null);
  const [workspace, setWorkspace] = useState<GraphWorkspaceOut | null>(null);
  const [hints, setHints] = useState<SegmentHintsOut | null>(null);

  const [graphId, setGraphId] = useState('');
  const [fromEntityId, setFromEntityId] = useState('');
  const [toEntityId, setToEntityId] = useState('');
  const [kind, setKind] = useState('');
  const [credibility, setCredibility] = useState<Credibility>('evidenced');
  const [label, setLabel] = useState('');
  const [amountYuan, setAmountYuan] = useState('');
  const [currency, setCurrency] = useState('CNY');
  const [dateStart, setDateStart] = useState('');
  const [proves, setProves] = useState(() => compactSnippet(excerpt));
  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState('');

  useEffect(() => {
    mountedRef.current = true;
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    createdStateRef.current = null;
    setLoading(true);
    setLoadError('');
    setSubmitError('');

    const hintsPromise = segmentId
      ? graphAssistApi.segmentHints(caseId, segmentId, signal).catch((cause: unknown) => {
        if (signal.aborted) throw cause;
        return null;
      })
      : Promise.resolve(null);

    Promise.all([
      graphApi.rules(signal),
      graphApi.workspace(caseId, undefined, signal),
      hintsPromise,
    ])
      .then(([rulesRes, wsRes, hintsRes]) => {
        if (signal.aborted) return;
        const parsedRules = mapRules(rulesRes.rules);
        const activeGraphs = [...wsRes.graphs]
          .filter((item) => item.removed_at === null)
          .sort((a, b) => a.position - b.position);
        const activeEntities = wsRes.entities.filter((item) => item.removed_at === null);
        const activeEntityIds = new Set(activeEntities.map((item) => item.id));

        const matchedIds = (hintsRes?.entities ?? [])
          .map((item) => item.entity_id)
          .filter((id) => activeEntityIds.has(id));

        let nextFrom = activeEntities[0]?.id ?? '';
        let nextTo = activeEntities[1]?.id ?? '';
        if (matchedIds.length >= 2) {
          nextFrom = matchedIds[0];
          nextTo = matchedIds[1];
        } else if (matchedIds.length === 1) {
          nextFrom = matchedIds[0];
          nextTo = activeEntities.find((item) => item.id !== matchedIds[0])?.id ?? '';
        }

        const amountDefaults = extractAmountDefault(hintsRes);
        const dateDefault = extractDateDefault(hintsRes);
        const defaultSnippet = compactSnippet(excerptRef.current) || compactSnippet(hintsRes?.fragment.location.text);

        setRules(parsedRules);
        setWorkspace(wsRes);
        setHints(hintsRes);
        setGraphId(activeGraphs[0]?.id ?? '');
        setFromEntityId(nextFrom);
        setToEntityId(nextTo);
        setKind(parsedRules.relation_kinds[0]?.value ?? 'other');
        setCredibility('evidenced');
        setAmountYuan(amountDefaults.amount);
        setCurrency(amountDefaults.currency);
        setDateStart(dateDefault);
        setProves(defaultSnippet);
        requestAnimationFrame(() => fromSelectRef.current?.focus());
      })
      .catch((cause: unknown) => {
        if (signal.aborted) return;
        setLoadError(describeError(cause));
      })
      .finally(() => {
        if (!signal.aborted) setLoading(false);
      });

    return () => controller.abort();
  }, [caseId, segmentId, annotationId]);

  const activeGraphs = workspace
    ? [...workspace.graphs]
      .filter((item) => item.removed_at === null)
      .sort((a, b) => a.position - b.position)
    : [];
  const activeEntities = workspace
    ? workspace.entities.filter((item) => item.removed_at === null)
    : [];
  const archived = Boolean(workspace?.archived);
  const missingSourceRef = !segmentId && !annotationId;
  const sameEntities = Boolean(fromEntityId && toEntityId && fromEntityId === toEntityId);
  const canSubmit = !loading && !busy && !archived && !missingSourceRef
    && activeEntities.length >= 2 && Boolean(fromEntityId) && Boolean(toEntityId) && !sameEntities && Boolean(kind);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit || !workspace) return;
    setBusy(true);
    setSubmitError('');
    try {
      const parsedAmount = amountYuan.trim() ? Number(amountYuan.trim()) : null;
      const amountCents = parsedAmount !== null && Number.isFinite(parsedAmount)
        ? Math.round(parsedAmount * 100)
        : null;
      const attrs: Schemas['RelationAttrs'] = {
        share_pct: null,
        amount_cents: amountCents,
        currency: amountCents !== null ? (currency.trim() || 'CNY') : null,
        date_start: dateStart.trim() || null,
        date_end: null,
      };
      let progress = createdStateRef.current;
      if (
        progress
        && (progress.fromEntityId !== fromEntityId || progress.toEntityId !== toEntityId || progress.kind !== kind)
      ) {
        progress = null;
        createdStateRef.current = null;
      }

      if (!progress) {
        const previousIds = new Set(workspace.relations.map((item) => item.id));
        const initialCredibility: Credibility = credibility === 'evidenced' ? 'suspected' : credibility;
        const createdWs = await graphApi.addRelation(caseId, {
          from_entity_id: fromEntityId,
          to_entity_id: toEntityId,
          kind,
          label: label.trim() || null,
          credibility: initialCredibility,
          directed: true,
          attrs,
          note: null,
          graph_id: graphId || null,
          request_key: null,
        });

        const relationId = findCreatedRelationId(createdWs, previousIds, fromEntityId, toEntityId, kind);
        if (!relationId) {
          throw new Error('未能定位新建的关系记录');
        }
        progress = {
          relationId,
          fromEntityId,
          toEntityId,
          kind,
          sourceAdded: false,
          latestWs: createdWs,
        };
        createdStateRef.current = progress;
      }

      const ref: Schemas['SourceRefInput'] = segmentId
        ? { segment_id: segmentId, material_id: null, annotation_id: null }
        : { segment_id: null, material_id: null, annotation_id: annotationId! };

      if (!progress.sourceAdded) {
        try {
          const sourcedWs = await graphApi.addSource(caseId, {
            entity_id: null,
            relation_id: progress.relationId,
            ref,
            proves: proves.trim() || null,
            request_key: null,
          });
          progress.sourceAdded = true;
          progress.latestWs = sourcedWs;
        } catch (cause) {
          if (!mountedRef.current) return;
          setWorkspace(progress.latestWs);
          setSubmitError(`关系已新建，但挂载出处失败：${describeError(cause)}（可直接点击「送入关系图」续接，无需重复建边）`);
          return;
        }
      }

      let finalWs = progress.latestWs;
      if (credibility === 'evidenced') {
        const currentRel = finalWs.relations.find((item) => item.id === progress!.relationId);
        if (currentRel && currentRel.credibility !== 'evidenced') {
          try {
            finalWs = await graphApi.updateRelation(caseId, progress.relationId, {
              expected_revision: currentRel.revision,
              changes: { credibility: 'evidenced' } as unknown as Schemas['RelationChanges'],
            });
          } catch (cause) {
            if (!mountedRef.current) return;
            setWorkspace(finalWs);
            setSubmitError(`关系与出处已保存，但升级「有证据」状态失败：${describeError(cause)}（可直接点击「送入关系图」重试状态升级）`);
            return;
          }
        }
      }

      if (!mountedRef.current) return;
      createdStateRef.current = null;
      onCreated?.(finalWs);
      onClose();
    } catch (cause) {
      if (!mountedRef.current) return;
      setSubmitError(describeError(cause));
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  }

  return (
    <dialog
      ref={dialogRef}
      className="work-dialog graph-dialog graph-dialog-wide"
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <h2 id={titleId}>送入关系图</h2>
      {loading ? <p role="status" className="muted">正在加载关系图与片段匹配…</p> : null}
      {loadError ? <p role="alert" className="graph-error-inline">错误：{loadError}</p> : null}

      {!loading && !loadError && workspace && rules ? (
        <form method="post" onSubmit={(event) => void handleSubmit(event)} {...imeGuard}>
          {archived ? (
            <p role="status" className="graph-archived">
              本案已归档：关系图处于只读状态，不能新建关系或添加出处。
            </p>
          ) : null}

          {missingSourceRef ? (
            <p role="alert" className="graph-error-inline">缺少可挂载的片段或标注标识。</p>
          ) : null}

          {hints && hints.entities.length > 0 ? (
            <p className="graph-hint" role="status">
              已识别本段主体：{hints.entities.map((entity) => entity.name).join('、')}
            </p>
          ) : null}

          {activeEntities.length < 2 ? (
            <p className="graph-hint warn">
              本案有效主体不足 2 个（当前 {activeEntities.length} 个），请先在「关系图」页面补充主体。
            </p>
          ) : null}

          {activeGraphs.length > 1 ? (
            <label htmlFor={`${baseId}-graph`}>目标关系图
              <select id={`${baseId}-graph`} name="graph_id" value={graphId} disabled={busy || archived}
                onChange={(event) => setGraphId(event.target.value)}>
                {activeGraphs.map((graph) => (
                  <option key={graph.id} value={graph.id}>{graph.title}</option>
                ))}
              </select>
            </label>
          ) : activeGraphs.length === 1 ? (
            <p className="muted graph-hint">目标关系图：{activeGraphs[0].title}</p>
          ) : (
            <p className="muted graph-hint">当前尚无关系图，将直接写入案件关系池。</p>
          )}

          <div className="graph-form-row">
            <label htmlFor={`${baseId}-from`}>起点主体
              <select ref={fromSelectRef} id={`${baseId}-from`} name="from_entity_id" value={fromEntityId}
                autoFocus required disabled={busy || archived || activeEntities.length === 0}
                onChange={(event) => setFromEntityId(event.target.value)}>
                <option value="">请选择主体…</option>
                {activeEntities.map((entity) => (
                  <option key={entity.id} value={entity.id}>{entity.name}</option>
                ))}
              </select>
            </label>
            <label htmlFor={`${baseId}-to`}>终点主体
              <select id={`${baseId}-to`} name="to_entity_id" value={toEntityId}
                required disabled={busy || archived || activeEntities.length === 0}
                onChange={(event) => setToEntityId(event.target.value)}>
                <option value="">请选择主体…</option>
                {activeEntities.map((entity) => (
                  <option key={entity.id} value={entity.id}>{entity.name}</option>
                ))}
              </select>
            </label>
          </div>
          {sameEntities ? (
            <p role="alert" className="graph-hint warn">起点主体与终点主体不能相同。</p>
          ) : null}

          <div className="graph-form-row">
            <label htmlFor={`${baseId}-kind`}>关系类型
              <select id={`${baseId}-kind`} name="kind" value={kind}
                required disabled={busy || archived}
                onChange={(event) => setKind(event.target.value)}>
                {rules.relation_kinds.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
            <label htmlFor={`${baseId}-credibility`}>可信状态
              <select id={`${baseId}-credibility`} name="credibility" value={credibility}
                disabled={busy || archived}
                onChange={(event) => setCredibility(event.target.value as Credibility)}>
                {rules.credibility.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
          </div>

          <div className="graph-form-row">
            <label htmlFor={`${baseId}-label`}>边标签（可选）
              <input id={`${baseId}-label`} name="label" value={label} maxLength={200}
                disabled={busy || archived} placeholder="例如：借款 50 万元"
                onChange={(event) => setLabel(event.target.value)} />
            </label>
            <label htmlFor={`${baseId}-amount`}>金额（元，可选）
              <input id={`${baseId}-amount`} name="amount" type="number" min={0} step="0.01"
                value={amountYuan} disabled={busy || archived}
                onChange={(event) => setAmountYuan(event.target.value)} />
            </label>
            <label htmlFor={`${baseId}-date`}>发生日期（可选）
              <input id={`${baseId}-date`} name="date_start" type="date"
                value={dateStart} disabled={busy || archived}
                onChange={(event) => setDateStart(event.target.value)} />
            </label>
          </div>

          <label htmlFor={`${baseId}-proves`}>证明要点（可选）
            <textarea id={`${baseId}-proves`} name="proves" rows={2} maxLength={500}
              value={proves} disabled={busy || archived} placeholder="简要说明该出处证明的事实"
              onChange={(event) => setProves(event.target.value)} />
          </label>

          {submitError ? <p role="alert" className="graph-error-inline">错误：{submitError}</p> : null}

          <div className="work-actions">
            <button type="button" onClick={onClose} disabled={busy}>取消</button>
            <button type="submit" disabled={!canSubmit}>
              {busy ? '提交中…' : '送入关系图'}
            </button>
          </div>
        </form>
      ) : (
        <div className="work-actions">
          <button type="button" onClick={onClose} disabled={busy}>关闭</button>
        </div>
      )}
    </dialog>
  );
}
