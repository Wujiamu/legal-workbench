/**
 * P4 HTTP 执行器：把 progressApi 适配成与 P1 内存执行器相同的 ProgressExecutor 接口，
 * useCaseProgress 与全部视图组件不改即可切换到真实后端。
 *
 * 命令分发到对应端点；服务端返回的快照（生成契约类型）映射为前端进度类型。
 * actor 由 HTTP 入口固定为 human，调用方不能自报。错误归一为 CommandResult。
 */
import { ApiError } from '../api/client';
import { progressApi } from '../api/endpoints';
import type { ProgressSnapshot as ApiSnapshot, Schemas } from '../api/endpoints';
import type {
  Actor, AsyncProgressExecutor, AuditEntry, CommandResult, Entry, ProgressChange, ProgressCommand,
  ProgressLink, ProgressNode, ProgressRelation, ProgressRules,
  ProgressSnapshot,
} from './types';

function mapNode(node: ApiSnapshot['nodes'][number]): ProgressNode {
  return {
    id: node.id, case_id: node.case_id, parent_id: node.parent_id,
    position: node.position, kind: node.kind, state: node.state, title: node.title,
    note: node.note, date_start: node.date_start, date_end: node.date_end,
    template_ref: node.template_ref, created_by: node.created_by as Actor,
    updated_by: node.updated_by as Actor, request_key: node.request_key,
    removed_at: node.removed_at, revision: node.revision,
    created_at: node.created_at, updated_at: node.updated_at,
  };
}

function mapRelation(relation: ApiSnapshot['relations'][number]): ProgressRelation {
  return {
    id: relation.id, case_id: relation.case_id, from_node_id: relation.from_node_id,
    to_node_id: relation.to_node_id, type: relation.type, note: relation.note,
    created_by: relation.created_by as Actor, removed_at: relation.removed_at,
    created_at: relation.created_at,
  };
}

function mapLink(link: ApiSnapshot['links'][number]): ProgressLink {
  return {
    id: link.id, case_id: link.case_id, node_id: link.node_id,
    material_id: link.material_id, analysis_id: link.analysis_id,
    annotation_id: link.annotation_id, created_by: link.created_by as Actor,
    created_at: link.created_at, display_name: link.display_name,
  };
}

function mapChange(change: ApiSnapshot['changes'][number]): ProgressChange {
  return {
    id: change.id, case_id: change.case_id, seq: change.seq, command: change.command,
    actor: change.actor as Actor, entry: change.entry as Entry,
    diff: change.diff.map((entry) => ({
      target: entry.target, id: entry.id, before: entry.before, after: entry.after,
      before_rev: entry.before_rev, after_rev: entry.after_rev,
    })),
    review_state: change.review_state, reviewed_at: change.reviewed_at,
    reverted_by: change.reverted_by, created_at: change.created_at,
  };
}

export function mapSnapshot(api: ApiSnapshot): ProgressSnapshot {
  return {
    case_id: api.case_id, seq: api.seq,
    nodes: api.nodes.map(mapNode),
    relations: api.relations.map(mapRelation),
    links: api.links.map(mapLink),
    pending_changes: api.pending_changes.map(mapChange),
    changes: api.changes.map(mapChange),
    last_change_id: api.last_change_id ?? null,
  };
}

function failFrom(error: unknown): CommandResult {
  if (error instanceof ApiError) {
    const code = error.code === 'conflict' ? 'conflict'
      : error.code === 'case_mismatch' ? 'case_mismatch'
      : error.code === 'not_found' ? 'not_found' : 'validation';
    return { ok: false, code, message: error.message };
  }
  return { ok: false, code: 'validation', message: '无法连接本地服务' };
}

/** 异步执行器：getSnapshot/getSeqs 返回 Promise，useCaseProgress 已支持。 */
export function createHttpExecutor(caseId: string, rules: ProgressRules): AsyncProgressExecutor {
  async function run(command: ProgressCommand): Promise<CommandResult> {
    try {
      let api: ApiSnapshot;
      switch (command.type) {
        case 'add_node':
          api = await progressApi.addNode(caseId, {
            parent_id: command.parent_id, kind: command.kind, title: command.title,
            position: command.position ?? null, state: command.state ?? null,
            note: command.note ?? null, date_start: command.date_start ?? null,
            date_end: command.date_end ?? null, request_key: command.request_key ?? null,
          });
          break;
        case 'update_node': {
          // 审查 11：只发送实际提供的字段；显式 null 表示清空，未提供＝不动。
          const changes: Record<string, unknown> = {};
          for (const key of ['title', 'state', 'note', 'date_start', 'date_end'] as const) {
            if (key in command.changes) changes[key] = command.changes[key] ?? null;
          }
          api = await progressApi.updateNode(caseId, command.node_id, command.expected_revision,
            changes as Schemas['NodeChanges']);
          break;
        }
        case 'move_node':
          api = await progressApi.moveNode(caseId, command.node_id, command.expected_revision,
            command.new_parent_id, command.before_node_id ?? null);
          break;
        case 'set_active_stage':
          api = await progressApi.setActiveStage(caseId, command.stage_id, command.expected_revision);
          break;
        case 'insert_fragment':
          api = await progressApi.insertFragment(caseId, command.fragment_key, command.parent_id,
            command.before_node_id ?? null);
          break;
        case 'remove_node':
          api = await progressApi.removeNode(caseId, command.node_id, command.expected_revision);
          break;
        case 'restore_node':
          api = await progressApi.restoreNode(caseId, command.node_id, command.expected_revision);
          break;
        case 'add_relation':
          api = await progressApi.addRelation(caseId, command.from_node_id, command.to_node_id,
            command.relation_type, command.note ?? null);
          break;
        case 'remove_relation':
          api = await progressApi.removeRelation(caseId, command.relation_id);
          break;
        case 'link': {
          const target = command.target;
          api = await progressApi.link(caseId, command.node_id, {
            material_id: 'material_id' in target ? target.material_id : null,
            analysis_id: 'analysis_id' in target ? target.analysis_id : null,
            annotation_id: 'annotation_id' in target ? target.annotation_id : null,
          });
          break;
        }
        case 'unlink':
          api = await progressApi.unlink(caseId, command.link_id);
          break;
        case 'accept_change':
          api = await progressApi.acceptChange(caseId, command.change_id);
          break;
        case 'accept_all':
          api = await progressApi.acceptAll(caseId, command.change_ids, command.expected_seq ?? null);
          break;
        case 'revert_change':
          api = await progressApi.revertChange(caseId, command.change_id);
          break;
      }
      return { ok: true, snapshot: mapSnapshot(api) };
    } catch (error) {
      return failFrom(error);
    }
  }

  return {
    getSnapshot: () => progressApi.snapshot(caseId).then(mapSnapshot),
    getRules: () => rules,
    getSeqs: async () => {
      const result = await progressApi.seqs(caseId);
      return result.seqs;
    },
    human: (command) => run(command),
    agent: async () => ({ ok: false, code: 'validation', message: '网页入口不以 Agent 身份写入' }),
    auditLog: (): AuditEntry[] => [],
  };
}