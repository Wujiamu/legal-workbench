/**
 * P1 内存执行器：与后端 graph / evidence_catalog 服务同一套命令语义。
 * 固定顺序：归档守卫 → 权限 → 校验 → 变更 → diff → 变更记录 → 序号 +1 → 审计 → 返回快照。
 * 同步执行即原子；P2 后端在同一短事务里完成同样的步骤。
 *
 * 原型口径（P2 由服务层与数据库实现）：
 * - 主体/别名/关系/出处属于案件，图只是成员与坐标（D1）；
 * - 证据编号只由已确认目录解析，待确认导入不参与（D2）；
 * - 快照冻结排除待确认变更，快照不可变；客户同意只追加（D3）；
 * - 「有效出处」只算已确认的出处：Agent 待确认的出处不能支撑「有证据」，
 *   人工升级前须先确认该出处；冻结载荷绝不出现无出处的实线；
 * - 复核按写入顺序：更早的待确认未处理不得超前确认，更晚的未处理不得先撤回；
 *   人工已改同一字段时旧提案过期，只能撤回（仅标记，不改工作稿）；
 * - 撤销：原型保存写命令前的工作区副本，撤销时整体恢复并追加一条变更记录，
 *   仅用于演示；P2 用逆命令与 CAS 实现，语义相同。
 */
import { computeEvidenceNumbers, resolveAllSources, resolveSource } from './evidence';
import { buildSnapshotPayload, pendingChangesInGraph } from './model';
import type {
  Actor, Annotation, AuditEntry, CaseEntity, CaseRelation, ChangeDiffEntry, CommandResult,
  ConsentAction, Credibility, DiffTarget, EntityAlias, EntityChanges, Entry,
  EvidenceBinding, EvidenceGroup, EvidenceItem, EvidenceReview, GraphChange, GraphCommand,
  GraphExecutor, GraphRules, GraphSnapshot, GraphSource, Material, Option, RelationAttrs,
  RelationChanges, RelationGraph, RelationGraphEdge, RelationGraphGroup, RelationGraphNode,
  RelationGraphSnapshot, Segment, SnapshotConsent, SnapshotPayload, SourceRef,
} from './types';

export interface LocalExecutorOptions {
  caseId: string;
  caseTitle: string;
  rules: GraphRules;
  materials: Material[];
  segments: Segment[];
  annotations: Annotation[];
  progressNodes?: Option[];
}

const NAME_MAX = 200;
const NOTE_MAX = 4000;
const LABEL_MAX = 200;
const PROVES_MAX = 500;
const GROUP_TITLE_MAX = 200;
const ITEM_TITLE_MAX = 300;
const ITEM_PURPOSE_MAX = 2000;
const STAGE_MAX = 100;
const SNAPSHOT_NOTE_MAX = 2000;
const CONSENT_DETAIL_MAX = 1000;
const CONSENT_NOTE_MAX = 2000;

/** 这些命令只允许人工入口；Agent 白名单也不包含它们（双保险）。 */
const HUMAN_ONLY = new Set<string>([
  'create_snapshot', 'restore_snapshot', 'add_snapshot_consent',
  'accept_change', 'accept_all', 'revert_change',
  'confirm_evidence_item', 'reject_evidence_item',
  'confirm_evidence_binding', 'reject_evidence_binding',
  'accept_evidence_change', 'revert_evidence_change',
  'save_layout', 'set_node_group', 'upsert_graph_group', 'remove_graph_group',
  'remove_entity', 'restore_entity', 'remove_relation', 'restore_relation',
  'remove_source', 'remove_graph', 'remove_from_graph', 'remove_alias',
  'reorder_evidence_groups', 'remove_evidence_group', 'remove_evidence_item',
  'remove_evidence_binding',
]);

/** 不进入撤销栈：快照、客户同意与复核动作本身有独立留痕。 */
const NON_UNDOABLE = new Set<string>([
  'create_snapshot', 'add_snapshot_consent',
  'accept_change', 'accept_all', 'revert_change',
  'accept_evidence_change', 'revert_evidence_change',
]);

function now(): string {
  return new Date().toISOString();
}

function newId(): string {
  return crypto.randomUUID();
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

type Failure = Extract<CommandResult, { ok: false }>;

function fail(code: Failure['code'], message: string): Failure {
  return { ok: false, code, message };
}

function isFailure(value: unknown): value is Failure {
  return typeof value === 'object' && value !== null
    && (value as { ok?: boolean }).ok === false;
}

function trimmed(value: string): string {
  return value.trim();
}

function within(value: string | null | undefined, max: number): boolean {
  return value === null || value === undefined || value.length <= max;
}

function validDateText(value: string): boolean {
  if (!value) return false;
  return !Number.isNaN(Date.parse(value));
}

interface DataSnapshot {
  entities: CaseEntity[];
  aliases: EntityAlias[];
  relations: CaseRelation[];
  sources: GraphSource[];
  graphs: RelationGraph[];
  nodes: RelationGraphNode[];
  edges: RelationGraphEdge[];
  groups: RelationGraphGroup[];
  evidenceGroups: EvidenceGroup[];
  evidenceItems: EvidenceItem[];
  evidenceBindings: EvidenceBinding[];
}

interface UndoEntry {
  label: string;
  data: DataSnapshot;
  changeId: string;
  seqs: { graph: number; evidence: number };
}

const COMMAND_LABELS: Record<string, string> = {
  add_entity: '新建主体', update_entity: '修改主体', remove_entity: '删除主体',
  restore_entity: '恢复主体', add_alias: '新增别名', remove_alias: '移除别名',
  add_relation: '新建关系', update_relation: '修改关系', remove_relation: '删除关系',
  restore_relation: '恢复关系', add_source: '添加出处', remove_source: '移除出处',
  create_graph: '新建关系图', update_graph: '重命名关系图', remove_graph: '删除关系图',
  add_to_graph: '加入本图', remove_from_graph: '从本图移除', save_layout: '调整布局',
  set_node_group: '调整主体分组',
  upsert_graph_group: '编辑分组', remove_graph_group: '删除分组',
  create_snapshot: '冻结快照', restore_snapshot: '恢复快照',
  add_snapshot_consent: '登记客户同意',
  add_evidence_group: '新建证据分组', update_evidence_group: '修改证据分组',
  reorder_evidence_groups: '调整证据分组顺序', remove_evidence_group: '删除证据分组',
  add_evidence_item: '新建证据条目', update_evidence_item: '修改证据条目',
  move_evidence_item: '移动证据条目', remove_evidence_item: '删除证据条目',
  add_evidence_binding: '绑定材料', remove_evidence_binding: '解除材料绑定',
  confirm_evidence_item: '确认证据条目', reject_evidence_item: '驳回证据条目',
  confirm_evidence_binding: '确认证据绑定', reject_evidence_binding: '驳回证据绑定',
  import_evidence_catalog: '导入外部证据目录',
};

export function createLocalGraphExecutor(options: LocalExecutorOptions): GraphExecutor {
  const { caseId, caseTitle, rules } = options;
  const entities = new Map<string, CaseEntity>();
  const aliases = new Map<string, EntityAlias>();
  const relations = new Map<string, CaseRelation>();
  const sources = new Map<string, GraphSource>();
  const graphs = new Map<string, RelationGraph>();
  const nodes = new Map<string, RelationGraphNode>();
  const edges = new Map<string, RelationGraphEdge>();
  const groups = new Map<string, RelationGraphGroup>();
  const snapshots = new Map<string, RelationGraphSnapshot>();
  const consents = new Map<string, SnapshotConsent>();
  const evidenceGroups = new Map<string, EvidenceGroup>();
  const evidenceItems = new Map<string, EvidenceItem>();
  const evidenceBindings = new Map<string, EvidenceBinding>();
  const materials = new Map(options.materials.map((material) => [material.id, material]));
  const segments = new Map(options.segments.map((segment) => [segment.id, segment]));
  const annotations = new Map(options.annotations.map((annotation) => [annotation.id, annotation]));
  const progressNodes = options.progressNodes ?? [];
  const changes: GraphChange[] = [];
  const audit: AuditEntry[] = [];
  const undoStack: UndoEntry[] = [];
  let seqGraph = 0;
  let seqEvidence = 0;
  let archived = false;
  let lastChangeId: string | null = null;

  /* ---------------- 读取 ---------------- */

  const evidenceContext = () => ({
    groups: [...evidenceGroups.values()],
    items: [...evidenceItems.values()],
    bindings: [...evidenceBindings.values()],
    materials: [...materials.values()],
    segments: [...segments.values()],
    annotations: [...annotations.values()],
  });

  function readSnapshot(): GraphSnapshot {
    const ctx = evidenceContext();
    const { labels, numbers } = resolveAllSources([...sources.values()], ctx);
    return {
      case_id: caseId,
      case_title: caseTitle,
      archived,
      seq: seqGraph + seqEvidence,
      seqs: { graph: seqGraph, evidence: seqEvidence },
      entities: [...entities.values()].map(clone),
      aliases: [...aliases.values()].map(clone),
      relations: [...relations.values()].map(clone),
      sources: [...sources.values()].map(clone),
      graphs: [...graphs.values()].map(clone),
      nodes: [...nodes.values()].map(clone),
      edges: [...edges.values()].map(clone),
      groups: [...groups.values()].map(clone),
      snapshots: [...snapshots.values()].map(clone),
      consents: [...consents.values()].map(clone),
      evidence: {
        groups: [...evidenceGroups.values()].map(clone),
        items: [...evidenceItems.values()].map(clone),
        bindings: [...evidenceBindings.values()].map(clone),
      },
      materials: [...materials.values()].map(clone),
      segments: [...segments.values()].map(clone),
      annotations: [...annotations.values()].map(clone),
      progress_nodes: progressNodes.map(clone),
      labels,
      evidence_numbers: numbers.numbers,
      pending_changes: changes.filter((change) => change.review_state === 'pending').map(clone),
      changes: changes.map(clone),
      last_change_id: lastChangeId,
    };
  }

  function snapshot(): CommandResult {
    return { ok: true, snapshot: readSnapshot(), last_change_id: lastChangeId };
  }

  function ok(notice?: string): CommandResult {
    return { ok: true, snapshot: readSnapshot(), notice, last_change_id: lastChangeId };
  }

  /* ---------------- 变更记录与审计 ---------------- */

  function recordChange(command: string, actor: Actor, entry: Entry,
                        diff: ChangeDiffEntry[], domain: 'graph' | 'evidence'): GraphChange {
    if (domain === 'graph') seqGraph += 1;
    else seqEvidence += 1;
    const change: GraphChange = {
      id: newId(), case_id: caseId, domain,
      seq: domain === 'graph' ? seqGraph : seqEvidence,
      command, actor, entry, diff,
      review_state: actor === 'agent' ? 'pending' : 'none',
      reviewed_at: null, reverted_by: null, created_at: now(),
    };
    changes.push(change);
    lastChangeId = change.id;
    return change;
  }

  function recordAudit(command: string, actor: Actor, entry: Entry,
                       targetIds: string[], domain: 'graph' | 'evidence'): void {
    audit.push({ operation: `${domain}.${command}`, actor, entry, target_ids: [...targetIds] });
  }

  function diffOf<T extends { id: string; revision?: number }>(target: DiffTarget, before: T | null,
                                                               after: T | null): ChangeDiffEntry {
    const record = (after ?? before)!;
    return {
      target,
      id: record.id,
      before: before ? clone(before) as unknown as Record<string, unknown> : null,
      after: after ? clone(after) as unknown as Record<string, unknown> : null,
      before_rev: typeof before?.revision === 'number' ? before.revision : null,
      after_rev: typeof after?.revision === 'number' ? after.revision : null,
    };
  }

  /* ---------------- 撤销（原型口径） ---------------- */

  function capture(): DataSnapshot {
    return {
      entities: [...entities.values()].map(clone),
      aliases: [...aliases.values()].map(clone),
      relations: [...relations.values()].map(clone),
      sources: [...sources.values()].map(clone),
      graphs: [...graphs.values()].map(clone),
      nodes: [...nodes.values()].map(clone),
      edges: [...edges.values()].map(clone),
      groups: [...groups.values()].map(clone),
      evidenceGroups: [...evidenceGroups.values()].map(clone),
      evidenceItems: [...evidenceItems.values()].map(clone),
      evidenceBindings: [...evidenceBindings.values()].map(clone),
    };
  }

  function applyData(data: DataSnapshot): void {
    const fill = <T extends { id: string }>(map: Map<string, T>, list: T[]) => {
      map.clear();
      list.forEach((record) => map.set(record.id, clone(record)));
    };
    fill(entities, data.entities);
    fill(aliases, data.aliases);
    fill(relations, data.relations);
    fill(sources, data.sources);
    fill(graphs, data.graphs);
    fill(nodes, data.nodes);
    fill(edges, data.edges);
    fill(groups, data.groups);
    fill(evidenceGroups, data.evidenceGroups);
    fill(evidenceItems, data.evidenceItems);
    fill(evidenceBindings, data.evidenceBindings);
  }

  function pushUndo(command: GraphCommand, data: DataSnapshot, changeId: string,
                    seqs: { graph: number; evidence: number }): void {
    const label = COMMAND_LABELS[command.type] ?? command.type;
    const top = undoStack[undoStack.length - 1];
    if (command.type === 'save_layout' && top && top.label === label) {
      // 连续拖拽合并为一步，保留最早的副本。
      top.changeId = changeId;
      top.seqs = seqs;
      return;
    }
    undoStack.push({ label, data, changeId, seqs });
    if (undoStack.length > 50) undoStack.shift();
  }

  /* ---------------- 通用校验 ---------------- */

  function activeEntities(): CaseEntity[] {
    return [...entities.values()].filter((entity) => entity.removed_at === null);
  }

  function activeRelations(): CaseRelation[] {
    return [...relations.values()].filter((relation) => relation.removed_at === null);
  }

  function activeSourcesOf(target: { entity_id?: string; relation_id?: string }): GraphSource[] {
    return [...sources.values()].filter((source) => source.removed_at === null
      && (target.entity_id ? source.entity_id === target.entity_id
        : target.relation_id ? source.relation_id === target.relation_id : false));
  }

  /* ---------------- 待确认口径（D3） ----------------
   * Agent 变更写入即作用于工作稿并标记 pending；在人工确认前：
   * - 不计入「有效出处」，不能支撑「有证据」；
   * - 不能作为人工写入的对象（先确认再改）；
   * - 复核按写入顺序；人工已改写的字段使旧提案过期，只能撤回。
   */

  const META_FIELDS = new Set(['revision', 'updated_at', 'updated_by', 'created_at']);

  /** 仍处于待确认的 Agent 新增对象 id（新增即 before=null）。 */
  function pendingAddIds(target: DiffTarget): Set<string> {
    const ids = new Set<string>();
    for (const change of changes) {
      if (change.review_state !== 'pending' || change.actor !== 'agent') continue;
      for (const entry of change.diff) {
        if (entry.target === target && entry.before === null) ids.add(entry.id);
      }
    }
    return ids;
  }

  function isPendingAddition(target: DiffTarget, id: string): boolean {
    return pendingAddIds(target).has(id);
  }

  /** 有效（已确认）出处：排除 Agent 待确认新增。 */
  function confirmedSourcesOf(target: { entity_id?: string; relation_id?: string }): GraphSource[] {
    const pending = pendingAddIds('source');
    return activeSourcesOf(target).filter((source) => !pending.has(source.id));
  }

  function changedFields(before: Record<string, unknown> | null,
                         after: Record<string, unknown> | null): Set<string> {
    const fields = new Set<string>();
    if (!before || !after) return fields;
    for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
      if (META_FIELDS.has(key)) continue;
      if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) fields.add(key);
    }
    return fields;
  }

  /** 两条变更是否触及同一对象的同一字段（新增/删除按整条对象算）。 */
  function overlaps(left: GraphChange, right: GraphChange): boolean {
    for (const entry of left.diff) {
      for (const other of right.diff) {
        if (entry.target !== other.target || entry.id !== other.id) continue;
        if (!entry.before || !entry.after || !other.before || !other.after) return true;
        const fields = changedFields(entry.before, entry.after);
        for (const field of changedFields(other.before, other.after)) {
          if (fields.has(field)) return true;
        }
      }
    }
    return false;
  }

  /** 同一对象上更早/更晚的待确认变更（复核顺序）。 */
  function pendingNeighbour(change: GraphChange, direction: 'earlier' | 'later'): GraphChange | undefined {
    const position = changes.indexOf(change);
    return changes.find((other) => {
      if (other === change || other.review_state !== 'pending') return false;
      const otherPosition = changes.indexOf(other);
      if (direction === 'earlier' ? otherPosition >= position : otherPosition <= position) return false;
      return overlaps(change, other);
    });
  }

  /** 人工写入已改同一字段：旧提案过期，不能再确认（只能撤回）。 */
  function supersededByHuman(change: GraphChange): boolean {
    const position = changes.indexOf(change);
    // 移动图上已有节点只改变视图坐标，不否定 Agent 提议的纳入关系。
    return changes.some((other) => other.actor === 'human'
      && other.command !== 'revert_change' && other.command !== 'undo'
      && other.command !== 'save_layout'
      && changes.indexOf(other) > position && overlaps(change, other));
  }

  /** 人工写入不得落在 Agent 待确认新增的对象上（先确认再改，避免改动被快照排除）。 */
  function pendingAdditionGuard(target: DiffTarget, id: string, actor: Actor): Failure | null {
    if (actor !== 'human' || !isPendingAddition(target, id)) return null;
    return fail('conflict', '该对象是 Agent 待确认新增：请先在待确认列表中确认后再修改');
  }

  /** 确认「有证据」前必须已有已确认出处；同批确认的出处一并计入。 */
  function evidencedGuard(accepting: GraphChange[]): Failure | null {
    const addedSources = new Map<string, Set<string>>();
    for (const change of accepting) {
      for (const entry of change.diff) {
        if (entry.target !== 'source' || entry.before !== null || !entry.after) continue;
        const source = entry.after as unknown as GraphSource;
        if (!source.relation_id) continue;
        const set = addedSources.get(source.relation_id) ?? new Set<string>();
        set.add(source.id);
        addedSources.set(source.relation_id, set);
      }
    }
    for (const change of accepting) {
      for (const entry of change.diff) {
        if (entry.target !== 'relation' || !entry.after) continue;
        const relation = entry.after as unknown as CaseRelation;
        if (relation.credibility !== 'evidenced') continue;
        const confirmed = confirmedSourcesOf({ relation_id: relation.id }).length;
        const inBatch = addedSources.get(relation.id)?.size ?? 0;
        if (confirmed + inBatch === 0) {
          return fail('conflict', '该关系没有已确认的出处：请先确认出处变更，再确认「有证据」升级');
        }
      }
    }
    return null;
  }

  function requireEntity(entityId: string): CaseEntity | Failure {
    const entity = entities.get(entityId);
    if (!entity) return fail('not_found', '主体不存在');
    if (entity.case_id !== caseId) return fail('case_mismatch', '主体不属于本案');
    return entity;
  }

  function requireActiveEntity(entityId: string): CaseEntity | Failure {
    const found = requireEntity(entityId);
    if (isFailure(found)) return found;
    if (found.removed_at !== null) return fail('validation', '主体已删除');
    return found;
  }

  function requireRelation(relationId: string): CaseRelation | Failure {
    const relation = relations.get(relationId);
    if (!relation) return fail('not_found', '关系不存在');
    if (relation.case_id !== caseId) return fail('case_mismatch', '关系不属于本案');
    return relation;
  }

  function requireActiveRelation(relationId: string): CaseRelation | Failure {
    const found = requireRelation(relationId);
    if (isFailure(found)) return found;
    if (found.removed_at !== null) return fail('validation', '关系已删除');
    return found;
  }

  function requireGraph(graphId: string): RelationGraph | Failure {
    const graph = graphs.get(graphId);
    if (!graph) return fail('not_found', '关系图不存在');
    if (graph.case_id !== caseId) return fail('case_mismatch', '关系图不属于本案');
    if (graph.removed_at !== null) return fail('validation', '关系图已删除');
    return graph;
  }

  function cas(current: { revision: number }, expected: number, label: string): Failure | null {
    if (current.revision !== expected) {
      return fail('conflict', `${label}已被修改，请刷新后重试`);
    }
    return null;
  }

  function validateRoles(roles: string[]): Failure | null {
    const allowed = new Set(rules.roles.map((role) => role.value));
    for (const role of roles) {
      if (!allowed.has(role)) return fail('validation', `未知诉讼角色：${role}`);
    }
    return null;
  }

  function validateAttrs(attrs: RelationAttrs): Failure | null {
    if (attrs.share_pct !== undefined && attrs.share_pct !== null
        && (Number.isNaN(attrs.share_pct) || attrs.share_pct < 0 || attrs.share_pct > 100)) {
      return fail('validation', '持股比例需在 0–100 之间');
    }
    if (attrs.amount_cents !== undefined && attrs.amount_cents !== null
        && (!Number.isInteger(attrs.amount_cents) || attrs.amount_cents < 0)) {
      return fail('validation', '金额必须是非负整数（分）');
    }
    if (attrs.date_start && !validDateText(attrs.date_start)) return fail('validation', '起始日期格式不正确');
    if (attrs.date_end && !validDateText(attrs.date_end)) return fail('validation', '结束日期格式不正确');
    return null;
  }

  function duplicateHint(name: string, excludeEntityId?: string): string | undefined {
    const target = name.toLowerCase();
    const sameName = activeEntities().some((entity) => entity.id !== excludeEntityId
      && entity.name.toLowerCase() === target);
    const sameAlias = [...aliases.values()].some((alias) => alias.removed_at === null
      && alias.entity_id !== excludeEntityId && alias.alias.toLowerCase() === target);
    return sameName || sameAlias
      ? '案件池中已有同名或疑似别名主体；系统不会自动合并，请人工核对。'
      : undefined;
  }

  function refExists(ref: SourceRef): boolean {
    if ('segment_id' in ref) return segments.has(ref.segment_id);
    if ('annotation_id' in ref) return annotations.has(ref.annotation_id);
    return materials.has(ref.material_id);
  }

  function refColumns(ref: SourceRef): Pick<GraphSource, 'segment_id' | 'material_id' | 'annotation_id'> {
    if ('segment_id' in ref) return { segment_id: ref.segment_id, material_id: null, annotation_id: null };
    if ('annotation_id' in ref) return { segment_id: null, material_id: null, annotation_id: ref.annotation_id };
    return { segment_id: null, material_id: ref.material_id, annotation_id: null };
  }

  function sameRef(source: GraphSource, ref: SourceRef): boolean {
    if ('segment_id' in ref) return source.segment_id === ref.segment_id;
    if ('annotation_id' in ref) return source.annotation_id === ref.annotation_id;
    return source.material_id === ref.material_id && source.segment_id === null
      && source.annotation_id === null;
  }

  function requestKeyExists(map: Map<string, { request_key: string | null; removed_at?: string | null }>,
                            key: string | undefined): boolean {
    if (!key) return false;
    return [...map.values()].some((record) => record.request_key === key
      && (record.removed_at === undefined || record.removed_at === null));
  }

  function defaultNodePosition(graphId: string): { x: number; y: number } {
    const count = [...nodes.values()].filter((node) => node.graph_id === graphId).length;
    return { x: 80 + (count % 4) * 270, y: 90 + Math.floor(count / 4) * 170 };
  }

  function ensureNode(graphId: string, entityId: string, diff: ChangeDiffEntry[],
                      position?: { x: number; y: number }): RelationGraphNode {
    const existing = [...nodes.values()].find((node) => node.graph_id === graphId
      && node.entity_id === entityId);
    if (existing) return existing;
    const place = position ?? defaultNodePosition(graphId);
    const node: RelationGraphNode = {
      id: newId(), case_id: caseId, graph_id: graphId, entity_id: entityId,
      group_id: null, x: place.x, y: place.y, revision: 0,
      created_at: now(), updated_at: now(),
    };
    nodes.set(node.id, node);
    diff.push(diffOf('graph_node', null, node));
    return node;
  }

  function ensureEdge(graphId: string, relationId: string, diff: ChangeDiffEntry[]): RelationGraphEdge {
    const existing = [...edges.values()].find((edge) => edge.graph_id === graphId
      && edge.relation_id === relationId);
    if (existing) return existing;
    const edge: RelationGraphEdge = {
      id: newId(), case_id: caseId, graph_id: graphId, relation_id: relationId,
      created_at: now(),
    };
    edges.set(edge.id, edge);
    diff.push(diffOf('graph_edge', null, edge));
    return edge;
  }

  /* ---------------- 主体与别名 ---------------- */

  function addEntity(command: Extract<GraphCommand, { type: 'add_entity' }>,
                     actor: Actor, entry: Entry): CommandResult {
    if (actor === 'agent' && !command.request_key) {
      return fail('validation', 'Agent 新建主体必须带 request_key');
    }
    if (requestKeyExists(entities, command.request_key)) return snapshot();
    const name = trimmed(command.name);
    if (!name) return fail('validation', '主体名称不能为空');
    if (name.length > NAME_MAX) return fail('validation', `主体名称不能超过 ${NAME_MAX} 字`);
    if (!rules.entity_kinds.some((kind) => kind.value === command.kind)) {
      return fail('validation', `未知主体类型：${command.kind}`);
    }
    const roles = command.roles ?? [];
    const roleError = validateRoles(roles);
    if (roleError) return roleError;
    if (!within(command.note, NOTE_MAX)) return fail('validation', `备注不能超过 ${NOTE_MAX} 字`);
    if (command.graph_id) {
      const graph = requireGraph(command.graph_id);
      if (isFailure(graph)) return graph;
    }
    const timestamp = now();
    const entity: CaseEntity = {
      id: newId(), case_id: caseId, kind: command.kind, name, roles: [...roles],
      note: command.note ?? null, created_by: actor, updated_by: actor,
      request_key: command.request_key ?? null, removed_at: null, revision: 0,
      created_at: timestamp, updated_at: timestamp,
    };
    entities.set(entity.id, entity);
    const diff: ChangeDiffEntry[] = [diffOf('entity', null, entity)];
    if (command.graph_id) ensureNode(command.graph_id, entity.id, diff);
    recordChange('add_entity', actor, entry, diff, 'graph');
    recordAudit('add_entity', actor, entry, [entity.id], 'graph');
    return ok(duplicateHint(name, entity.id));
  }

  function updateEntity(command: Extract<GraphCommand, { type: 'update_entity' }>,
                        actor: Actor, entry: Entry): CommandResult {
    const found = requireActiveEntity(command.entity_id);
    if (isFailure(found)) return found;
    const pendingGuard = pendingAdditionGuard('entity', found.id, actor);
    if (pendingGuard) return pendingGuard;
    const stale = cas(found, command.expected_revision, '主体');
    if (stale) return stale;
    const changes = command.changes;
    if (changes.name !== undefined) {
      const name = trimmed(changes.name);
      if (!name || name.length > NAME_MAX) return fail('validation', `名称需为 1–${NAME_MAX} 字`);
    }
    if (changes.kind !== undefined
        && !rules.entity_kinds.some((kind) => kind.value === changes.kind)) {
      return fail('validation', `未知主体类型：${changes.kind}`);
    }
    if (changes.roles !== undefined) {
      const roleError = validateRoles(changes.roles);
      if (roleError) return roleError;
    }
    if (!within(changes.note, NOTE_MAX)) return fail('validation', `备注不能超过 ${NOTE_MAX} 字`);
    const before = clone(found);
    if (changes.name !== undefined) found.name = trimmed(changes.name);
    if (changes.kind !== undefined) found.kind = changes.kind;
    if (changes.roles !== undefined) found.roles = [...changes.roles];
    if (changes.note !== undefined) found.note = changes.note;
    found.revision += 1;
    found.updated_by = actor;
    found.updated_at = now();
    recordChange('update_entity', actor, entry, [diffOf('entity', before, found)], 'graph');
    recordAudit('update_entity', actor, entry, [found.id], 'graph');
    return ok(found.name !== before.name ? duplicateHint(found.name, found.id) : undefined);
  }

  function removeEntity(command: Extract<GraphCommand, { type: 'remove_entity' }>,
                        actor: Actor, entry: Entry): CommandResult {
    const found = requireActiveEntity(command.entity_id);
    if (isFailure(found)) return found;
    const stale = cas(found, command.expected_revision, '主体');
    if (stale) return stale;
    const affected = [...graphs.values()].filter((graph) => graph.removed_at === null
      && [...nodes.values()].some((node) => node.graph_id === graph.id
        && node.entity_id === found.id));
    const linkedRelations = activeRelations().filter((relation) =>
      relation.from_entity_id === found.id || relation.to_entity_id === found.id).length;
    const before = clone(found);
    found.removed_at = now();
    found.revision += 1;
    found.updated_by = actor;
    found.updated_at = now();
    recordChange('remove_entity', actor, entry, [diffOf('entity', before, found)], 'graph');
    recordAudit('remove_entity', actor, entry, [found.id], 'graph');
    return ok(`已删除主体「${before.name}」（案件级软删除），影响 ${affected.length} 张图：`
      + `${affected.map((graph) => graph.title).join('、') || '无'}。`
      + (linkedRelations > 0 ? `其 ${linkedRelations} 条关系暂不显示，恢复主体后一并恢复。` : '')
      + '可用撤销恢复。');
  }

  function restoreEntity(command: Extract<GraphCommand, { type: 'restore_entity' }>,
                         actor: Actor, entry: Entry): CommandResult {
    const found = requireEntity(command.entity_id);
    if (isFailure(found)) return found;
    if (found.removed_at === null) return fail('validation', '主体未删除');
    const stale = cas(found, command.expected_revision, '主体');
    if (stale) return stale;
    const before = clone(found);
    found.removed_at = null;
    found.revision += 1;
    found.updated_by = actor;
    found.updated_at = now();
    recordChange('restore_entity', actor, entry, [diffOf('entity', before, found)], 'graph');
    recordAudit('restore_entity', actor, entry, [found.id], 'graph');
    return ok();
  }

  function addAlias(command: Extract<GraphCommand, { type: 'add_alias' }>,
                    actor: Actor, entry: Entry): CommandResult {
    const entity = requireActiveEntity(command.entity_id);
    if (isFailure(entity)) return entity;
    const pendingGuard = pendingAdditionGuard('entity', entity.id, actor);
    if (pendingGuard) return pendingGuard;
    const aliasText = trimmed(command.alias);
    if (!aliasText) return fail('validation', '别名不能为空');
    if (aliasText.length > NAME_MAX) return fail('validation', `别名不能超过 ${NAME_MAX} 字`);
    const duplicate = [...aliases.values()].some((alias) => alias.removed_at === null
      && alias.entity_id === entity.id && alias.alias.toLowerCase() === aliasText.toLowerCase());
    // Agent 重复提案按幂等处理，不重复建行也不报错。
    if (duplicate) return actor === 'agent' ? snapshot() : fail('validation', '该别名已存在');
    const alias: EntityAlias = {
      id: newId(), case_id: caseId, entity_id: entity.id, alias: aliasText,
      created_by: actor, removed_at: null, created_at: now(),
    };
    aliases.set(alias.id, alias);
    recordChange('add_alias', actor, entry, [diffOf('alias', null, alias)], 'graph');
    recordAudit('add_alias', actor, entry, [alias.id], 'graph');
    return ok(duplicateHint(aliasText, entity.id));
  }

  function removeAlias(command: Extract<GraphCommand, { type: 'remove_alias' }>,
                       actor: Actor, entry: Entry): CommandResult {
    const alias = aliases.get(command.alias_id);
    if (!alias || alias.removed_at !== null) return fail('not_found', '别名不存在');
    const before = clone(alias);
    alias.removed_at = now();
    recordChange('remove_alias', actor, entry, [diffOf('alias', before, alias)], 'graph');
    recordAudit('remove_alias', actor, entry, [alias.id], 'graph');
    return ok();
  }

  /* ---------------- 关系与出处 ---------------- */

  function addRelation(command: Extract<GraphCommand, { type: 'add_relation' }>,
                       actor: Actor, entry: Entry): CommandResult {
    if (actor === 'agent' && !command.request_key) {
      return fail('validation', 'Agent 新建关系必须带 request_key');
    }
    if (requestKeyExists(relations, command.request_key)) return snapshot();
    const from = requireActiveEntity(command.from_entity_id);
    if (isFailure(from)) return from;
    const to = requireActiveEntity(command.to_entity_id);
    if (isFailure(to)) return to;
    for (const endpoint of [from, to]) {
      const pendingGuard = pendingAdditionGuard('entity', endpoint.id, actor);
      if (pendingGuard) return pendingGuard;
    }
    if (from.id === to.id) return fail('validation', '关系起点与终点不能是同一主体');
    if (!rules.relation_kinds.some((kind) => kind.value === command.kind)) {
      return fail('validation', `未知关系类型：${command.kind}`);
    }
    if (!rules.credibility.some((rule) => rule.value === command.credibility)) {
      return fail('validation', `未知可信状态：${command.credibility}`);
    }
    if (command.credibility === 'evidenced') {
      return fail('validation', '「有证据」必须至少关联一条有效出处：请先以其他状态新建关系，添加出处后再改为有证据');
    }
    const label = command.label?.trim() || null;
    if (!within(label, LABEL_MAX)) return fail('validation', `关系标签不能超过 ${LABEL_MAX} 字`);
    if (!within(command.note, NOTE_MAX)) return fail('validation', `备注不能超过 ${NOTE_MAX} 字`);
    const attrs = command.attrs ?? {};
    const attrError = validateAttrs(attrs);
    if (attrError) return attrError;
    const duplicate = activeRelations().some((relation) =>
      relation.from_entity_id === from.id && relation.to_entity_id === to.id
      && relation.kind === command.kind);
    if (duplicate) return fail('validation', '这两个主体之间已有同类关系');
    if (command.graph_id) {
      const graph = requireGraph(command.graph_id);
      if (isFailure(graph)) return graph;
    }
    const timestamp = now();
    const relation: CaseRelation = {
      id: newId(), case_id: caseId, from_entity_id: from.id, to_entity_id: to.id,
      directed: command.directed ?? true, kind: command.kind, label,
      credibility: command.credibility, attrs: clone(attrs),
      note: command.note ?? null, created_by: actor, updated_by: actor,
      request_key: command.request_key ?? null, removed_at: null, revision: 0,
      created_at: timestamp, updated_at: timestamp,
    };
    relations.set(relation.id, relation);
    const diff: ChangeDiffEntry[] = [diffOf('relation', null, relation)];
    if (command.graph_id) {
      // 加入已有关系时，两端主体若不在本图则自动纳入（D1）。
      ensureNode(command.graph_id, from.id, diff);
      ensureNode(command.graph_id, to.id, diff);
      ensureEdge(command.graph_id, relation.id, diff);
    }
    recordChange('add_relation', actor, entry, diff, 'graph');
    recordAudit('add_relation', actor, entry, [relation.id], 'graph');
    return ok();
  }

  function updateRelation(command: Extract<GraphCommand, { type: 'update_relation' }>,
                          actor: Actor, entry: Entry): CommandResult {
    const found = requireActiveRelation(command.relation_id);
    if (isFailure(found)) return found;
    const pendingGuard = pendingAdditionGuard('relation', found.id, actor);
    if (pendingGuard) return pendingGuard;
    const stale = cas(found, command.expected_revision, '关系');
    if (stale) return stale;
    const patch = command.changes;
    if (patch.kind !== undefined
        && !rules.relation_kinds.some((kind) => kind.value === patch.kind)) {
      return fail('validation', `未知关系类型：${patch.kind}`);
    }
    if (patch.credibility !== undefined
        && !rules.credibility.some((rule) => rule.value === patch.credibility)) {
      return fail('validation', `未知可信状态：${patch.credibility}`);
    }
    if (patch.label !== undefined && !within(patch.label?.trim() || null, LABEL_MAX)) {
      return fail('validation', `关系标签不能超过 ${LABEL_MAX} 字`);
    }
    if (!within(patch.note, NOTE_MAX)) return fail('validation', `备注不能超过 ${NOTE_MAX} 字`);
    if (patch.attrs !== undefined) {
      const attrError = validateAttrs(patch.attrs);
      if (attrError) return attrError;
    }
    if (patch.credibility === 'evidenced') {
      const confirmed = confirmedSourcesOf({ relation_id: found.id }).length;
      if (confirmed === 0) {
        const pendingSources = activeSourcesOf({ relation_id: found.id }).length;
        if (actor === 'human') {
          return fail('validation', pendingSources > 0
            ? '该关系的出处于待确认状态（Agent 提交）：请先在待确认列表确认该出处，再把可信状态改为「有证据」'
            : '「有证据」必须至少关联一条有效出处：请先在出处中关联材料或片段');
        }
        // Agent 同批提案：允许与已提交的待确认出处一起提议升级，确认时再由 evidencedGuard 校验。
        if (pendingSources === 0) {
          return fail('validation', '「有证据」必须至少关联一条有效出处：请先在出处中关联材料或片段');
        }
      }
    }
    const before = clone(found);
    if (patch.kind !== undefined) found.kind = patch.kind;
    if (patch.label !== undefined) found.label = patch.label?.trim() || null;
    if (patch.credibility !== undefined) found.credibility = patch.credibility;
    if (patch.directed !== undefined) found.directed = patch.directed;
    if (patch.attrs !== undefined) found.attrs = clone(patch.attrs);
    if (patch.note !== undefined) found.note = patch.note;
    found.revision += 1;
    found.updated_by = actor;
    found.updated_at = now();
    recordChange('update_relation', actor, entry, [diffOf('relation', before, found)], 'graph');
    recordAudit('update_relation', actor, entry, [found.id], 'graph');
    return ok();
  }

  function removeRelation(command: Extract<GraphCommand, { type: 'remove_relation' }>,
                          actor: Actor, entry: Entry): CommandResult {
    const found = requireActiveRelation(command.relation_id);
    if (isFailure(found)) return found;
    const stale = cas(found, command.expected_revision, '关系');
    if (stale) return stale;
    const affected = [...graphs.values()].filter((graph) => graph.removed_at === null
      && [...edges.values()].some((edge) => edge.graph_id === graph.id
        && edge.relation_id === found.id));
    const before = clone(found);
    found.removed_at = now();
    found.revision += 1;
    found.updated_by = actor;
    found.updated_at = now();
    recordChange('remove_relation', actor, entry, [diffOf('relation', before, found)], 'graph');
    recordAudit('remove_relation', actor, entry, [found.id], 'graph');
    return ok(`已删除全案关系（软删除），影响 ${affected.length} 张图：`
      + `${affected.map((graph) => graph.title).join('、') || '无'}。可用撤销恢复。`);
  }

  function restoreRelation(command: Extract<GraphCommand, { type: 'restore_relation' }>,
                           actor: Actor, entry: Entry): CommandResult {
    const found = requireRelation(command.relation_id);
    if (isFailure(found)) return found;
    if (found.removed_at === null) return fail('validation', '关系未删除');
    const stale = cas(found, command.expected_revision, '关系');
    if (stale) return stale;
    const from = requireActiveEntity(found.from_entity_id);
    if (isFailure(from)) return fail('validation', '关系起点主体已删除，请先恢复主体');
    const to = requireActiveEntity(found.to_entity_id);
    if (isFailure(to)) return fail('validation', '关系终点主体已删除，请先恢复主体');
    const before = clone(found);
    found.removed_at = null;
    found.revision += 1;
    found.updated_by = actor;
    found.updated_at = now();
    recordChange('restore_relation', actor, entry, [diffOf('relation', before, found)], 'graph');
    recordAudit('restore_relation', actor, entry, [found.id], 'graph');
    return ok();
  }

  function addSource(command: Extract<GraphCommand, { type: 'add_source' }>,
                     actor: Actor, entry: Entry): CommandResult {
    if (actor === 'agent' && !command.request_key) {
      return fail('validation', 'Agent 新增出处必须带 request_key');
    }
    const hasEntity = Boolean(command.entity_id);
    const hasRelation = Boolean(command.relation_id);
    if (hasEntity === hasRelation) return fail('validation', '出处必须且只能挂在一个主体或一条关系上');
    if (!refExists(command.ref)) return fail('not_found', '出处指向的材料或片段不存在');
    if (!within(command.proves, PROVES_MAX)) return fail('validation', `证明要点不能超过 ${PROVES_MAX} 字`);
    if (command.relation_id) {
      const relation = requireActiveRelation(command.relation_id);
      if (isFailure(relation)) return relation;
      const pendingGuard = pendingAdditionGuard('relation', relation.id, actor);
      if (pendingGuard) return pendingGuard;
    }
    if (command.entity_id) {
      const entity = requireActiveEntity(command.entity_id);
      if (isFailure(entity)) return entity;
      const pendingGuard = pendingAdditionGuard('entity', entity.id, actor);
      if (pendingGuard) return pendingGuard;
    }
    const target = { entity_id: command.entity_id, relation_id: command.relation_id };
    const duplicate = activeSourcesOf(target).some((source) => sameRef(source, command.ref));
    // Agent 重复提案（含同一 request_key 重试）按幂等处理，不重复建行也不报错。
    if (duplicate) return actor === 'agent' ? snapshot() : fail('validation', '该出处已关联到此对象');
    const source: GraphSource = {
      id: newId(), case_id: caseId,
      entity_id: command.entity_id ?? null, relation_id: command.relation_id ?? null,
      ...refColumns(command.ref), proves: command.proves ?? null,
      created_by: actor, removed_at: null, created_at: now(),
    };
    sources.set(source.id, source);
    recordChange('add_source', actor, entry, [diffOf('source', null, source)], 'graph');
    recordAudit('add_source', actor, entry, [source.id], 'graph');
    const label = readSnapshot().labels[source.id]?.text ?? '';
    return ok(command.relation_id
      ? `出处已添加（${label}）。如已核实，可把可信状态改为「有证据」。`
      : `出处已添加（${label}）。`);
  }

  function removeSource(command: Extract<GraphCommand, { type: 'remove_source' }>,
                        actor: Actor, entry: Entry): CommandResult {
    const source = sources.get(command.source_id);
    if (!source || source.removed_at !== null) return fail('not_found', '出处不存在');
    if (isPendingAddition('source', source.id)) {
      return fail('conflict', '该出处于 Agent 待确认状态：请先在待确认列表中确认或撤回后再处理');
    }
    const relation = source.relation_id ? relations.get(source.relation_id) : null;
    // 「最后一条出处」只按已确认出处计算：待确认的 Agent 提议不能替代证据。
    const remaining = relation ? confirmedSourcesOf({ relation_id: relation.id })
      .filter((item) => item.id !== source.id).length : 0;
    if (relation && relation.removed_at === null && relation.credibility === 'evidenced'
        && remaining === 0) {
      if (!command.downgrade_to) {
        const pendingCount = activeSourcesOf({ relation_id: relation.id }).length
          - confirmedSourcesOf({ relation_id: relation.id }).length;
        return fail('validation', '这是「有证据」关系最后一条已确认出处：删除前必须同时选择降级后的可信状态'
          + '（待查 / 对方主张 / 已排除），系统不会静默降级'
          + (pendingCount > 0 ? '；若 Agent 提交的待确认出处已核实，也可先在待确认列表确认该出处' : ''));
      }
      if (command.downgrade_to === 'evidenced') {
        return fail('validation', '降级后的可信状态不能仍是「有证据」');
      }
      if (!rules.credibility.some((rule) => rule.value === command.downgrade_to)) {
        return fail('validation', `未知可信状态：${command.downgrade_to}`);
      }
    }
    const diff: ChangeDiffEntry[] = [];
    const before = clone(source);
    source.removed_at = now();
    diff.push(diffOf('source', before, source));
    if (relation && command.downgrade_to && relation.credibility === 'evidenced' && remaining === 0) {
      const relationBefore = clone(relation);
      relation.credibility = command.downgrade_to;
      relation.revision += 1;
      relation.updated_by = actor;
      relation.updated_at = now();
      diff.push(diffOf('relation', relationBefore, relation));
    }
    recordChange('remove_source', actor, entry, diff, 'graph');
    recordAudit('remove_source', actor, entry, [source.id], 'graph');
    return ok(command.downgrade_to && relation
      ? `已删除最后一条出处，关系降级为「${rules.credibility.find((rule) => rule.value === command.downgrade_to)?.label}」`
      : undefined);
  }

  /* ---------------- 图与布局 ---------------- */

  function createGraph(command: Extract<GraphCommand, { type: 'create_graph' }>,
                       actor: Actor, entry: Entry): CommandResult {
    if (actor === 'agent' && !command.request_key) {
      return fail('validation', 'Agent 新建关系图必须带 request_key');
    }
    if (requestKeyExists(graphs, command.request_key)) return snapshot();
    const title = trimmed(command.title);
    if (!title) return fail('validation', '关系图名称不能为空');
    if (title.length > NAME_MAX) return fail('validation', `关系图名称不能超过 ${NAME_MAX} 字`);
    if (!within(command.description, 2000)) return fail('validation', '说明不能超过 2000 字');
    const position = [...graphs.values()].filter((graph) => graph.removed_at === null).length;
    const timestamp = now();
    const graph: RelationGraph = {
      id: newId(), case_id: caseId, title, description: command.description ?? null,
      position, created_by: actor, updated_by: actor,
      request_key: command.request_key ?? null, removed_at: null, revision: 0,
      created_at: timestamp, updated_at: timestamp,
    };
    graphs.set(graph.id, graph);
    recordChange('create_graph', actor, entry, [diffOf('graph', null, graph)], 'graph');
    recordAudit('create_graph', actor, entry, [graph.id], 'graph');
    return ok();
  }

  function updateGraph(command: Extract<GraphCommand, { type: 'update_graph' }>,
                       actor: Actor, entry: Entry): CommandResult {
    const graph = requireGraph(command.graph_id);
    if (isFailure(graph)) return graph;
    const stale = cas(graph, command.expected_revision, '关系图');
    if (stale) return stale;
    if (command.changes.title !== undefined) {
      const title = trimmed(command.changes.title);
      if (!title || title.length > NAME_MAX) return fail('validation', `名称需为 1–${NAME_MAX} 字`);
    }
    if (!within(command.changes.description, 2000)) return fail('validation', '说明不能超过 2000 字');
    const before = clone(graph);
    if (command.changes.title !== undefined) graph.title = trimmed(command.changes.title);
    if (command.changes.description !== undefined) graph.description = command.changes.description;
    graph.revision += 1;
    graph.updated_by = actor;
    graph.updated_at = now();
    recordChange('update_graph', actor, entry, [diffOf('graph', before, graph)], 'graph');
    recordAudit('update_graph', actor, entry, [graph.id], 'graph');
    return ok();
  }

  function removeGraph(command: Extract<GraphCommand, { type: 'remove_graph' }>,
                       actor: Actor, entry: Entry): CommandResult {
    const graph = requireGraph(command.graph_id);
    if (isFailure(graph)) return graph;
    const stale = cas(graph, command.expected_revision, '关系图');
    if (stale) return stale;
    const activeGraphs = [...graphs.values()].filter((item) => item.removed_at === null);
    if (activeGraphs.length <= 1) return fail('validation', '案件至少保留一张关系图');
    const graphSnapshots = [...snapshots.values()].filter((snap) => snap.graph_id === graph.id);
    if (graphSnapshots.length > 0) {
      return fail('validation', `该图已有 ${graphSnapshots.length} 份快照，不能删除（保留历史）；可改名或新建图分专题`);
    }
    const before = clone(graph);
    graph.removed_at = now();
    graph.revision += 1;
    graph.updated_by = actor;
    graph.updated_at = now();
    recordChange('remove_graph', actor, entry, [diffOf('graph', before, graph)], 'graph');
    recordAudit('remove_graph', actor, entry, [graph.id], 'graph');
    return ok();
  }

  function addToGraph(command: Extract<GraphCommand, { type: 'add_to_graph' }>,
                      actor: Actor, entry: Entry): CommandResult {
    const graph = requireGraph(command.graph_id);
    if (isFailure(graph)) return graph;
    const entityIds = command.entity_ids ?? [];
    const relationIds = command.relation_ids ?? [];
    if (entityIds.length === 0 && relationIds.length === 0) {
      return fail('validation', '请选择要加入本图的主体或关系');
    }
    for (const id of entityIds) {
      const entity = requireActiveEntity(id);
      if (isFailure(entity)) return entity;
    }
    for (const id of relationIds) {
      const relation = requireActiveRelation(id);
      if (isFailure(relation)) return relation;
    }
    const diff: ChangeDiffEntry[] = [];
    for (const id of entityIds) ensureNode(graph.id, id, diff);
    for (const id of relationIds) {
      const relation = relations.get(id)!;
      ensureNode(graph.id, relation.from_entity_id, diff);
      ensureNode(graph.id, relation.to_entity_id, diff);
      ensureEdge(graph.id, relation.id, diff);
    }
    if (diff.length === 0) return ok('所选对象已全部在本图中');
    recordChange('add_to_graph', actor, entry, diff, 'graph');
    recordAudit('add_to_graph', actor, entry, [graph.id], 'graph');
    return ok(`已加入本图：${diff.filter((item) => item.target === 'graph_node').length} 个主体、`
      + `${diff.filter((item) => item.target === 'graph_edge').length} 条关系。`);
  }

  function removeFromGraph(command: Extract<GraphCommand, { type: 'remove_from_graph' }>,
                           actor: Actor, entry: Entry): CommandResult {
    const graph = requireGraph(command.graph_id);
    if (isFailure(graph)) return graph;
    if (Boolean(command.entity_id) === Boolean(command.relation_id)) {
      return fail('validation', '请指定要移除的一个主体或一条关系');
    }
    const diff: ChangeDiffEntry[] = [];
    if (command.entity_id) {
      const node = [...nodes.values()].find((item) => item.graph_id === graph.id
        && item.entity_id === command.entity_id);
      if (!node) return fail('not_found', '该主体不在本图中');
      // 仅删除图内成员；案件级主体与关系不受影响（D1）。
      nodes.delete(node.id);
      diff.push(diffOf('graph_node', node, null));
      for (const edge of [...edges.values()].filter((item) => item.graph_id === graph.id)) {
        const relation = relations.get(edge.relation_id);
        if (!relation) continue;
        if (relation.from_entity_id === command.entity_id
            || relation.to_entity_id === command.entity_id) {
          edges.delete(edge.id);
          diff.push(diffOf('graph_edge', edge, null));
        }
      }
    } else {
      const edge = [...edges.values()].find((item) => item.graph_id === graph.id
        && item.relation_id === command.relation_id);
      if (!edge) return fail('not_found', '该关系不在本图中');
      edges.delete(edge.id);
      diff.push(diffOf('graph_edge', edge, null));
    }
    recordChange('remove_from_graph', actor, entry, diff, 'graph');
    recordAudit('remove_from_graph', actor, entry, [graph.id], 'graph');
    return ok('已从本图移除；案件池与其他图不受影响。');
  }

  function saveLayout(command: Extract<GraphCommand, { type: 'save_layout' }>,
                      actor: Actor, entry: Entry): CommandResult {
    const graph = requireGraph(command.graph_id);
    if (isFailure(graph)) return graph;
    const diff: ChangeDiffEntry[] = [];
    for (const placement of command.nodes ?? []) {
      const node = [...nodes.values()].find((item) => item.id === placement.node_id);
      if (!node || node.graph_id !== graph.id) return fail('not_found', '节点不在本图中');
      if (node.x === placement.x && node.y === placement.y) continue;
      const before = clone(node);
      node.x = placement.x;
      node.y = placement.y;
      node.revision += 1;
      node.updated_at = now();
      diff.push(diffOf('graph_node', before, node));
    }
    for (const placement of command.groups ?? []) {
      const group = groups.get(placement.group_id);
      if (!group || group.graph_id !== graph.id || group.removed_at !== null) {
        return fail('not_found', '分组不在本图中');
      }
      const before = clone(group);
      group.x = placement.x;
      group.y = placement.y;
      if (placement.width !== undefined) group.width = placement.width;
      if (placement.height !== undefined) group.height = placement.height;
      group.revision += 1;
      group.updated_at = now();
      diff.push(diffOf('graph_group', before, group));
    }
    if (diff.length === 0) return snapshot();
    recordChange('save_layout', actor, entry, diff, 'graph');
    recordAudit('save_layout', actor, entry, [graph.id], 'graph');
    return snapshot();
  }

  function setNodeGroup(command: Extract<GraphCommand, { type: 'set_node_group' }>,
                        actor: Actor, entry: Entry): CommandResult {
    const graph = requireGraph(command.graph_id);
    if (isFailure(graph)) return graph;
    const entity = requireActiveEntity(command.entity_id);
    if (isFailure(entity)) return entity;
    const node = [...nodes.values()].find((item) => item.graph_id === graph.id
      && item.entity_id === entity.id);
    if (!node) return fail('not_found', '该主体不在本图中');
    let groupId: string | null = null;
    if (command.group_id) {
      const group = groups.get(command.group_id);
      if (!group || group.removed_at !== null || group.graph_id !== graph.id) {
        return fail('not_found', '分组不存在');
      }
      groupId = group.id;
    }
    if (node.group_id === groupId) return snapshot();
    const before = clone(node);
    node.group_id = groupId;
    node.revision += 1;
    node.updated_at = now();
    recordChange('set_node_group', actor, entry, [diffOf('graph_node', before, node)], 'graph');
    recordAudit('set_node_group', actor, entry, [node.id], 'graph');
    return ok(groupId ? '已加入分组。' : '已移出分组。');
  }

  function upsertGraphGroup(command: Extract<GraphCommand, { type: 'upsert_graph_group' }>,
                            actor: Actor, entry: Entry): CommandResult {
    const graph = requireGraph(command.graph_id);
    if (isFailure(graph)) return graph;
    const title = trimmed(command.title);
    if (!title) return fail('validation', '分组名称不能为空');
    if (title.length > GROUP_TITLE_MAX) return fail('validation', `分组名称不能超过 ${GROUP_TITLE_MAX} 字`);
    const diff: ChangeDiffEntry[] = [];
    let group: RelationGraphGroup;
    if (command.group_id) {
      const found = groups.get(command.group_id);
      if (!found || found.graph_id !== graph.id || found.removed_at !== null) {
        return fail('not_found', '分组不存在');
      }
      const before = clone(found);
      found.title = title;
      if (command.color !== undefined) found.color = command.color;
      if (command.x !== undefined) found.x = command.x;
      if (command.y !== undefined) found.y = command.y;
      if (command.width !== undefined) found.width = command.width;
      if (command.height !== undefined) found.height = command.height;
      if (command.collapsed !== undefined) found.collapsed = command.collapsed;
      found.revision += 1;
      found.updated_at = now();
      diff.push(diffOf('graph_group', before, found));
      group = found;
    } else {
      group = {
        id: newId(), case_id: caseId, graph_id: graph.id, title,
        color: command.color ?? null, x: command.x ?? 60, y: command.y ?? 60,
        width: command.width ?? 420, height: command.height ?? 260,
        collapsed: command.collapsed ?? false, revision: 0,
        removed_at: null, created_at: now(), updated_at: now(),
      };
      groups.set(group.id, group);
      diff.push(diffOf('graph_group', null, group));
    }
    for (const entityId of command.entity_ids ?? []) {
      const node = [...nodes.values()].find((item) => item.graph_id === graph.id
        && item.entity_id === entityId);
      if (!node) return fail('not_found', '分组包含的主体不在本图中');
      if (node.group_id === group.id) continue;
      const before = clone(node);
      node.group_id = group.id;
      node.revision += 1;
      node.updated_at = now();
      diff.push(diffOf('graph_node', before, node));
    }
    recordChange('upsert_graph_group', actor, entry, diff, 'graph');
    recordAudit('upsert_graph_group', actor, entry, [group.id], 'graph');
    return ok();
  }

  function removeGraphGroup(command: Extract<GraphCommand, { type: 'remove_graph_group' }>,
                            actor: Actor, entry: Entry): CommandResult {
    const group = groups.get(command.group_id);
    if (!group || group.removed_at !== null) return fail('not_found', '分组不存在');
    const diff: ChangeDiffEntry[] = [];
    for (const node of [...nodes.values()].filter((item) => item.group_id === group.id)) {
      const before = clone(node);
      node.group_id = null;
      node.revision += 1;
      node.updated_at = now();
      diff.push(diffOf('graph_node', before, node));
    }
    const before = clone(group);
    group.removed_at = now();
    group.revision += 1;
    group.updated_at = now();
    diff.push(diffOf('graph_group', before, group));
    recordChange('remove_graph_group', actor, entry, diff, 'graph');
    recordAudit('remove_graph_group', actor, entry, [group.id], 'graph');
    return ok();
  }

  /* ---------------- 快照与客户同意 ---------------- */

  function createSnapshot(command: Extract<GraphCommand, { type: 'create_snapshot' }>,
                          actor: Actor, entry: Entry): CommandResult {
    if (actor !== 'human') return fail('validation', '快照冻结仅限人工入口');
    const graph = requireGraph(command.graph_id);
    if (isFailure(graph)) return graph;
    const stage = trimmed(command.stage_label);
    if (!stage) return fail('validation', '请选择或填写阶段标签');
    if (stage.length > STAGE_MAX) return fail('validation', `阶段标签不能超过 ${STAGE_MAX} 字`);
    if (!within(command.note, SNAPSHOT_NOTE_MAX)) {
      return fail('validation', `说明不能超过 ${SNAPSHOT_NOTE_MAX} 字`);
    }
    if (command.progress_node_id
        && !progressNodes.some((node) => node.value === command.progress_node_id)) {
      return fail('validation', '关联的进度节点不存在');
    }
    const ws = readSnapshot();
    if (isPendingAddition('graph', graph.id)) {
      return fail('conflict', '该关系图是 Agent 待确认新建：请先在待确认列表中确认后再冻结快照');
    }
    const pending = pendingChangesInGraph(ws, graph.id);
    if (pending.length > 0 && !command.acknowledge_pending) {
      return fail('validation', `本图有 ${pending.length} 条 Agent 待确认变更。继续冻结将只包含已确认状态；`
        + '请先复核，或勾选「已知悉，不含待确认变更」后重试');
    }
    const payload: SnapshotPayload = buildSnapshotPayload(ws, graph.id, { excludePending: true });
    // 不变量：快照里的实线（有证据）必须带真实出处，绝不冻结无出处的「有证据」边。
    const unsourced = payload.edges.filter((edge) => edge.credibility === 'evidenced'
      && edge.sources.length === 0);
    if (unsourced.length > 0) {
      return fail('validation', `快照不能包含没有有效出处的「有证据」关系（${unsourced.length} 条）：`
        + '请先确认出处或调整该关系的可信状态后重试');
    }
    const version = Math.max(0, ...[...snapshots.values()]
      .filter((snap) => snap.graph_id === graph.id)
      .map((snap) => snap.version_no)) + 1;
    const record: RelationGraphSnapshot = {
      id: newId(), case_id: caseId, graph_id: graph.id, version_no: version,
      stage_label: stage, note: command.note ?? null,
      progress_node_id: command.progress_node_id ?? null,
      payload, excluded_pending_count: pending.length,
      created_by: 'human', created_at: now(),
    };
    snapshots.set(record.id, record);
    recordChange('create_snapshot', actor, entry, [diffOf('snapshot', null, {
      id: record.id, version_no: version, stage_label: stage,
      excluded_pending_count: pending.length,
    })], 'graph');
    recordAudit('create_snapshot', actor, entry, [record.id], 'graph');
    return ok(`已冻结快照 v${version}「${stage}」${pending.length > 0
      ? `；不含 ${pending.length} 条待确认变更` : ''}。`);
  }

  function restoreSnapshot(command: Extract<GraphCommand, { type: 'restore_snapshot' }>,
                           actor: Actor, entry: Entry): CommandResult {
    if (actor !== 'human') return fail('validation', '快照恢复仅限人工入口');
    const snap = snapshots.get(command.snapshot_id);
    if (!snap) return fail('not_found', '快照不存在');
    const graph = requireGraph(snap.graph_id);
    if (isFailure(graph)) return graph;
    const pendingBeforeRestore = pendingChangesInGraph(readSnapshot(), graph.id);
    if (pendingBeforeRestore.length > 0) {
      return fail('conflict', `本图有 ${pendingBeforeRestore.length} 条 Agent 待确认变更，请先复核（确认或撤回）后再恢复快照`);
    }
    const payload = snap.payload;
    const diff: ChangeDiffEntry[] = [];
    let skipped = 0;
    let restoredSources = 0;

    const restoreSource = (source: SnapshotSourceLike, entityId: string | null, relationId: string | null) => {
      if (!refExists(source.ref)) { skipped += 1; return; }
      const existing = sources.get(source.source_id);
      if (!existing) {
        const created: GraphSource = {
          id: source.source_id, case_id: caseId, entity_id: entityId, relation_id: relationId,
          ...refColumns(source.ref), proves: source.proves, created_by: 'human',
          removed_at: null, created_at: now(),
        };
        sources.set(created.id, created);
        diff.push(diffOf('source', null, created));
        restoredSources += 1;
      } else if (existing.removed_at !== null) {
        const before = clone(existing);
        existing.removed_at = null;
        diff.push(diffOf('source', before, existing));
        restoredSources += 1;
      }
    };

    // 主体与别名：冻结后软删除的主体一并恢复，避免快照悬空。
    const availableEntities = new Set<string>();
    for (const node of payload.nodes) {
      const entity = entities.get(node.entity_id);
      if (!entity) { skipped += 1; continue; }
      availableEntities.add(entity.id);
      if (entity.removed_at !== null) {
        const before = clone(entity);
        entity.removed_at = null;
        entity.revision += 1;
        entity.updated_by = actor;
        entity.updated_at = now();
        diff.push(diffOf('entity', before, entity));
      }
      for (const aliasText of node.aliases) {
        const exists = [...aliases.values()].some((alias) => alias.removed_at === null
          && alias.entity_id === entity.id && alias.alias === aliasText);
        if (exists) continue;
        const alias: EntityAlias = {
          id: newId(), case_id: caseId, entity_id: entity.id, alias: aliasText,
          created_by: 'human', removed_at: null, created_at: now(),
        };
        aliases.set(alias.id, alias);
        diff.push(diffOf('alias', null, alias));
      }
      for (const source of node.sources) restoreSource(source, entity.id, null);
    }

    // 关系属性与出处
    const availableRelations = new Set<string>();
    for (const edge of payload.edges) {
      const relation = relations.get(edge.relation_id);
      if (!relation || !availableEntities.has(edge.from_entity_id)
          || !availableEntities.has(edge.to_entity_id)) {
        skipped += 1;
        continue;
      }
      availableRelations.add(relation.id);
      if (relation.removed_at !== null) {
        const before = clone(relation);
        relation.removed_at = null;
        relation.revision += 1;
        relation.updated_by = actor;
        relation.updated_at = now();
        diff.push(diffOf('relation', before, relation));
      }
      const before = clone(relation);
      const changed = relation.credibility !== edge.credibility || relation.label !== edge.label
        || relation.kind !== edge.kind || relation.directed !== edge.directed
        || JSON.stringify(relation.attrs) !== JSON.stringify(edge.attrs)
        || relation.note !== edge.note;
      if (changed) {
        relation.credibility = edge.credibility;
        relation.label = edge.label;
        relation.kind = edge.kind;
        relation.directed = edge.directed;
        relation.attrs = clone(edge.attrs);
        relation.note = edge.note;
        relation.revision += 1;
        relation.updated_by = actor;
        relation.updated_at = now();
        diff.push(diffOf('relation', before, relation));
      }
      for (const source of edge.sources) restoreSource(source, null, relation.id);
    }

    // 分组
    const availableGroups = new Set<string>();
    for (const group of payload.groups) {
      const existing = groups.get(group.group_id);
      if (!existing) {
        const created: RelationGraphGroup = {
          id: group.group_id, case_id: caseId, graph_id: graph.id, title: group.title,
          color: group.color, x: group.x, y: group.y, width: group.width,
          height: group.height, collapsed: group.collapsed, revision: 0,
          removed_at: null, created_at: now(), updated_at: now(),
        };
        groups.set(created.id, created);
        diff.push(diffOf('graph_group', null, created));
        availableGroups.add(created.id);
        continue;
      }
      if (existing.graph_id !== graph.id || existing.removed_at !== null) {
        if (existing.graph_id !== graph.id) { skipped += 1; continue; }
        const before = clone(existing);
        existing.removed_at = null;
        existing.revision += 1;
        existing.updated_at = now();
        diff.push(diffOf('graph_group', before, existing));
      }
      availableGroups.add(existing.id);
      const before = clone(existing);
      const changed = existing.title !== group.title || existing.color !== group.color
        || existing.x !== group.x || existing.y !== group.y
        || existing.width !== group.width || existing.height !== group.height
        || existing.collapsed !== group.collapsed;
      if (changed) {
        existing.title = group.title;
        existing.color = group.color;
        existing.x = group.x;
        existing.y = group.y;
        existing.width = group.width;
        existing.height = group.height;
        existing.collapsed = group.collapsed;
        existing.revision += 1;
        existing.updated_at = now();
        diff.push(diffOf('graph_group', before, existing));
      }
    }

    // 节点成员与坐标
    const wantedEntities = new Set<string>();
    for (const node of payload.nodes) {
      if (!availableEntities.has(node.entity_id)) continue;
      wantedEntities.add(node.entity_id);
      const existing = [...nodes.values()].find((item) => item.graph_id === graph.id
        && item.entity_id === node.entity_id);
      const groupId = node.group_id && availableGroups.has(node.group_id) ? node.group_id : null;
      if (!existing) {
        const created: RelationGraphNode = {
          id: newId(), case_id: caseId, graph_id: graph.id, entity_id: node.entity_id,
          group_id: groupId, x: node.x, y: node.y, revision: 0,
          created_at: now(), updated_at: now(),
        };
        nodes.set(created.id, created);
        diff.push(diffOf('graph_node', null, created));
        continue;
      }
      if (existing.x !== node.x || existing.y !== node.y || existing.group_id !== groupId) {
        const before = clone(existing);
        existing.x = node.x;
        existing.y = node.y;
        existing.group_id = groupId;
        existing.revision += 1;
        existing.updated_at = now();
        diff.push(diffOf('graph_node', before, existing));
      }
    }
    for (const node of [...nodes.values()].filter((item) => item.graph_id === graph.id)) {
      if (!wantedEntities.has(node.entity_id)) {
        nodes.delete(node.id);
        diff.push(diffOf('graph_node', node, null));
      }
    }

    // 边成员
    const wantedRelations = new Set<string>();
    for (const edge of payload.edges) {
      if (!availableRelations.has(edge.relation_id)) continue;
      const relation = relations.get(edge.relation_id)!;
      if (relation.removed_at !== null) continue;
      if (!wantedEntities.has(relation.from_entity_id) || !wantedEntities.has(relation.to_entity_id)) continue;
      wantedRelations.add(relation.id);
      const existing = [...edges.values()].find((item) => item.graph_id === graph.id
        && item.relation_id === relation.id);
      if (!existing) {
        const created: RelationGraphEdge = {
          id: newId(), case_id: caseId, graph_id: graph.id, relation_id: relation.id,
          created_at: now(),
        };
        edges.set(created.id, created);
        diff.push(diffOf('graph_edge', null, created));
      }
    }
    for (const edge of [...edges.values()].filter((item) => item.graph_id === graph.id)) {
      if (!wantedRelations.has(edge.relation_id)) {
        edges.delete(edge.id);
        diff.push(diffOf('graph_edge', edge, null));
      }
    }

    // 防御：恢复后若「有证据」关系没有任何有效出处（材料已不存在），显式降级并说明。
    let downgraded = 0;
    for (const relation of activeRelations()) {
      if (relation.credibility !== 'evidenced') continue;
      if (activeSourcesOf({ relation_id: relation.id }).length > 0) continue;
      const before = clone(relation);
      relation.credibility = 'suspected';
      relation.revision += 1;
      relation.updated_by = actor;
      relation.updated_at = now();
      diff.push(diffOf('relation', before, relation));
      downgraded += 1;
    }

    // 覆盖语义：冻结后才添加的出处（属于本图主体/关系）一并移除，保证工作稿与快照一致。
    const payloadSourceIds = new Set<string>([
      ...payload.nodes.flatMap((node) => node.sources.map((source) => source.source_id)),
      ...payload.edges.flatMap((edge) => edge.sources.map((source) => source.source_id)),
    ]);
    for (const source of [...sources.values()]) {
      if (source.removed_at !== null) continue;
      const inGraph = (source.entity_id && wantedEntities.has(source.entity_id))
        || (source.relation_id && wantedRelations.has(source.relation_id));
      if (!inGraph || payloadSourceIds.has(source.id)) continue;
      const before = clone(source);
      source.removed_at = now();
      diff.push(diffOf('source', before, source));
    }

    recordChange('restore_snapshot', actor, entry, diff, 'graph');
    recordAudit('restore_snapshot', actor, entry, [snap.id], 'graph');
    return ok(`已恢复快照 v${snap.version_no}「${snap.stage_label}」到工作稿：`
      + `${payload.nodes.length} 个主体、${payload.edges.length} 条关系`
      + (restoredSources > 0 ? `、补回 ${restoredSources} 条出处` : '')
      + (skipped > 0 ? `；跳过 ${skipped} 条已不存在的引用` : '')
      + (downgraded > 0 ? `；${downgraded} 条关系因出处材料不存在已降级为待查` : '')
      + '。可用撤销（⌘Z）回到恢复前。');
  }

  function addSnapshotConsent(command: Extract<GraphCommand, { type: 'add_snapshot_consent' }>,
                              actor: Actor, entry: Entry): CommandResult {
    if (actor !== 'human') return fail('validation', '客户同意登记仅限人工入口');
    const snap = snapshots.get(command.snapshot_id);
    if (!snap) return fail('not_found', '快照不存在');
    const action: ConsentAction = command.action;
    if (!['confirm', 'amend', 'revoke'].includes(action)) return fail('validation', '未知同意动作');
    let parent: SnapshotConsent | null = null;
    if (action === 'amend' || action === 'revoke') {
      if (!command.parent_consent_id) {
        return fail('validation', '更正或撤回必须指向原同意记录');
      }
      parent = consents.get(command.parent_consent_id) ?? null;
      if (!parent || parent.snapshot_id !== snap.id) {
        return fail('validation', '原同意记录不属于该快照');
      }
      if (parent.action === 'revoke') return fail('validation', '已撤回的记录不能再更正或撤回');
    }
    const consenter = trimmed(command.consenter);
    if (!consenter) return fail('validation', '请填写同意人');
    if (consenter.length > NAME_MAX) return fail('validation', `同意人不能超过 ${NAME_MAX} 字`);
    if (!validDateText(command.consented_at)) return fail('validation', '请填写有效的客户同意时间');
    if (!rules.consent_channels.some((channel) => channel.value === command.channel)) {
      return fail('validation', '请选择同意方式');
    }
    const detail = trimmed(command.source_detail);
    if (!detail) return fail('validation', '请填写同意来源说明');
    if (detail.length > CONSENT_DETAIL_MAX) {
      return fail('validation', `来源说明不能超过 ${CONSENT_DETAIL_MAX} 字`);
    }
    if (!within(command.note, CONSENT_NOTE_MAX)) {
      return fail('validation', `备注不能超过 ${CONSENT_NOTE_MAX} 字`);
    }
    if (command.evidence_material_id && !materials.has(command.evidence_material_id)) {
      return fail('not_found', '凭证材料不存在');
    }
    const consent: SnapshotConsent = {
      id: newId(), case_id: caseId, snapshot_id: snap.id, action,
      parent_consent_id: parent?.id ?? null,
      consenter, consented_at: command.consented_at, channel: command.channel,
      source_detail: detail, evidence_material_id: command.evidence_material_id ?? null,
      note: command.note ?? null, created_by: 'human', created_at: now(),
    };
    consents.set(consent.id, consent);
    recordChange('add_snapshot_consent', actor, entry, [diffOf('consent', null, consent)], 'graph');
    recordAudit('add_snapshot_consent', actor, entry, [consent.id], 'graph');
    return ok(action === 'confirm' ? '客户同意已登记（不可修改；如需更正或撤回，请追加记录）。'
      : action === 'amend' ? '已追加更正记录，原记录保留。' : '已追加撤回记录，原记录保留。');
  }

  /* ---------------- Agent 变更复核 ---------------- */

  function acceptChange(command: Extract<GraphCommand, { type: 'accept_change' }>,
                        actor: Actor, entry: Entry): CommandResult {
    if (actor !== 'human') return fail('validation', '确认 Agent 变更仅限人工入口');
    const change = changes.find((item) => item.id === command.change_id);
    if (!change) return fail('not_found', '变更记录不存在');
    if (change.review_state !== 'pending') return fail('validation', '该变更不处于待确认状态');
    if (pendingNeighbour(change, 'earlier')) {
      return fail('conflict', '请先处理更早的待确认变更（按写入顺序复核），再确认本条');
    }
    if (supersededByHuman(change)) {
      return fail('conflict', '该对象的相关字段已被人工修改，Agent 变更已过期：请撤回该变更');
    }
    const evidenceIssue = evidencedGuard([change]);
    if (evidenceIssue) return evidenceIssue;
    change.review_state = 'accepted';
    change.reviewed_at = now();
    if (change.domain === 'graph') seqGraph += 1; else seqEvidence += 1;
    recordAudit('accept_change', actor, entry, [change.id], change.domain);
    return snapshot();
  }

  function acceptAll(command: Extract<GraphCommand, { type: 'accept_all' }>,
                     actor: Actor, entry: Entry): CommandResult {
    if (actor !== 'human') return fail('validation', '确认 Agent 变更仅限人工入口');
    const idsProvided = command.change_ids !== undefined;
    if (idsProvided && (command.change_ids as string[]).length === 0) {
      return fail('validation', '确认范围不能为空：请提交已展示的变更 ID');
    }
    if (!idsProvided && (command.expected_seq === undefined || command.expected_seq === null)) {
      return fail('validation', '一键确认须提交已展示的变更 ID 或操作序号');
    }
    if (command.expected_seq !== undefined && command.expected_seq !== null
        && command.expected_seq !== seqGraph + seqEvidence) {
      return fail('conflict', '待确认列表已变化，请刷新后重新审核');
    }
    let pending = changes.filter((change) => change.review_state === 'pending');
    if (idsProvided) {
      const shown = new Set(command.change_ids as string[]);
      pending = pending.filter((change) => shown.has(change.id));
    }
    if (pending.length === 0) return fail('validation', '没有待确认的 Agent 修改');
    // 已被人工改写覆盖的提案不再确认，逐条提示撤回；同批内按写入顺序整体确认。
    const accepting = pending.filter((change) => !supersededByHuman(change));
    const skipped = pending.length - accepting.length;
    if (accepting.length === 0) {
      return fail('conflict', '待确认变更均已被人工修改覆盖：请逐条撤回后再操作');
    }
    const evidenceIssue = evidencedGuard(accepting);
    if (evidenceIssue) return evidenceIssue;
    const timestamp = now();
    for (const change of accepting) {
      change.review_state = 'accepted';
      change.reviewed_at = timestamp;
    }
    seqGraph += 1;
    recordAudit('accept_all', actor, entry, accepting.map((change) => change.id), 'graph');
    return ok(`已确认 ${accepting.length} 条 Agent 修改。`
      + (skipped > 0 ? `另有 ${skipped} 条已被人工修改覆盖，未确认（请撤回）。` : ''));
  }

  function revertChange(command: Extract<GraphCommand, { type: 'revert_change' }>,
                        actor: Actor, entry: Entry): CommandResult {
    if (actor !== 'human') return fail('validation', '撤回 Agent 变更仅限人工入口');
    const change = changes.find((item) => item.id === command.change_id);
    if (!change) return fail('not_found', '变更记录不存在');
    if (change.review_state !== 'pending' && change.review_state !== 'accepted'
        && change.review_state !== 'none') {
      return fail('validation', '该变更已撤回或不可撤回');
    }
    if (pendingNeighbour(change, 'later')) {
      return fail('conflict', '请先处理更晚的待确认变更（按写入顺序复核），再撤回本条');
    }
    // 人工已改写同一字段：提案过期，只标记撤回，不改动当前工作稿。
    if (supersededByHuman(change)) {
      change.review_state = 'reverted';
      change.reviewed_at = now();
      const record = recordChange('revert_change', actor, entry, [], change.domain);
      change.reverted_by = record.id;
      recordAudit('revert_change', actor, entry, [change.id], change.domain);
      return ok('该变更已被人工修改覆盖，仅标记撤回，当前工作稿未改动。');
    }
    // 先整体核对，任一目标已被后续修改则拒绝，不做半截撤回。
    for (const item of change.diff) {
      const guard = revertGuard(item);
      if (guard) return guard;
    }
    const reverse: ChangeDiffEntry[] = [];
    for (const item of change.diff) {
      const result = revertEntry(item, reverse);
      if (result) return result;
    }
    change.review_state = 'reverted';
    change.reviewed_at = now();
    const record = recordChange('revert_change', actor, entry, reverse, change.domain);
    change.reverted_by = record.id;
    recordAudit('revert_change', actor, entry, [change.id], change.domain);
    return ok();
  }

  function revertGuard(item: ChangeDiffEntry): Failure | null {
    switch (item.target) {
      case 'entity': {
        const current = entities.get(item.id);
        if (!current) return null;
        if (item.before === null) {
          const linked = activeRelations().filter((relation) =>
            relation.from_entity_id === item.id || relation.to_entity_id === item.id);
          if (linked.length > 0) return fail('conflict', '该主体已有有效关系，请先撤回相关关系');
          return null;
        }
        return matchesAfter(current, item) ? null : fail('conflict', '主体已被后续修改，无法撤回，请人工处理');
      }
      case 'relation': {
        const current = relations.get(item.id);
        if (!current) return null;
        if (item.before === null) {
          if (activeSourcesOf({ relation_id: item.id }).length > 0) {
            return fail('conflict', '该关系已有出处，请先撤回出处变更');
          }
          return null;
        }
        if (item.after === null) {
          const from = entities.get(current.from_entity_id);
          const to = entities.get(current.to_entity_id);
          if (!from || from.removed_at !== null || !to || to.removed_at !== null) {
            return fail('conflict', '关系端点已删除，无法撤回该移除');
          }
          return null;
        }
        return matchesAfter(current, item) ? null : fail('conflict', '关系已被后续修改，无法撤回，请人工处理');
      }
      case 'source':
      case 'alias':
      case 'evidence_binding': {
        const map = item.target === 'source' ? sources : item.target === 'alias' ? aliases : evidenceBindings;
        const current = map.get(item.id);
        if (!current) return null;
        if (item.before === null) {
          // 撤回一条出处：若是「有证据」关系仅剩的有效出处，先要求处理关系状态，避免出现无出处实线。
          if (item.target === 'source') {
            const source = current as GraphSource;
            const relation = source.relation_id ? relations.get(source.relation_id) : null;
            if (relation && relation.removed_at === null && relation.credibility === 'evidenced') {
              const remaining = activeSourcesOf({ relation_id: relation.id })
                .filter((entry) => entry.id !== source.id).length;
              if (remaining === 0) {
                return fail('conflict', '该出处是「有证据」关系的唯一有效出处：'
                  + '请先撤回或降低该关系的可信状态，再撤回出处');
              }
            }
          }
          return null;
        }
        return matchesAfter(current as { revision?: number; removed_at: string | null }, item)
          ? null : fail('conflict', '条目已被后续修改，无法撤回，请人工处理');
      }
      case 'evidence_item': {
        const current = evidenceItems.get(item.id);
        if (!current) return null;
        if (item.before === null) {
          if (current.review_state === 'confirmed') {
            return fail('conflict', '该条目已人工确认，不能整体撤回；可改为驳回或删除');
          }
          return null;
        }
        return matchesAfter(current, item) ? null : fail('conflict', '证据条目已被后续修改，无法撤回');
      }
      case 'snapshot':
      case 'consent':
        return fail('validation', '快照与客户同意不可撤回');
      default:
        return null;
    }
  }

  function matchesAfter(current: { revision?: number; removed_at?: string | null },
                        item: ChangeDiffEntry): boolean {
    const after = item.after as Record<string, unknown> | null;
    if (!after) return false;
    if (item.after_rev !== null && current.revision === item.after_rev) return true;
    // revision 已被复核/撤销等动作推进时，退化为「该变更改过的字段是否仍是 after 值」；
    // 人工改写同一字段的情况由 supersededByHuman 单独拦截。
    const fields = changedFields(item.before, item.after);
    if (fields.size === 0) return (current.removed_at ?? null) === (after.removed_at ?? null);
    const record = current as Record<string, unknown>;
    for (const field of fields) {
      if (JSON.stringify(record[field]) !== JSON.stringify(after[field])) return false;
    }
    return true;
  }

  function revertEntry(item: ChangeDiffEntry, reverse: ChangeDiffEntry[]): Failure | null {
    const restore = (record: Record<string, unknown> | null) => record as Record<string, unknown>;
    switch (item.target) {
      case 'entity': {
        const current = entities.get(item.id);
        if (!current) return null;
        if (item.before === null) {
          const before = clone(current);
          current.removed_at = now();
          current.revision += 1;
          current.updated_by = 'human';
          current.updated_at = now();
          reverse.push(diffOf('entity', before, current));
          return null;
        }
        const before = clone(current);
        Object.assign(current, clone(restore(item.before)), {
          revision: current.revision + 1, updated_by: 'human', updated_at: now(),
        });
        reverse.push(diffOf('entity', before, current));
        return null;
      }
      case 'alias': {
        const current = aliases.get(item.id);
        if (!current) {
          if (item.before) {
            const record = clone(restore(item.before)) as unknown as EntityAlias;
            aliases.set(record.id, record);
            reverse.push(diffOf('alias', null, record));
          }
          return null;
        }
        if (item.before === null) {
          aliases.delete(current.id);
          reverse.push(diffOf('alias', current, null));
          return null;
        }
        const before = clone(current);
        Object.assign(current, clone(restore(item.before)));
        reverse.push(diffOf('alias', before, current));
        return null;
      }
      case 'source': {
        const current = sources.get(item.id);
        if (!current) {
          if (item.before) {
            const record = clone(restore(item.before)) as unknown as GraphSource;
            sources.set(record.id, record);
            reverse.push(diffOf('source', null, record));
          }
          return null;
        }
        if (item.before === null) {
          sources.delete(current.id);
          reverse.push(diffOf('source', current, null));
          return null;
        }
        const before = clone(current);
        Object.assign(current, clone(restore(item.before)));
        reverse.push(diffOf('source', before, current));
        return null;
      }
      case 'relation': {
        const current = relations.get(item.id);
        if (!current) return null;
        if (item.before === null) {
          const before = clone(current);
          current.removed_at = now();
          current.revision += 1;
          current.updated_by = 'human';
          current.updated_at = now();
          reverse.push(diffOf('relation', before, current));
          return null;
        }
        const before = clone(current);
        Object.assign(current, clone(restore(item.before)), {
          revision: current.revision + 1, updated_by: 'human', updated_at: now(),
        });
        reverse.push(diffOf('relation', before, current));
        return null;
      }
      case 'graph_node': {
        const current = nodes.get(item.id);
        if (!current) {
          if (item.before) {
            const record = clone(restore(item.before)) as unknown as RelationGraphNode;
            nodes.set(record.id, record);
            reverse.push(diffOf('graph_node', null, record));
          }
          return null;
        }
        if (item.before === null) {
          nodes.delete(current.id);
          reverse.push(diffOf('graph_node', current, null));
          return null;
        }
        const before = clone(current);
        Object.assign(current, clone(restore(item.before)));
        reverse.push(diffOf('graph_node', before, current));
        return null;
      }
      case 'graph_edge': {
        const current = edges.get(item.id);
        if (!current) {
          if (item.before) {
            const record = clone(restore(item.before)) as unknown as RelationGraphEdge;
            edges.set(record.id, record);
            reverse.push(diffOf('graph_edge', null, record));
          }
          return null;
        }
        if (item.before === null) {
          edges.delete(current.id);
          reverse.push(diffOf('graph_edge', current, null));
          return null;
        }
        const before = clone(current);
        Object.assign(current, clone(restore(item.before)));
        reverse.push(diffOf('graph_edge', before, current));
        return null;
      }
      case 'graph_group': {
        const current = groups.get(item.id);
        if (!current) return null;
        if (item.before === null) {
          for (const node of [...nodes.values()].filter((nodeItem) => nodeItem.group_id === current.id)) {
            const before = clone(node);
            node.group_id = null;
            node.revision += 1;
            node.updated_at = now();
            reverse.push(diffOf('graph_node', before, node));
          }
          groups.delete(current.id);
          reverse.push(diffOf('graph_group', current, null));
          return null;
        }
        const before = clone(current);
        Object.assign(current, clone(restore(item.before)));
        reverse.push(diffOf('graph_group', before, current));
        return null;
      }
      case 'graph': {
        const current = graphs.get(item.id);
        if (!current) return null;
        if (item.before === null) {
          const before = clone(current);
          current.removed_at = now();
          current.revision += 1;
          current.updated_at = now();
          reverse.push(diffOf('graph', before, current));
          return null;
        }
        const before = clone(current);
        Object.assign(current, clone(restore(item.before)), {
          revision: current.revision + 1, updated_at: now(),
        });
        reverse.push(diffOf('graph', before, current));
        return null;
      }
      case 'evidence_item': {
        const current = evidenceItems.get(item.id);
        if (!current) return null;
        if (item.before === null) {
          const before = clone(current);
          current.removed_at = now();
          current.revision += 1;
          current.updated_at = now();
          reverse.push(diffOf('evidence_item', before, current));
          return null;
        }
        const before = clone(current);
        Object.assign(current, clone(restore(item.before)), {
          revision: current.revision + 1, updated_at: now(),
        });
        reverse.push(diffOf('evidence_item', before, current));
        return null;
      }
      case 'evidence_binding': {
        const current = evidenceBindings.get(item.id);
        if (!current) {
          if (item.before) {
            const record = clone(restore(item.before)) as unknown as EvidenceBinding;
            evidenceBindings.set(record.id, record);
            reverse.push(diffOf('evidence_binding', null, record));
          }
          return null;
        }
        if (item.before === null) {
          evidenceBindings.delete(current.id);
          reverse.push(diffOf('evidence_binding', current, null));
          return null;
        }
        const before = clone(current);
        Object.assign(current, clone(restore(item.before)));
        reverse.push(diffOf('evidence_binding', before, current));
        return null;
      }
      case 'evidence_group': {
        const current = evidenceGroups.get(item.id);
        if (!current) return null;
        if (item.before === null) {
          const before = clone(current);
          current.removed_at = now();
          current.revision += 1;
          current.updated_at = now();
          reverse.push(diffOf('evidence_group', before, current));
          return null;
        }
        const before = clone(current);
        Object.assign(current, clone(restore(item.before)), {
          revision: current.revision + 1, updated_at: now(),
        });
        reverse.push(diffOf('evidence_group', before, current));
        return null;
      }
      default:
        return fail('validation', '该变更类型不可撤回');
    }
  }

  /* ---------------- 证据目录 ---------------- */

  function activeEvidenceGroups(): EvidenceGroup[] {
    return [...evidenceGroups.values()].filter((group) => group.removed_at === null)
      .sort((a, b) => a.position - b.position || a.id.localeCompare(b.id));
  }

  function itemsOfGroup(groupId: string): EvidenceItem[] {
    return [...evidenceItems.values()].filter((item) => item.group_id === groupId
      && item.removed_at === null)
      .sort((a, b) => a.position - b.position || a.id.localeCompare(b.id));
  }

  function normalizeItems(groupId: string): void {
    itemsOfGroup(groupId).forEach((item, index) => { item.position = index; });
  }

  function addEvidenceGroup(command: Extract<GraphCommand, { type: 'add_evidence_group' }>,
                            actor: Actor, entry: Entry): CommandResult {
    if (actor === 'agent' && !command.request_key) {
      return fail('validation', 'Agent 新建证据分组必须带 request_key');
    }
    if (requestKeyExists(evidenceGroups, command.request_key)) return snapshot();
    const title = trimmed(command.title);
    if (!title) return fail('validation', '分组名称不能为空');
    if (title.length > GROUP_TITLE_MAX) return fail('validation', `分组名称不能超过 ${GROUP_TITLE_MAX} 字`);
    if (!within(command.purpose, 1000)) return fail('validation', '分组说明不能超过 1000 字');
    const group: EvidenceGroup = {
      id: newId(), case_id: caseId,
      position: activeEvidenceGroups().length, title,
      purpose: command.purpose ?? null, created_by: actor,
      request_key: command.request_key ?? null, removed_at: null, revision: 0,
      created_at: now(), updated_at: now(),
    };
    evidenceGroups.set(group.id, group);
    recordChange('add_evidence_group', actor, entry, [diffOf('evidence_group', null, group)], 'evidence');
    recordAudit('add_evidence_group', actor, entry, [group.id], 'evidence');
    return ok();
  }

  function updateEvidenceGroup(command: Extract<GraphCommand, { type: 'update_evidence_group' }>,
                               actor: Actor, entry: Entry): CommandResult {
    const group = evidenceGroups.get(command.group_id);
    if (!group || group.removed_at !== null) return fail('not_found', '证据分组不存在');
    const stale = cas(group, command.expected_revision, '证据分组');
    if (stale) return stale;
    if (command.changes.title !== undefined) {
      const title = trimmed(command.changes.title);
      if (!title || title.length > GROUP_TITLE_MAX) {
        return fail('validation', `分组名称需为 1–${GROUP_TITLE_MAX} 字`);
      }
    }
    if (!within(command.changes.purpose, 1000)) return fail('validation', '分组说明不能超过 1000 字');
    const before = clone(group);
    if (command.changes.title !== undefined) group.title = trimmed(command.changes.title);
    if (command.changes.purpose !== undefined) group.purpose = command.changes.purpose;
    group.revision += 1;
    group.updated_at = now();
    recordChange('update_evidence_group', actor, entry, [diffOf('evidence_group', before, group)], 'evidence');
    recordAudit('update_evidence_group', actor, entry, [group.id], 'evidence');
    return ok();
  }

  function reorderEvidenceGroups(command: Extract<GraphCommand, { type: 'reorder_evidence_groups' }>,
                                 actor: Actor, entry: Entry): CommandResult {
    const current = activeEvidenceGroups();
    const wanted = command.group_ids;
    if (wanted.length !== current.length || new Set(wanted).size !== wanted.length) {
      return fail('validation', '分组顺序必须包含全部有效分组且不重复');
    }
    const currentIds = new Set(current.map((group) => group.id));
    if (wanted.some((id) => !currentIds.has(id))) return fail('validation', '分组顺序包含未知分组');
    const diff: ChangeDiffEntry[] = [];
    wanted.forEach((id, index) => {
      const group = evidenceGroups.get(id)!;
      if (group.position === index) return;
      const before = clone(group);
      group.position = index;
      group.revision += 1;
      group.updated_at = now();
      diff.push(diffOf('evidence_group', before, group));
    });
    if (diff.length === 0) return snapshot();
    recordChange('reorder_evidence_groups', actor, entry, diff, 'evidence');
    recordAudit('reorder_evidence_groups', actor, entry, wanted, 'evidence');
    return ok('分组顺序已更新，编号已重算。');
  }

  function removeEvidenceGroup(command: Extract<GraphCommand, { type: 'remove_evidence_group' }>,
                               actor: Actor, entry: Entry): CommandResult {
    const group = evidenceGroups.get(command.group_id);
    if (!group || group.removed_at !== null) return fail('not_found', '证据分组不存在');
    const stale = cas(group, command.expected_revision, '证据分组');
    if (stale) return stale;
    if (itemsOfGroup(group.id).length > 0) {
      return fail('validation', '请先移除或移动到其他分组，再删除分组');
    }
    const before = clone(group);
    group.removed_at = now();
    group.revision += 1;
    group.updated_at = now();
    recordChange('remove_evidence_group', actor, entry, [diffOf('evidence_group', before, group)], 'evidence');
    recordAudit('remove_evidence_group', actor, entry, [group.id], 'evidence');
    return ok();
  }

  function addEvidenceItem(command: Extract<GraphCommand, { type: 'add_evidence_item' }>,
                           actor: Actor, entry: Entry): CommandResult {
    if (actor === 'agent' && !command.request_key) {
      return fail('validation', 'Agent 新建证据条目必须带 request_key');
    }
    if (requestKeyExists(evidenceItems, command.request_key)) return snapshot();
    const group = evidenceGroups.get(command.group_id);
    if (!group || group.removed_at !== null) return fail('not_found', '证据分组不存在');
    const title = trimmed(command.title);
    if (!title) return fail('validation', '证据名称不能为空');
    if (title.length > ITEM_TITLE_MAX) return fail('validation', `证据名称不能超过 ${ITEM_TITLE_MAX} 字`);
    if (!within(command.purpose, ITEM_PURPOSE_MAX)) {
      return fail('validation', `证明目的不能超过 ${ITEM_PURPOSE_MAX} 字`);
    }
    if (command.import_material_id && !materials.has(command.import_material_id)) {
      return fail('not_found', '导入来源材料不存在');
    }
    const item: EvidenceItem = {
      id: newId(), case_id: caseId, group_id: group.id,
      position: itemsOfGroup(group.id).length, title,
      purpose: command.purpose ?? null,
      source_kind: command.source_kind ?? 'manual',
      import_material_id: command.import_material_id ?? null,
      review_state: actor === 'agent' ? 'pending' : 'confirmed',
      created_by: actor, request_key: command.request_key ?? null,
      removed_at: null, revision: 0, created_at: now(), updated_at: now(),
    };
    evidenceItems.set(item.id, item);
    recordChange('add_evidence_item', actor, entry, [diffOf('evidence_item', null, item)], 'evidence');
    recordAudit('add_evidence_item', actor, entry, [item.id], 'evidence');
    return ok(actor === 'agent' ? '已提交证据条目草稿，待人工确认后参与编号。' : undefined);
  }

  function updateEvidenceItem(command: Extract<GraphCommand, { type: 'update_evidence_item' }>,
                              actor: Actor, entry: Entry): CommandResult {
    const item = evidenceItems.get(command.item_id);
    if (!item || item.removed_at !== null) return fail('not_found', '证据条目不存在');
    const stale = cas(item, command.expected_revision, '证据条目');
    if (stale) return stale;
    if (command.changes.title !== undefined) {
      const title = trimmed(command.changes.title);
      if (!title || title.length > ITEM_TITLE_MAX) {
        return fail('validation', `证据名称需为 1–${ITEM_TITLE_MAX} 字`);
      }
    }
    if (!within(command.changes.purpose, ITEM_PURPOSE_MAX)) {
      return fail('validation', `证明目的不能超过 ${ITEM_PURPOSE_MAX} 字`);
    }
    const before = clone(item);
    if (command.changes.title !== undefined) item.title = trimmed(command.changes.title);
    if (command.changes.purpose !== undefined) item.purpose = command.changes.purpose;
    if (actor === 'agent') item.review_state = 'pending';
    item.revision += 1;
    item.updated_at = now();
    recordChange('update_evidence_item', actor, entry, [diffOf('evidence_item', before, item)], 'evidence');
    recordAudit('update_evidence_item', actor, entry, [item.id], 'evidence');
    return ok();
  }

  function moveEvidenceItem(command: Extract<GraphCommand, { type: 'move_evidence_item' }>,
                            actor: Actor, entry: Entry): CommandResult {
    const item = evidenceItems.get(command.item_id);
    if (!item || item.removed_at !== null) return fail('not_found', '证据条目不存在');
    const target = evidenceGroups.get(command.to_group_id);
    if (!target || target.removed_at !== null) return fail('not_found', '目标证据分组不存在');
    if (command.before_item_id) {
      const anchor = evidenceItems.get(command.before_item_id);
      if (!anchor || anchor.removed_at !== null || anchor.group_id !== target.id) {
        return fail('validation', '参照条目不在目标分组');
      }
      if (anchor.id === item.id) return snapshot();
    }
    const oldGroupId = item.group_id;
    // 记录受影响条目（原组 + 目标组）的移动前状态，按最终 DOM 顺序整体归一化。
    const affected = new Map<string, EvidenceItem>();
    for (const record of evidenceItems.values()) {
      if (record.removed_at !== null) continue;
      if (record.group_id === oldGroupId || record.group_id === target.id) {
        affected.set(record.id, clone(record));
      }
    }
    const ordered = itemsOfGroup(target.id).filter((entryItem) => entryItem.id !== item.id);
    let index = ordered.length;
    if (command.before_item_id) {
      const foundIndex = ordered.findIndex((entryItem) => entryItem.id === command.before_item_id);
      index = foundIndex >= 0 ? foundIndex : ordered.length;
    }
    ordered.splice(index, 0, item);
    item.group_id = target.id;
    ordered.forEach((entryItem, position) => { entryItem.position = position; });
    normalizeItems(oldGroupId);
    const diff: ChangeDiffEntry[] = [];
    for (const [id, before] of affected) {
      const after = evidenceItems.get(id);
      if (!after) continue;
      if (after.group_id === before.group_id && after.position === before.position) continue;
      after.revision += 1;
      after.updated_at = now();
      diff.push(diffOf('evidence_item', before, after));
    }
    recordChange('move_evidence_item', actor, entry, diff, 'evidence');
    recordAudit('move_evidence_item', actor, entry, [item.id], 'evidence');
    return ok('条目已移动，编号已按新顺序重算。');
  }

  function removeEvidenceItem(command: Extract<GraphCommand, { type: 'remove_evidence_item' }>,
                              actor: Actor, entry: Entry): CommandResult {
    const item = evidenceItems.get(command.item_id);
    if (!item || item.removed_at !== null) return fail('not_found', '证据条目不存在');
    const stale = cas(item, command.expected_revision, '证据条目');
    if (stale) return stale;
    const diff: ChangeDiffEntry[] = [];
    const before = clone(item);
    item.removed_at = now();
    item.revision += 1;
    item.updated_at = now();
    diff.push(diffOf('evidence_item', before, item));
    for (const binding of [...evidenceBindings.values()].filter((entryBinding) =>
      entryBinding.item_id === item.id && entryBinding.removed_at === null)) {
      const bindingBefore = clone(binding);
      binding.removed_at = now();
      diff.push(diffOf('evidence_binding', bindingBefore, binding));
    }
    normalizeItems(item.group_id);
    recordChange('remove_evidence_item', actor, entry, diff, 'evidence');
    recordAudit('remove_evidence_item', actor, entry, [item.id], 'evidence');
    return ok('条目已删除，编号已重算。');
  }

  function addEvidenceBinding(command: Extract<GraphCommand, { type: 'add_evidence_binding' }>,
                              actor: Actor, entry: Entry): CommandResult {
    if (actor === 'agent' && !command.request_key) {
      return fail('validation', 'Agent 提交绑定建议必须带 request_key');
    }
    const item = evidenceItems.get(command.item_id);
    if (!item || item.removed_at !== null) return fail('not_found', '证据条目不存在');
    const material = materials.get(command.material_id);
    if (!material) return fail('not_found', '材料不存在');
    const pageStart = command.page_start ?? null;
    const pageEnd = command.page_end ?? null;
    if (pageStart !== null && (!Number.isInteger(pageStart) || pageStart < 1)) {
      return fail('validation', '起始页码必须 ≥ 1');
    }
    if (pageEnd !== null && (!Number.isInteger(pageEnd) || pageEnd < (pageStart ?? 1))) {
      return fail('validation', '结束页码必须 ≥ 起始页码');
    }
    if (pageEnd !== null && pageEnd > material.page_count) {
      return fail('validation', `页码超出材料范围（共 ${material.page_count} 页）`);
    }
    const duplicate = [...evidenceBindings.values()].some((binding) =>
      binding.removed_at === null && binding.item_id === item.id
      && binding.material_id === material.id && binding.page_start === pageStart
      && binding.page_end === pageEnd);
    if (duplicate) return fail('validation', '该材料与页码范围已绑定到此条目');
    const binding: EvidenceBinding = {
      id: newId(), case_id: caseId, item_id: item.id, material_id: material.id,
      page_start: pageStart, page_end: pageEnd,
      review_state: actor === 'agent' ? 'pending' : 'confirmed',
      created_by: actor, removed_at: null, created_at: now(),
    };
    evidenceBindings.set(binding.id, binding);
    recordChange('add_evidence_binding', actor, entry, [diffOf('evidence_binding', null, binding)], 'evidence');
    recordAudit('add_evidence_binding', actor, entry, [binding.id], 'evidence');
    return ok(actor === 'agent' ? '绑定建议已提交，待人工确认后参与编号。' : undefined);
  }

  function removeEvidenceBinding(command: Extract<GraphCommand, { type: 'remove_evidence_binding' }>,
                                 actor: Actor, entry: Entry): CommandResult {
    const binding = evidenceBindings.get(command.binding_id);
    if (!binding || binding.removed_at !== null) return fail('not_found', '绑定不存在');
    const before = clone(binding);
    binding.removed_at = now();
    recordChange('remove_evidence_binding', actor, entry, [diffOf('evidence_binding', before, binding)], 'evidence');
    recordAudit('remove_evidence_binding', actor, entry, [binding.id], 'evidence');
    return ok('绑定已解除，编号已重算。');
  }

  function setEvidenceReview(command: Extract<GraphCommand, { type: 'confirm_evidence_item' | 'reject_evidence_item' }>,
                             actor: Actor, entry: Entry, review: EvidenceReview): CommandResult {
    if (actor !== 'human') return fail('validation', '证据复核仅限人工入口');
    const item = evidenceItems.get(command.item_id);
    if (!item || item.removed_at !== null) return fail('not_found', '证据条目不存在');
    const stale = cas(item, command.expected_revision, '证据条目');
    if (stale) return stale;
    const before = clone(item);
    item.review_state = review;
    item.revision += 1;
    item.updated_at = now();
    recordChange(review === 'confirmed' ? 'confirm_evidence_item' : 'reject_evidence_item',
      actor, entry, [diffOf('evidence_item', before, item)], 'evidence');
    recordAudit(review === 'confirmed' ? 'confirm_evidence_item' : 'reject_evidence_item',
      actor, entry, [item.id], 'evidence');
    return ok(review === 'confirmed'
      ? '条目已确认，参与现行编号。'
      : '条目已驳回，不参与编号（记录保留）。');
  }

  function setBindingReview(command: Extract<GraphCommand, { type: 'confirm_evidence_binding' | 'reject_evidence_binding' }>,
                            actor: Actor, entry: Entry, review: EvidenceReview): CommandResult {
    if (actor !== 'human') return fail('validation', '证据复核仅限人工入口');
    const binding = evidenceBindings.get(command.binding_id);
    if (!binding || binding.removed_at !== null) return fail('not_found', '绑定不存在');
    const before = clone(binding);
    binding.review_state = review;
    recordChange(review === 'confirmed' ? 'confirm_evidence_binding' : 'reject_evidence_binding',
      actor, entry, [diffOf('evidence_binding', before, binding)], 'evidence');
    recordAudit(review === 'confirmed' ? 'confirm_evidence_binding' : 'reject_evidence_binding',
      actor, entry, [binding.id], 'evidence');
    return ok(review === 'confirmed'
      ? '绑定已确认，编号可能随之变化。'
      : '绑定已驳回，不参与编号。');
  }

  function importEvidenceCatalog(command: Extract<GraphCommand, { type: 'import_evidence_catalog' }>,
                                 actor: Actor, entry: Entry): CommandResult {
    if (actor !== 'agent') return fail('validation', '外部目录导入由 Agent 解析后提交');
    if (requestKeyExists(evidenceGroups, command.request_key)) return snapshot();
    const diff: ChangeDiffEntry[] = [];
    const group: EvidenceGroup = {
      id: newId(), case_id: caseId, position: activeEvidenceGroups().length,
      title: '外部导入（待确认）',
      purpose: '由外部 Word / Excel 证据目录解析生成；人工确认条目与绑定后才参与编号。',
      created_by: actor, request_key: command.request_key ?? null,
      removed_at: null, revision: 0, created_at: now(), updated_at: now(),
    };
    evidenceGroups.set(group.id, group);
    diff.push(diffOf('evidence_group', null, group));
    const drafts: { title: string; purpose: string; material_id: string; page_start: number; page_end: number }[] = [
      {
        title: 'TEST-ONLY 补充协议（外部目录第 12 项）',
        purpose: '证明王五的代理权限及签署时间',
        material_id: 'mat-supplement', page_start: 1, page_end: 1,
      },
      {
        title: 'TEST-ONLY 微信记录（外部目录第 13 项）',
        purpose: '证明被告确认欠款金额',
        material_id: 'mat-chat', page_start: 2, page_end: 2,
      },
    ];
    drafts.forEach((draft, index) => {
      if (!materials.has(draft.material_id)) return;
      const item: EvidenceItem = {
        id: newId(), case_id: caseId, group_id: group.id, position: index,
        title: draft.title, purpose: draft.purpose, source_kind: 'imported',
        import_material_id: draft.material_id, review_state: 'pending',
        created_by: 'agent', request_key: null, removed_at: null, revision: 0,
        created_at: now(), updated_at: now(),
      };
      evidenceItems.set(item.id, item);
      diff.push(diffOf('evidence_item', null, item));
      const binding: EvidenceBinding = {
        id: newId(), case_id: caseId, item_id: item.id, material_id: draft.material_id,
        page_start: draft.page_start, page_end: draft.page_end,
        review_state: 'pending', created_by: 'agent', removed_at: null, created_at: now(),
      };
      evidenceBindings.set(binding.id, binding);
      diff.push(diffOf('evidence_binding', null, binding));
    });
    recordChange('import_evidence_catalog', actor, entry, diff, 'evidence');
    recordAudit('import_evidence_catalog', actor, entry, [group.id], 'evidence');
    return ok('外部目录已解析为草稿：2 条证据与绑定建议待人工确认，确认前不参与编号。');
  }

  function acceptEvidenceChange(command: Extract<GraphCommand, { type: 'accept_evidence_change' }>,
                                actor: Actor, entry: Entry): CommandResult {
    if (actor !== 'human') return fail('validation', '证据复核仅限人工入口');
    const change = changes.find((item) => item.id === command.change_id);
    if (!change || change.domain !== 'evidence') return fail('not_found', '证据变更不存在');
    if (change.review_state !== 'pending') return fail('validation', '该变更不处于待确认状态');
    const diff: ChangeDiffEntry[] = [];
    for (const item of change.diff) {
      if (item.target === 'evidence_item' && item.after) {
        const record = evidenceItems.get(item.id);
        if (record && record.review_state === 'pending') {
          const before = clone(record);
          record.review_state = 'confirmed';
          record.revision += 1;
          record.updated_at = now();
          diff.push(diffOf('evidence_item', before, record));
        }
      }
      if (item.target === 'evidence_binding' && item.after) {
        const record = evidenceBindings.get(item.id);
        if (record && record.review_state === 'pending') {
          const before = clone(record);
          record.review_state = 'confirmed';
          diff.push(diffOf('evidence_binding', before, record));
        }
      }
    }
    change.review_state = 'accepted';
    change.reviewed_at = now();
    seqEvidence += 1;
    recordAudit('accept_evidence_change', actor, entry, [change.id], 'evidence');
    return ok('导入草稿已全部确认，参与现行编号。');
  }

  function revertEvidenceChange(command: Extract<GraphCommand, { type: 'revert_evidence_change' }>,
                                actor: Actor, entry: Entry): CommandResult {
    if (actor !== 'human') return fail('validation', '证据复核仅限人工入口');
    const change = changes.find((item) => item.id === command.change_id);
    if (!change || change.domain !== 'evidence') return fail('not_found', '证据变更不存在');
    if (change.review_state !== 'pending') return fail('validation', '该变更不处于待确认状态');
    let removed = 0;
    for (const item of change.diff) {
      if (item.target === 'evidence_binding' && item.before === null) {
        const binding = evidenceBindings.get(item.id);
        if (binding && binding.review_state === 'pending' && binding.removed_at === null) {
          binding.removed_at = now();
          removed += 1;
        }
      }
      if (item.target === 'evidence_item' && item.before === null) {
        const record = evidenceItems.get(item.id);
        if (record && record.review_state === 'pending' && record.removed_at === null) {
          record.removed_at = now();
          record.revision += 1;
          record.updated_at = now();
          removed += 1;
        }
      }
    }
    change.review_state = 'reverted';
    change.reviewed_at = now();
    seqEvidence += 1;
    recordAudit('revert_evidence_change', actor, entry, [change.id], 'evidence');
    return ok(`已撤回导入草稿（${removed} 项）；已人工确认的条目保留。`);
  }

  /* ---------------- 分发 ---------------- */

  function dispatch(command: GraphCommand, actor: Actor, entry: Entry): CommandResult {
    switch (command.type) {
      case 'add_entity': return addEntity(command, actor, entry);
      case 'update_entity': return updateEntity(command, actor, entry);
      case 'remove_entity': return removeEntity(command, actor, entry);
      case 'restore_entity': return restoreEntity(command, actor, entry);
      case 'add_alias': return addAlias(command, actor, entry);
      case 'remove_alias': return removeAlias(command, actor, entry);
      case 'add_relation': return addRelation(command, actor, entry);
      case 'update_relation': return updateRelation(command, actor, entry);
      case 'remove_relation': return removeRelation(command, actor, entry);
      case 'restore_relation': return restoreRelation(command, actor, entry);
      case 'add_source': return addSource(command, actor, entry);
      case 'remove_source': return removeSource(command, actor, entry);
      case 'create_graph': return createGraph(command, actor, entry);
      case 'update_graph': return updateGraph(command, actor, entry);
      case 'remove_graph': return removeGraph(command, actor, entry);
      case 'add_to_graph': return addToGraph(command, actor, entry);
      case 'remove_from_graph': return removeFromGraph(command, actor, entry);
      case 'save_layout': return saveLayout(command, actor, entry);
      case 'set_node_group': return setNodeGroup(command, actor, entry);
      case 'upsert_graph_group': return upsertGraphGroup(command, actor, entry);
      case 'remove_graph_group': return removeGraphGroup(command, actor, entry);
      case 'create_snapshot': return createSnapshot(command, actor, entry);
      case 'restore_snapshot': return restoreSnapshot(command, actor, entry);
      case 'add_snapshot_consent': return addSnapshotConsent(command, actor, entry);
      case 'accept_change': return acceptChange(command, actor, entry);
      case 'accept_all': return acceptAll(command, actor, entry);
      case 'revert_change': return revertChange(command, actor, entry);
      case 'add_evidence_group': return addEvidenceGroup(command, actor, entry);
      case 'update_evidence_group': return updateEvidenceGroup(command, actor, entry);
      case 'reorder_evidence_groups': return reorderEvidenceGroups(command, actor, entry);
      case 'remove_evidence_group': return removeEvidenceGroup(command, actor, entry);
      case 'add_evidence_item': return addEvidenceItem(command, actor, entry);
      case 'update_evidence_item': return updateEvidenceItem(command, actor, entry);
      case 'move_evidence_item': return moveEvidenceItem(command, actor, entry);
      case 'remove_evidence_item': return removeEvidenceItem(command, actor, entry);
      case 'add_evidence_binding': return addEvidenceBinding(command, actor, entry);
      case 'remove_evidence_binding': return removeEvidenceBinding(command, actor, entry);
      case 'confirm_evidence_item': return setEvidenceReview(command, actor, entry, 'confirmed');
      case 'reject_evidence_item': return setEvidenceReview(command, actor, entry, 'rejected');
      case 'confirm_evidence_binding': return setBindingReview(command, actor, entry, 'confirmed');
      case 'reject_evidence_binding': return setBindingReview(command, actor, entry, 'rejected');
      case 'import_evidence_catalog': return importEvidenceCatalog(command, actor, entry);
      case 'accept_evidence_change': return acceptEvidenceChange(command, actor, entry);
      case 'revert_evidence_change': return revertEvidenceChange(command, actor, entry);
    }
  }

  function execute(command: GraphCommand, actor: Actor, entry: Entry): CommandResult {
    if (archived) {
      return fail('read_only', '案件已归档，关系图与证据目录为只读；查看与导出不受影响');
    }
    if (actor === 'agent') {
      if (HUMAN_ONLY.has(command.type) || !rules.agent_commands.includes(command.type)) {
        return fail('validation', `Agent 无权执行命令：${command.type}`);
      }
    }
    lastChangeId = null;
    const undoable = actor === 'human' && !NON_UNDOABLE.has(command.type);
    const data = undoable ? capture() : null;
    const seqs = { graph: seqGraph, evidence: seqEvidence };
    let result: CommandResult;
    try {
      result = dispatch(command, actor, entry);
    } catch (error) {
      result = fail('validation', `命令执行失败：${error instanceof Error ? error.message : String(error)}`);
    }
    if (result.ok && undoable && data && lastChangeId) {
      pushUndo(command, data, lastChangeId, seqs);
    }
    return result;
  }

  return {
    getSnapshot: readSnapshot,
    getRules: () => clone(rules),
    getSeqs: () => ({ graph: seqGraph, evidence: seqEvidence }),
    human: (command) => execute(command, 'human', 'prototype'),
    agent: (command) => execute(command, 'agent', 'prototype'),
    undo() {
      const entry = undoStack[undoStack.length - 1];
      if (!entry) return fail('validation', '没有可撤销的操作');
      const newerPending = changes.some((change) => change.review_state === 'pending'
        && ((change.domain === 'graph' && change.seq > entry.seqs.graph)
          || (change.domain === 'evidence' && change.seq > entry.seqs.evidence)));
      if (newerPending) {
        return fail('conflict', '存在更新的 Agent 待确认修改，请先复核后再撤销');
      }
      applyData(entry.data);
      undoStack.pop();
      const change = recordChange('undo', 'human', 'prototype', [], 'graph');
      const original = changes.find((item) => item.id === entry.changeId);
      if (original) original.reverted_by = change.id;
      recordAudit('undo', 'human', 'prototype', [entry.changeId], 'graph');
      return { ok: true, snapshot: readSnapshot(), notice: `已撤销：${entry.label}`, last_change_id: change.id };
    },
    getImpact(target) {
      const result = [...graphs.values()].filter((graph) => graph.removed_at === null
        && (target.entity_id
          ? [...nodes.values()].some((node) => node.graph_id === graph.id
            && node.entity_id === target.entity_id)
          : target.relation_id
            ? [...edges.values()].some((edge) => edge.graph_id === graph.id
              && edge.relation_id === target.relation_id)
            : false));
      return { graphs: result.map((graph) => ({ id: graph.id, title: graph.title })) };
    },
    setArchived(value) {
      archived = value;
    },
    auditLog: () => clone(audit),
  };
}

/** 恢复快照时使用的出处形状（避免与 SnapshotSource 名称循环依赖）。 */
type SnapshotSourceLike = {
  source_id: string;
  ref: SourceRef;
  proves: string | null;
  label: string;
};
