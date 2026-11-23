/**
 * 撤销栈（用户反馈第七轮，P1 原型口径）。
 * 按工作区域分栈：graph＝节点图/大纲的人工操作；review＝Agent 确认框的撤回。
 * Cmd/Ctrl+Z 只作用于当前焦点区域；页面用高亮框标示焦点。
 * 人工命令入栈时即构造好逆命令（含最新修订号）；冲突时撤销失败并提示，不静默覆盖。
 */
import type { CommandResult, ProgressCommand, ProgressExecutor } from './types';

export type UndoZone = 'graph' | 'review';

export interface UndoStacks {
  pushHuman(zone: UndoZone, inverse: ProgressCommand): void;
  pushReview(changeId: string): void;
  depth(zone: UndoZone): number;
  undo(zone: UndoZone, executor: ProgressExecutor): { ok: boolean; message?: string };
  /** 审查 03：异步执行器（P4 HTTP）用此入口，等待命令真正完成后再决定出栈。 */
  undoAsync(zone: UndoZone, run: (command: ProgressCommand) => Promise<CommandResult>): Promise<{ ok: boolean; message?: string }>;
  clear(): void;
}

export function createUndoStacks(limit = 50): UndoStacks {
  const human: Record<UndoZone, ProgressCommand[]> = { graph: [], review: [] };
  const review: string[] = [];

  return {
    pushHuman(zone, inverse) {
      human[zone].push(inverse);
      if (human[zone].length > limit) human[zone].shift();
    },
    pushReview(changeId) {
      review.push(changeId);
      if (review.length > limit) review.shift();
    },
    depth(zone) {
      return zone === 'review' ? review.length : human[zone].length;
    },
    undo(zone, executor) {
      if (zone === 'review') {
        const changeId = review.pop();
        if (!changeId) return { ok: false, message: '该区域没有可撤销的操作' };
        const result: CommandResult = executor.human({ type: 'revert_change', change_id: changeId });
        if (!result.ok) {
          review.push(changeId); // 失败保留，允许处理后重试
          return { ok: false, message: result.message };
        }
        return { ok: true };
      }
      const inverse = human[zone].pop();
      if (!inverse) return { ok: false, message: '该区域没有可撤销的操作' };
      const result = executor.human(inverse);
      if (!result.ok) {
        human[zone].push(inverse);
        return { ok: false, message: result.message };
      }
      return { ok: true };
    },
    async undoAsync(zone, run) {
      // 审查 03：先 peek 不 pop，命令真正成功后才出栈；失败保留栈项允许重试。
      if (zone === 'review') {
        const changeId = review[review.length - 1];
        if (!changeId) return { ok: false, message: '该区域没有可撤销的操作' };
        const result = await run({ type: 'revert_change', change_id: changeId });
        if (!result.ok) return { ok: false, message: result.message };
        review.pop();
        return { ok: true };
      }
      const inverse = human[zone][human[zone].length - 1];
      if (!inverse) return { ok: false, message: '该区域没有可撤销的操作' };
      const result = await run(inverse);
      if (!result.ok) return { ok: false, message: result.message };
      human[zone].pop();
      return { ok: true };
    },
    clear() {
      human.graph = [];
      human.review = [];
      review.length = 0;
    },
  };
}

/** 由人工命令构造逆命令（原型口径：覆盖 add/update/move/set_active_stage/remove）。 */
export function inverseOf(command: ProgressCommand, before: { revision: number; parent_id: string | null; position: number; state: string; title: string; note: string | null; date_start: string | null; date_end: string | null } | null,
                          createdNodeId?: string): ProgressCommand | null {
  switch (command.type) {
    case 'add_node':
      // 新建节点的逆操作是删除；修订号由调用方用执行后的最新值覆盖。
      return createdNodeId ? { type: 'remove_node', node_id: createdNodeId, expected_revision: before?.revision ?? 1 } : null;
    case 'update_node':
      if (!before) return null;
      return {
        type: 'update_node', node_id: command.node_id, expected_revision: before.revision,
        changes: {
          title: before.title, state: before.state, note: before.note,
          date_start: before.date_start, date_end: before.date_end,
        },
      };
    case 'move_node':
      if (!before) return null;
      return {
        type: 'move_node', node_id: command.node_id, expected_revision: before.revision,
        new_parent_id: before.parent_id,
      };
    case 'set_active_stage':
      if (!before) return null;
      return { type: 'update_node', node_id: command.stage_id, expected_revision: before.revision, changes: { state: before.state } };
    case 'remove_node':
      return null; // 软删除恢复走编辑界面，原型不入撤销栈
    default:
      return null;
  }
}