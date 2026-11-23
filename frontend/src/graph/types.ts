/**
 * 案件主体关系图（P1 原型）的 TS 契约。
 * 与 docs/changes/20261120-案件主体关系图.md 5.1/5.2 的数据库结构和服务命令对应：
 * 主体、别名、关系、出处属于案件（D1）；图只是命名视图，布局按图保存；
 * 证据编号由已确认目录动态解析（D2）；快照不可变、客户同意追加留痕（D3）。
 * P3 生成 OpenAPI 类型后，这里只保留前端补充类型。
 */

export type Actor = 'human' | 'agent';
export type Entry = 'local-web' | 'mcp' | 'cli' | 'prototype';

export type Credibility = 'evidenced' | 'suspected' | 'disputed' | 'ruled_out';
export type EvidenceReview = 'confirmed' | 'pending' | 'rejected';
export type ReviewState = 'none' | 'pending' | 'accepted' | 'reverted';
export type ConsentAction = 'confirm' | 'amend' | 'revoke';

export interface Option {
  value: string;
  label: string;
}

/** 四档可信状态的线型由规则配置提供，视图组件不写死颜色。 */
export interface CredibilityRule extends Option {
  /** SVG stroke-dasharray；空字符串 = 实线。 */
  dash: string;
  stroke: string;
  width: number;
  /** 图例下的一句说明。 */
  hint: string;
  /** 短标记，用于徽标与导出的行首。 */
  badge: string;
}

export type GraphCommandType =
  | 'add_entity' | 'update_entity' | 'remove_entity' | 'restore_entity'
  | 'add_alias' | 'remove_alias'
  | 'add_relation' | 'update_relation' | 'remove_relation' | 'restore_relation'
  | 'add_source' | 'remove_source'
  | 'create_graph' | 'update_graph' | 'remove_graph'
  | 'add_to_graph' | 'remove_from_graph' | 'save_layout'
  | 'set_node_group' | 'upsert_graph_group' | 'remove_graph_group'
  | 'create_snapshot' | 'restore_snapshot' | 'add_snapshot_consent'
  | 'accept_change' | 'accept_all' | 'revert_change'
  | 'add_evidence_group' | 'update_evidence_group' | 'reorder_evidence_groups'
  | 'remove_evidence_group'
  | 'add_evidence_item' | 'update_evidence_item' | 'move_evidence_item'
  | 'remove_evidence_item'
  | 'add_evidence_binding' | 'remove_evidence_binding'
  | 'confirm_evidence_item' | 'reject_evidence_item'
  | 'confirm_evidence_binding' | 'reject_evidence_binding'
  | 'import_evidence_catalog'
  | 'accept_evidence_change' | 'revert_evidence_change';

export interface GraphRules {
  entity_kinds: Option[];
  /** 诉讼角色预设；可多选，不在预设内的自由文本不开放。 */
  roles: Option[];
  relation_kinds: Option[];
  credibility: CredibilityRule[];
  snapshot_stages: string[];
  consent_channels: Option[];
  /** Agent 可用命令白名单；冻结、恢复、同意、确认、撤回绝不在其中。 */
  agent_commands: GraphCommandType[];
  /** 双主体路径高亮的搜索上限（防止密集图卡死）。 */
  max_path_depth: number;
  max_paths: number;
}

/* ---------- 案件级事实（D1） ---------- */

export interface CaseEntity {
  id: string;
  case_id: string;
  kind: string;
  name: string;
  roles: string[];
  note: string | null;
  created_by: Actor;
  updated_by: Actor;
  request_key: string | null;
  removed_at: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface EntityAlias {
  id: string;
  case_id: string;
  entity_id: string;
  alias: string;
  created_by: Actor;
  removed_at: string | null;
  created_at: string;
}

export interface RelationAttrs {
  share_pct?: number | null;
  amount_cents?: number | null;
  currency?: string | null;
  date_start?: string | null;
  date_end?: string | null;
}

export interface CaseRelation {
  id: string;
  case_id: string;
  from_entity_id: string;
  to_entity_id: string;
  directed: boolean;
  kind: string;
  label: string | null;
  credibility: Credibility;
  attrs: RelationAttrs;
  note: string | null;
  created_by: Actor;
  updated_by: Actor;
  request_key: string | null;
  removed_at: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
}

/** 出处三选一：segment（不可变 source_ref）/ material / annotation。 */
export type SourceRef =
  | { segment_id: string }
  | { material_id: string }
  | { annotation_id: string };

export interface GraphSource {
  id: string;
  case_id: string;
  entity_id: string | null;
  relation_id: string | null;
  segment_id: string | null;
  material_id: string | null;
  annotation_id: string | null;
  proves: string | null;
  created_by: Actor;
  removed_at: string | null;
  created_at: string;
}

/* ---------- 一案多图（D1） ---------- */

export interface RelationGraph {
  id: string;
  case_id: string;
  title: string;
  description: string | null;
  position: number;
  created_by: Actor;
  updated_by: Actor;
  request_key: string | null;
  removed_at: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface RelationGraphGroup {
  id: string;
  case_id: string;
  graph_id: string;
  title: string;
  color: string | null;
  x: number;
  y: number;
  width: number;
  height: number;
  collapsed: boolean;
  revision: number;
  removed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface RelationGraphNode {
  id: string;
  case_id: string;
  graph_id: string;
  entity_id: string;
  group_id: string | null;
  x: number;
  y: number;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface RelationGraphEdge {
  id: string;
  case_id: string;
  graph_id: string;
  relation_id: string;
  created_at: string;
}

/* ---------- 快照与客户同意（D3） ---------- */

export interface SnapshotSource {
  source_id: string;
  ref: SourceRef;
  proves: string | null;
  /** 冻结时的证据编号文本（如「证据1-1 第3页」）。 */
  label: string;
}

export interface SnapshotEntityNode {
  entity_id: string;
  name: string;
  kind: string;
  roles: string[];
  aliases: string[];
  x: number;
  y: number;
  group_id: string | null;
  sources: SnapshotSource[];
}

export interface SnapshotRelationEdge {
  relation_id: string;
  from_entity_id: string;
  to_entity_id: string;
  directed: boolean;
  kind: string;
  label: string | null;
  credibility: Credibility;
  attrs: RelationAttrs;
  note: string | null;
  sources: SnapshotSource[];
}

export interface SnapshotGroup {
  group_id: string;
  title: string;
  color: string | null;
  x: number;
  y: number;
  width: number;
  height: number;
  collapsed: boolean;
}

export interface SnapshotPayload {
  graph: { id: string; title: string; description: string | null };
  nodes: SnapshotEntityNode[];
  edges: SnapshotRelationEdge[];
  groups: SnapshotGroup[];
}

export interface RelationGraphSnapshot {
  id: string;
  case_id: string;
  graph_id: string;
  version_no: number;
  stage_label: string;
  note: string | null;
  progress_node_id: string | null;
  payload: SnapshotPayload;
  excluded_pending_count: number;
  created_by: 'human';
  created_at: string;
}

export interface SnapshotConsent {
  id: string;
  case_id: string;
  snapshot_id: string;
  action: ConsentAction;
  parent_consent_id: string | null;
  consenter: string;
  consented_at: string;
  channel: string;
  source_detail: string;
  evidence_material_id: string | null;
  note: string | null;
  created_by: 'human';
  created_at: string;
}

/* ---------- 证据目录（D2） ---------- */

export interface EvidenceGroup {
  id: string;
  case_id: string;
  position: number;
  title: string;
  purpose: string | null;
  created_by: Actor;
  request_key: string | null;
  removed_at: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface EvidenceItem {
  id: string;
  case_id: string;
  group_id: string;
  position: number;
  title: string;
  purpose: string | null;
  source_kind: 'manual' | 'imported';
  import_material_id: string | null;
  review_state: EvidenceReview;
  created_by: Actor;
  request_key: string | null;
  removed_at: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface EvidenceBinding {
  id: string;
  case_id: string;
  item_id: string;
  material_id: string;
  page_start: number | null;
  page_end: number | null;
  review_state: EvidenceReview;
  created_by: Actor;
  removed_at: string | null;
  created_at: string;
}

/* ---------- 合成材料（P1 只读目录） ---------- */

export interface Material {
  id: string;
  case_id: string;
  title: string;
  kind: 'pdf' | 'image' | 'sheet' | 'doc';
  page_count: number;
}

export interface Segment {
  id: string;
  case_id: string;
  material_id: string;
  page: number;
  text: string;
}

export interface Annotation {
  id: string;
  case_id: string;
  material_id: string;
  page: number;
  text: string;
  /** 真实案件的标注只带文件名（契约没有 material_id）；出处选择器优先用它。 */
  label?: string;
}

/* ---------- 变更记录 ---------- */

export type DiffTarget =
  | 'entity' | 'alias' | 'relation' | 'source'
  | 'graph' | 'graph_node' | 'graph_edge' | 'graph_group' | 'layout'
  | 'snapshot' | 'consent'
  | 'evidence_group' | 'evidence_item' | 'evidence_binding';

export interface ChangeDiffEntry {
  target: DiffTarget;
  id: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  before_rev: number | null;
  after_rev: number | null;
}

export interface GraphChange {
  id: string;
  case_id: string;
  domain: 'graph' | 'evidence';
  seq: number;
  command: string;
  actor: Actor;
  entry: Entry;
  diff: ChangeDiffEntry[];
  review_state: ReviewState;
  reviewed_at: string | null;
  reverted_by: string | null;
  created_at: string;
}

export interface AuditEntry {
  operation: string;
  actor: Actor;
  entry: Entry;
  target_ids: string[];
}

/* ---------- 读快照 ---------- */

export interface ResolvedLabel {
  /** 命中的全部编号（按目录顺序），未命中为空数组。 */
  labels: string[];
  /** 直接展示文本：编号以「、」连接；未命中为「材料名（未编号）」。 */
  text: string;
  material_id: string;
  material_title: string;
  page: number | null;
  text_excerpt: string | null;
  /** 片段出处的不可变 source_ref；P4 用它打开原文。 */
  source_ref?: string | null;
  /** 出处解析到的文件版本；P4 交给 SourcePreview 打开版本/页码。 */
  file_version_id?: string | null;
}

export interface GraphSnapshot {
  case_id: string;
  case_title: string;
  archived: boolean;
  seq: number;
  seqs: { graph: number; evidence: number };
  entities: CaseEntity[];
  aliases: EntityAlias[];
  relations: CaseRelation[];
  sources: GraphSource[];
  graphs: RelationGraph[];
  nodes: RelationGraphNode[];
  edges: RelationGraphEdge[];
  groups: RelationGraphGroup[];
  snapshots: RelationGraphSnapshot[];
  consents: SnapshotConsent[];
  evidence: {
    groups: EvidenceGroup[];
    items: EvidenceItem[];
    bindings: EvidenceBinding[];
    pending_changes?: GraphChange[];
  };
  materials: Material[];
  segments: Segment[];
  annotations: Annotation[];
  progress_nodes: Option[];
  /** 出处 id → 现行目录编号（已确认条目与绑定）。 */
  labels: Record<string, ResolvedLabel>;
  /** 证据条目 id → 现行编号（待确认/已驳回不参与）。 */
  evidence_numbers: Record<string, string>;
  pending_changes: GraphChange[];
  changes: GraphChange[];
  last_change_id?: string | null;
}

/* ---------- 画布模型（工作稿与快照共用） ---------- */

export interface CanvasNode {
  id: string;
  node_id: string;
  entity_id: string;
  name: string;
  kind: string;
  roles: string[];
  aliases: string[];
  x: number;
  y: number;
  group_id: string | null;
  source_labels: string[];
  source_count: number;
  pending: boolean;
  accepted: boolean;
}

export interface CanvasEdge {
  id: string;
  relation_id: string;
  source: string;
  target: string;
  from_entity_id: string;
  to_entity_id: string;
  kind: string;
  label: string | null;
  credibility: Credibility;
  directed: boolean;
  source_labels: string[];
  pending: boolean;
  accepted: boolean;
}

export interface CanvasGroup {
  id: string;
  title: string;
  color: string | null;
  x: number;
  y: number;
  width: number;
  height: number;
  collapsed: boolean;
}

export interface CanvasModel {
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  groups: CanvasGroup[];
}

/* ---------- 命令 ---------- */

export type EntityChanges = Partial<Pick<CaseEntity, 'kind' | 'name' | 'roles' | 'note'>>;
export type RelationChanges = Partial<Pick<CaseRelation,
  'kind' | 'label' | 'credibility' | 'directed' | 'attrs' | 'note'>>;

export type GraphCommand =
  | { type: 'add_entity'; kind: string; name: string; roles?: string[];
      note?: string | null; graph_id?: string | null; request_key?: string }
  | { type: 'update_entity'; entity_id: string; expected_revision: number; changes: EntityChanges }
  | { type: 'remove_entity'; entity_id: string; expected_revision: number }
  | { type: 'restore_entity'; entity_id: string; expected_revision: number }
  | { type: 'add_alias'; entity_id: string; alias: string }
  | { type: 'remove_alias'; alias_id: string }
  | { type: 'add_relation'; from_entity_id: string; to_entity_id: string;
      kind: string; label?: string | null; credibility: Credibility;
      directed?: boolean; attrs?: RelationAttrs; note?: string | null;
      graph_id?: string | null; request_key?: string }
  | { type: 'update_relation'; relation_id: string; expected_revision: number;
      changes: RelationChanges }
  | { type: 'remove_relation'; relation_id: string; expected_revision: number }
  | { type: 'restore_relation'; relation_id: string; expected_revision: number }
  | { type: 'add_source'; entity_id?: string; relation_id?: string;
      ref: SourceRef; proves?: string | null; request_key?: string }
  | { type: 'remove_source'; source_id: string; downgrade_to?: Credibility }
  | { type: 'create_graph'; title: string; description?: string | null;
      request_key?: string }
  | { type: 'update_graph'; graph_id: string; expected_revision: number;
      changes: { title?: string; description?: string | null } }
  | { type: 'remove_graph'; graph_id: string; expected_revision: number }
  | { type: 'add_to_graph'; graph_id: string; entity_ids?: string[];
      relation_ids?: string[] }
  | { type: 'remove_from_graph'; graph_id: string; entity_id?: string;
      relation_id?: string }
  | { type: 'save_layout'; graph_id: string;
      nodes?: { node_id: string; x: number; y: number }[];
      groups?: { group_id: string; x: number; y: number; width?: number; height?: number }[] }
  | { type: 'set_node_group'; graph_id: string; entity_id: string;
      group_id: string | null }
  | { type: 'upsert_graph_group'; graph_id: string; group_id?: string;
      title: string; color?: string | null; x?: number; y?: number;
      width?: number; height?: number; collapsed?: boolean;
      entity_ids?: string[] }
  | { type: 'remove_graph_group'; group_id: string }
  | { type: 'create_snapshot'; graph_id: string; stage_label: string;
      note?: string | null; progress_node_id?: string | null;
      acknowledge_pending?: boolean }
  | { type: 'restore_snapshot'; snapshot_id: string }
  | { type: 'add_snapshot_consent'; snapshot_id: string; action: ConsentAction;
      parent_consent_id?: string | null; consenter: string; consented_at: string;
      channel: string; source_detail: string;
      evidence_material_id?: string | null; note?: string | null }
  | { type: 'accept_change'; change_id: string }
  | { type: 'accept_all'; change_ids?: string[]; expected_seq?: number | null }
  | { type: 'revert_change'; change_id: string }
  | { type: 'add_evidence_group'; title: string; purpose?: string | null;
      request_key?: string }
  | { type: 'update_evidence_group'; group_id: string; expected_revision: number;
      changes: { title?: string; purpose?: string | null } }
  | { type: 'reorder_evidence_groups'; group_ids: string[] }
  | { type: 'remove_evidence_group'; group_id: string; expected_revision: number }
  | { type: 'add_evidence_item'; group_id: string; title: string;
      purpose?: string | null; source_kind?: 'manual' | 'imported';
      import_material_id?: string | null; request_key?: string }
  | { type: 'update_evidence_item'; item_id: string; expected_revision: number;
      changes: { title?: string; purpose?: string | null } }
  | { type: 'move_evidence_item'; item_id: string; to_group_id: string;
      before_item_id?: string | null }
  | { type: 'remove_evidence_item'; item_id: string; expected_revision: number }
  | { type: 'add_evidence_binding'; item_id: string; material_id: string;
      page_start?: number | null; page_end?: number | null; request_key?: string }
  | { type: 'remove_evidence_binding'; binding_id: string }
  | { type: 'confirm_evidence_item'; item_id: string; expected_revision: number }
  | { type: 'reject_evidence_item'; item_id: string; expected_revision: number }
  | { type: 'confirm_evidence_binding'; binding_id: string }
  | { type: 'reject_evidence_binding'; binding_id: string }
  | { type: 'import_evidence_catalog'; request_key?: string }
  | { type: 'accept_evidence_change'; change_id: string }
  | { type: 'revert_evidence_change'; change_id: string };

export type CommandResult =
  | { ok: true; snapshot: GraphSnapshot; notice?: string; last_change_id?: string | null }
  | { ok: false; code: 'conflict' | 'validation' | 'not_found' | 'case_mismatch' | 'read_only';
      message: string };

export type MaybePromise<T> = T | Promise<T>;

/** 视图层统一的命令入口：P1 内存执行器同步返回，P4 HTTP 源返回 Promise。 */
export type GraphCommandRunner = (command: GraphCommand) => MaybePromise<CommandResult>;

/** 出处选择项（真实案件页按需从检索结果/材料/标注生成）。 */
export interface SourceOption {
  value: string;
  label: string;
}

/**
 * 出处候选检索：真实案件页接 /search 让用户挑解析段；原型不提供。
 * 返回的 value 必须是 `segment:<id>` / `material:<id>` / `annotation:<id>`。
 */
export type SourceOptionSearch = (query: string) => Promise<SourceOption[]>;

/**
 * 可注入的数据源边界（P4 接口契约确认后由 httpExecutor 实现）。
 * 视图只依赖这里的方法；内存源与远程源的差异（同步/异步、撤销、影响面）
 * 全部由实现方承担，组件不判断数据来自哪里。
 */
export interface GraphWorkspaceSource {
  load(): MaybePromise<GraphSnapshot>;
  rules(): MaybePromise<GraphRules>;
  /** 变化序号轮询；不提供时视图不做后台刷新。 */
  seqs?(): MaybePromise<{ graph: number; evidence: number }>;
  human(command: GraphCommand): MaybePromise<CommandResult>;
  /** 真实案件页不提供 Agent 写入；原型用它演示 MCP 提交。 */
  agent?(command: GraphCommand): MaybePromise<CommandResult>;
  /** 撤销最近一条人工写命令；真实源可用 revert_change 实现。 */
  undo?(): MaybePromise<CommandResult>;
  /** 删除主体/关系前的影响面；真实源缺失时界面不显示伪造数量。 */
  impact?(target: { entity_id?: string; relation_id?: string }):
    MaybePromise<{ graphs: { id: string; title: string }[] }>;
  /** 案件归档开关（案件级操作）；原型为内存状态，P4 走 casesApi。 */
  archive?(archived: boolean): MaybePromise<void>;
}

/** P1 内存执行器（同步）；P4 HTTP 执行器异步，视图层经 useGraphWorkspace 统一。 */
export interface GraphExecutor {
  getSnapshot(): GraphSnapshot;
  getRules(): GraphRules;
  getSeqs(): { graph: number; evidence: number };
  human(command: GraphCommand): CommandResult;
  agent(command: GraphCommand): CommandResult;
  /** 撤销最近一条人工写命令（快照冻结/客户同意/复核确认不进入撤销栈）。 */
  undo(): CommandResult;
  /** 删除主体/关系前查询受影响的图。 */
  getImpact(target: { entity_id?: string; relation_id?: string }): {
    graphs: { id: string; title: string }[];
  };
  /** 原型专用：模拟案件归档后的只读守卫。 */
  setArchived(archived: boolean): void;
  auditLog(): AuditEntry[];
}
