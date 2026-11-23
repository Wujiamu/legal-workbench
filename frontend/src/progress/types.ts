/**
 * 办案进度模型的 TS 类型（P1 原型）。
 * 与总体计划 6.1/6.2 的数据库结构和服务命令一一对应；
 * P3 生成 OpenAPI 类型后，这里只保留前端补充类型。
 */

export type Actor = 'human' | 'agent';
export type Entry = 'local-web' | 'mcp' | 'cli' | 'prototype';

/** 节点种类与合法状态由规则配置定义，数据库只校验非空（K2/K3）。 */
export interface KindRule {
  label: string;
  states: { value: string; label: string }[];
  default_state: string;
}

/** 新建节点时的标题预设（工作/事件等）；空数组 = 自由输入。选「其他」才填自定义。 */
export type TitlePresets = Record<string, string[]>;

export interface FragmentNode {
  kind: string;
  title: string;
  state?: string;
  children?: FragmentNode[];
}

export interface FragmentRule {
  label: string;
  version: number;
  roots: FragmentNode[];
}

export interface ProgressRules {
  kinds: Record<string, KindRule>;
  /** 父种类（根用 'case'）→ 允许的子种类；层级限制只在这里（K1）。 */
  containment: Record<string, string[]>;
  /** 首版界面只开放「引起」「关联」（K4）。 */
  relation_types: { value: string; label: string }[];
  fragments: Record<string, FragmentRule>;
  default_fragment: string;
  /** Agent 可用命令；确认与撤回不在其中（K5/6.3）。 */
  agent_commands: string[];
  /** 新建节点的标题预设，按种类配置；缺省 = 自由输入。 */
  title_presets?: TitlePresets;
}

export interface ProgressNode {
  id: string;
  case_id: string;
  parent_id: string | null;
  position: number;
  kind: string;
  state: string;
  title: string;
  note: string | null;
  date_start: string | null;
  date_end: string | null;
  template_ref: string | null;
  created_by: Actor;
  updated_by: Actor;
  request_key: string | null;
  removed_at: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface ProgressRelation {
  id: string;
  case_id: string;
  from_node_id: string;
  to_node_id: string;
  type: string;
  note: string | null;
  created_by: Actor;
  removed_at: string | null;
  created_at: string;
}

export interface ProgressLink {
  id: string;
  case_id: string;
  node_id: string;
  material_id: string | null;
  analysis_id: string | null;
  annotation_id: string | null;
  created_by: Actor;
  created_at: string;
  /** 快照附带显示名，便于视图直接展示（6.2）。 */
  display_name: string;
}

export interface ChangeDiffEntry {
  target: 'node' | 'relation' | 'link';
  id: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  before_rev: number | null;
  after_rev: number | null;
}

export type ReviewState = 'none' | 'pending' | 'accepted' | 'reverted';

export interface ProgressChange {
  id: string;
  case_id: string;
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

export interface ProgressSnapshot {
  case_id: string;
  seq: number;
  nodes: ProgressNode[];
  relations: ProgressRelation[];
  links: ProgressLink[];
  pending_changes: ProgressChange[];
  /** 全部变更历史（含已确认/已撤回），供 Agent 徽标与撤销栈使用。 */
  changes: ProgressChange[];
  /** 审查 N3：写命令响应携带本次变更 ID，前端按此入撤销栈（并发下不错认）；读快照为 null。 */
  last_change_id?: string | null;
}

export type NodeChanges = Partial<Pick<ProgressNode,
  'title' | 'state' | 'note' | 'date_start' | 'date_end'>>;

export type ProgressCommand =
  | { type: 'add_node'; parent_id: string | null; kind: string; title: string;
      position?: number; state?: string; note?: string | null;
      date_start?: string | null; date_end?: string | null; request_key?: string }
  | { type: 'update_node'; node_id: string; expected_revision: number; changes: NodeChanges }
  | { type: 'move_node'; node_id: string; expected_revision: number;
      new_parent_id: string | null; before_node_id?: string | null }
  | { type: 'set_active_stage'; stage_id: string; expected_revision: number }
  | { type: 'insert_fragment'; parent_id: string | null; fragment_key: string;
      before_node_id?: string | null }
  | { type: 'remove_node'; node_id: string; expected_revision: number }
  | { type: 'restore_node'; node_id: string; expected_revision: number }
  | { type: 'add_relation'; from_node_id: string; to_node_id: string;
      relation_type: string; note?: string }
  | { type: 'remove_relation'; relation_id: string }
  | { type: 'link'; node_id: string;
      target: { material_id: string } | { analysis_id: string } | { annotation_id: string } }
  | { type: 'unlink'; link_id: string }
  | { type: 'accept_change'; change_id: string }
  | { type: 'accept_all'; change_ids?: string[]; expected_seq?: number | null }
  | { type: 'revert_change'; change_id: string };

export type CommandResult =
  | { ok: true; snapshot: ProgressSnapshot }
  | { ok: false; code: 'conflict' | 'validation' | 'not_found' | 'case_mismatch'; message: string };

/** P4 HTTP 执行器异步返回；P1 内存执行器同步返回。钩子两者都支持。 */
export type MaybePromise<T> = T | Promise<T>;

export interface AuditEntry {
  operation: string;
  actor: Actor;
  entry: Entry;
  target_ids: string[];
}

/** 原型与组件测试用的内存执行器接口（同步）；P4 HTTP 执行器异步，见 AsyncProgressExecutor。 */
export interface ProgressExecutor {
  getSnapshot(): ProgressSnapshot;
  getRules(): ProgressRules;
  getSeqs(): Record<string, number>;
  /** actor 由入口决定，调用方不能自报（子计划审核清单 2）。 */
  human(command: ProgressCommand): CommandResult;
  agent(command: ProgressCommand): CommandResult;
  auditLog(): AuditEntry[];
}

/** P4 HTTP 执行器：同样的命令语义，异步返回。useCaseProgress 两者都接受。 */
export interface AsyncProgressExecutor {
  getSnapshot(): Promise<ProgressSnapshot>;
  getRules(): ProgressRules;
  getSeqs(): Promise<Record<string, number>>;
  human(command: ProgressCommand): Promise<CommandResult>;
  agent(command: ProgressCommand): Promise<CommandResult>;
  auditLog(): AuditEntry[];
}

export type AnyProgressExecutor = ProgressExecutor | AsyncProgressExecutor;