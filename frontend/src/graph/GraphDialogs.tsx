/**
 * 关系图原型用原生 <dialog> 表单。
 * 全部走原生 form 提交；中文输入法组词期间按回车不会触发提交（guardComposition）。
 * 弹窗使用 showModal，不重复设置 dialog role（Modern Web Guidance）。
 */
import { useEffect, useId, useRef, useState } from 'react';
import type { FormEvent, KeyboardEvent, ReactNode } from 'react';
import { entityKindLabel, relationKindLabel } from './rules';
import type {
  CanvasGroup, CaseEntity, CaseRelation, CommandResult, Credibility, GraphCommand,
  GraphCommandRunner, GraphRules, GraphSnapshot, RelationAttrs, SourceRef,
} from './types';

/** 组词中的回车只用于确认候选，不提交表单。 */
export function guardComposition(event: KeyboardEvent<HTMLFormElement>): void {
  const native = event.nativeEvent as globalThis.KeyboardEvent & { isComposing?: boolean };
  if (event.key === 'Enter' && (native.isComposing || native.keyCode === 229 || event.keyCode === 229)) {
    event.preventDefault();
  }
}

/** 兼容 macOS Safari `compositionend` 先于 Enter keydown 的中文输入法防误提交守卫。 */
export function useCompositionGuard() {
  const composingRef = useRef(false);
  const endedAtRef = useRef(0);
  return {
    onCompositionStart: () => { composingRef.current = true; },
    onCompositionEnd: () => {
      composingRef.current = false;
      endedAtRef.current = Date.now();
    },
    onKeyDown: (event: KeyboardEvent<HTMLFormElement>) => {
      if (event.key !== 'Enter') return;
      const native = event.nativeEvent as globalThis.KeyboardEvent & { isComposing?: boolean };
      const isIme = composingRef.current
        || Date.now() - endedAtRef.current < 60
        || Boolean(native.isComposing)
        || native.keyCode === 229
        || event.keyCode === 229;
      if (isIme) {
        event.preventDefault();
      }
    },
  };
}

function Modal({ title, onClose, children, className }: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  className?: string;
}) {
  const id = useId();
  return (
    <dialog className={`work-dialog graph-dialog ${className ?? ''}`} aria-labelledby={id}
      ref={(element) => { if (element && !element.open) element.showModal(); }}
      onCancel={(event) => { event.preventDefault(); onClose(); }}>
      <h2 id={id}>{title}</h2>
      {children}
    </dialog>
  );
}

export function EntityDialog({ mode, entity, rules, graphId, onClose, onSubmit }: {
  mode: 'add' | 'edit';
  entity?: CaseEntity;
  rules: GraphRules;
  graphId?: string;
  onClose: () => void;
  onSubmit: GraphCommandRunner;
}) {
  const [name, setName] = useState(entity?.name ?? '');
  const [kind, setKind] = useState(entity?.kind ?? rules.entity_kinds[0].value);
  const [roles, setRoles] = useState<string[]>(entity?.roles ?? []);
  const [note, setNote] = useState(entity?.note ?? '');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const imeGuard = useCompositionGuard();

  function toggleRole(role: string) {
    setRoles((current) => current.includes(role)
      ? current.filter((value) => value !== role)
      : [...current, role]);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const command: GraphCommand = mode === 'add'
      ? {
        type: 'add_entity', kind, name, roles, note: note.trim() || null,
        ...(graphId ? { graph_id: graphId } : {}),
      }
      : {
        type: 'update_entity', entity_id: entity!.id, expected_revision: entity!.revision,
        changes: { name, kind, roles, note: note.trim() || null },
      };
    setBusy(true);
    try {
      const result = await onSubmit(command);
      if (!result.ok) { setError(result.message); return; }
      onClose();
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={mode === 'add' ? '新建主体' : '编辑主体'} onClose={() => { if (!busy) onClose(); }}>
      <form onSubmit={submit} {...imeGuard}>
        <label>名称
          <input autoFocus required value={name} maxLength={200}
            onChange={(event) => setName(event.target.value)} />
        </label>
        <label>类型
          <select value={kind} onChange={(event) => setKind(event.target.value)}>
            {rules.entity_kinds.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </label>
        <fieldset className="graph-role-fieldset">
          <legend>诉讼角色（可多选）</legend>
          <div className="graph-role-options">
            {rules.roles.map((role) => (
              <label key={role.value} className="graph-checkbox">
                <input type="checkbox" checked={roles.includes(role.value)}
                  onChange={() => toggleRole(role.value)} />
                {role.label}
              </label>
            ))}
          </div>
        </fieldset>
        <label>备注
          <textarea value={note} maxLength={4000} rows={3}
            onChange={(event) => setNote(event.target.value)} />
        </label>
        {error ? <p role="alert" className="graph-error-inline">错误：{error}</p> : null}
        <div className="work-actions">
          <button type="button" onClick={onClose}>取消</button>
          <button type="submit" disabled={busy || !name.trim()}>
            {busy ? '保存中…' : mode === 'add' ? '新建主体' : '保存'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export interface RelationDialogPrefill {
  fromEntityId?: string;
  toEntityId?: string;
  kind?: string;
  credibility?: Credibility;
  label?: string;
  amountYuan?: string;
  dateStart?: string;
  note?: string;
  initialSources?: { ref: SourceRef; proves?: string }[];
}

export function RelationDialog({ mode, relation, ws, rules, graphId, prefill, onClose, onSubmit }: {
  mode: 'add' | 'edit';
  relation?: CaseRelation;
  ws: GraphSnapshot;
  rules: GraphRules;
  graphId: string;
  prefill?: RelationDialogPrefill;
  onClose: () => void;
  onSubmit: GraphCommandRunner;
}) {
  const entities = ws.entities.filter((entity) => entity.removed_at === null);
  const [from, setFrom] = useState(relation?.from_entity_id ?? prefill?.fromEntityId ?? entities[0]?.id ?? '');
  const [to, setTo] = useState(relation?.to_entity_id ?? prefill?.toEntityId ?? entities[1]?.id ?? '');
  const [kind, setKind] = useState(relation?.kind ?? prefill?.kind ?? rules.relation_kinds[0].value);
  const [label, setLabel] = useState(relation?.label ?? prefill?.label ?? '');
  const [directed, setDirected] = useState(relation?.directed ?? true);
  const [credibility, setCredibility] = useState<Credibility>(
    relation?.credibility ?? prefill?.credibility ?? 'suspected',
  );
  const [share, setShare] = useState(relation?.attrs.share_pct != null
    ? String(relation.attrs.share_pct) : '');
  const [amount, setAmount] = useState(relation?.attrs.amount_cents != null
    ? String(relation.attrs.amount_cents / 100) : (prefill?.amountYuan ?? ''));
  const [currency, setCurrency] = useState(relation?.attrs.currency ?? 'CNY');
  const [dateStart, setDateStart] = useState(relation?.attrs.date_start ?? prefill?.dateStart ?? '');
  const [dateEnd, setDateEnd] = useState(relation?.attrs.date_end ?? '');
  const [note, setNote] = useState(relation?.note ?? prefill?.note ?? '');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const imeGuard = useCompositionGuard();
  const pendingCreatedRef = useRef<{
    relationId: string;
    fromEntityId: string;
    toEntityId: string;
    kind: string;
    addedSources: number;
    latestSnapshot: GraphSnapshot;
  } | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError('');
    const attrs: RelationAttrs = {};
    if (share.trim()) attrs.share_pct = Number(share);
    if (amount.trim()) attrs.amount_cents = Math.round(Number(amount) * 100);
    if (currency.trim()) attrs.currency = currency.trim();
    if (dateStart) attrs.date_start = dateStart;
    if (dateEnd) attrs.date_end = dateEnd;

    const initialSources = mode === 'add' ? (prefill?.initialSources ?? []) : [];
    const wantEvidencedWithSources = mode === 'add' && credibility === 'evidenced' && initialSources.length > 0;
    const initialCredibility: Credibility = wantEvidencedWithSources ? 'suspected' : credibility;

    setBusy(true);
    try {
      if (mode === 'edit') {
        const result = await onSubmit({
          type: 'update_relation', relation_id: relation!.id,
          expected_revision: relation!.revision,
          changes: {
            kind, label: label.trim() || null, directed, credibility, attrs,
            note: note.trim() || null,
          },
        });
        if (!result.ok) { setError(result.message); return; }
        onClose();
        return;
      }

      let progress = pendingCreatedRef.current;
      if (progress && (progress.fromEntityId !== from || progress.toEntityId !== to || progress.kind !== kind)) {
        progress = null;
        pendingCreatedRef.current = null;
      }

      if (!progress) {
        const prevIds = new Set(ws.relations.map((r) => r.id));
        const added = await onSubmit({
          type: 'add_relation', from_entity_id: from, to_entity_id: to, kind,
          label: label.trim() || null, directed, credibility: initialCredibility, attrs,
          note: note.trim() || null, graph_id: graphId,
        });
        if (!added.ok) { setError(added.message); return; }
        if (initialSources.length === 0) {
          onClose();
          return;
        }
        const afterAdd = added.snapshot;
        const lastChange = afterAdd.last_change_id
          ? afterAdd.changes.find((c) => c.id === afterAdd.last_change_id)
          : undefined;
        const createdId = lastChange?.diff.find((d) => d.target === 'relation' && d.before === null)?.id
          ?? afterAdd.relations.find((r) =>
            r.removed_at === null && !prevIds.has(r.id) && r.from_entity_id === from && r.to_entity_id === to && r.kind === kind)?.id
          ?? afterAdd.relations.find((r) =>
            r.removed_at === null && r.from_entity_id === from && r.to_entity_id === to && r.kind === kind)?.id;
        if (!createdId) {
          setError('关系已创建，但未能定位新建记录，请关闭后检查关系图。');
          return;
        }
        progress = {
          relationId: createdId,
          fromEntityId: from,
          toEntityId: to,
          kind,
          addedSources: 0,
          latestSnapshot: afterAdd,
        };
        pendingCreatedRef.current = progress;
      }

      for (let i = progress.addedSources; i < initialSources.length; i += 1) {
        const source = initialSources[i];
        const srcRes = await onSubmit({
          type: 'add_source', relation_id: progress.relationId, ref: source.ref,
          proves: source.proves ?? null,
        });
        if (!srcRes.ok) {
          setError(`关系已创建，但挂载预设出处（第 ${i + 1}/${initialSources.length} 条）失败：${srcRes.message}（点击「新建关系」可直接续接，无需重复建边）`);
          return;
        }
        progress.addedSources = i + 1;
        progress.latestSnapshot = srcRes.snapshot;
      }

      if (wantEvidencedWithSources) {
        const latestRel = progress.latestSnapshot.relations.find((r) => r.id === progress!.relationId);
        if (!latestRel) {
          setError('关系与出处已创建，但未能定位最新关系修订号，请刷新后手动升级状态。');
          return;
        }
        if (latestRel.credibility !== 'evidenced') {
          const updRes = await onSubmit({
            type: 'update_relation', relation_id: progress.relationId,
            expected_revision: latestRel.revision,
            changes: { credibility: 'evidenced' },
          });
          if (!updRes.ok) {
            setError(`关系及出处已创建，但升级为「有证据」失败：${updRes.message}（点击「新建关系」可重试升级）`);
            return;
          }
        }
      }
      pendingCreatedRef.current = null;
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '操作失败，请稍后重试。');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={mode === 'add' ? '新建关系' : '编辑关系'} onClose={() => { if (!busy) onClose(); }} className="graph-dialog-wide">
      <form onSubmit={submit} {...imeGuard}>
        <div className="graph-form-row">
          <label>起点主体
            <select value={from} onChange={(event) => setFrom(event.target.value)}>
              {entities.map((entity) => (
                <option key={entity.id} value={entity.id}>{entity.name}</option>
              ))}
            </select>
          </label>
          <label>终点主体
            <select value={to} onChange={(event) => setTo(event.target.value)}>
              {entities.map((entity) => (
                <option key={entity.id} value={entity.id}>{entity.name}</option>
              ))}
            </select>
          </label>
        </div>
        <div className="graph-form-row">
          <label>关系类型
            <select value={kind} onChange={(event) => setKind(event.target.value)}>
              {rules.relation_kinds.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label>可信状态
            <select value={credibility}
              onChange={(event) => setCredibility(event.target.value as Credibility)}>
              {rules.credibility.map((rule) => (
                <option key={rule.value} value={rule.value}>{rule.label}</option>
              ))}
            </select>
          </label>
        </div>
        {credibility === 'evidenced' && mode === 'add' ? (
          prefill?.initialSources?.length ? (
            <p className="graph-hint">保存时将自动挂载 {prefill.initialSources.length} 条预设出处并设为「有证据」。</p>
          ) : (
            <p className="graph-hint warn">「有证据」需要至少一条出处：请先以其他状态新建，添加出处后再升级。</p>
          )
        ) : prefill?.initialSources?.length && mode === 'add' ? (
          <p className="graph-hint">保存时将自动挂载 {prefill.initialSources.length} 条预设出处。</p>
        ) : null}
        <div className="graph-form-row">
          <label>边标签（可选）
            <input value={label} maxLength={200} placeholder="例如：持股 60%"
              onChange={(event) => setLabel(event.target.value)} />
          </label>
          <label className="graph-checkbox graph-checkbox-inline">
            <input type="checkbox" checked={directed}
              onChange={(event) => setDirected(event.target.checked)} />
            有向关系（显示箭头）
          </label>
        </div>
        <div className="graph-form-row">
          <label>持股比例（%）
            <input type="number" min={0} max={100} step="0.01" value={share}
              onChange={(event) => setShare(event.target.value)} />
          </label>
          <label>金额（元）
            <input type="number" min={0} step="0.01" value={amount}
              onChange={(event) => setAmount(event.target.value)} />
          </label>
          <label>币种
            <input value={currency} maxLength={8}
              onChange={(event) => setCurrency(event.target.value)} />
          </label>
        </div>
        <div className="graph-form-row">
          <label>起始日期
            <input type="date" value={dateStart}
              onChange={(event) => setDateStart(event.target.value)} />
          </label>
          <label>结束日期
            <input type="date" value={dateEnd}
              onChange={(event) => setDateEnd(event.target.value)} />
          </label>
        </div>
        <label>备注
          <textarea value={note} maxLength={4000} rows={3}
            onChange={(event) => setNote(event.target.value)} />
        </label>
        {error ? <p role="alert" className="graph-error-inline">错误：{error}</p> : null}
        <div className="work-actions">
          <button type="button" onClick={onClose}>取消</button>
          <button type="submit" disabled={busy || !from || !to || from === to}>
            {busy ? '保存中…' : mode === 'add' ? '新建关系' : '保存'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function GraphDialog({ mode, graph, onClose, onSubmit }: {
  mode: 'add' | 'rename';
  graph?: { id: string; title: string; description: string | null; revision: number };
  onClose: () => void;
  onSubmit: GraphCommandRunner;
}) {
  const [title, setTitle] = useState(graph?.title ?? '');
  const [description, setDescription] = useState(graph?.description ?? '');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const imeGuard = useCompositionGuard();
  return (
    <Modal title={mode === 'add' ? '新建关系图' : '重命名关系图'} onClose={onClose}>
      <form onSubmit={async (event) => {
        event.preventDefault();
        if (busy) return;
        const command: GraphCommand = mode === 'add'
          ? { type: 'create_graph', title, description: description.trim() || null }
          : {
            type: 'update_graph', graph_id: graph!.id, expected_revision: graph!.revision,
            changes: { title, description: description.trim() || null },
          };
        setBusy(true);
        try {
          const result = await onSubmit(command);
          if (!result.ok) { setError(result.message); return; }
          onClose();
        } finally {
          setBusy(false);
        }
      }} {...imeGuard}>
        <label>名称
          <input autoFocus required value={title} maxLength={200}
            onChange={(event) => setTitle(event.target.value)} />
        </label>
        <label>说明（可选）
          <textarea value={description} maxLength={2000} rows={3}
            onChange={(event) => setDescription(event.target.value)} />
        </label>
        {error ? <p role="alert" className="graph-error-inline">错误：{error}</p> : null}
        <div className="work-actions">
          <button type="button" onClick={onClose}>取消</button>
          <button type="submit" disabled={busy || !title.trim()}>
            {busy ? '保存中…' : mode === 'add' ? '创建' : '保存'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function AddToGraphDialog({ ws, rules, graphId, onClose, onSubmit }: {
  ws: GraphSnapshot;
  rules: GraphRules;
  graphId: string;
  onClose: () => void;
  onSubmit: GraphCommandRunner;
}) {
  const memberEntities = new Set(ws.nodes.filter((node) => node.graph_id === graphId)
    .map((node) => node.entity_id));
  const memberRelations = new Set(ws.edges.filter((edge) => edge.graph_id === graphId)
    .map((edge) => edge.relation_id));
  const entities = ws.entities.filter((entity) => entity.removed_at === null
    && !memberEntities.has(entity.id));
  const relations = ws.relations.filter((relation) => relation.removed_at === null
    && !memberRelations.has(relation.id));
  const [entityIds, setEntityIds] = useState<string[]>([]);
  const [relationIds, setRelationIds] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const imeGuard = useCompositionGuard();
  const entityName = (id: string) => ws.entities.find((entity) => entity.id === id)?.name ?? '?';
  return (
    <Modal title="加入本图" onClose={onClose} className="graph-dialog-wide">
      <p className="muted">从案件主体/关系池挑选加入本图；加入关系时两端主体会自动纳入。</p>
      <form onSubmit={async (event) => {
        event.preventDefault();
        if (busy) return;
        setBusy(true);
        try {
          const result = await onSubmit({ type: 'add_to_graph', graph_id: graphId, entity_ids: entityIds, relation_ids: relationIds });
          if (!result.ok) { setError(result.message); return; }
          onClose();
        } finally {
          setBusy(false);
        }
      }} {...imeGuard}>
        <fieldset className="graph-pick-fieldset">
          <legend>主体（{entities.length} 个可加入）</legend>
          {entities.length === 0 ? <p className="muted">案件池中的主体都已在图中。</p> : (
            <ul className="graph-pick-list">
              {entities.map((entity) => (
                <li key={entity.id}>
                  <label className="graph-checkbox">
                    <input type="checkbox" checked={entityIds.includes(entity.id)}
                      onChange={(event) => setEntityIds((current) => event.target.checked
                        ? [...current, entity.id]
                        : current.filter((id) => id !== entity.id))} />
                    {entity.name}
                    <span className="muted">（{entityKindLabel(rules, entity.kind)}）</span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </fieldset>
        <fieldset className="graph-pick-fieldset">
          <legend>关系（{relations.length} 条可加入）</legend>
          {relations.length === 0 ? <p className="muted">案件池中的关系都已在图中。</p> : (
            <ul className="graph-pick-list">
              {relations.map((relation) => (
                <li key={relation.id}>
                  <label className="graph-checkbox">
                    <input type="checkbox" checked={relationIds.includes(relation.id)}
                      onChange={(event) => setRelationIds((current) => event.target.checked
                        ? [...current, relation.id]
                        : current.filter((id) => id !== relation.id))} />
                    {entityName(relation.from_entity_id)} → {entityName(relation.to_entity_id)}
                    <span className="muted">（{relation.label ?? relationKindLabel(rules, relation.kind)}）</span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </fieldset>
        {error ? <p role="alert" className="graph-error-inline">错误：{error}</p> : null}
        <div className="work-actions">
          <button type="button" onClick={onClose}>取消</button>
          <button type="submit" disabled={busy || (entityIds.length === 0 && relationIds.length === 0)}>
            {busy ? '提交中…' : '加入本图'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function GroupDialog({ group, ws, graphId, onClose, onSubmit }: {
  group: CanvasGroup | null;
  ws: GraphSnapshot;
  graphId: string;
  onClose: () => void;
  onSubmit: GraphCommandRunner;
}) {
  const colors = ['#dbe7df', '#f0e4d2', '#e3e7f2', '#f2e3e3', '#e9e3f2'];
  const [title, setTitle] = useState(group?.title ?? '');
  const [color, setColor] = useState(group?.color ?? colors[0]);
  const ungrouped = ws.nodes.filter((node) => node.graph_id === graphId
    && !node.group_id).map((node) => node.entity_id);
  const [members, setMembers] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const imeGuard = useCompositionGuard();
  return (
    <Modal title={group ? '编辑分组' : '新建分组'} onClose={onClose}>
      <form onSubmit={async (event) => {
        event.preventDefault();
        if (busy) return;
        const command: GraphCommand = group
          ? {
            type: 'upsert_graph_group', graph_id: graphId, group_id: group.id,
            title, color,
          }
          : {
            type: 'upsert_graph_group', graph_id: graphId, title, color,
            x: 60, y: 60, width: 440, height: 280, entity_ids: members,
          };
        setBusy(true);
        try {
          const result = await onSubmit(command);
          if (!result.ok) { setError(result.message); return; }
          onClose();
        } finally {
          setBusy(false);
        }
      }} {...imeGuard}>
        <label>分组名称
          <input autoFocus required value={title} maxLength={200}
            onChange={(event) => setTitle(event.target.value)} />
        </label>
        <label>颜色
          <select value={color} onChange={(event) => setColor(event.target.value)}>
            {colors.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
        {!group ? (
          <fieldset className="graph-pick-fieldset">
            <legend>纳入本组的主体（未分组）</legend>
            {ungrouped.length === 0 ? <p className="muted">没有未分组主体。</p> : (
              <ul className="graph-pick-list">
                {ungrouped.map((entityId) => {
                  const entity = ws.entities.find((item) => item.id === entityId);
                  return (
                    <li key={entityId}>
                      <label className="graph-checkbox">
                        <input type="checkbox" checked={members.includes(entityId)}
                          onChange={(event) => setMembers((current) => event.target.checked
                            ? [...current, entityId]
                            : current.filter((id) => id !== entityId))} />
                        {entity?.name ?? entityId}
                      </label>
                    </li>
                  );
                })}
              </ul>
            )}
          </fieldset>
        ) : (
          <p className="muted graph-hint">成员在右侧「主体详情 → 本图分组」里调整。</p>
        )}
        {error ? <p role="alert" className="graph-error-inline">错误：{error}</p> : null}
        <div className="work-actions">
          <button type="button" onClick={onClose}>取消</button>
          <button type="submit" disabled={busy || !title.trim()}>
            {busy ? '保存中…' : group ? '保存' : '创建分组'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function DeleteDialog({ kind, name, loadImpact, onClose, onConfirm }: {
  kind: 'entity' | 'relation' | 'graph';
  name: string;
  /** 删除前影响面；未提供时明确说明由服务端校验，不显示伪造数量。 */
  loadImpact?: () => Promise<{ id: string; title: string }[] | null>;
  onClose: () => void;
  onConfirm: () => Promise<CommandResult>;
}) {
  const [impact, setImpact] = useState<{ id: string; title: string }[] | null>(null);
  const [impactState, setImpactState] = useState<'loading' | 'ready' | 'unavailable'>(
    loadImpact && kind !== 'graph' ? 'loading' : 'ready');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const loadRef = useRef(loadImpact);
  loadRef.current = loadImpact;

  useEffect(() => {
    if (!loadRef.current || kind === 'graph') return;
    let cancelled = false;
    loadRef.current().then((rows) => {
      if (cancelled) return;
      if (rows === null) { setImpactState('unavailable'); return; }
      setImpact(rows);
      setImpactState('ready');
    }).catch(() => { if (!cancelled) setImpactState('unavailable'); });
    return () => { cancelled = true; };
  }, [kind]);

  async function confirm() {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const result = await onConfirm();
      if (!result.ok) setError(result.message);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '删除失败');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="确认全案删除" onClose={onClose}>
      <p>将软删除{kind === 'entity' ? '主体' : kind === 'relation' ? '关系' : '关系图'}「{name}」。</p>
      {kind !== 'graph' ? (
        impactState === 'loading' ? (
          <p className="muted">正在读取受影响的图…</p>
        ) : impactState === 'unavailable' ? (
          <p className="muted">无法读取受影响的图清单；删除时服务端仍会校验跨图引用。</p>
        ) : (
          <p className="muted">
            受影响的图（{impact?.length ?? 0}）：{impact?.map((graph) => graph.title).join('、') || '无'}。
            {kind === 'entity' ? '其关系暂不显示，恢复主体后一并恢复。' : ''}
          </p>
        )
      ) : null}
      <p className="muted">删除写入变更记录，可用撤销（⌘Z）恢复；已有快照的图不能删除。</p>
      {error ? <p role="alert" className="graph-error-inline">错误：{error}</p> : null}
      <div className="work-actions">
        <button type="button" onClick={onClose}>取消</button>
        <button type="button" className="danger" disabled={busy} onClick={() => void confirm()}>
          {busy ? '删除中…' : '确认删除'}
        </button>
      </div>
    </Modal>
  );
}

export function ArchiveDialog({ archived, busy = false, error = '', onClose, onConfirm }: {
  archived: boolean;
  busy?: boolean;
  error?: string;
  onClose: () => void;
  onConfirm: () => Promise<void> | void;
}) {
  return (
    <Modal title={archived ? '取消归档' : '结案归档'} onClose={onClose}>
      <p className="muted">
        {archived
          ? '取消归档后，本案关系图与证据目录恢复可写。'
          : '归档后关系图、证据目录、快照与客户同意全部只读，查看与导出仍可用。'}
      </p>
      {error ? <p role="alert" className="graph-error-inline">错误：{error}</p> : null}
      <div className="work-actions">
        <button type="button" disabled={busy} onClick={onClose}>取消</button>
        <button type="button" disabled={busy} onClick={() => void onConfirm()}>
          {busy ? '处理中…' : archived ? '确认取消归档' : '确认归档'}
        </button>
      </div>
    </Modal>
  );
}

export function SourcePreviewDialog({ ws, sourceId, onClose }: {
  ws: GraphSnapshot;
  sourceId: string;
  onClose: () => void;
}) {
  const source = ws.sources.find((item) => item.id === sourceId);
  const label = ws.labels[sourceId];
  const segment = source?.segment_id
    ? ws.segments.find((item) => item.id === source.segment_id) : null;
  const annotation = source?.annotation_id
    ? ws.annotations.find((item) => item.id === source.annotation_id) : null;
  const material = ws.materials.find((item) => item.id === label?.material_id);
  return (
    <Modal title="出处原文（P1 模拟）" onClose={onClose} className="graph-dialog-wide">
      {!source ? <p className="muted">出处不存在或已删除。</p> : (
        <>
          <p className="graph-source-meta">
            {material?.title ?? '材料'}{label?.page ? ` · 第${label.page}页` : ' · 整份材料'}
          </p>
          <p><strong>{label?.text ?? '（未编号）'}</strong></p>
          {segment || annotation ? (
            <blockquote className="graph-source-excerpt">
              {(segment ?? annotation)!.text}
            </blockquote>
          ) : (
            <p className="muted">该出处指向整份材料，未指定页码。</p>
          )}
          {source.proves ? <p className="graph-source-proves">证明要点：{source.proves}</p> : null}
          <p className="muted graph-hint">
            P1 使用合成材料展示不可变 source_ref 的原文卡片；P4 接入 SourcePreview 后显示真实材料版本与页码。
          </p>
        </>
      )}
      <div className="work-actions">
        <button type="button" onClick={onClose}>关闭</button>
      </div>
    </Modal>
  );
}
