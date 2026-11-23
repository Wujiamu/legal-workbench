/**
 * P1 内存执行器：与后端 progress 服务同一套命令语义（总体计划 6.2）。
 * 固定顺序：校验 → 变更 → diff → 变更记录 → 序号 +1 → 审计 → 返回快照。
 * 同步执行即原子；P2 后端在同一短事务里完成同样的步骤。
 * 仅供原型与组件测试使用，不接真实接口。
 */
import type {
  Actor, AuditEntry, ChangeDiffEntry, CommandResult, Entry, FragmentNode,
  ProgressChange, ProgressCommand, ProgressExecutor, ProgressLink, ProgressNode,
  ProgressRelation, ProgressRules, ProgressSnapshot,
} from './types';

export interface CatalogEntry {
  id: string;
  case_id: string;
  title: string;
}

export interface LocalExecutorOptions {
  caseId: string;
  rules: ProgressRules;
  /** 原型用的案内业务对象目录；P2 以后由数据库组合外键保证同案。 */
  catalog?: {
    materials?: CatalogEntry[];
    analyses?: CatalogEntry[];
    annotations?: CatalogEntry[];
  };
}

const TITLE_MAX = 200;
const NOTE_MAX = 4000;

function now(): string {
  return new Date().toISOString();
}

function newId(): string {
  return crypto.randomUUID();
}

function fail(code: 'conflict' | 'validation' | 'not_found' | 'case_mismatch', message: string): CommandResult {
  return { ok: false, code, message };
}

export function createLocalExecutor({ caseId, rules, catalog }: LocalExecutorOptions): ProgressExecutor {
  const nodes = new Map<string, ProgressNode>();
  const relations = new Map<string, ProgressRelation>();
  const links = new Map<string, ProgressLink>();
  const changes: ProgressChange[] = [];
  const audit: AuditEntry[] = [];
  let seq = 0;

  function snapshot(): ProgressSnapshot {
    return {
      case_id: caseId,
      seq,
      nodes: [...nodes.values()].map((node) => structuredClone(node)),
      relations: [...relations.values()].map((relation) => structuredClone(relation)),
      links: [...links.values()].map((link) => structuredClone(link)),
      pending_changes: changes.filter((change) => change.review_state === 'pending')
        .map((change) => structuredClone(change)),
      changes: changes.map((change) => structuredClone(change)),
    };
  }

  function activeChildren(parentId: string | null): ProgressNode[] {
    return [...nodes.values()]
      .filter((node) => node.parent_id === parentId && node.removed_at === null)
      .sort((a, b) => a.position - b.position);
  }

  function parentKind(parentId: string | null): string {
    if (parentId === null) return 'case';
    return nodes.get(parentId)?.kind ?? 'case';
  }

  function validateNewNode(kind: string, title: string, state: string | undefined,
                           parentId: string | null): CommandResult | null {
    const kindRule = rules.kinds[kind];
    if (!kindRule) return fail('validation', `未知节点种类：${kind}`);
    const trimmed = title.trim();
    if (!trimmed) return fail('validation', '标题不能为空');
    if (trimmed.length > TITLE_MAX) return fail('validation', `标题不能超过 ${TITLE_MAX} 字`);
    if (state !== undefined && !kindRule.states.some((entry) => entry.value === state)) {
      return fail('validation', `种类「${kindRule.label}」没有状态：${state}`);
    }
    if (parentId !== null) {
      const parent = nodes.get(parentId);
      if (!parent || parent.removed_at !== null) return fail('not_found', '父节点不存在');
      if (parent.case_id !== caseId) return fail('case_mismatch', '父节点不属于本案');
    }
    const allowed = rules.containment[parentKind(parentId)] ?? [];
    if (!allowed.includes(kind)) {
      return fail('validation', `「${kindRule.label}」不能放在该位置（包含规则）`);
    }
    return null;
  }

  function insertPosition(parentId: string | null, position?: number): number {
    const siblings = activeChildren(parentId);
    const target = position === undefined ? siblings.length : Math.max(0, Math.min(position, siblings.length));
    for (const sibling of siblings) {
      if (sibling.position >= target) sibling.position += 1;
    }
    return target;
  }

  function recordChange(command: string, actor: Actor, entry: Entry,
                        diff: ChangeDiffEntry[]): ProgressChange {
    seq += 1;
    const change: ProgressChange = {
      id: newId(), case_id: caseId, seq, command, actor, entry, diff,
      review_state: actor === 'agent' ? 'pending' : 'none',
      reviewed_at: null, reverted_by: null, created_at: now(),
    };
    changes.push(change);
    return change;
  }

  function recordAudit(command: string, actor: Actor, entry: Entry, targetIds: string[]): void {
    audit.push({ operation: `progress.${command}`, actor, entry, target_ids: [...targetIds] });
  }

  function nodeDiff(before: ProgressNode | null, after: ProgressNode | null): ChangeDiffEntry {
    return {
      target: 'node',
      id: (after ?? before)!.id,
      before: before ? structuredClone(before) as unknown as Record<string, unknown> : null,
      after: after ? structuredClone(after) as unknown as Record<string, unknown> : null,
      before_rev: before?.revision ?? null,
      after_rev: after?.revision ?? null,
    };
  }

  function requireNode(nodeId: string): ProgressNode | CommandResult {
    const node = nodes.get(nodeId);
    if (!node) return fail('not_found', '节点不存在');
    if (node.case_id !== caseId) return fail('case_mismatch', '节点不属于本案');
    return node;
  }

  function cas(node: ProgressNode, expectedRevision: number): CommandResult | null {
    if (node.revision !== expectedRevision) {
      return fail('conflict', '节点已被修改，请刷新后重试');
    }
    return null;
  }

  function addNode(command: Extract<ProgressCommand, { type: 'add_node' }>,
                   actor: Actor, entry: Entry): CommandResult {
    if (actor === 'agent' && !command.request_key) {
      return fail('validation', 'Agent 新建节点必须带 request_key');
    }
    if (command.request_key) {
      const existing = [...nodes.values()].find((node) => node.request_key === command.request_key);
      if (existing) return { ok: true, snapshot: snapshot() }; // 幂等：不新增变更与序号
    }
    const invalid = validateNewNode(command.kind, command.title, command.state, command.parent_id);
    if (invalid) return invalid;
    const kindRule = rules.kinds[command.kind];
    // 审查 N6/R4-1：记录让位同级修改前状态，撤回新建时整体还原（与后端同口径）。
    const siblingsBefore = new Map(activeChildren(command.parent_id).map((s) => [s.id, structuredClone(s)]));
    const position = insertPosition(command.parent_id, command.position);
    const timestamp = now();
    const node: ProgressNode = {
      id: newId(), case_id: caseId, parent_id: command.parent_id, position,
      kind: command.kind, state: command.state ?? kindRule.default_state,
      title: command.title.trim(), note: command.note ?? null,
      date_start: command.date_start ?? null, date_end: command.date_end ?? null,
      template_ref: null, created_by: actor, updated_by: actor,
      request_key: command.request_key ?? null, removed_at: null, revision: 0,
      created_at: timestamp, updated_at: timestamp,
    };
    nodes.set(node.id, node);
    const diff: ChangeDiffEntry[] = [nodeDiff(null, node)];
    for (const [id, sibBefore] of siblingsBefore) {
      const sibling = nodes.get(id)!;
      if (sibling.position !== sibBefore.position) {
        sibling.revision += 1;
        sibling.updated_by = actor;
        sibling.updated_at = timestamp;
        diff.push(nodeDiff(sibBefore, sibling));
      }
    }
    enforceSingleActiveStage(node, actor, diff);
    recordChange('add_node', actor, entry, diff);
    recordAudit('add_node', actor, entry, [node.id]);
    return { ok: true, snapshot: snapshot() };
  }

  function updateNode(command: Extract<ProgressCommand, { type: 'update_node' }>,
                      actor: Actor, entry: Entry): CommandResult {
    const found = requireNode(command.node_id);
    if ('ok' in found) return found;
    if (found.removed_at !== null) return fail('validation', '节点已删除');
    const stale = cas(found, command.expected_revision);
    if (stale) return stale;
    const changes = command.changes;
    if (changes.title !== undefined) {
      const trimmed = changes.title.trim();
      if (!trimmed || trimmed.length > TITLE_MAX) return fail('validation', `标题需为 1–${TITLE_MAX} 字`);
    }
    if (changes.note !== undefined && changes.note !== null && changes.note.length > NOTE_MAX) {
      return fail('validation', `备注不能超过 ${NOTE_MAX} 字`);
    }
    if (changes.state !== undefined
        && !rules.kinds[found.kind].states.some((state) => state.value === changes.state)) {
      return fail('validation', `种类「${rules.kinds[found.kind].label}」没有状态：${changes.state}`);
    }
    const before = structuredClone(found);
    const diff: ChangeDiffEntry[] = [];
    if (changes.title !== undefined) found.title = changes.title.trim();
    if (changes.state !== undefined) found.state = changes.state;
    if (changes.note !== undefined) found.note = changes.note;
    if (changes.date_start !== undefined) found.date_start = changes.date_start;
    if (changes.date_end !== undefined) found.date_end = changes.date_end;
    found.revision += 1;
    found.updated_by = actor;
    found.updated_at = now();
    diff.push(nodeDiff(before, found));
    // K3（审查 07）：普通编辑把阶段设为进行中，也联动同程序原进行中阶段。
    enforceSingleActiveStage(found, actor, diff);
    recordChange('update_node', actor, entry, diff);
    recordAudit('update_node', actor, entry, [found.id]);
    return { ok: true, snapshot: snapshot() };
  }

  /** K3：任何把阶段置为进行中的路径都联动同程序原进行中阶段为已完成（审查 07）。 */
  function enforceSingleActiveStage(node: ProgressNode, actor: Actor, diff: ChangeDiffEntry[]) {
    if (node.kind !== 'stage' || node.state !== 'in_progress' || node.removed_at !== null) return;
    for (const sibling of activeChildren(node.parent_id)) {
      if (sibling.id !== node.id && sibling.kind === 'stage' && sibling.state === 'in_progress') {
        const before = structuredClone(sibling);
        sibling.state = 'completed';
        sibling.revision += 1;
        sibling.updated_by = actor;
        sibling.updated_at = now();
        diff.push(nodeDiff(before, sibling));
      }
    }
  }

  function setActiveStage(command: Extract<ProgressCommand, { type: 'set_active_stage' }>,
                          actor: Actor, entry: Entry): CommandResult {
    const found = requireNode(command.stage_id);
    if ('ok' in found) return found;
    if (found.removed_at !== null) return fail('validation', '节点已删除');
    if (found.kind !== 'stage') return fail('validation', '只有阶段可以设为进行中');
    const stale = cas(found, command.expected_revision);
    if (stale) return stale;
    const diff: ChangeDiffEntry[] = [];
    // 同一程序内最多一个进行中阶段（K3）：原进行中阶段一并调整。
    // 原型口径：原阶段记为已完成；撤回时按 diff 恢复。P1 请承办人确认。
    for (const sibling of activeChildren(found.parent_id)) {
      if (sibling.id !== found.id && sibling.kind === 'stage' && sibling.state === 'in_progress') {
        const before = structuredClone(sibling);
        sibling.state = 'completed';
        sibling.revision += 1;
        sibling.updated_by = actor;
        sibling.updated_at = now();
        diff.push(nodeDiff(before, sibling));
      }
    }
    const before = structuredClone(found);
    found.state = 'in_progress';
    found.revision += 1;
    found.updated_by = actor;
    found.updated_at = now();
    diff.push(nodeDiff(before, found));
    recordChange('set_active_stage', actor, entry, diff);
    recordAudit('set_active_stage', actor, entry, diff.map((entryItem) => entryItem.id));
    return { ok: true, snapshot: snapshot() };
  }

  function acceptChange(command: Extract<ProgressCommand, { type: 'accept_change' }>,
                        actor: Actor, entry: Entry): CommandResult {
    const change = changes.find((item) => item.id === command.change_id);
    if (!change) return fail('not_found', '变更记录不存在');
    if (change.review_state !== 'pending') return fail('validation', '该变更不处于待确认状态');
    change.review_state = 'accepted';
    change.reviewed_at = now();
    seq += 1;
    recordAudit('accept_change', actor, entry, [change.id]);
    return { ok: true, snapshot: snapshot() };
  }

  function acceptAll(command: Extract<ProgressCommand, { type: 'accept_all' }>,
                     actor: Actor, entry: Entry): CommandResult {
    // 审查 04 / 末节：只确认客户端已展示的条目。区分「显式空集合」与「省略 IDs」：
    // 显式提交 change_ids=[] 一律拒绝；省略 IDs 时必须携带匹配的 expected_seq。
    const idsProvided = 'change_ids' in command && command.change_ids !== undefined;
    if (idsProvided && (command.change_ids as string[]).length === 0) {
      return fail('validation', '确认范围不能为空：请提交已展示的变更 ID');
    }
    if (!idsProvided && (command.expected_seq === undefined || command.expected_seq === null)) {
      return fail('validation', '一键确认须提交已展示的变更 ID 或操作序号');
    }
    if (command.expected_seq !== undefined && command.expected_seq !== null
        && command.expected_seq !== seq) {
      return fail('conflict', '待确认列表已变化，请刷新后重新审核');
    }
    let pending = changes.filter((change) => change.review_state === 'pending');
    if (idsProvided) {
      const shown = new Set(command.change_ids as string[]);
      pending = pending.filter((change) => shown.has(change.id));
    }
    if (pending.length === 0) return fail('validation', '没有待确认的 Agent 修改');
    const timestamp = now();
    for (const change of pending) {
      change.review_state = 'accepted';
      change.reviewed_at = timestamp;
    }
    seq += 1;
    recordAudit('accept_all', actor, entry, pending.map((change) => change.id));
    return { ok: true, snapshot: snapshot() };
  }

  function revertChange(command: Extract<ProgressCommand, { type: 'revert_change' }>,
                        actor: Actor, entry: Entry): CommandResult {
    const change = changes.find((item) => item.id === command.change_id);
    if (!change) return fail('not_found', '变更记录不存在');
    // 待确认可撤回；已确认也可撤回（一键确认后的撤销，用户反馈第七轮）；
    // 人工变更 review_state='none' 也允许撤回（与后端口径一致，审查 05）。
    if (change.review_state !== 'pending' && change.review_state !== 'accepted'
        && change.review_state !== 'none') {
      return fail('validation', '该变更已撤回或不可撤回');
    }
    // 先整体核对：任一受影响节点已被后续修改则拒绝，不做半截撤回。
    for (const item of change.diff) {
      if (item.target !== 'node') continue;
      const current = nodes.get(item.id);
      if (!current) return fail('conflict', '节点已被后续修改，无法撤回，请人工处理');
      if (item.after_rev !== null && current.revision !== item.after_rev) {
        return fail('conflict', '节点已被后续修改，无法撤回，请人工处理');
      }
    }
    // 审查 05/N4/R4-1：撤回新建＝软删除，若其下此后又挂了有效子节点，整笔拒绝。
    const createdIds = new Set(change.diff
      .filter((item) => item.target === 'node' && item.before === null && item.after !== null)
      .map((item) => item.id));
    // 软删型逆操作（remove_node 本身、restore 的逆操作）同样检查其下有效子节点。
    const softdeleteIds = new Set(change.diff
      .filter((item) => item.target === 'node' && item.before !== null && item.after !== null
        && ((item.after as unknown as ProgressNode).removed_at !== null)
          !== ((item.before as unknown as ProgressNode).removed_at !== null))
      .map((item) => item.id));
    const guardIds = new Set([...createdIds, ...softdeleteIds]);
    if (guardIds.size > 0) {
      for (const node of nodes.values()) {
        if (node.removed_at === null && node.parent_id !== null
            && guardIds.has(node.parent_id) && !guardIds.has(node.id)) {
          return fail('conflict', '该节点下已有后续新增的子节点，无法整体撤回，请人工处理');
        }
      }
    }
    // 审查 05：撤回移动＝还原父级，原父级须仍有效。
    for (const item of change.diff) {
      if (item.target !== 'node' || item.before === null) continue;
      const beforeParent = (item.before as unknown as ProgressNode).parent_id;
      if (beforeParent !== null) {
        const parent = nodes.get(beforeParent);
        if (!parent || parent.removed_at !== null) {
          return fail('conflict', '原父节点已删除，无法撤回移动，请人工处理');
        }
      }
    }
    const reverseDiff: ChangeDiffEntry[] = [];
    for (const item of change.diff) {
      if (item.target === 'relation') {
        const current = relations.get(item.id);
        if (!current) continue;
        const before = structuredClone(current);
        if (item.before === null && item.after !== null) {
          current.removed_at = now();
        } else if (item.before !== null && item.after === null) {
          // 审查 06：恢复旧关系前查重，同向同类已有有效关系则冲突。
          const dup = [...relations.values()].find((rel) => rel.id !== current.id
            && rel.removed_at === null && rel.from_node_id === current.from_node_id
            && rel.to_node_id === current.to_node_id && rel.type === current.type);
          if (dup) return fail('conflict', '已存在同向同类有效关系，无法撤回该移除，请人工处理');
          // 审查 N5/R4-1：端点此后已被软删则拒绝恢复，避免悬空关系。
          for (const endpointId of [current.from_node_id, current.to_node_id]) {
            const endpoint = nodes.get(endpointId);
            if (!endpoint || endpoint.removed_at !== null) {
              return fail('conflict', '关系端点已删除，无法撤回该移除，请人工处理');
            }
          }
          current.removed_at = null;
        } else if (item.before !== null) {
          Object.assign(current, structuredClone(item.before as unknown as ProgressRelation));
        }
        reverseDiff.push(relationDiff(before, current));
        continue;
      }
      if (item.target === 'link') {
        if (item.before === null && item.after !== null) {
          const current = links.get(item.id);
          if (current) {
            links.delete(item.id);
            reverseDiff.push(linkDiff(current, null));
          }
        } else if (item.before !== null && item.after === null) {
          const snap = item.before as unknown as ProgressLink;
          const dup = [...links.values()].find((link) => link.node_id === snap.node_id
            && link.material_id === snap.material_id && link.analysis_id === snap.analysis_id
            && link.annotation_id === snap.annotation_id);
          if (dup) return fail('conflict', '已存在同节点同对象的有效关联，无法撤回该移除，请人工处理');
          // 审查 N5/R4-1：关联节点此后已被软删则拒绝恢复，避免悬空关联。
          const linkNode = nodes.get(snap.node_id);
          if (!linkNode || linkNode.removed_at !== null) {
            return fail('conflict', '关联节点已删除，无法撤回该移除，请人工处理');
          }
          const restored = structuredClone(snap);
          links.set(restored.id, restored);
          reverseDiff.push(linkDiff(null, restored));
        }
        continue;
      }
      const current = nodes.get(item.id)!;
      const before = structuredClone(current);
      if (item.before === null && item.after !== null) {
        current.removed_at = now(); // 撤回新建 = 软删除
      } else if (item.before !== null && item.after !== null) {
        // 审查 N4/R3-1/R4-1：按 before/after 的 removed_at 判断恢复/软删
        // （remove_node 的 diff after 非 null，只是 removed_at 置位）。
        const beforeRemoved = (item.before as unknown as ProgressNode).removed_at !== null;
        const afterRemoved = (item.after as unknown as ProgressNode).removed_at !== null;
        if (!beforeRemoved && afterRemoved) {
          // 撤回删除＝恢复：还原修改前快照（含 state/position），被占用则顺延让位并入 reverse_diff。
          const restore = item.before as unknown as ProgressNode;
          Object.assign(current, structuredClone(restore), {
            revision: current.revision + 1, updated_by: 'human', updated_at: now(),
          });
          const targetPos = restore.position;
          const occupied = activeChildren(current.parent_id).filter(
            (sibling) => sibling.id !== current.id && sibling.position >= targetPos);
          for (const sibling of occupied) {
            const sibBefore = structuredClone(sibling);
            sibling.position += 1;
            sibling.revision += 1;
            sibling.updated_by = 'human';
            sibling.updated_at = now();
            reverseDiff.push(nodeDiff(sibBefore, sibling));
          }
          current.position = targetPos;
        } else if (beforeRemoved && !afterRemoved) {
          current.removed_at = now(); // 撤回恢复＝软删
        } else {
          const restore = item.before as unknown as ProgressNode;
          Object.assign(current, structuredClone(restore), {
            revision: current.revision + 1, updated_by: 'human', updated_at: now(),
          });
        }
      } else if (item.before !== null) {
        const restore = item.before as unknown as ProgressNode;
        Object.assign(current, structuredClone(restore), {
          revision: current.revision + 1, updated_by: 'human', updated_at: now(),
        });
      }
      reverseDiff.push(nodeDiff(before, current));
    }
    // 审查 07/R4-2：撤回可能把某阶段恢复为进行中，联动同程序其他进行中阶段；
    // 跳过本次刚恢复/刚置 in_progress 的节点自身，避免自联动抵消。
    // restoredInProgress 只含主循环真正从软删恢复的节点（before.removed_at 非空），
    // 不含让位同级（让位只改 position，state 不变；原本 in_progress 会误入集合导致漏联动）。
    const restoredInProgress = new Set(reverseDiff
      .filter((item) => item.target === 'node' && item.after && item.before
        && (item.after as unknown as ProgressNode).state === 'in_progress'
        && (item.before as unknown as ProgressNode).removed_at !== null)
      .map((item) => item.id));
    const cascadeTargets: ProgressNode[] = [];
    for (const id of restoredInProgress) {
      const node = nodes.get(id)!;
      if (node.kind !== 'stage') continue;
      for (const sibling of activeChildren(node.parent_id)) {
        if (sibling.id !== node.id && !restoredInProgress.has(sibling.id)
            && sibling.kind === 'stage' && sibling.state === 'in_progress'
            && !cascadeTargets.includes(sibling)) {
          cascadeTargets.push(sibling);
        }
      }
    }
    for (const sibling of cascadeTargets) {
      const sibBefore = structuredClone(sibling);
      sibling.state = 'completed';
      sibling.revision += 1;
      sibling.updated_by = 'human';
      sibling.updated_at = now();
      reverseDiff.push(nodeDiff(sibBefore, sibling));
    }
    change.review_state = 'reverted';
    change.reviewed_at = now();
    const revertRecord = recordChange('revert_change', actor, entry, reverseDiff);
    change.reverted_by = revertRecord.id;
    recordAudit('revert_change', actor, entry, [change.id]);
    return { ok: true, snapshot: snapshot() };
  }

  function moveNode(command: Extract<ProgressCommand, { type: 'move_node' }>,
                    actor: Actor, entry: Entry): CommandResult {
    const found = requireNode(command.node_id);
    if ('ok' in found) return found;
    if (found.removed_at !== null) return fail('validation', '节点已删除');
    const stale = cas(found, command.expected_revision);
    if (stale) return stale;
    const newParentId = command.new_parent_id;
    if (newParentId === found.id) return fail('validation', '不能移动到自身之下');
    if (newParentId !== null) {
      const parent = nodes.get(newParentId);
      if (!parent || parent.removed_at !== null) return fail('not_found', '目标父节点不存在');
      if (parent.case_id !== caseId) return fail('case_mismatch', '目标父节点不属于本案');
      for (let cursor: string | null = newParentId; cursor !== null;) {
        if (cursor === found.id) return fail('validation', '移动会形成循环');
        cursor = nodes.get(cursor)?.parent_id ?? null;
      }
    }
    const allowed = rules.containment[parentKind(newParentId)] ?? [];
    if (!allowed.includes(found.kind)) {
      return fail('validation', '目标位置不允许该种类（包含规则）');
    }
    const before = structuredClone(found);
    const oldParentId = found.parent_id;
    // 审查 05：记录所有受影响同级（旧父级重排＋新父级让位），撤回时整体还原。
    const affected = new Map<string, ProgressNode>();
    for (const sibling of activeChildren(oldParentId)) {
      if (sibling.id !== found.id) affected.set(sibling.id, structuredClone(sibling));
    }
    for (const sibling of activeChildren(newParentId)) {
      if (sibling.id !== found.id && !affected.has(sibling.id)) {
        affected.set(sibling.id, structuredClone(sibling));
      }
    }
    // 旧同级重排
    const oldSiblings = activeChildren(oldParentId).filter((node) => node.id !== found.id);
    oldSiblings.forEach((node, index) => { node.position = index; });
    // 新同级插入
    const newSiblings = activeChildren(newParentId).filter((node) => node.id !== found.id);
    let target = newSiblings.length;
    if (command.before_node_id) {
      const anchor = newSiblings.find((node) => node.id === command.before_node_id);
      if (!anchor) return fail('validation', '参照节点不在目标位置');
      target = anchor.position;
    }
    for (const sibling of newSiblings) {
      if (sibling.position >= target) sibling.position += 1;
    }
    found.parent_id = newParentId;
    found.position = target;
    found.revision += 1;
    found.updated_by = actor;
    found.updated_at = now();
    const diff: ChangeDiffEntry[] = [nodeDiff(before, found)];
    for (const [id, sibBefore] of affected) {
      const sibling = nodes.get(id)!;
      if (sibling.position !== sibBefore.position || sibling.parent_id !== sibBefore.parent_id) {
        sibling.revision += 1;
        sibling.updated_by = actor;
        sibling.updated_at = now();
        diff.push(nodeDiff(sibBefore, sibling));
      }
    }
    enforceSingleActiveStage(found, actor, diff);
    recordChange('move_node', actor, entry, diff);
    recordAudit('move_node', actor, entry, [found.id]);
    return { ok: true, snapshot: snapshot() };
  }

  function insertFragment(command: Extract<ProgressCommand, { type: 'insert_fragment' }>,
                          actor: Actor, entry: Entry): CommandResult {
    const fragment = rules.fragments[command.fragment_key];
    if (!fragment) return fail('not_found', `未知片段：${command.fragment_key}`);
    if (command.parent_id !== null) {
      const parent = nodes.get(command.parent_id);
      if (!parent || parent.removed_at !== null) return fail('not_found', '父节点不存在');
      if (parent.case_id !== caseId) return fail('case_mismatch', '父节点不属于本案');
    }
    // 片段自身也必须符合包含规则，配置错误在实例化前拦截。
    const invalid = (function check(specs: FragmentNode[], parentKindValue: string): string | null {
      for (const spec of specs) {
        if (!rules.kinds[spec.kind]) return `未知节点种类：${spec.kind}`;
        if (!(rules.containment[parentKindValue] ?? []).includes(spec.kind)) {
          return `片段种类「${spec.kind}」不能放在该位置`;
        }
        const childError = check(spec.children ?? [], spec.kind);
        if (childError) return childError;
      }
      return null;
    })(fragment.roots, parentKind(command.parent_id));
    if (invalid) return fail('validation', invalid);
    const templateRef = `${command.fragment_key}@${fragment.version}`;
    const timestamp = now();
    const created: ProgressNode[] = [];
    const siblings = activeChildren(command.parent_id);
    let basePosition = siblings.length;
    // 审查 14：支持 before_node_id，后续同级让位且记入变更。
    const shifted = new Map<string, ProgressNode>();
    if (command.before_node_id) {
      const anchor = siblings.find((node) => node.id === command.before_node_id);
      if (!anchor) return fail('validation', '参照节点不在目标位置');
      basePosition = anchor.position;
      for (const sibling of siblings) {
        if (sibling.position >= basePosition) {
          shifted.set(sibling.id, structuredClone(sibling));
          sibling.position += fragment.roots.length;
        }
      }
    }
    function walk(spec: FragmentNode, parentId: string | null, position: number): void {
      const node: ProgressNode = {
        id: newId(), case_id: caseId, parent_id: parentId, position,
        kind: spec.kind, state: spec.state ?? rules.kinds[spec.kind].default_state,
        title: spec.title, note: null, date_start: null, date_end: null,
        template_ref: templateRef, created_by: actor, updated_by: actor,
        request_key: null, removed_at: null, revision: 0,
        created_at: timestamp, updated_at: timestamp,
      };
      nodes.set(node.id, node);
      created.push(node);
      (spec.children ?? []).forEach((child, index) => walk(child, node.id, index));
    }
    fragment.roots.forEach((root, index) => walk(root, command.parent_id, basePosition + index));
    const diff: ChangeDiffEntry[] = created.map((node) => nodeDiff(null, node));
    for (const [id, sibBefore] of shifted) {
      const sibling = nodes.get(id)!;
      sibling.revision += 1;
      sibling.updated_by = actor;
      sibling.updated_at = timestamp;
      diff.push(nodeDiff(sibBefore, sibling));
    }
    recordChange('insert_fragment', actor, entry, diff);
    recordAudit('insert_fragment', actor, entry, created.map((node) => node.id));
    return { ok: true, snapshot: snapshot() };
  }

  function removeNode(command: Extract<ProgressCommand, { type: 'remove_node' }>,
                      actor: Actor, entry: Entry): CommandResult {
    const found = requireNode(command.node_id);
    if ('ok' in found) return found;
    if (found.removed_at !== null) return fail('validation', '节点已删除');
    const stale = cas(found, command.expected_revision);
    if (stale) return stale;
    if (activeChildren(found.id).length > 0) {
      return fail('validation', '请先移除或移动子节点');
    }
    const before = structuredClone(found);
    found.removed_at = now();
    found.revision += 1;
    found.updated_by = actor;
    found.updated_at = now();
    recordChange('remove_node', actor, entry, [nodeDiff(before, found)]);
    recordAudit('remove_node', actor, entry, [found.id]);
    return { ok: true, snapshot: snapshot() };
  }

  function restoreNode(command: Extract<ProgressCommand, { type: 'restore_node' }>,
                       actor: Actor, entry: Entry): CommandResult {
    const found = requireNode(command.node_id);
    if ('ok' in found) return found;
    if (found.removed_at === null) return fail('validation', '节点未删除');
    const stale = cas(found, command.expected_revision);
    if (stale) return stale;
    if (found.parent_id !== null) {
      const parent = nodes.get(found.parent_id);
      if (!parent || parent.removed_at !== null) return fail('validation', '父节点已删除，请先恢复父节点');
    }
    const before = structuredClone(found);
    found.removed_at = null;
    found.position = activeChildren(found.parent_id).length;
    found.revision += 1;
    found.updated_by = actor;
    found.updated_at = now();
    const diff: ChangeDiffEntry[] = [nodeDiff(before, found)];
    enforceSingleActiveStage(found, actor, diff);
    recordChange('restore_node', actor, entry, diff);
    recordAudit('restore_node', actor, entry, [found.id]);
    return { ok: true, snapshot: snapshot() };
  }

  function relationDiff(before: ProgressRelation | null, after: ProgressRelation | null): ChangeDiffEntry {
    return {
      target: 'relation', id: (after ?? before)!.id,
      before: before ? structuredClone(before) as unknown as Record<string, unknown> : null,
      after: after ? structuredClone(after) as unknown as Record<string, unknown> : null,
      before_rev: null, after_rev: null,
    };
  }

  function linkDiff(before: ProgressLink | null, after: ProgressLink | null): ChangeDiffEntry {
    return {
      target: 'link', id: (after ?? before)!.id,
      before: before ? structuredClone(before) as unknown as Record<string, unknown> : null,
      after: after ? structuredClone(after) as unknown as Record<string, unknown> : null,
      before_rev: null, after_rev: null,
    };
  }

  function addRelation(command: Extract<ProgressCommand, { type: 'add_relation' }>,
                       actor: Actor, entry: Entry): CommandResult {
    const from = requireNode(command.from_node_id);
    if ('ok' in from) return from;
    const to = requireNode(command.to_node_id);
    if ('ok' in to) return to;
    if (from.removed_at !== null || to.removed_at !== null) return fail('validation', '节点已删除');
    if (from.id === to.id) return fail('validation', '关系不能指向自身');
    if (!rules.relation_types.some((type) => type.value === command.relation_type)) {
      return fail('validation', `未知关系类型：${command.relation_type}`);
    }
    const duplicate = [...relations.values()].some((relation) =>
      relation.removed_at === null && relation.from_node_id === from.id
      && relation.to_node_id === to.id && relation.type === command.relation_type);
    if (duplicate) return fail('validation', '该关系已存在');
    const relation: ProgressRelation = {
      id: newId(), case_id: caseId, from_node_id: from.id, to_node_id: to.id,
      type: command.relation_type, note: command.note ?? null,
      created_by: actor, removed_at: null, created_at: now(),
    };
    relations.set(relation.id, relation);
    recordChange('add_relation', actor, entry, [relationDiff(null, relation)]);
    recordAudit('add_relation', actor, entry, [relation.id]);
    return { ok: true, snapshot: snapshot() };
  }

  function removeRelation(command: Extract<ProgressCommand, { type: 'remove_relation' }>,
                          actor: Actor, entry: Entry): CommandResult {
    const relation = relations.get(command.relation_id);
    if (!relation || relation.removed_at !== null) return fail('not_found', '关系不存在');
    const before = structuredClone(relation);
    relation.removed_at = now();
    // after=null 表示「该关系被移除」，撤回时据此走恢复分支（审查 06）。
    recordChange('remove_relation', actor, entry, [relationDiff(before, null)]);
    recordAudit('remove_relation', actor, entry, [relation.id]);
    return { ok: true, snapshot: snapshot() };
  }

  function catalogEntry(target: { material_id?: string; analysis_id?: string; annotation_id?: string }):
      { entry: CatalogEntry } | CommandResult {
    const [kind, id] = Object.entries(target)[0] as [string, string];
    const list = kind === 'material_id' ? catalog?.materials
      : kind === 'analysis_id' ? catalog?.analyses : catalog?.annotations;
    const entry = list?.find((item) => item.id === id);
    if (!entry) return fail('not_found', '关联对象不存在');
    if (entry.case_id !== caseId) return fail('case_mismatch', '关联对象不属于本案');
    return { entry };
  }

  function linkNode(command: Extract<ProgressCommand, { type: 'link' }>,
                    actor: Actor, entry: Entry): CommandResult {
    const found = requireNode(command.node_id);
    if ('ok' in found) return found;
    if (found.removed_at !== null) return fail('validation', '节点已删除');
    const keys = Object.keys(command.target);
    if (keys.length !== 1) return fail('validation', '关联必须且只能指向一个对象');
    const looked = catalogEntry(command.target);
    if ('ok' in looked) return looked;
    const target = command.target as Record<string, string>;
    const duplicate = [...links.values()].some((link) =>
      link.node_id === found.id
      && link.material_id === (target.material_id ?? null)
      && link.analysis_id === (target.analysis_id ?? null)
      && link.annotation_id === (target.annotation_id ?? null));
    if (duplicate) return fail('validation', '该关联已存在');
    const link: ProgressLink = {
      id: newId(), case_id: caseId, node_id: found.id,
      material_id: target.material_id ?? null,
      analysis_id: target.analysis_id ?? null,
      annotation_id: target.annotation_id ?? null,
      created_by: actor, created_at: now(), display_name: looked.entry.title,
    };
    links.set(link.id, link);
    recordChange('link', actor, entry, [linkDiff(null, link)]);
    recordAudit('link', actor, entry, [link.id]);
    return { ok: true, snapshot: snapshot() };
  }

  function unlinkNode(command: Extract<ProgressCommand, { type: 'unlink' }>,
                      actor: Actor, entry: Entry): CommandResult {
    const link = links.get(command.link_id);
    if (!link) return fail('not_found', '关联不存在');
    links.delete(link.id);
    recordChange('unlink', actor, entry, [linkDiff(link, null)]);
    recordAudit('unlink', actor, entry, [link.id]);
    return { ok: true, snapshot: snapshot() };
  }

  function dispatch(command: ProgressCommand, actor: Actor, entry: Entry): CommandResult {
    switch (command.type) {
      case 'add_node': return addNode(command, actor, entry);
      case 'update_node': return updateNode(command, actor, entry);
      case 'move_node': return moveNode(command, actor, entry);
      case 'set_active_stage': return setActiveStage(command, actor, entry);
      case 'insert_fragment': return insertFragment(command, actor, entry);
      case 'remove_node': return removeNode(command, actor, entry);
      case 'restore_node': return restoreNode(command, actor, entry);
      case 'add_relation': return addRelation(command, actor, entry);
      case 'remove_relation': return removeRelation(command, actor, entry);
      case 'link': return linkNode(command, actor, entry);
      case 'unlink': return unlinkNode(command, actor, entry);
      case 'accept_change': return acceptChange(command, actor, entry);
      case 'accept_all': return acceptAll(command, actor, entry);
      case 'revert_change': return revertChange(command, actor, entry);
    }
  }

  return {
    getSnapshot: snapshot,
    getRules: () => structuredClone(rules),
    getSeqs: () => ({ progress: seq }),
    human(command) {
      return dispatch(command, 'human', 'prototype');
    },
    agent(command) {
      if (!rules.agent_commands.includes(command.type)) {
        return fail('validation', `Agent 无权执行命令：${command.type}`);
      }
      return dispatch(command, 'agent', 'prototype');
    },
    auditLog: () => structuredClone(audit),
  };
}