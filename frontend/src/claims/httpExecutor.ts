/**
 * P4 诉请金额 HTTP 数据源：把 claims 契约适配成 ClaimWorkspaceSource。
 *
 * - 金额一律整数分：录入字符串在本地换算成分后提交，界面展示也只用分；
 * - 候选金额走真实 GET /claims/amount-candidates，由界面按同币种过滤；
 * - 出处三选一（片段 / 材料 / 标注）在读取时按需解析成可预览的定位，
 *   解析失败只影响该条出处的展示，不阻塞整页；
 * - 写命令需要 revision 时取自最近一次快照，缺失则明确报冲突，不猜测。
 */
import { ApiError } from '../api/client';
import {
  annotationsApi, claimsApi, evidenceApi, materialsApi, searchApi,
} from '../api/endpoints';
import type { ClaimAmountCandidatesOut, ClaimSnapshotOut } from '../api/endpoints';
import { formatCents, currencyMismatchMessage, parseAmountToCents } from './money';
import type {
  AmountCandidate, ClaimCategory, ClaimChangeRecord, ClaimCommand, ClaimGroup, ClaimItem,
  ClaimSnapshot, ClaimSource, ClaimWorkspaceSource, CommandResult, Currency,
  PendingClaimChange,
} from './types';

type ApiItem = ClaimSnapshotOut['items'][number];

interface Lookups {
  materials: Map<string, string>;
  annotations: Map<string, { version: string | null; page: number | null; text: string }>;
}

function pageOf(locator: Record<string, unknown> | null | undefined): number | null {
  const value = locator?.page;
  return typeof value === 'number' && value >= 1 ? value : null;
}

function locatorText(locator: Record<string, unknown> | null | undefined): string {
  const unit = locator?.unit_key;
  return typeof unit === 'string' && unit ? unit : '片段';
}

function spanOf(span: Record<string, unknown> | null | undefined): { start: number; end: number } {
  const start = span?.start;
  const end = span?.end;
  return {
    start: typeof start === 'number' ? start : 0,
    end: typeof end === 'number' ? end : 0,
  };
}

function failFrom(error: unknown): CommandResult {
  if (error instanceof ApiError) {
    const code = error.code === 'conflict' ? 'conflict'
      : error.code === 'not_found' ? 'not_found'
        : error.code === 'forbidden' ? 'forbidden' : 'validation';
    return { ok: false, code, message: error.message };
  }
  return { ok: false, code: 'validation', message: '无法连接本地服务。' };
}

export interface ClaimExtras {
  materials: { id: string; display_name: string }[];
  annotations: { id: string; page: number | null; text: string; version: string | null }[];
}

/** 读取时把 API 快照映射成视图类型；出处按需解析（失败只丢该条定位）。 */
export async function mapClaimSnapshot(
  caseId: string, api: ClaimSnapshotOut, extras: ClaimExtras,
): Promise<ClaimSnapshot> {
  const lookups: Lookups = {
    materials: new Map(extras.materials.map((row) => [row.id, row.display_name])),
    annotations: new Map(extras.annotations.map((row) => [row.id, {
      version: row.version, page: row.page, text: row.text,
    }])),
  };
  const cache = new Map<string, ClaimSource | null>();

  async function resolve(ref: {
    segment_id?: string | null; material_id?: string | null; annotation_id?: string | null;
  }): Promise<ClaimSource | null> {
    const key = ref.segment_id ? `s:${ref.segment_id}`
      : ref.material_id ? `m:${ref.material_id}`
        : ref.annotation_id ? `a:${ref.annotation_id}` : '';
    if (!key) return null;
    if (cache.has(key)) return cache.get(key) ?? null;
    let source: ClaimSource | null = null;
    try {
      if (ref.segment_id) {
        const hit = await searchApi.readSource(caseId, ref.segment_id, 120);
        source = {
          segment_id: ref.segment_id, material_id: hit.material_id,
          material_title: hit.display_name ?? '材料', page: pageOf(hit.locator),
          locator: locatorText(hit.locator), excerpt: hit.text,
          span: { start: 0, end: 0 }, raw_text: '',
          source_ref: ref.segment_id, file_version_id: hit.file_version_id,
        };
      } else if (ref.material_id) {
        const original = await materialsApi.original(caseId, ref.material_id);
        source = {
          segment_id: '', material_id: ref.material_id,
          material_title: lookups.materials.get(ref.material_id) ?? '材料', page: null,
          locator: '整份材料', excerpt: '', span: { start: 0, end: 0 }, raw_text: '',
          file_version_id: original.file_version_id,
        };
      } else if (ref.annotation_id) {
        const annotation = lookups.annotations.get(ref.annotation_id);
        source = {
          segment_id: '', material_id: '', material_title: '标注出处',
          annotation_id: ref.annotation_id,
          page: annotation?.page ?? null, locator: '标注', excerpt: annotation?.text ?? '',
          span: { start: 0, end: 0 }, raw_text: '',
          file_version_id: annotation?.version ?? null,
        };
      }
    } catch {
      source = null;
    }
    cache.set(key, source);
    return source;
  }

  const items: ClaimItem[] = await Promise.all(api.items.map(async (item) => ({
    id: item.id, case_id: item.case_id, group_id: item.group_id, title: item.title,
    category: item.category as ClaimCategory, amount_cents: item.amount_cents,
    calc_note: item.calc_note ?? '',
    source: await resolve(item),
    position: item.position, origin: item.created_by,
    // pending 修改不在明细行里；agent 写入的行只有在确认后才成为有效行。
    review_state: item.created_by === 'agent' ? 'accepted' : 'none',
    revision: item.revision, removed_at: item.removed_at,
  })));

  const groups: ClaimGroup[] = api.groups.map((group) => ({
    id: group.id, case_id: group.case_id, title: group.title,
    currency: group.currency as Currency, include_in_total: group.include_in_total,
    position: group.position, note: group.note ?? '', revision: group.revision,
    removed_at: group.removed_at,
  }));
  const groupTitles = new Map(groups.map((group) => [group.id, group.title]));

  function itemFromDict(dict: Record<string, unknown>): ClaimItem {
    return {
      id: String(dict.id ?? ''),
      case_id: String(dict.case_id ?? api.case_id),
      group_id: String(dict.group_id ?? ''),
      title: String(dict.title ?? ''),
      category: (dict.category ?? 'other') as ClaimCategory,
      amount_cents: Number(dict.amount_cents ?? 0),
      calc_note: typeof dict.calc_note === 'string' ? dict.calc_note : '',
      source: null,
      position: Number(dict.position ?? 0),
      origin: (dict.created_by === 'agent' ? 'agent' : 'human'),
      review_state: 'pending',
      revision: Number(dict.revision ?? 0),
      removed_at: typeof dict.removed_at === 'string' ? dict.removed_at : null,
    };
  }

  const pending: PendingClaimChange[] = [];
  for (const change of api.pending_changes) {
    for (const entry of change.diff) {
      if (entry.target !== 'item') continue;
      const afterDict = entry.after;
      if (!afterDict) continue;
      const after = itemFromDict(afterDict);
      after.source = await resolve({
        segment_id: (afterDict.segment_id as string | null) ?? null,
        material_id: (afterDict.material_id as string | null) ?? null,
        annotation_id: (afterDict.annotation_id as string | null) ?? null,
      });
      pending.push({
        id: change.id, seq: change.seq,
        command: change.command.includes('add') ? 'add_item' : 'update_item',
        created_at: change.created_at, group_id: after.group_id,
        group_title: groupTitles.get(after.group_id) ?? '（已删除分组）',
        before: entry.before ? itemFromDict(entry.before) : null,
        after,
      });
    }
  }

  const changes: ClaimChangeRecord[] = api.changes.map((change) => {
    const itemEntry = change.diff.find((entry) => entry.target === 'item');
    const groupEntry = change.diff.find((entry) => entry.target === 'group');
    const record = (itemEntry?.after ?? itemEntry?.before ?? groupEntry?.after
      ?? groupEntry?.before ?? {}) as Record<string, unknown>;
    const title = typeof record.title === 'string' ? record.title : '';
    const action = change.command.startsWith('add') ? '新增'
      : change.command.startsWith('remove') ? '删除'
        : change.command.startsWith('accept') ? '确认'
          : change.command.startsWith('revert') ? '撤回' : '修改';
    return {
      id: change.id, seq: change.seq, command: change.command,
      actor: change.actor, review_state: change.review_state,
      created_at: change.created_at,
      group_id: typeof record.group_id === 'string' ? record.group_id : null,
      item_id: itemEntry ? String(record.id ?? '') : null,
      summary: `${action}${groupEntry && !itemEntry ? '分组' : '金额项'}${title ? `「${title}」` : ''}`,
    };
  });

  return {
    case_id: api.case_id, case_title: '', seq: api.seq, groups, items,
    pending_changes: pending, changes,
    group_subtotals: api.group_subtotals,
    totals_by_currency: api.totals_by_currency.map((total) => ({
      currency: total.currency as Currency,
      total_cents: total.total_cents,
      included_group_ids: total.included_group_ids,
    })),
    pending_count: api.pending_count,
    excluded_group_count: api.excluded_group_count,
    included_group_count: api.included_group_count,
  };
}

function centsOf(amount: string, currency: Currency): { cents: number } | { message: string } {
  const parsed = parseAmountToCents(amount);
  if (!parsed.ok) return { message: parsed.message };
  const mismatch = currencyMismatchMessage(parsed.currency, currency);
  if (mismatch) return { message: mismatch };
  return { cents: parsed.cents };
}

function refOf(source: ClaimSource | null | undefined): {
  segment_id: string | null; material_id: string | null; annotation_id: string | null;
} {
  if (source?.segment_id) {
    return { segment_id: source.segment_id, material_id: null, annotation_id: null };
  }
  if (source?.material_id) {
    return { segment_id: null, material_id: source.material_id, annotation_id: null };
  }
  if (source?.annotation_id) {
    return { segment_id: null, material_id: null, annotation_id: source.annotation_id };
  }
  return { segment_id: null, material_id: null, annotation_id: null };
}

/** 真实案件页的金额候选：GET 本案精确值，金额一律转成整数分。 */
export function candidateSearch(caseId: string): (query: string) => Promise<AmountCandidate[]> {
  return async (query: string) => {
    const out: ClaimAmountCandidatesOut = await claimsApi.amountCandidates(caseId, query.trim());
    return out.candidates.map((row) => ({
      id: row.exact_value_id,
      label: `${row.material_title}${row.page ? ` · 第${row.page}页` : ''}`,
      raw_text: row.raw_text,
      amount_text: row.amount_cents !== null ? formatCents(row.amount_cents) : '',
      currency: row.currency as Currency,
      amount_cents: row.amount_cents,
      source: {
        segment_id: row.segment_id, material_id: row.material_id,
        material_title: row.material_title, page: row.page,
        locator: locatorText(row.locator), excerpt: row.excerpt,
        span: spanOf(row.span), raw_text: row.raw_text,
        source_ref: row.segment_id, file_version_id: null,
      },
    }));
  };
}

export function createHttpClaimSource(caseId: string): ClaimWorkspaceSource {
  let last: ClaimSnapshot | null = null;
  let extras: ClaimExtras | null = null;

  /** 材料名/标注只需读一次；读失败不阻塞金额，只是出处少一个标题。 */
  async function lookups(): Promise<ClaimExtras> {
    if (!extras) {
      try {
        extras = await loadClaimExtras(caseId);
      } catch {
        extras = { materials: [], annotations: [] };
      }
    }
    return extras;
  }

  async function load(): Promise<ClaimSnapshot> {
    const api = await claimsApi.snapshot(caseId);
    const snapshot = await mapClaimSnapshot(caseId, api, await lookups());
    // 页头标题来自案件概览；不影响金额与出处。
    last = snapshot;
    return snapshot;
  }

  function revisionOf(kind: 'group' | 'item', id: string): number | null {
    if (!last) return null;
    const row = (kind === 'group' ? last.groups : last.items).find((entry) => entry.id === id);
    return row ? row.revision : null;
  }

  function requireRevision(kind: 'group' | 'item', id: string): number {
    const revision = revisionOf(kind, id);
    if (revision === null) {
      throw new ApiError(409, {
        code: 'conflict', message: '本地数据已过期，请刷新后重试。',
        details: {}, request_id: '',
      });
    }
    return revision;
  }

  async function run(command: ClaimCommand): Promise<CommandResult> {
    try {
      let api: ClaimSnapshotOut;
      switch (command.type) {
        case 'add_group':
          api = await claimsApi.addGroup(caseId, {
            title: command.title, currency: command.currency, include_in_total: true,
            note: command.note ?? null, request_key: null,
          });
          break;
        case 'update_group': {
          const changes: Record<string, unknown> = {};
          for (const key of ['title', 'currency', 'note', 'include_in_total'] as const) {
            if (key in command) changes[key] = command[key] ?? null;
          }
          api = await claimsApi.updateGroup(caseId, command.group_id, {
            expected_revision: requireRevision('group', command.group_id),
            changes: changes as never,
            // 人工入口不需要幂等键；显式传 null，避免契约默认值歧义。
            request_key: null,
          });
          break;
        }
        case 'reorder_groups':
          api = await claimsApi.moveGroup(caseId, command.group_id,
            requireRevision('group', command.group_id), command.direction);
          break;
        case 'remove_group':
          api = await claimsApi.removeGroup(caseId, command.group_id,
            requireRevision('group', command.group_id));
          break;
        case 'add_item': {
          const group = last?.groups.find((entry) => entry.id === command.group_id);
          const amount = centsOf(command.amount, group?.currency ?? 'CNY');
          if ('message' in amount) return { ok: false, code: 'validation', message: amount.message };
          api = await claimsApi.addItem(caseId, {
            group_id: command.group_id, title: command.title, category: command.category,
            amount_cents: amount.cents, calc_note: command.calc_note ?? null,
            ...refOf(command.source), request_key: null,
          });
          break;
        }
        case 'update_item': {
          const item = last?.items.find((entry) => entry.id === command.item_id);
          const group = last?.groups.find((entry) => entry.id === item?.group_id);
          const changes: Record<string, unknown> = {};
          if (command.title !== undefined) changes.title = command.title;
          if (command.category !== undefined) changes.category = command.category;
          if (command.calc_note !== undefined) changes.calc_note = command.calc_note;
          if (command.amount !== undefined) {
            const amount = centsOf(command.amount, group?.currency ?? 'CNY');
            if ('message' in amount) return { ok: false, code: 'validation', message: amount.message };
            changes.amount_cents = amount.cents;
          }
          if (command.source !== undefined) Object.assign(changes, refOf(command.source));
          api = await claimsApi.updateItem(caseId, command.item_id, {
            expected_revision: requireRevision('item', command.item_id),
            changes: changes as never,
            request_key: null,
          });
          break;
        }
        case 'reorder_items':
          api = await claimsApi.moveItem(caseId, command.item_id,
            requireRevision('item', command.item_id), command.direction);
          break;
        case 'remove_item':
          api = await claimsApi.removeItem(caseId, command.item_id,
            requireRevision('item', command.item_id));
          break;
        case 'accept_change':
          api = await claimsApi.acceptChange(caseId, command.change_id);
          break;
        case 'revert_change':
          api = await claimsApi.revertChange(caseId, command.change_id);
          break;
        default:
          return { ok: false, code: 'validation', message: '未知命令' };
      }
      const snapshot = await mapClaimSnapshot(caseId, api, await lookups());
      last = snapshot;
      return { ok: true, snapshot };
    } catch (error) {
      return failFrom(error);
    }
  }

  return {
    load,
    seq: async () => (await claimsApi.seq(caseId)).seqs.claims ?? 0,
    human: (command) => run(command),
  };
}

/** 页头与出处选择需要的案内材料/标注清单（一次读取，随快照刷新）。 */
export async function loadClaimExtras(caseId: string): Promise<ClaimExtras> {
  const [catalog, annotations] = await Promise.all([
    evidenceApi.catalog(caseId),
    annotationsApi.list(caseId, '', 0, 200),
  ]);
  return {
    materials: catalog.materials.map((row) => ({ id: row.id, display_name: row.display_name })),
    annotations: annotations.items.map((row) => ({
      id: row.annotation_id, page: row.page ?? null,
      text: row.reviewed_text || row.body, version: row.file_version_id ?? null,
    })),
  };
}
