/**
 * 画布模型与快照载荷（纯函数）。
 * - 工作稿画布：案件池 + 本图成员 + 现行证据编号，Agent 待确认条目带 pending 标记；
 * - 快照载荷：冻结时按「已确认状态」序列化（D3）：从当前记录倒序撤销待确认字段，
 *   叠加的多次待确认回退到最早确认值，人工后续改写保持人工值；新增/别名/出处/分图名同样排除；
 * - 快照对比：按 entity_id / relation_id 比对两份载荷，标出新增、删除、状态与布局变化。
 */
import type {
  CanvasEdge, CanvasGroup, CanvasModel, CanvasNode, CaseEntity, CaseRelation,
  ChangeDiffEntry, DiffTarget, EntityAlias, EvidenceBinding, EvidenceItem, EvidenceGroup,
  GraphChange, GraphRules, GraphSnapshot, GraphSource,  RelationGraph, RelationGraphEdge,
  RelationGraphGroup, RelationGraphNode, RelationAttrs, ResolvedLabel, SnapshotEntityNode, SnapshotGroup,
  SnapshotPayload, SnapshotRelationEdge, SnapshotSource,
} from './types';

const active = <T extends { removed_at: string | null }>(list: T[]): T[] =>
  list.filter((record) => record.removed_at === null);

function clone<T>(value: T): T {
  return structuredClone(value);
}

function sortByPosition<T extends { position: number; id: string }>(list: T[]): T[] {
  return [...list].sort((a, b) => a.position - b.position || a.id.localeCompare(b.id));
}

/* ---------------- Agent 待确认叠加（快照取「已确认状态」） ---------------- */

interface PendingEntry {
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

type PendingMap = Map<string, PendingEntry[]>;

/** 记录元数据不参与字段级回退（revision 等由写入命令维护）。 */
const META_FIELDS = new Set(['revision', 'updated_at', 'updated_by', 'created_at']);

function pendingEntries(changes: GraphChange[]): PendingMap {
  const map: PendingMap = new Map();
  for (const change of changes) {
    if (change.review_state !== 'pending' || change.actor !== 'agent') continue;
    for (const entry of change.diff) {
      if (!isGraphScopeTarget(entry.target)) continue;
      const key = `${entry.target}:${entry.id}`;
      const list = map.get(key) ?? [];
      // 保持写入顺序：同一对象的多次待确认按时间先后排列。
      list.push({ before: entry.before, after: entry.after });
      map.set(key, list);
    }
  }
  return map;
}

const GRAPH_SCOPE: DiffTarget[] = ['entity', 'alias', 'relation', 'source',
  'graph', 'graph_node', 'graph_edge', 'graph_group'];

function isGraphScopeTarget(target: DiffTarget): boolean {
  return GRAPH_SCOPE.includes(target);
}

function sameValue(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (left === null || right === null || left === undefined || right === undefined) return false;
  return JSON.stringify(left) === JSON.stringify(right);
}

/**
 * 从当前记录反向撤销待确认字段，得到「已确认状态」。
 * - 倒序逐条撤销，字段只在当前值仍等于该条 after 时回退，避免把人工后续改写覆盖回旧值；
 * - 连续叠加（董事 → 待确认A → 待确认B）最终回退到最早状态「董事」；
 * - 人工改写过的字段保持人工值，不会被待确认的 before 拉回。
 */
function confirmedFields(record: Record<string, unknown>, entries: PendingEntry[]): Record<string, unknown> {
  const result = { ...record };
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const { before, after } = entries[index];
    if (!before || !after) continue;
    const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
    for (const key of keys) {
      if (META_FIELDS.has(key)) continue;
      if (sameValue(before[key], after[key])) continue;
      if (!sameValue(result[key], after[key])) continue;
      result[key] = clone(before[key]);
    }
  }
  return result;
}

/** 单条记录的已确认状态；null 表示该对象只存在于待确认新增里。 */
function confirmedRecord<T>(record: T, entries: PendingEntry[] | undefined): T | null {
  if (!entries || entries.length === 0) return record;
  const first = entries[0];
  if (first.before === null) return null;
  const removal = entries.find((entry) => entry.after === null);
  if (removal?.before) return clone(removal.before) as unknown as T;
  return confirmedFields(record as Record<string, unknown>, entries) as unknown as T;
}

/** 过滤软删除并取已确认状态（主体/关系/别名/出处/分组）。 */
function confirmedList<T extends { id: string; removed_at?: string | null }>(
  list: T[], target: DiffTarget, pending: PendingMap,
): T[] {
  const result: T[] = [];
  for (const record of list) {
    const base = confirmedRecord(record, pending.get(`${target}:${record.id}`));
    if (!base) continue;
    if (base.removed_at !== undefined && base.removed_at !== null) continue;
    result.push(base);
  }
  return result;
}

/** 被 pending 变更影响、且属于指定图的变更条数（冻结前提示用）。 */
export function pendingChangesInGraph(ws: GraphSnapshot, graphId: string): GraphChange[] {
  const nodeEntityIds = new Set(ws.nodes.filter((node) => node.graph_id === graphId)
    .map((node) => node.entity_id));
  const edgeRelationIds = new Set(ws.edges.filter((edge) => edge.graph_id === graphId)
    .map((edge) => edge.relation_id));
  const sourceIds = new Set(ws.sources.filter((source) =>
    (source.entity_id && nodeEntityIds.has(source.entity_id))
    || (source.relation_id && edgeRelationIds.has(source.relation_id)))
    .map((source) => source.id));
  const graphMaterialIds = new Set<string>();
  for (const source of ws.sources) {
    if (!sourceIds.has(source.id)) continue;
    const label = ws.labels[source.id];
    if (label?.material_id) graphMaterialIds.add(label.material_id);
  }
  return ws.pending_changes.filter((change) => {
    if (change.review_state !== 'pending') return false;
    return change.diff.some((entry) => {
      const record = (entry.after ?? entry.before) as Record<string, unknown> | null;
      if (entry.target === 'entity') return nodeEntityIds.has(entry.id);
      if (entry.target === 'relation') return edgeRelationIds.has(entry.id);
      if (entry.target === 'source') return sourceIds.has(entry.id);
      if (entry.target === 'graph') return entry.id === graphId;
      if (entry.target === 'alias') return nodeEntityIds.has(String(record?.entity_id ?? ''));
      if (entry.target === 'graph_node' || entry.target === 'graph_edge'
          || entry.target === 'graph_group') {
        return record?.graph_id === graphId;
      }
      if (entry.target === 'evidence_binding') {
        const material = record as { material_id?: string } | null;
        return Boolean(material?.material_id && graphMaterialIds.has(material.material_id));
      }
      if (entry.target === 'evidence_item') {
        const item = record as { id?: string } | null;
        if (!item?.id) return false;
        return ws.evidence.bindings.some((binding) => binding.item_id === item.id
          && graphMaterialIds.has(binding.material_id));
      }
      return false;
    });
  });
}

/* ---------------- 快照载荷 ---------------- */

function snapshotSource(source: GraphSource, labels: Record<string, ResolvedLabel>): SnapshotSource {
  return {
    source_id: source.id,
    ref: source.segment_id ? { segment_id: source.segment_id }
      : source.annotation_id ? { annotation_id: source.annotation_id }
        : { material_id: source.material_id ?? '' },
    proves: source.proves,
    label: labels[source.id]?.text ?? '（出处已失效）',
  };
}

/**
 * 生成单张图的快照载荷。
 * excludePending=true 时按「已确认状态」序列化（D3）：Agent 待确认的新增不出现，
 * 修改回退到最早确认值，人工后续改写保持人工值；图名/别名/出处同样排除待确认。
 */
export function buildSnapshotPayload(ws: GraphSnapshot, graphId: string,
                                     { excludePending }: { excludePending: boolean }): SnapshotPayload {
  const graph = ws.graphs.find((item) => item.id === graphId);
  if (!graph) throw new Error('关系图不存在');
  const pending = excludePending ? pendingEntries(ws.pending_changes) : new Map<string, PendingEntry[]>();
  const nodes = confirmedList(ws.nodes.filter((node) => node.graph_id === graphId), 'graph_node', pending);
  const edges = confirmedList(ws.edges.filter((edge) => edge.graph_id === graphId), 'graph_edge', pending);
  const groups = confirmedList(ws.groups.filter((group) => group.graph_id === graphId), 'graph_group', pending);
  const entities = confirmedList(ws.entities, 'entity', pending);
  const relations = confirmedList(ws.relations, 'relation', pending);
  const aliases = confirmedList(ws.aliases, 'alias', pending);
  const sources = confirmedList(ws.sources, 'source', pending);
  // 分图自身（重命名/说明）也遵循排除待确认；图本身若是待确认新建则由执行器先行拒绝冻结。
  const confirmedGraph = confirmedRecord(graph, pending.get(`graph:${graph.id}`)) ?? graph;
  const entityById = new Map(entities.map((entity) => [entity.id, entity]));
  const relationById = new Map(relations.map((relation) => [relation.id, relation]));

  const payloadNodes: SnapshotEntityNode[] = [];
  for (const node of nodes) {
    const entity = entityById.get(node.entity_id);
    if (!entity) continue;
    payloadNodes.push({
      entity_id: entity.id,
      name: entity.name,
      kind: entity.kind,
      roles: [...entity.roles],
      aliases: aliases.filter((alias) => alias.entity_id === entity.id).map((alias) => alias.alias),
      x: node.x,
      y: node.y,
      group_id: node.group_id,
      sources: sources.filter((source) => source.entity_id === entity.id)
        .map((source) => snapshotSource(source, ws.labels)),
    });
  }
  const nodeEntityIds = new Set(payloadNodes.map((node) => node.entity_id));
  const payloadEdges: SnapshotRelationEdge[] = [];
  for (const edge of edges) {
    const relation = relationById.get(edge.relation_id);
    if (!relation) continue;
    if (!nodeEntityIds.has(relation.from_entity_id) || !nodeEntityIds.has(relation.to_entity_id)) continue;
    payloadEdges.push({
      relation_id: relation.id,
      from_entity_id: relation.from_entity_id,
      to_entity_id: relation.to_entity_id,
      directed: relation.directed,
      kind: relation.kind,
      label: relation.label,
      credibility: relation.credibility,
      attrs: { ...relation.attrs },
      note: relation.note,
      sources: sources.filter((source) => source.relation_id === relation.id)
        .map((source) => snapshotSource(source, ws.labels)),
    });
  }
  const payloadGroups: SnapshotGroup[] = groups.map((group) => ({
    group_id: group.id, title: group.title, color: group.color,
    x: group.x, y: group.y, width: group.width, height: group.height,
    collapsed: group.collapsed,
  }));
  return {
    graph: {
      id: confirmedGraph.id, title: confirmedGraph.title,
      description: confirmedGraph.description,
    },
    nodes: payloadNodes.sort((a, b) => a.entity_id.localeCompare(b.entity_id)),
    edges: payloadEdges.sort((a, b) => a.relation_id.localeCompare(b.relation_id)),
    groups: payloadGroups.sort((a, b) => a.group_id.localeCompare(b.group_id)),
  };
}

/* ---------------- 画布模型 ---------------- */

type AgentState = 'pending' | 'accepted' | null;

/** 目标对象最近的 Agent 变更状态：黄标 pending / 绿标 accepted / 无。 */
function agentStates(changes: GraphChange[], target: DiffTarget): Map<string, AgentState> {
  const states = new Map<string, AgentState>();
  for (const change of changes) {
    if (change.actor !== 'agent') continue;
    for (const entry of change.diff) {
      if (entry.target !== target || !entry.after) continue;
      if (change.review_state === 'pending') states.set(entry.id, 'pending');
      else if (change.review_state === 'accepted') states.set(entry.id, 'accepted');
      else if (change.review_state === 'reverted') states.delete(entry.id);
    }
  }
  return states;
}

export function buildCanvasModel(ws: GraphSnapshot, graphId: string): CanvasModel {
  const nodes = ws.nodes.filter((node) => node.graph_id === graphId);
  const groups = active(ws.groups.filter((group) => group.graph_id === graphId));
  const entities = new Map(active(ws.entities).map((entity) => [entity.id, entity]));
  const relations = new Map(active(ws.relations).map((relation) => [relation.id, relation]));
  const nodeEntityIds = new Set(nodes.map((node) => node.entity_id));
  const entityStates = agentStates(ws.changes, 'entity');
  const relationStates = agentStates(ws.changes, 'relation');

  const canvasNodes: CanvasNode[] = [];
  for (const node of nodes) {
    const entity = entities.get(node.entity_id);
    if (!entity) continue;
    const sourceIds = new Set(active(ws.sources).filter((source) => source.entity_id === entity.id)
      .map((source) => source.id));
    canvasNodes.push({
      id: entity.id,
      node_id: node.id,
      entity_id: entity.id,
      name: entity.name,
      kind: entity.kind,
      roles: [...entity.roles],
      aliases: active(ws.aliases).filter((alias) => alias.entity_id === entity.id)
        .map((alias) => alias.alias),
      x: node.x,
      y: node.y,
      group_id: node.group_id,
      source_labels: [...sourceIds].map((id) => ws.labels[id]?.text ?? '').filter(Boolean),
      source_count: sourceIds.size,
      pending: entityStates.get(entity.id) === 'pending',
      accepted: entityStates.get(entity.id) === 'accepted',
    });
  }
  const canvasEdges: CanvasEdge[] = [];
  for (const edge of ws.edges.filter((item) => item.graph_id === graphId)) {
    const relation = relations.get(edge.relation_id);
    if (!relation) continue;
    if (!nodeEntityIds.has(relation.from_entity_id) || !nodeEntityIds.has(relation.to_entity_id)) continue;
    const sourceIds = new Set(active(ws.sources).filter((source) => source.relation_id === relation.id)
      .map((source) => source.id));
    canvasEdges.push({
      id: relation.id,
      relation_id: relation.id,
      source: relation.from_entity_id,
      target: relation.to_entity_id,
      from_entity_id: relation.from_entity_id,
      to_entity_id: relation.to_entity_id,
      kind: relation.kind,
      label: relation.label,
      credibility: relation.credibility,
      directed: relation.directed,
      source_labels: [...sourceIds].map((id) => ws.labels[id]?.text ?? '').filter(Boolean),
      pending: relationStates.get(relation.id) === 'pending',
      accepted: relationStates.get(relation.id) === 'accepted',
    });
  }
  const canvasGroups: CanvasGroup[] = groups.map((group) => ({
    id: group.id, title: group.title, color: group.color,
    x: group.x, y: group.y, width: group.width, height: group.height,
    collapsed: group.collapsed,
  }));
  return { nodes: canvasNodes, edges: canvasEdges, groups: canvasGroups };
}

export function buildSnapshotCanvasModel(snapshot: { payload: SnapshotPayload }): CanvasModel {
  const { payload } = snapshot;
  return {
    nodes: payload.nodes.map((node) => ({
      id: node.entity_id,
      node_id: node.entity_id,
      entity_id: node.entity_id,
      name: node.name,
      kind: node.kind,
      roles: node.roles,
      aliases: node.aliases,
      x: node.x,
      y: node.y,
      group_id: node.group_id,
      source_labels: node.sources.map((source) => source.label),
      source_count: node.sources.length,
      pending: false,
      accepted: false,
    })),
    edges: payload.edges.map((edge) => ({
      id: edge.relation_id,
      relation_id: edge.relation_id,
      source: edge.from_entity_id,
      target: edge.to_entity_id,
      from_entity_id: edge.from_entity_id,
      to_entity_id: edge.to_entity_id,
      kind: edge.kind,
      label: edge.label,
      credibility: edge.credibility,
      directed: edge.directed,
      source_labels: edge.sources.map((source) => source.label),
      pending: false,
      accepted: false,
    })),
    groups: payload.groups.map((group) => ({
      id: group.group_id, title: group.title, color: group.color,
      x: group.x, y: group.y, width: group.width, height: group.height,
      collapsed: group.collapsed,
    })),
  };
}

/* ---------------- 快照对比 ---------------- */

export interface FieldChange {
  field: string;
  from: string;
  to: string;
}

export interface SnapshotDiff {
  addedNodes: SnapshotEntityNode[];
  removedNodes: SnapshotEntityNode[];
  addedEdges: SnapshotRelationEdge[];
  removedEdges: SnapshotRelationEdge[];
  changedNodes: { entity_id: string; name: string; fields: FieldChange[] }[];
  changedEdges: { relation_id: string; label: string; fields: FieldChange[] }[];
  movedNodes: { entity_id: string; name: string; from: { x: number; y: number }; to: { x: number; y: number } }[];
  addedGroups: SnapshotGroup[];
  removedGroups: SnapshotGroup[];
  changedGroups: { group_id: string; title: string; fields: FieldChange[] }[];
  totalChanges: number;
}

const ATTR_LABELS: Record<string, string> = {
  share_pct: '持股比例', amount_cents: '金额（分）', currency: '币种',
  date_start: '起始日期', date_end: '结束日期',
};

function attrText(attrs: RelationAttrs | Record<string, unknown> | null | undefined): string {
  if (!attrs) return '（空）';
  const parts = Object.entries(attrs as Record<string, unknown>)
    .filter(([, value]) => value !== null && value !== undefined && value !== '')
    .map(([key, value]) => `${ATTR_LABELS[key] ?? key}=${String(value)}`);
  return parts.length > 0 ? parts.join('，') : '（空）';
}

function sourceKey(edge: SnapshotRelationEdge): string {
  return edge.sources.map((source) => source.source_id).sort().join('|');
}

export function diffPayloads(base: SnapshotPayload, target: SnapshotPayload): SnapshotDiff {
  const baseNodes = new Map(base.nodes.map((node) => [node.entity_id, node]));
  const targetNodes = new Map(target.nodes.map((node) => [node.entity_id, node]));
  const baseEdges = new Map(base.edges.map((edge) => [edge.relation_id, edge]));
  const targetEdges = new Map(target.edges.map((edge) => [edge.relation_id, edge]));
  const baseGroups = new Map(base.groups.map((group) => [group.group_id, group]));
  const targetGroups = new Map(target.groups.map((group) => [group.group_id, group]));

  const diff: SnapshotDiff = {
    addedNodes: [], removedNodes: [], addedEdges: [], removedEdges: [],
    changedNodes: [], changedEdges: [], movedNodes: [],
    addedGroups: [], removedGroups: [], changedGroups: [], totalChanges: 0,
  };

  for (const node of target.nodes) {
    if (!baseNodes.has(node.entity_id)) diff.addedNodes.push(node);
  }
  for (const node of base.nodes) {
    if (!targetNodes.has(node.entity_id)) diff.removedNodes.push(node);
  }
  for (const edge of target.edges) {
    if (!baseEdges.has(edge.relation_id)) diff.addedEdges.push(edge);
  }
  for (const edge of base.edges) {
    if (!targetEdges.has(edge.relation_id)) diff.removedEdges.push(edge);
  }
  for (const node of target.nodes) {
    const before = baseNodes.get(node.entity_id);
    if (!before) continue;
    const fields: FieldChange[] = [];
    if (before.name !== node.name) fields.push({ field: '名称', from: before.name, to: node.name });
    if (before.kind !== node.kind) fields.push({ field: '类型', from: before.kind, to: node.kind });
    if (before.roles.join('、') !== node.roles.join('、')) {
      fields.push({ field: '诉讼角色', from: before.roles.join('、') || '（空）', to: node.roles.join('、') || '（空）' });
    }
    if (before.aliases.join('、') !== node.aliases.join('、')) {
      fields.push({ field: '别名', from: before.aliases.join('、') || '（空）', to: node.aliases.join('、') || '（空）' });
    }
    if (fields.length > 0) diff.changedNodes.push({ entity_id: node.entity_id, name: node.name, fields });
    if (before.x !== node.x || before.y !== node.y) {
      diff.movedNodes.push({
        entity_id: node.entity_id, name: node.name,
        from: { x: before.x, y: before.y }, to: { x: node.x, y: node.y },
      });
    }
  }
  for (const edge of target.edges) {
    const before = baseEdges.get(edge.relation_id);
    if (!before) continue;
    const fields: FieldChange[] = [];
    if (before.credibility !== edge.credibility) {
      fields.push({ field: '可信状态', from: before.credibility, to: edge.credibility });
    }
    if ((before.label ?? '') !== (edge.label ?? '')) {
      fields.push({ field: '关系标签', from: before.label ?? '（空）', to: edge.label ?? '（空）' });
    }
    if (before.kind !== edge.kind) fields.push({ field: '关系类型', from: before.kind, to: edge.kind });
    if (before.directed !== edge.directed) {
      fields.push({ field: '方向', from: before.directed ? '有向' : '无向', to: edge.directed ? '有向' : '无向' });
    }
    if (attrText(before.attrs) !== attrText(edge.attrs)) {
      fields.push({ field: '属性', from: attrText(before.attrs), to: attrText(edge.attrs) });
    }
    if ((before.note ?? '') !== (edge.note ?? '')) {
      fields.push({ field: '备注', from: before.note ?? '（空）', to: edge.note ?? '（空）' });
    }
    if (sourceKey(before) !== sourceKey(edge)) {
      fields.push({ field: '出处', from: `${before.sources.length} 条`, to: `${edge.sources.length} 条` });
    }
    if (before.from_entity_id !== edge.from_entity_id || before.to_entity_id !== edge.to_entity_id) {
      fields.push({ field: '端点', from: '已变更', to: '已变更' });
    }
    if (fields.length > 0) {
      diff.changedEdges.push({ relation_id: edge.relation_id, label: edge.label ?? edge.kind, fields });
    }
  }
  for (const group of target.groups) {
    const before = baseGroups.get(group.group_id);
    if (!before) { diff.addedGroups.push(group); continue; }
    const fields: FieldChange[] = [];
    if (before.title !== group.title) fields.push({ field: '分组名', from: before.title, to: group.title });
    if (before.collapsed !== group.collapsed) {
      fields.push({ field: '收起', from: before.collapsed ? '是' : '否', to: group.collapsed ? '是' : '否' });
    }
    if (fields.length > 0) diff.changedGroups.push({ group_id: group.group_id, title: group.title, fields });
  }
  for (const group of base.groups) {
    if (!targetGroups.has(group.group_id)) diff.removedGroups.push(group);
  }
  diff.totalChanges = diff.addedNodes.length + diff.removedNodes.length
    + diff.addedEdges.length + diff.removedEdges.length
    + diff.changedNodes.length + diff.changedEdges.length
    + diff.movedNodes.length + diff.addedGroups.length + diff.removedGroups.length
    + diff.changedGroups.length;
  return diff;
}

/** 对比高亮：目标画布上新增/变化/移动的元素集合。 */
export interface DiffHighlight {
  addedNodes: Set<string>;
  addedEdges: Set<string>;
  changedNodes: Set<string>;
  changedEdges: Set<string>;
  movedNodes: Set<string>;
}

export function diffHighlight(diff: SnapshotDiff): DiffHighlight {
  return {
    addedNodes: new Set(diff.addedNodes.map((node) => node.entity_id)),
    addedEdges: new Set(diff.addedEdges.map((edge) => edge.relation_id)),
    changedNodes: new Set(diff.changedNodes.map((node) => node.entity_id)),
    changedEdges: new Set(diff.changedEdges.map((edge) => edge.relation_id)),
    movedNodes: new Set(diff.movedNodes.map((node) => node.entity_id)),
  };
}

/** 变更记录里可读的目标名称（Agent 复核面板用）。 */
export function describeDiffEntry(entry: ChangeDiffEntry, ws: GraphSnapshot,
                                  rules?: GraphRules): string {
  const record = (entry.after ?? entry.before) as Record<string, unknown> | null;
  const before = entry.before as Record<string, unknown> | null;
  const after = entry.after as Record<string, unknown> | null;
  const credibilityText = (value: unknown) => rules?.credibility
    .find((rule) => rule.value === value)?.label ?? String(value);
  const kindText = (value: unknown) => rules?.entity_kinds
    .find((kind) => kind.value === value)?.label ?? String(value);
  const consentText: Record<string, string> = { confirm: '确认', amend: '更正', revoke: '撤回' };
  switch (entry.target) {
    case 'entity': {
      const name = String(record?.name ?? '');
      if (!before) return `新增主体：${name}`;
      if (!after) return `删除主体：${name}`;
      const fields: string[] = [];
      if (before.name !== after.name) fields.push(`名称 ${String(before.name)} → ${String(after.name)}`);
      if (before.kind !== after.kind) {
        fields.push(`类型 ${kindText(before.kind)} → ${kindText(after.kind)}`);
      }
      if (JSON.stringify(before.roles) !== JSON.stringify(after.roles)) fields.push('诉讼角色变化');
      if (before.note !== after.note) fields.push('备注变化');
      return `修改主体：${name}（${fields.join('；') || '无字段变化'}）`;
    }
    case 'alias':
      return `${before ? '移除' : '新增'}别名：${String(record?.alias ?? '')}`;
    case 'relation': {
      const from = ws.entities.find((entity) => entity.id === (record?.from_entity_id as string))?.name ?? '?';
      const to = ws.entities.find((entity) => entity.id === (record?.to_entity_id as string))?.name ?? '?';
      if (!before) return `新增关系：${from} → ${to}`;
      if (!after) return `删除关系：${from} → ${to}`;
      const fields: string[] = [];
      if (before.credibility !== after.credibility) {
        fields.push(`可信状态 ${credibilityText(before.credibility)} → ${credibilityText(after.credibility)}`);
      }
      if (before.label !== after.label) fields.push('关系标签变化');
      if (JSON.stringify(before.attrs) !== JSON.stringify(after.attrs)) fields.push('属性变化');
      return `修改关系：${from} → ${to}（${fields.join('；') || '无字段变化'}）`;
    }
    case 'source':
      return `${before ? '移除' : '新增'}出处${record?.proves ? `：${String(record.proves)}` : ''}`;
    case 'graph_node':
      return `${before ? '从本图移除' : '加入本图'}主体`;
    case 'graph_edge':
      return `${before ? '从本图移除' : '加入本图'}关系`;
    case 'graph':
      return `${before ? '修改' : '新建'}关系图：${String(record?.title ?? '')}`;
    case 'graph_group':
      return `${before ? '修改' : '新建'}分组：${String(record?.title ?? '')}`;
    case 'snapshot':
      return `冻结快照 v${String(record?.version_no ?? '')}（${String(record?.stage_label ?? '')}）`;
    case 'consent':
      return `客户同意记录：${consentText[String(record?.action ?? '')] ?? String(record?.action ?? '')}`;
    case 'evidence_group':
      return `${before ? '修改' : '新建'}证据分组：${String(record?.title ?? '')}`;
    case 'evidence_item':
      return `${before ? '修改' : '新增'}证据条目：${String(record?.title ?? '')}`;
    case 'evidence_binding':
      return `${before ? '移除' : '新增'}证据绑定`;
    case 'layout':
      return '调整布局';
    default:
      return entry.target;
  }
}

export function evidenceItemLabel(item: EvidenceItem, ws: GraphSnapshot): string {
  return ws.evidence_numbers[item.id] ?? (item.review_state === 'pending' ? '待确认' : '未编号');
}

export function groupTitle(ws: GraphSnapshot, groupId: string | null): string {
  if (!groupId) return '未分组';
  return ws.groups.find((group) => group.id === groupId)?.title ?? '未知分组';
}

export function activeEvidenceGroups(ws: GraphSnapshot): EvidenceGroup[] {
  return sortByPosition(active(ws.evidence.groups));
}

export function evidenceItemsOf(ws: GraphSnapshot, groupId: string): EvidenceItem[] {
  return sortByPosition(active(ws.evidence.items).filter((item) => item.group_id === groupId));
}

export function evidenceBindingsOf(ws: GraphSnapshot, itemId: string): EvidenceBinding[] {
  return active(ws.evidence.bindings).filter((binding) => binding.item_id === itemId);
}

export function entitiesById(ws: GraphSnapshot): Map<string, CaseEntity> {
  return new Map(ws.entities.map((entity) => [entity.id, entity]));
}

export function relationsById(ws: GraphSnapshot): Map<string, CaseRelation> {
  return new Map(ws.relations.map((relation) => [relation.id, relation]));
}

export function graphNodesOf(ws: GraphSnapshot, graphId: string): RelationGraphNode[] {
  return ws.nodes.filter((node) => node.graph_id === graphId);
}

export function graphEdgesOf(ws: GraphSnapshot, graphId: string): RelationGraphEdge[] {
  return ws.edges.filter((edge) => edge.graph_id === graphId);
}

export function graphsActive(ws: GraphSnapshot): RelationGraph[] {
  return sortByPosition(active(ws.graphs));
}

export function groupsOf(ws: GraphSnapshot, graphId: string): RelationGraphGroup[] {
  return active(ws.groups.filter((group) => group.graph_id === graphId));
}

export function aliasesOf(ws: GraphSnapshot, entityId: string): EntityAlias[] {
  return active(ws.aliases.filter((alias) => alias.entity_id === entityId));
}

export function sourcesOf(ws: GraphSnapshot, target: { entity_id?: string; relation_id?: string }): GraphSource[] {
  return active(ws.sources.filter((source) =>
    (target.entity_id ? source.entity_id === target.entity_id : false)
    || (target.relation_id ? source.relation_id === target.relation_id : false)));
}
