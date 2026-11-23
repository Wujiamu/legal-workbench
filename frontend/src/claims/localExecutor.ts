/**
 * P1 诉请金额内存执行器：与计划中的 claims 服务同一套命令语义。
 * 校验 → 变更/暂存 → 变更记录 → 序号 +1 → 返回快照。
 * Agent 提交的金额项先进入待确认区，人工确认前不写入明细、不计入合计。
 * 仅供 /prototype/claims 与组件测试使用，不接真实接口、不写数据库。
 */
import { currencyMismatchMessage, parseAmountToCents, signedCents, sumCents } from './money';
import {
  CATEGORY_LABELS, CURRENCIES,
} from './types';
import type {
  ClaimCategory, ClaimChangeRecord, ClaimCommand, ClaimExecutor, ClaimGroup,
  ClaimItem, ClaimSnapshot, ClaimSource, CommandResult, Currency, CurrencyTotal,
  PendingClaimChange,
} from './types';

const TITLE_MAX = 200;
const NOTE_MAX = 2000;

function now(): string {
  return new Date().toISOString();
}

function newId(): string {
  return crypto.randomUUID();
}

function fail(code: CommandResult['code'], message: string): CommandResult {
  return { ok: false, code, message };
}

function textError(value: string, label: string, max: number): string | null {
  const trimmed = value.trim();
  if (!trimmed) return `${label}不能为空`;
  if (trimmed.length > max) return `${label}不能超过 ${max} 字`;
  return null;
}

function isCategory(value: string): value is ClaimCategory {
  return Object.prototype.hasOwnProperty.call(CATEGORY_LABELS, value);
}

function isCurrency(value: string): value is Currency {
  return (CURRENCIES as readonly string[]).includes(value);
}

export interface ClaimExecutorOptions {
  caseId: string;
  caseTitle: string;
}

interface ItemFields {
  title: string;
  category: ClaimCategory;
  amount_cents: number;
  calc_note: string;
  source: ClaimSource | null;
}

export function createClaimExecutor({ caseId, caseTitle }: ClaimExecutorOptions): ClaimExecutor {
  const groups = new Map<string, ClaimGroup>();
  const items = new Map<string, ClaimItem>();
  const pending: PendingClaimChange[] = [];
  const changes: ClaimChangeRecord[] = [];
  let seq = 0;

  function activeGroups(): ClaimGroup[] {
    return [...groups.values()]
      .filter((group) => group.removed_at === null)
      .sort((a, b) => a.position - b.position);
  }

  function activeItems(groupId?: string): ClaimItem[] {
    const groupPositions = new Map(activeGroups().map((group) => [group.id, group.position]));
    return [...items.values()]
      .filter((item) => item.removed_at === null && (!groupId || item.group_id === groupId))
      .sort((a, b) => (groupPositions.get(a.group_id) ?? 0) - (groupPositions.get(b.group_id) ?? 0)
        || a.position - b.position);
  }

  function snapshot(): ClaimSnapshot {
    const groupList = activeGroups();
    const itemList = activeItems();
    const groupSubtotals: Record<string, number> = {};
    for (const group of groupList) {
      groupSubtotals[group.id] = sumCents(itemList
        .filter((item) => item.group_id === group.id)
        .map((item) => signedCents(item.category, item.amount_cents)));
    }
    const totals = new Map<Currency, CurrencyTotal>();
    for (const group of groupList) {
      if (!group.include_in_total) continue;
      const entry = totals.get(group.currency)
        ?? { currency: group.currency, total_cents: 0, included_group_ids: [] };
      entry.total_cents = sumCents([entry.total_cents, groupSubtotals[group.id]]);
      entry.included_group_ids.push(group.id);
      totals.set(group.currency, entry);
    }
    const order: Currency[] = [...CURRENCIES];
    const totalsByCurrency = [...totals.values()]
      .sort((a, b) => order.indexOf(a.currency) - order.indexOf(b.currency));
    return {
      case_id: caseId,
      case_title: caseTitle,
      seq,
      groups: groupList.map((group) => structuredClone(group)),
      items: itemList.map((item) => structuredClone(item)),
      pending_changes: pending.map((change) => structuredClone(change)),
      changes: changes.map((change) => structuredClone(change)),
      group_subtotals: groupSubtotals,
      totals_by_currency: totalsByCurrency,
      pending_count: pending.length,
      excluded_group_count: groupList.filter((group) => !group.include_in_total).length,
      included_group_count: groupList.filter((group) => group.include_in_total).length,
    };
  }

  interface StateBackup {
    groups: Map<string, ClaimGroup>;
    items: Map<string, ClaimItem>;
    pending: PendingClaimChange[];
    changes: ClaimChangeRecord[];
    seq: number;
  }

  function captureState(): StateBackup {
    return {
      groups: new Map([...groups].map(([id, group]) => [id, structuredClone(group)])),
      items: new Map([...items].map(([id, item]) => [id, structuredClone(item)])),
      pending: structuredClone(pending),
      changes: structuredClone(changes),
      seq,
    };
  }

  function restoreState(backup: StateBackup): void {
    groups.clear();
    for (const [id, group] of backup.groups) groups.set(id, group);
    items.clear();
    for (const [id, item] of backup.items) items.set(id, item);
    pending.splice(0, pending.length, ...backup.pending);
    changes.splice(0, changes.length, ...backup.changes);
    seq = backup.seq;
  }

  /**
   * 命令边界：写入后的快照可能因整数分溢出（例如本金 90 万亿元再加 0.01）而拒绝，
   * 此时整体回滚，返回受控失败，明细、待确认、变更记录与 seq 都保持写前状态。
   */
  function guard(run: () => CommandResult): CommandResult {
    const backup = captureState();
    try {
      return run();
    } catch (error) {
      restoreState(backup);
      const detail = error instanceof Error ? error.message : String(error);
      return fail('validation', `${detail}；本次修改未保存，明细、待确认与变更序号保持修改前状态`);
    }
  }

  function recordChange(command: string, actor: 'human' | 'agent',
                        reviewState: ClaimChangeRecord['review_state'], summary: string,
                        groupId: string | null, itemId: string | null): ClaimChangeRecord {
    seq += 1;
    const change: ClaimChangeRecord = {
      id: newId(), seq, command, actor, review_state: reviewState,
      created_at: now(), group_id: groupId, item_id: itemId, summary,
    };
    changes.push(change);
    return change;
  }

  function requireGroup(groupId: string): ClaimGroup | CommandResult {
    const group = groups.get(groupId);
    if (!group || group.removed_at !== null) return fail('not_found', '诉请分组不存在');
    if (group.case_id !== caseId) return fail('conflict', '分组不属于本案');
    return group;
  }

  function requireItem(itemId: string): ClaimItem | CommandResult {
    const item = items.get(itemId);
    if (!item || item.removed_at !== null) return fail('not_found', '金额项不存在');
    if (item.case_id !== caseId) return fail('conflict', '金额项不属于本案');
    return item;
  }

  function pendingForItem(itemId: string): PendingClaimChange | undefined {
    return pending.find((change) => change.after.id === itemId || change.before?.id === itemId);
  }

  function nextItemPosition(groupId: string): number {
    const positions = [
      ...activeItems(groupId).map((item) => item.position),
      ...pending.filter((change) => change.command === 'add_item' && change.group_id === groupId)
        .map((change) => change.after.position),
    ];
    return positions.length ? Math.max(...positions) + 1 : 0;
  }

  function normalizeGroupPositions(): void {
    activeGroups().forEach((group, index) => { group.position = index; });
  }

  function normalizeItemPositions(groupId: string): void {
    activeItems(groupId).forEach((item, index) => { item.position = index; });
  }

  function readGroupFields(command: { title?: string; currency?: string; note?: string },
                           existing: ClaimGroup | null): { ok: true; fields: Partial<ClaimGroup> } | CommandResult {
    const fields: Partial<ClaimGroup> = {};
    if (command.title !== undefined || !existing) {
      const title = (command.title ?? '').trim();
      const error = textError(title, '分组名称', TITLE_MAX);
      if (error) return fail('validation', error);
      fields.title = title;
    }
    if (command.currency !== undefined || !existing) {
      const currency = command.currency ?? 'CNY';
      if (!isCurrency(currency)) return fail('validation', `不支持的币种：${currency}`);
      fields.currency = currency;
    }
    if (command.note !== undefined || !existing) {
      const note = (command.note ?? '').trim();
      if (note.length > NOTE_MAX) return fail('validation', `备注不能超过 ${NOTE_MAX} 字`);
      fields.note = note;
    }
    return { ok: true, fields };
  }

  function readItemFields(command: Extract<ClaimCommand, { type: 'add_item' | 'update_item' }>,
                          existing: ClaimItem | null,
                          groupCurrency: Currency): { ok: true; fields: ItemFields } | CommandResult {
    const title = command.title !== undefined ? command.title.trim() : existing?.title ?? '';
    if (command.title !== undefined || !existing) {
      const error = textError(title, '金额项名称', TITLE_MAX);
      if (error) return fail('validation', error);
    }
    const category = command.category ?? existing?.category;
    if (!category || !isCategory(category)) return fail('validation', '请选择金额类别');
    let amountCents = existing?.amount_cents;
    if (command.amount !== undefined) {
      const parsed = parseAmountToCents(command.amount);
      if (!parsed.ok) return fail('validation', parsed.message);
      const mismatch = currencyMismatchMessage(parsed.currency, groupCurrency);
      if (mismatch) return fail('validation', mismatch);
      amountCents = parsed.cents;
    }
    if (amountCents === undefined) return fail('validation', '请输入金额');
    const calcNote = command.calc_note !== undefined ? command.calc_note.trim() : existing?.calc_note ?? '';
    if (calcNote.length > NOTE_MAX) return fail('validation', `计算说明不能超过 ${NOTE_MAX} 字`);
    const source = command.source !== undefined ? command.source : existing?.source ?? null;
    return {
      ok: true,
      fields: { title, category, amount_cents: amountCents, calc_note: calcNote,
        source: source ? structuredClone(source) : null },
    };
  }

  function makeItem(groupId: string, fields: ItemFields, position: number,
                    reviewState: ClaimItem['review_state']): ClaimItem {
    return {
      id: newId(), case_id: caseId, group_id: groupId,
      title: fields.title, category: fields.category, amount_cents: fields.amount_cents,
      calc_note: fields.calc_note, source: fields.source, position,
      origin: reviewState === 'pending' ? 'agent' : 'human',
      review_state: reviewState, revision: 0, removed_at: null,
    };
  }

  function addGroup(command: Extract<ClaimCommand, { type: 'add_group' }>): CommandResult {
    const read = readGroupFields(command, null);
    if ('ok' in read && read.ok === false) return read;
    if (!('fields' in read)) return read;
    const group: ClaimGroup = {
      id: newId(), case_id: caseId,
      title: read.fields.title!, currency: read.fields.currency!, include_in_total: true,
      position: activeGroups().length, note: read.fields.note ?? '',
      revision: 0, removed_at: null,
    };
    groups.set(group.id, group);
    recordChange('add_group', 'human', 'none', `新建分组「${group.title}」`, group.id, null);
    return { ok: true, snapshot: snapshot() };
  }

  function updateGroup(command: Extract<ClaimCommand, { type: 'update_group' }>): CommandResult {
    const group = requireGroup(command.group_id);
    if (!('id' in group)) return group;
    const read = readGroupFields(command, group);
    if ('ok' in read && read.ok === false) return read;
    if (!('fields' in read)) return read;
    Object.assign(group, read.fields);
    if (command.include_in_total !== undefined) group.include_in_total = command.include_in_total;
    group.revision += 1;
    const summary = command.include_in_total !== undefined && Object.keys(read.fields).length === 0
      ? `${group.title}：${group.include_in_total ? '计入' : '不计入'}全案总计`
      : `修改分组「${group.title}」`;
    recordChange('update_group', 'human', 'none', summary, group.id, null);
    return { ok: true, snapshot: snapshot() };
  }

  function reorderGroups(command: Extract<ClaimCommand, { type: 'reorder_groups' }>): CommandResult {
    const list = activeGroups();
    const index = list.findIndex((group) => group.id === command.group_id);
    if (index < 0) return fail('not_found', '诉请分组不存在');
    const target = command.direction === 'up' ? index - 1 : index + 1;
    if (target < 0 || target >= list.length) {
      return fail('validation', command.direction === 'up' ? '已经是第一个分组' : '已经是最后一个分组');
    }
    [list[index].position, list[target].position] = [list[target].position, list[index].position];
    normalizeGroupPositions();
    recordChange('reorder_groups', 'human', 'none', `调整分组顺序：${list[index].title}`, list[index].id, null);
    return { ok: true, snapshot: snapshot() };
  }

  function removeGroup(command: Extract<ClaimCommand, { type: 'remove_group' }>): CommandResult {
    const group = requireGroup(command.group_id);
    if (!('id' in group)) return group;
    const stamp = now();
    group.removed_at = stamp;
    group.revision += 1;
    let removedItems = 0;
    for (const item of items.values()) {
      if (item.group_id === group.id && item.removed_at === null) {
        item.removed_at = stamp;
        item.revision += 1;
        removedItems += 1;
      }
    }
    for (let index = pending.length - 1; index >= 0; index -= 1) {
      if (pending[index].group_id === group.id) pending.splice(index, 1);
    }
    recordChange('remove_group', 'human', 'none',
      `删除分组「${group.title}」（含 ${removedItems} 个金额项）`, group.id, null);
    return { ok: true, snapshot: snapshot() };
  }

  function stageOrAddItem(command: Extract<ClaimCommand, { type: 'add_item' }>, actor: 'human' | 'agent'): CommandResult {
    const group = requireGroup(command.group_id);
    if (!('id' in group)) return group;
    const read = readItemFields(command, null, group.currency);
    if ('ok' in read && read.ok === false) return read;
    if (!('fields' in read)) return read;
    const reviewState: ClaimItem['review_state'] = actor === 'agent' ? 'pending' : 'none';
    const item = makeItem(group.id, read.fields, nextItemPosition(group.id), reviewState);
    if (actor === 'human') {
      items.set(item.id, item);
      recordChange('add_item', 'human', 'none', `新增金额项「${item.title}」`, group.id, item.id);
      return { ok: true, snapshot: snapshot() };
    }
    const record = recordChange('add_item', 'agent', 'pending',
      `Agent 候选金额项「${item.title}」`, group.id, item.id);
    pending.push({
      id: record.id, seq: record.seq, command: 'add_item', created_at: record.created_at,
      group_id: group.id, group_title: group.title, before: null, after: item,
    });
    return { ok: true, snapshot: snapshot() };
  }

  function stageOrUpdateItem(command: Extract<ClaimCommand, { type: 'update_item' }>, actor: 'human' | 'agent'): CommandResult {
    const item = requireItem(command.item_id);
    if (!('id' in item)) return item;
    if (pendingForItem(item.id)) {
      return fail('conflict', '该金额项有待确认的 Agent 修改，请先确认或撤回');
    }
    const group = groups.get(item.group_id);
    if (!group) return fail('not_found', '金额项所属分组不存在');
    const read = readItemFields(command, item, group.currency);
    if ('ok' in read && read.ok === false) return read;
    if (!('fields' in read)) return read;
    if (actor === 'human') {
      Object.assign(item, read.fields, { origin: 'human', review_state: 'none', revision: item.revision + 1 });
      recordChange('update_item', 'human', 'none', `修改金额项「${item.title}」`, group.id, item.id);
      return { ok: true, snapshot: snapshot() };
    }
    const before = structuredClone(item);
    const after: ClaimItem = {
      ...structuredClone(item), ...read.fields, origin: 'agent', review_state: 'pending',
      revision: item.revision + 1,
    };
    const record = recordChange('update_item', 'agent', 'pending',
      `Agent 修改金额项「${after.title}」`, group.id, item.id);
    pending.push({
      id: record.id, seq: record.seq, command: 'update_item', created_at: record.created_at,
      group_id: group.id, group_title: group.title, before, after,
    });
    return { ok: true, snapshot: snapshot() };
  }

  function reorderItems(command: Extract<ClaimCommand, { type: 'reorder_items' }>): CommandResult {
    const item = requireItem(command.item_id);
    if (!('id' in item)) return item;
    const list = activeItems(item.group_id);
    const index = list.findIndex((entry) => entry.id === item.id);
    const target = command.direction === 'up' ? index - 1 : index + 1;
    if (target < 0 || target >= list.length) {
      return fail('validation', command.direction === 'up' ? '已经是第一项' : '已经是最后一项');
    }
    [list[index].position, list[target].position] = [list[target].position, list[index].position];
    normalizeItemPositions(item.group_id);
    recordChange('reorder_items', 'human', 'none', `调整金额项顺序：${item.title}`, item.group_id, item.id);
    return { ok: true, snapshot: snapshot() };
  }

  function removeItem(command: Extract<ClaimCommand, { type: 'remove_item' }>): CommandResult {
    const item = requireItem(command.item_id);
    if (!('id' in item)) return item;
    if (pendingForItem(item.id)) {
      return fail('conflict', '该金额项有待确认的 Agent 修改，请先确认或撤回');
    }
    item.removed_at = now();
    item.revision += 1;
    recordChange('remove_item', 'human', 'none', `删除金额项「${item.title}」`, item.group_id, item.id);
    return { ok: true, snapshot: snapshot() };
  }

  function acceptChange(command: Extract<ClaimCommand, { type: 'accept_change' }>): CommandResult {
    const index = pending.findIndex((change) => change.id === command.change_id);
    if (index < 0) return fail('not_found', '待确认修改不存在或已处理');
    const change = pending[index];
    const group = groups.get(change.group_id);
    if (!group || group.removed_at !== null) return fail('conflict', '分组已删除，无法确认');
    if (change.command === 'add_item') {
      items.set(change.after.id, {
        ...structuredClone(change.after), review_state: 'accepted', origin: 'agent', revision: 1,
      });
    } else {
      const current = items.get(change.after.id);
      if (!current || current.removed_at !== null) return fail('conflict', '金额项已删除，无法确认');
      Object.assign(current, {
        title: change.after.title, category: change.after.category,
        amount_cents: change.after.amount_cents, calc_note: change.after.calc_note,
        source: change.after.source ? structuredClone(change.after.source) : null,
        origin: 'agent', review_state: 'accepted', revision: current.revision + 1,
      });
    }
    pending.splice(index, 1);
    recordChange(change.command, 'human', 'accepted',
      `确认 Agent 修改：${change.after.title}`, change.group_id, change.after.id);
    return { ok: true, snapshot: snapshot() };
  }

  function revertChange(command: Extract<ClaimCommand, { type: 'revert_change' }>): CommandResult {
    const index = pending.findIndex((change) => change.id === command.change_id);
    if (index < 0) return fail('not_found', '待确认修改不存在或已处理');
    const change = pending[index];
    pending.splice(index, 1);
    recordChange(change.command, 'human', 'reverted',
      `撤回 Agent 修改：${change.after.title}`, change.group_id, change.after.id);
    return { ok: true, snapshot: snapshot() };
  }

  function runHuman(command: ClaimCommand): CommandResult {
    switch (command.type) {
      case 'add_group': return addGroup(command);
      case 'update_group': return updateGroup(command);
      case 'reorder_groups': return reorderGroups(command);
      case 'remove_group': return removeGroup(command);
      case 'add_item': return stageOrAddItem(command, 'human');
      case 'update_item': return stageOrUpdateItem(command, 'human');
      case 'reorder_items': return reorderItems(command);
      case 'remove_item': return removeItem(command);
      case 'accept_change': return acceptChange(command);
      case 'revert_change': return revertChange(command);
      default: return fail('validation', '未知命令');
    }
  }

  function runAgent(command: ClaimCommand): CommandResult {
    switch (command.type) {
      case 'add_item': return stageOrAddItem(command, 'agent');
      case 'update_item': return stageOrUpdateItem(command, 'agent');
      case 'accept_change':
      case 'revert_change':
        return fail('forbidden', '确认与撤回仅限人工入口');
      default:
        return fail('forbidden', 'Agent 入口在原型中仅开放金额项候选提交');
    }
  }

  return {
    getSnapshot: snapshot,
    human: (command) => guard(() => runHuman(command)),
    agent: (command) => guard(() => runAgent(command)),
  };
}
