/**
 * 右侧详情：选中主体或关系后维护属性、别名与出处，并演示「有证据必须有出处」。
 * 出处行显示现行目录编号（证据X-Y 第N页），点击查看模拟原文（P4 接 SourcePreview）。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { describeError } from '../api/client';
import { graphAssistApi } from '../api/endpoints';
import type { CooccurrenceOut, Schemas } from '../api/endpoints';
import { credibilityRule, entityKindLabel, relationKindLabel, roleLabel } from './rules';
import type {
  CaseEntity, CaseRelation, Credibility, GraphCommandRunner, GraphRules,
  GraphSnapshot, GraphSource, SourceOption, SourceOptionSearch, SourceRef,
} from './types';
import type { Selection } from './GraphCanvas';

function amountText(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return '';
  return `${(cents / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} 元`;
}

function attrsSummary(rules: GraphRules, relation: CaseRelation): string {
  const parts: string[] = [];
  if (relation.attrs.share_pct !== null && relation.attrs.share_pct !== undefined) {
    parts.push(`持股 ${relation.attrs.share_pct}%`);
  }
  if (relation.attrs.amount_cents !== null && relation.attrs.amount_cents !== undefined) {
    parts.push(`金额 ${amountText(relation.attrs.amount_cents)}`);
  }
  if (relation.attrs.date_start || relation.attrs.date_end) {
    parts.push(`${relation.attrs.date_start ?? '…'} 至 ${relation.attrs.date_end ?? '…'}`);
  }
  return parts.length > 0 ? parts.join(' · ') : `关系类型：${relationKindLabel(rules, relation.kind)}`;
}

function refOptions(ws: GraphSnapshot): { value: string; label: string }[] {
  const options: { value: string; label: string }[] = [];
  for (const material of ws.materials) {
    for (const segment of ws.segments.filter((item) => item.material_id === material.id)) {
      options.push({
        value: `segment:${segment.id}`,
        label: `${material.title} · 第${segment.page}页`,
      });
    }
    options.push({ value: `material:${material.id}`, label: `${material.title} · 整份材料` });
  }
  for (const annotation of ws.annotations) {
    const material = ws.materials.find((item) => item.id === annotation.material_id);
    options.push({
      value: `annotation:${annotation.id}`,
      label: `标注：${annotation.label ?? material?.title ?? '材料'} · 第${annotation.page}页`,
    });
  }
  return options;
}

function parseRef(value: string): SourceRef | null {
  const [kind, id] = value.split(':');
  if (!id) return null;
  if (kind === 'segment') return { segment_id: id };
  if (kind === 'material') return { material_id: id };
  if (kind === 'annotation') return { annotation_id: id };
  return null;
}

function SourcesEditor({ ws, rules, target, relation, readOnly, run, onOpenSource, searchSources }: {
  ws: GraphSnapshot;
  rules: GraphRules;
  target: { entity_id?: string; relation_id?: string };
  relation: CaseRelation | null;
  readOnly: boolean;
  run: GraphCommandRunner;
  onOpenSource: (sourceId: string) => void;
  /** 真实案件页：按检索命中补充解析段候选；原型不传。 */
  searchSources?: SourceOptionSearch;
}) {
  const staticOptions = useMemo(() => refOptions(ws), [ws]);
  const [found, setFound] = useState<SourceOption[]>([]);
  const [term, setTerm] = useState('');
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState('');
  const options = useMemo(() => {
    const seen = new Set(staticOptions.map((option) => option.value));
    return [...staticOptions, ...found.filter((option) => !seen.has(option.value))];
  }, [staticOptions, found]);
  const [refValue, setRefValue] = useState('');
  const [proves, setProves] = useState('');
  const [downgrade, setDowngrade] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const sources = ws.sources.filter((source) => source.removed_at === null
    && (target.entity_id ? source.entity_id === target.entity_id
      : target.relation_id ? source.relation_id === target.relation_id : false));
  const lastEvidenced = Boolean(relation && relation.credibility === 'evidenced'
    && sources.length === 1);

  async function addSource(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const ref = parseRef(refValue);
    if (!ref) { setError('请选择材料或片段'); return; }
    setBusy(true);
    try {
      const result = await run({
        type: 'add_source', ...target, ref, proves: proves.trim() || null,
      });
      if (!result.ok) { setError(result.message); return; }
      setError('');
      setRefValue('');
      setProves('');
    } finally {
      setBusy(false);
    }
  }

  async function removeSource(source: GraphSource) {
    if (busy) return;
    setBusy(true);
    try {
      const result = await run({
        type: 'remove_source', source_id: source.id,
        downgrade_to: downgrade ? downgrade as Credibility : undefined,
      });
      setError(result.ok ? '' : result.message);
      if (result.ok) setDowngrade('');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="graph-sources" aria-label="出处">
      <h3>出处 <span className="muted">（{sources.length} 条）</span></h3>
      {sources.length === 0 ? (
        <p className="muted graph-hint">
          尚无出处。{relation ? '「有证据」状态必须先关联至少一条出处。' : ''}
        </p>
      ) : (
        <ul className="graph-source-list">
          {sources.map((source) => {
            const label = ws.labels[source.id];
            return (
              <li key={source.id}>
                <div className="graph-source-main">
                  <strong>{label?.text ?? '（出处已失效）'}</strong>
                  {source.proves ? <span className="graph-source-proves"> · {source.proves}</span> : null}
                </div>
                <div className="graph-source-meta">
                  {label?.material_title || '材料'}{label?.page ? ` 第${label.page}页` : ''}
                  {source.created_by === 'agent' ? ' · Agent 提交' : ' · 人工'}
                </div>
                <div className="graph-source-actions">
                  <button type="button" onClick={() => onOpenSource(source.id)}>查看原文</button>
                  {lastEvidenced ? (
                    <>
                      <select aria-label="降级后的可信状态" value={downgrade}
                        onChange={(event) => setDowngrade(event.target.value)} disabled={readOnly}>
                        <option value="">选择降级状态…</option>
                        {rules.credibility.filter((rule) => rule.value !== 'evidenced').map((rule) => (
                          <option key={rule.value} value={rule.value}>{rule.label}</option>
                        ))}
                      </select>
                      <button type="button" onClick={() => void removeSource(source)} disabled={readOnly || busy}>
                        删除并降级
                      </button>
                    </>
                  ) : (
                    <button type="button" onClick={() => void removeSource(source)} disabled={readOnly || busy}>移除</button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {lastEvidenced ? (
        <p className="graph-hint warn">
          这是最后一条出处：删除时必须同时选择降级状态，系统不会静默降级。
        </p>
      ) : null}
      {error ? <p role="alert" className="graph-error-inline">错误：{error}</p> : null}
      {!readOnly ? (
        <form className="graph-source-form" onSubmit={addSource}>
          {searchSources ? (
            <div className="graph-source-search">
              <label htmlFor={`source-search-${target.entity_id ?? target.relation_id}`}>检索原文片段
                <input id={`source-search-${target.entity_id ?? target.relation_id}`} type="search"
                  value={term} placeholder="输入关键词，例如：股东会决议"
                  onChange={(event) => setTerm(event.target.value)} />
              </label>
              <button type="button" disabled={searching || !term.trim()}
                onClick={async () => {
                  if (!searchSources) return;
                  setSearching(true);
                  setSearchError('');
                  try {
                    setFound(await searchSources(term));
                  } catch (cause) {
                    setSearchError(cause instanceof Error ? cause.message : '检索失败');
                  } finally {
                    setSearching(false);
                  }
                }}>{searching ? '检索中…' : '检索'}</button>
              {searchError ? <p role="alert" className="graph-error-inline">检索错误：{searchError}</p> : null}
              {found.length > 0 ? (
                <p className="muted graph-hint">已补充 {found.length} 条解析段候选，可在下方选择。</p>
              ) : null}
            </div>
          ) : null}
          <label htmlFor={`source-ref-${target.entity_id ?? target.relation_id}`}>关联材料或片段
            <select id={`source-ref-${target.entity_id ?? target.relation_id}`} value={refValue}
              onChange={(event) => setRefValue(event.target.value)}>
              <option value="">选择…</option>
              {options.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label>证明要点（可选）
            <input value={proves} maxLength={500} placeholder="例如：证明持股 60%"
              onChange={(event) => setProves(event.target.value)} />
          </label>
          <button type="submit" disabled={busy || !refValue}>添加出处</button>
        </form>
      ) : null}
    </section>
  );
}

function firstExactAmount(values: Schemas['AssistExactValue'][]): string | undefined {
  const match = values.find((item) => item.kind === 'amount');
  if (!match) return undefined;
  if (match.amount) return match.amount;
  const val = match.canonical_value?.value;
  return typeof val === 'string' || typeof val === 'number' ? String(val) : undefined;
}

function firstExactDate(values: Schemas['AssistExactValue'][]): string | undefined {
  const match = values.find((item) => item.kind === 'date');
  if (!match) return undefined;
  const dateVal = match.canonical_value?.date ?? match.canonical_value?.iso;
  if (typeof dateVal === 'string' && dateVal.trim()) return dateVal.trim();
  return match.raw_text || undefined;
}

function fragmentPage(fragment: Schemas['AssistSegment']): number | undefined {
  const locPage = fragment.location.locator?.page;
  if (typeof locPage === 'number') return locPage;
  return fragment.citation.page ?? undefined;
}

function CooccurrenceSection({
  caseId,
  entityAId,
  entityBId,
  entityAName,
  entityBName,
  readOnly,
  activeRelation,
  busy,
  onAttachSource,
  onOpenSource,
  onCreateRelation,
}: {
  caseId: string;
  entityAId: string;
  entityBId: string;
  entityAName: string;
  entityBName: string;
  readOnly: boolean;
  activeRelation: CaseRelation | null;
  busy: boolean;
  onAttachSource: (segmentId: string, proves: string | null) => Promise<boolean>;
  onOpenSource?: (info: { fileVersionId: string; title: string; page?: number; segmentId?: string }) => void;
  onCreateRelation?: (prefill: {
    fromEntityId: string;
    toEntityId: string;
    segmentId: string;
    amount?: string;
    date?: string;
  }) => void;
}) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<CooccurrenceOut | null>(null);
  const [attachNotice, setAttachNotice] = useState('');
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => () => {
    controllerRef.current?.abort();
  }, []);

  async function lookup() {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setOpen(true);
    setLoading(true);
    setError('');
    setAttachNotice('');
    try {
      const payload = await graphAssistApi.cooccurrence(
        caseId,
        { entity_a_id: entityAId, entity_b_id: entityBId, scope: 'originals' },
        controller.signal,
      );
      if (!controller.signal.aborted) {
        setResult(payload);
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(describeError(cause));
      }
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
        setLoading(false);
      }
    }
  }

  return (
    <section className="graph-sources" aria-label="全案共现秒查" style={{ marginTop: 12 }}>
      <div className="graph-form-actions" style={{ justifyContent: 'space-between' }}>
        <button type="button" onClick={() => void lookup()} disabled={loading}>
          {loading ? '检索共现中…' : '全案共现秒查'}
        </button>
        {open ? (
          <button
            type="button"
            onClick={() => {
              controllerRef.current?.abort();
              controllerRef.current = null;
              setLoading(false);
              setOpen(false);
            }}
          >
            收起
          </button>
        ) : null}
      </div>
      <p className="graph-hint">
        在原件中检索「{entityAName}」与「{entityBName}」同段或同页/同表共现片段。
      </p>
      {attachNotice ? <p role="status" className="graph-notice-inline">{attachNotice}</p> : null}
      {error ? <p role="alert" className="graph-error-inline">错误：{error}</p> : null}
      {open && result ? (
        <div style={{ display: 'grid', gap: 8, marginTop: 8 }}>
          <p className="graph-hint" style={{ margin: 0 }}>
            {entityAName} 词条：{result.entity_a_terms.join('、') || '（无）'}
            {' · '}
            {entityBName} 词条：{result.entity_b_terms.join('、') || '（无）'}
          </p>
          {result.hits.length === 0 ? (
            <p className="muted graph-hint">原件中未检索到两个主体的共现片段。</p>
          ) : (
            <ul className="graph-source-list">
              {result.hits.map((hit, hitIndex) => {
                const hitKindLabel = hit.kind === 'segment' ? '同段命中' : '同页/同表命中';
                return (
                  <li key={`${hit.kind}-${hit.fragments.map((f) => f.location.source_ref).join('|')}-${hitIndex}`}>
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center', marginBottom: 4 }}>
                      <span className={`graph-badge ${hit.kind === 'segment' ? 'ok' : 'pending'}`} style={{ marginLeft: 0 }}>
                        {hitKindLabel}
                      </span>
                      {hit.kind !== 'segment' ? (
                        <span className="muted" style={{ fontSize: 11 }}>
                          {hit.kind === 'page' ? '同页' : '同表'}
                        </span>
                      ) : null}
                    </div>
                    {hit.fragments.map((fragment) => {
                      const page = fragmentPage(fragment);
                      const snippet = fragment.location.text.trim().replace(/\s+/g, ' ').slice(0, 120);
                      const amount = firstExactAmount(fragment.exact_values);
                      const date = firstExactDate(fragment.exact_values);
                      return (
                        <div key={fragment.location.source_ref} style={{ marginTop: 4, paddingTop: 4, borderTop: '1px dashed var(--line)' }}>
                          <div className="graph-source-meta">
                            {fragment.citation.text || fragment.citation.material_title || '原件材料'}
                            {page ? ` · 第 ${page} 页` : ''}
                          </div>
                          <div className="graph-source-main" style={{ margin: '3px 0' }}>
                            {fragment.location.text}
                          </div>
                          {fragment.exact_values.length > 0 ? (
                            <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', margin: '4px 0' }}>
                              {fragment.exact_values.map((ev, evIndex) => (
                                <span
                                  key={`${fragment.location.source_ref}-${ev.kind}-${evIndex}`}
                                  className="graph-role"
                                >
                                  {ev.kind === 'amount' ? `金额：${ev.raw_text}` : `日期：${ev.raw_text}`}
                                </span>
                              ))}
                            </div>
                          ) : null}
                          <div className="graph-source-actions">
                            <button
                              type="button"
                              onClick={() => onOpenSource?.({
                                fileVersionId: fragment.location.file_version_id,
                                title: fragment.citation.material_title || '原件材料',
                                page,
                                segmentId: fragment.location.source_ref,
                              })}
                            >
                              查看原件
                            </button>
                            {!readOnly ? (
                              activeRelation ? (
                                <button
                                  type="button"
                                  disabled={busy}
                                  onClick={async () => {
                                    const ok = await onAttachSource(fragment.location.source_ref, snippet || null);
                                    if (ok) setAttachNotice('已挂载为关系出处');
                                  }}
                                >
                                  挂为关系出处
                                </button>
                              ) : (
                                <button
                                  type="button"
                                  onClick={() => onCreateRelation?.({
                                    fromEntityId: entityAId,
                                    toEntityId: entityBId,
                                    segmentId: fragment.location.source_ref,
                                    amount,
                                    date,
                                  })}
                                >
                                  以本段建关系
                                </button>
                              )
                            ) : null}
                          </div>
                        </div>
                      );
                    })}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      ) : null}
    </section>
  );
}

export function InspectorPanel({
  ws, rules, graphId, selection, readOnly, run, onSelect, onOpenSource,
  searchSources, onEditEntity, onEditRelation, onDeleteEntity, onDeleteRelation,
  caseId, pathFrom, pathTo, onOpenCooccurrenceSource, onCreateRelationFromCooccurrence,
}: {
  ws: GraphSnapshot;
  rules: GraphRules;
  graphId: string;
  selection: Selection;
  readOnly: boolean;
  run: GraphCommandRunner;
  onSelect: (selection: Selection) => void;
  onOpenSource: (sourceId: string) => void;
  searchSources?: SourceOptionSearch;
  onEditEntity: (entity: CaseEntity) => void;
  onEditRelation: (relation: CaseRelation) => void;
  onDeleteEntity: (entity: CaseEntity) => void;
  onDeleteRelation: (relation: CaseRelation) => void;
  caseId?: string;
  pathFrom?: string;
  pathTo?: string;
  onOpenCooccurrenceSource?: (info: {
    fileVersionId: string;
    title: string;
    page?: number;
    segmentId?: string;
  }) => void;
  onCreateRelationFromCooccurrence?: (prefill: {
    fromEntityId: string;
    toEntityId: string;
    segmentId: string;
    amount?: string;
    date?: string;
  }) => void;
}) {
  const [alias, setAlias] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const entity = selection?.type === 'entity'
    ? ws.entities.find((item) => item.id === selection.id && item.removed_at === null) ?? null
    : null;
  const relation = selection?.type === 'relation'
    ? ws.relations.find((item) => item.id === selection.id && item.removed_at === null) ?? null
    : null;

  /** 等待命令真正完成再更新错误提示；提交期间禁用本面板的写控件。 */
  async function exec(command: Parameters<GraphCommandRunner>[0]) {
    if (busy) return null;
    setBusy(true);
    try {
      const result = await run(command);
      setError(result.ok ? '' : result.message);
      return result;
    } finally {
      setBusy(false);
    }
  }

  if (!entity && !relation) {
    const canPairCooccur = Boolean(caseId && pathFrom && pathTo && pathFrom !== pathTo);
    const pairAName = ws.entities.find((item) => item.id === pathFrom)?.name ?? '起点主体';
    const pairBName = ws.entities.find((item) => item.id === pathTo)?.name ?? '终点主体';
    return (
      <section className="graph-inspector" aria-label="主体或关系详情">
        <h2>详情</h2>
        <p className="muted">在画布上选择主体或关系，查看并维护属性、别名与出处。</p>
        <p className="graph-hint">提示：关系线颜色与线型表示可信状态；红色粗虚线是待查。</p>
        {canPairCooccur ? (
          <CooccurrenceSection
            key={`${caseId}:${pathFrom}:${pathTo}`}
            caseId={caseId!}
            entityAId={pathFrom!}
            entityBId={pathTo!}
            entityAName={pairAName}
            entityBName={pairBName}
            readOnly={readOnly}
            activeRelation={null}
            busy={busy}
            onAttachSource={async () => false}
            onOpenSource={onOpenCooccurrenceSource}
            onCreateRelation={onCreateRelationFromCooccurrence}
          />
        ) : null}
      </section>
    );
  }

  const pending = ws.pending_changes.some((change) => change.review_state === 'pending'
    && change.diff.some((entry) => (entity ? entry.target === 'entity' && entry.id === entity.id
      : entry.target === 'relation' && entry.id === relation!.id) && entry.after));

  if (entity) {
    const node = ws.nodes.find((item) => item.graph_id === graphId && item.entity_id === entity.id);
    const aliases = ws.aliases.filter((item) => item.entity_id === entity.id && item.removed_at === null);
    const groups = ws.groups.filter((group) => group.graph_id === graphId && group.removed_at === null);
    return (
      <section className="graph-inspector" aria-label="主体详情">
        <header className="graph-inspector-head">
          <div>
            <p className="graph-inspector-kind">主体 · {entityKindLabel(rules, entity.kind)}</p>
            <h2>{entity.name}</h2>
          </div>
          {pending ? <span className="agent-badge agent-pending">Agent 待确认</span> : null}
        </header>
        <p className="graph-inspector-roles">
          {entity.roles.length > 0
            ? entity.roles.map((role) => <span key={role} className="graph-role">{roleLabel(rules, role)}</span>)
            : <span className="muted">未标注诉讼角色</span>}
        </p>
        {entity.note ? <p className="graph-inspector-note">{entity.note}</p> : null}
        {node ? (
          <label className="graph-field">本图分组
            <select value={node.group_id ?? ''} disabled={readOnly || busy}
              onChange={(event) => {
                void exec({
                  type: 'set_node_group', graph_id: graphId, entity_id: entity.id,
                  group_id: event.target.value || null,
                });
              }}>
              <option value="">未分组</option>
              {groups.map((group) => <option key={group.id} value={group.id}>{group.title}</option>)}
            </select>
          </label>
        ) : null}

        <section className="graph-aliases" aria-label="别名">
          <h3>别名 <span className="muted">（{aliases.length}）</span></h3>
          {aliases.length === 0 ? <p className="muted graph-hint">暂无别名。</p> : (
            <ul>
              {aliases.map((item) => (
                <li key={item.id}>
                  <span>{item.alias}</span>
                  <button type="button" disabled={readOnly || busy}
                    aria-label={`移除别名 ${item.alias}`}
                    onClick={() => { void exec({ type: 'remove_alias', alias_id: item.id }); }}>移除</button>
                </li>
              ))}
            </ul>
          )}
          {!readOnly ? (
            <form className="graph-inline-form" onSubmit={async (event) => {
              event.preventDefault();
              const result = await exec({ type: 'add_alias', entity_id: entity.id, alias });
              if (result?.ok) setAlias('');
            }}>
              <label htmlFor="alias-input">新增别名
                <input id="alias-input" value={alias} maxLength={200}
                  onChange={(event) => setAlias(event.target.value)} />
              </label>
              <button type="submit" disabled={busy || !alias.trim()}>添加</button>
            </form>
          ) : null}
        </section>

        <SourcesEditor ws={ws} rules={rules} target={{ entity_id: entity.id }} relation={null}
          readOnly={readOnly} run={run} onOpenSource={onOpenSource} searchSources={searchSources} />
        {error ? <p role="alert" className="graph-error-inline">错误：{error}</p> : null}
        <div className="graph-inspector-actions">
          <button type="button" onClick={() => onEditEntity(entity)} disabled={readOnly}>编辑资料</button>
          <button type="button" disabled={readOnly || busy || !node}
            onClick={() => {
              void exec({ type: 'remove_from_graph', graph_id: graphId, entity_id: entity.id })
                .then((result) => { if (result?.ok) onSelect(null); });
            }}>从本图移除</button>
          <button type="button" className="danger" disabled={readOnly}
            onClick={() => onDeleteEntity(entity)}>全案删除…</button>
        </div>
        <p className="graph-hint">「从本图移除」只影响本图成员；「全案删除」会软删除案件级主体并列出受影响的图。</p>
      </section>
    );
  }

  const activeRelation = relation!;
  const rule = credibilityRule(rules, activeRelation.credibility);
  const from = ws.entities.find((item) => item.id === activeRelation.from_entity_id);
  const to = ws.entities.find((item) => item.id === activeRelation.to_entity_id);
  const sources = ws.sources.filter((source) => source.removed_at === null
    && source.relation_id === activeRelation.id);
  return (
    <section className="graph-inspector" aria-label="关系详情">
      <header className="graph-inspector-head">
        <div>
          <p className="graph-inspector-kind">关系 · {relationKindLabel(rules, activeRelation.kind)}</p>
          <h2>{from?.name ?? '?'} → {to?.name ?? '?'}</h2>
        </div>
        {pending ? <span className="agent-badge agent-pending">Agent 待确认</span> : null}
      </header>
      <p className={`graph-credibility cred-${activeRelation.credibility}`}>
        <span className="graph-legend-line" aria-hidden="true"
          style={{ borderTopColor: rule.stroke, borderTopStyle: rule.dash ? 'dashed' : 'solid',
            borderTopWidth: rule.width }} />
        {rule.label}
        <span className="muted">（{rule.hint}）</span>
      </p>
      {activeRelation.label ? <p className="graph-inspector-note">{activeRelation.label}</p> : null}
      <p className="graph-inspector-note">{attrsSummary(rules, activeRelation)}
        {activeRelation.directed ? ' · 有向' : ' · 无向'}</p>
      {activeRelation.note ? <p className="graph-inspector-note">{activeRelation.note}</p> : null}

      <label className="graph-field">可信状态
        <select value={activeRelation.credibility} disabled={readOnly || busy}
          onChange={(event) => {
            void exec({
              type: 'update_relation', relation_id: activeRelation.id,
              expected_revision: activeRelation.revision,
              changes: { credibility: event.target.value as Credibility },
            });
          }}>
          {rules.credibility.map((entry) => (
            <option key={entry.value} value={entry.value}>{entry.label}</option>
          ))}
        </select>
      </label>
      {sources.length === 0 ? (
        <p className="graph-hint warn">无出处时不能设为「有证据」；请先添加出处。</p>
      ) : null}

      <SourcesEditor ws={ws} rules={rules} target={{ relation_id: activeRelation.id }}
        relation={activeRelation} readOnly={readOnly} run={run} onOpenSource={onOpenSource}
        searchSources={searchSources} />
      {caseId && activeRelation.from_entity_id !== activeRelation.to_entity_id ? (
        <CooccurrenceSection
          key={`${caseId}:${activeRelation.id}:${activeRelation.from_entity_id}:${activeRelation.to_entity_id}`}
          caseId={caseId}
          entityAId={activeRelation.from_entity_id}
          entityBId={activeRelation.to_entity_id}
          entityAName={from?.name ?? '?'}
          entityBName={to?.name ?? '?'}
          readOnly={readOnly}
          activeRelation={activeRelation}
          busy={busy}
          onAttachSource={async (segmentId, proves) => {
            const result = await exec({
              type: 'add_source',
              relation_id: activeRelation.id,
              ref: { segment_id: segmentId },
              proves,
            });
            return Boolean(result?.ok);
          }}
          onOpenSource={onOpenCooccurrenceSource}
          onCreateRelation={onCreateRelationFromCooccurrence}
        />
      ) : null}
      {error ? <p role="alert" className="graph-error-inline">错误：{error}</p> : null}
      <div className="graph-inspector-actions">
        <button type="button" onClick={() => onEditRelation(activeRelation)} disabled={readOnly}>编辑关系</button>
        <button type="button" disabled={readOnly || busy}
          onClick={() => {
            void exec({ type: 'remove_from_graph', graph_id: graphId, relation_id: activeRelation.id })
              .then((result) => { if (result?.ok) onSelect(null); });
          }}>从本图移除</button>
        <button type="button" className="danger" disabled={readOnly}
          onClick={() => onDeleteRelation(activeRelation)}>全案删除…</button>
      </div>
    </section>
  );
}
