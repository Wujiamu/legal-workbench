/**
 * 诉请金额 P1 原型的数据契约（TEST-ONLY，内存）。
 * 与 docs/changes/20261120-期限时间轴与诉请金额.md 第二节、决策 D5 对齐：
 * 金额一律整数分，类别决定加减，一案多组、按币种汇总。
 */

export type ClaimCategory =
  | 'principal'
  | 'interest'
  | 'liquidated_damages'
  | 'expense'
  | 'deduction'
  | 'other';

export const CLAIM_CATEGORIES: { value: ClaimCategory; label: string }[] = [
  { value: 'principal', label: '本金' },
  { value: 'interest', label: '利息' },
  { value: 'liquidated_damages', label: '违约金' },
  { value: 'expense', label: '费用' },
  { value: 'deduction', label: '抵扣' },
  { value: 'other', label: '其他' },
];

export const CATEGORY_LABELS: Record<ClaimCategory, string> = {
  principal: '本金',
  interest: '利息',
  liquidated_damages: '违约金',
  expense: '费用',
  deduction: '抵扣',
  other: '其他',
};

export const CURRENCIES = ['CNY', 'USD', 'EUR'] as const;
export type Currency = (typeof CURRENCIES)[number];

export const CURRENCY_LABELS: Record<Currency, string> = {
  CNY: '人民币',
  USD: '美元',
  EUR: '欧元',
};

/** 出处：与后端 claim_items 的三选一引用同形；原型只存合成材料与片段。 */
export interface ClaimSource {
  segment_id: string;
  material_id: string;
  material_title: string;
  /** 标注出处（与片段/材料三选一）。 */
  annotation_id?: string | null;
  /** 页码；整份材料或无法定位时为 null。 */
  page: number | null;
  locator: string;
  /** 合成原文（P1 用于模拟 SourcePreview；不请求真实文件接口）。 */
  excerpt: string;
  span: { start: number; end: number };
  /** 抽取时的原文金额写法，例如「人民币1,280,000元」。 */
  raw_text: string;
  /** 真实案件页：定位到不可变片段 / 文件版本，交给 SourcePreview。 */
  source_ref?: string | null;
  file_version_id?: string | null;
}

export interface ClaimGroup {
  id: string;
  case_id: string;
  title: string;
  currency: Currency;
  include_in_total: boolean;
  position: number;
  note: string;
  revision: number;
  removed_at: string | null;
}

export interface ClaimItem {
  id: string;
  case_id: string;
  group_id: string;
  title: string;
  category: ClaimCategory;
  /** 整数分，>= 0；抵扣方向由 category 决定。 */
  amount_cents: number;
  calc_note: string;
  source: ClaimSource | null;
  position: number;
  origin: 'human' | 'agent';
  review_state: 'none' | 'pending' | 'accepted';
  revision: number;
  removed_at: string | null;
}

/** Agent 提交的候选修改：暂存，人工确认前不写入明细、不计入合计。 */
export interface PendingClaimChange {
  id: string;
  seq: number;
  command: 'add_item' | 'update_item';
  created_at: string;
  group_id: string;
  group_title: string;
  before: ClaimItem | null;
  after: ClaimItem;
}

export interface ClaimChangeRecord {
  id: string;
  seq: number;
  command: string;
  actor: 'human' | 'agent';
  review_state: 'none' | 'pending' | 'accepted' | 'reverted';
  created_at: string;
  group_id: string | null;
  item_id: string | null;
  summary: string;
}

export interface CurrencyTotal {
  currency: Currency;
  total_cents: number;
  included_group_ids: string[];
}

export interface ClaimSnapshot {
  case_id: string;
  case_title: string;
  seq: number;
  groups: ClaimGroup[];
  items: ClaimItem[];
  pending_changes: PendingClaimChange[];
  changes: ClaimChangeRecord[];
  group_subtotals: Record<string, number>;
  totals_by_currency: CurrencyTotal[];
  pending_count: number;
  excluded_group_count: number;
  included_group_count: number;
}

export type ClaimCommand =
  | { type: 'add_group'; title: string; currency: Currency; note?: string }
  | {
      type: 'update_group';
      group_id: string;
      title?: string;
      currency?: Currency;
      note?: string;
      include_in_total?: boolean;
    }
  | { type: 'reorder_groups'; group_id: string; direction: 'up' | 'down' }
  | { type: 'remove_group'; group_id: string }
  | {
      type: 'add_item';
      group_id: string;
      title: string;
      category: ClaimCategory;
      /** 金额字符串（元），由执行器统一换算为整数分。 */
      amount: string;
      calc_note?: string;
      source?: ClaimSource | null;
    }
  | {
      type: 'update_item';
      item_id: string;
      title?: string;
      category?: ClaimCategory;
      amount?: string;
      calc_note?: string;
      source?: ClaimSource | null;
    }
  | { type: 'reorder_items'; item_id: string; direction: 'up' | 'down' }
  | { type: 'remove_item'; item_id: string }
  | { type: 'accept_change'; change_id: string }
  | { type: 'revert_change'; change_id: string };

/** 页面传入的金额候选检索：真实案件页用 GET /claims/amount-candidates，原型用合成数据。 */
export interface AmountCandidate {
  id: string;
  label: string;
  raw_text: string;
  /** 可填入金额输入框的字符串（元）；由整数分格式化，避免二次解析误差。 */
  amount_text: string;
  currency: Currency;
  /** 整数分；服务端判定无法精确到分时为 null（缺省表示由 amount_text 解析）。 */
  amount_cents?: number | null;
  source: ClaimSource | null;
}

export type AmountCandidateSearch = (query: string) => Promise<AmountCandidate[]>;

export interface CommandResult {
  ok: boolean;
  code?: 'validation' | 'not_found' | 'conflict' | 'forbidden';
  message?: string;
  snapshot?: ClaimSnapshot;
}

export type MaybePromise<T> = T | Promise<T>;

/** 视图层统一命令入口：P1 内存执行器同步，P4 HTTP 源异步。 */
export type ClaimCommandRunner = (command: ClaimCommand) => MaybePromise<CommandResult>;

/**
 * 可注入的数据源边界（P4 接口契约确认后由 httpExecutor 实现）。
 * 视图不判断数据来自内存还是 HTTP；快照仍以服务端返回为准。
 */
export interface ClaimWorkspaceSource {
  load(): MaybePromise<ClaimSnapshot>;
  human(command: ClaimCommand): MaybePromise<CommandResult>;
  /** 真实案件页不提供 Agent 写入；原型用它演示 MCP 提交。 */
  agent?(command: ClaimCommand): MaybePromise<CommandResult>;
  /** 变化序号轮询；不提供时不做后台刷新。 */
  seq?(): MaybePromise<number>;
}

export interface ClaimExecutor {
  getSnapshot(): ClaimSnapshot;
  /** 人工入口：全部命令。 */
  human(command: ClaimCommand): CommandResult;
  /** Agent 入口（模拟 MCP/CLI）：只允许提交分组/金额项，确认与撤回被拒。 */
  agent(command: ClaimCommand): CommandResult;
}
