/**
 * P4 HTTP 数据源：把后端 graph / evidence 契约适配成视图使用的 GraphWorkspaceSource。
 *
 * 约定：
 * - 视图组件零改动：服务端工作区映射为前端 GraphSnapshot（含现行证据编号、出处定位）；
 * - 写命令统一返回「重新读取的整案工作区」，避免用局部响应拼装出前后不一致的画布；
 * - 证据目录命令返回的是目录快照，因此写后补一次工作区读取，编号与出处标签一起刷新；
 * - 归档只读由服务端兜底，这里只把服务端错误原样交给界面，不静默改写状态；
 * - 撤销以服务端本次变更 ID 为准（revert_change），不做本地逆向猜测。
 */
import { ApiError } from '../api/client';
import {
  annotationsApi, casesApi, evidenceApi, graphApi, progressApi, searchApi,
} from '../api/endpoints';
import type { EvidenceCatalog, GraphWorkspaceOut } from '../api/endpoints';
import type {
  ChangeDiffEntry, CommandResult, EntityAlias, GraphChange, GraphCommand,
  GraphRules, GraphSnapshot, GraphSource, GraphWorkspaceSource, RelationAttrs,
  SnapshotPayload, SourceOption, SourceOptionSearch,
} from './types';
import { defaultRules } from './rules';

type ApiWorkspace = GraphWorkspaceOut;
type ApiCatalog = EvidenceCatalog;
type ApiSnapshot = NonNullable<ApiWorkspace['snapshots']>[number];

/** 视图只用到「出处 id → 现行编号 + 定位」这一层，直接沿用服务端解析结果。 */
function mapLabels(api: ApiWorkspace): GraphSnapshot['labels'] {
  const labels: GraphSnapshot['labels'] = {};
  for (const [key, value] of Object.entries(api.labels)) {
    labels[key] = {
      labels: value.labels, text: value.text,
      material_id: value.material_id ?? '', material_title: value.material_title,
      page: value.page, text_excerpt: value.text_excerpt,
      source_ref: value.source_ref ?? null, file_version_id: value.file_version_id ?? null,
    };
  }
  return labels;
}

function mapChange(change: ApiWorkspace['changes'][number], domain: 'graph' | 'evidence'): GraphChange {
  return {
    id: change.id, case_id: change.case_id, domain, seq: change.seq,
    command: change.command, actor: change.actor as GraphChange['actor'],
    entry: change.entry as GraphChange['entry'],
    diff: change.diff.map((entry): ChangeDiffEntry => ({
      target: entry.target, id: entry.id, before: entry.before, after: entry.after,
      before_rev: entry.before_rev, after_rev: entry.after_rev,
    })),
    review_state: change.review_state, reviewed_at: change.reviewed_at,
    reverted_by: change.reverted_by ?? null, created_at: change.created_at,
  };
}

function mapSnapshot(row: ApiSnapshot): GraphSnapshot['snapshots'][number] {
  return {
    id: row.id, case_id: row.case_id, graph_id: row.graph_id, version_no: row.version_no,
    stage_label: row.stage_label, note: row.note, progress_node_id: row.progress_node_id,
    payload: row.payload as unknown as SnapshotPayload,
    excluded_pending_count: row.excluded_pending_count,
    created_by: 'human', created_at: row.created_at,
  };
}

/** 材料目录来自工作区的证据目录（含本案全部材料），只做展示所需的最小映射。 */
function mapMaterials(api: ApiWorkspace, caseId: string): GraphSnapshot['materials'] {
  return api.evidence.materials.map((material) => ({
    id: material.id, case_id: caseId, title: material.display_name,
    // 目录只给名称；页数与格式不在契约里，界面据此隐藏未知值而不是编造。
    kind: 'doc', page_count: 0,
  }));
}

export interface WorkspaceExtras {
  annotations: GraphSnapshot['annotations'];
  progressNodes: GraphSnapshot['progress_nodes'];
}

export function mapWorkspace(api: ApiWorkspace, extras: WorkspaceExtras): GraphSnapshot {
  return {
    case_id: api.case_id, case_title: api.case_title, archived: api.archived,
    seq: api.seq, seqs: { graph: api.seqs.graph ?? 0, evidence: api.seqs.evidence ?? 0 },
    entities: api.entities as GraphSnapshot['entities'],
    aliases: api.aliases as EntityAlias[],
    relations: api.relations.map((relation) => ({
      ...relation,
      attrs: (relation.attrs ?? {}) as RelationAttrs,
    })) as GraphSnapshot['relations'],
    sources: api.sources as GraphSource[],
    graphs: api.graphs as GraphSnapshot['graphs'],
    nodes: api.nodes as GraphSnapshot['nodes'],
    edges: api.edges as GraphSnapshot['edges'],
    groups: api.groups as GraphSnapshot['groups'],
    snapshots: api.snapshots.map(mapSnapshot),
    consents: api.consents as GraphSnapshot['consents'],
    evidence: {
      groups: api.evidence.groups,
      items: api.evidence.items,
      bindings: api.evidence.bindings,
      pending_changes: (api.evidence.pending_changes ?? []).map((change) => mapChange(change, 'evidence')),
    },
    materials: mapMaterials(api, api.case_id),
    // 服务端不提供整案的解析段清单：出处选择走 /search 的命中片段（见 sourceSearch）。
    segments: [],
    annotations: extras.annotations,
    progress_nodes: extras.progressNodes,
    labels: mapLabels(api),
    evidence_numbers: api.evidence.numbers,
    pending_changes: api.pending_changes.map((change) => mapChange(change, 'graph')),
    changes: api.changes.map((change) => mapChange(change, 'graph')),
    last_change_id: api.last_change_id ?? null,
  };
}

function failFrom(error: unknown): CommandResult {
  if (error instanceof ApiError) {
    const code = error.code === 'conflict' ? 'conflict'
      : error.code === 'case_mismatch' ? 'case_mismatch'
        : error.code === 'not_found' ? 'not_found' : 'validation';
    // 服务端消息已说明原因（例如归档只读、最后一条出处需降级），原样展示。
    return { ok: false, code, message: error.message };
  }
  return { ok: false, code: 'validation', message: '无法连接本地服务。' };
}

/** 服务端的规则字典与前端 GraphRules 同构；缺字段时用本地默认值兜底。 */
export function mapRules(raw: Record<string, unknown> | null | undefined): GraphRules {
  if (!raw) return defaultRules;
  return {
    entity_kinds: (raw.entity_kinds as GraphRules['entity_kinds']) ?? defaultRules.entity_kinds,
    roles: (raw.roles as GraphRules['roles']) ?? defaultRules.roles,
    relation_kinds: (raw.relation_kinds as GraphRules['relation_kinds']) ?? defaultRules.relation_kinds,
    credibility: (raw.credibility as GraphRules['credibility']) ?? defaultRules.credibility,
    snapshot_stages: (raw.snapshot_stages as string[]) ?? defaultRules.snapshot_stages,
    consent_channels: (raw.consent_channels as GraphRules['consent_channels'])
      ?? defaultRules.consent_channels,
    agent_commands: (raw.agent_commands as GraphRules['agent_commands'])
      ?? defaultRules.agent_commands,
    max_path_depth: (raw.max_path_depth as number) ?? defaultRules.max_path_depth,
    max_paths: (raw.max_paths as number) ?? defaultRules.max_paths,
  };
}

/** 标注清单：先读第一页拿总数，再按需补页（轮询时通常只有一次请求）。 */
async function loadAnnotations(caseId: string): Promise<GraphSnapshot['annotations']> {
  const PAGE = 200;
  const MAX = 1000; // 出处选择器只需覆盖常见案卷；超出的部分由检索命中补充。
  const first = await annotationsApi.list(caseId, '', 0, PAGE);
  const wanted = Math.min(first.total, MAX);
  const rest: typeof first.items = [];
  const pages = Math.ceil(wanted / PAGE);
  if (pages > 1) {
    const others = await Promise.all(
      Array.from({ length: pages - 1 }, (_, index) =>
        annotationsApi.list(caseId, '', (index + 1) * PAGE, PAGE)),
    );
    for (const page of others) rest.push(...page.items);
  }
  return [...first.items, ...rest].map((item) => ({
    id: item.annotation_id, case_id: item.case_id,
    // 标注契约只给文件版本与显示名，没有 material_id；材料上下文用 label 展示。
    material_id: '',
    page: item.page ?? 1, text: item.reviewed_text || item.body,
    label: item.display_name,
  }));
}

async function loadProgressNodes(caseId: string): Promise<GraphSnapshot['progress_nodes']> {
  // 进度节点只用于快照对话框的可选关联；读失败不影响关系图本身。
  try {
    const snapshot = await progressApi.snapshot(caseId);
    return snapshot.nodes
      .filter((node) => node.removed_at === null)
      .map((node) => ({ value: node.id, label: node.title }));
  } catch {
    return [];
  }
}

/**
 * 出处检索：真实案件页用检索命中提供解析段候选（不可变 source_ref），
 * 标注与整份材料由工作区快照直接给出（InspectorPanel 静态选项）。
 */
export function createSourceSearch(caseId: string): SourceOptionSearch {
  return async (query: string): Promise<SourceOption[]> => {
    const term = query.trim();
    if (!term) return [];
    const result = await searchApi.search(caseId, { query: term, mode: 'literal', limit: 10 });
    const options: SourceOption[] = [];
    for (const hit of result.hits) {
      if (!hit.source) continue;
      const page = (hit.source.locator as { page?: number }).page;
      const text = hit.source.text.replace(/\s+/g, ' ').slice(0, 60);
      options.push({
        value: `segment:${hit.source.source_ref}`,
        label: `片段${page ? ` · 第${page}页` : ''} · ${text}`,
      });
    }
    return options;
  };
}

export function createHttpGraphSource(caseId: string): GraphWorkspaceSource & {
  /** 证据目录导入需要真实材料清单；与工作区同一来源，避免两套名称。 */
  catalog: () => Promise<ApiCatalog>;
} {
  /** 最近一次工作区：删除分组等命令需要 revision，界面不逐条携带。 */
  let last: GraphSnapshot | null = null;
  /**
   * 最近一次可撤销的写命令变更 ID。只由写命令响应写入，后台刷新不清空；
   * 撤销成功后清空，避免第二次撤销把「撤销」本身再撤回去。
   */
  let undoChangeId: string | null = null;

  async function load(): Promise<GraphSnapshot> {
    const [api, annotations, progressNodes] = await Promise.all([
      graphApi.workspace(caseId), loadAnnotations(caseId), loadProgressNodes(caseId),
    ]);
    const snapshot = mapWorkspace(api, { annotations, progressNodes });
    last = snapshot;
    return snapshot;
  }

  function revisionOf(kind: 'group' | 'entity' | 'relation', id: string): number | null {
    if (!last) return null;
    const list = kind === 'group' ? last.groups
      : kind === 'entity' ? last.entities : last.relations;
    const found = list.find((row) => row.id === id);
    return found ? found.revision : null;
  }

  function requireRevision(kind: 'group' | 'entity' | 'relation', id: string): number {
    const revision = revisionOf(kind, id);
    if (revision === null) throw new ApiError(409, {
      code: 'conflict', message: '本地数据已过期，请刷新后重试。',
      details: {}, request_id: '',
    });
    return revision;
  }

  /** 写命令后的统一处理：接受服务端返回的工作区，必要时补读目录快照。 */
  async function afterWrite(api: ApiWorkspace, notice?: string | null): Promise<CommandResult> {
    const snapshot = mapWorkspace(api, {
      annotations: last?.annotations ?? [], progressNodes: last?.progress_nodes ?? [],
    });
    last = snapshot;
    undoChangeId = api.last_change_id ?? null;
    return { ok: true, snapshot, notice: notice ?? api.notice ?? '', last_change_id: api.last_change_id ?? null };
  }

  async function afterEvidence(api: ApiCatalog): Promise<CommandResult> {
    const fresh = await graphApi.workspace(caseId);
    const snapshot = mapWorkspace(fresh, {
      annotations: last?.annotations ?? [], progressNodes: last?.progress_nodes ?? [],
    });
    last = snapshot;
    undoChangeId = api.last_change_id ?? null;
    return { ok: true, snapshot, notice: api.notice ?? '', last_change_id: api.last_change_id ?? null };
  }

  async function run(command: GraphCommand): Promise<CommandResult> {
    try {
      switch (command.type) {
        case 'add_entity':
          return await afterWrite(await graphApi.addEntity(caseId, {
            kind: command.kind, name: command.name, roles: command.roles ?? [],
            note: command.note ?? null, graph_id: command.graph_id ?? null,
            request_key: command.request_key ?? null,
          }));
        case 'update_entity': {
          const changes: Record<string, unknown> = {};
          for (const key of ['kind', 'name', 'roles', 'note'] as const) {
            if (key in command.changes) changes[key] = command.changes[key] ?? null;
          }
          return await afterWrite(await graphApi.updateEntity(caseId, command.entity_id, {
            expected_revision: command.expected_revision,
            changes: changes as never,
          }));
        }
        case 'remove_entity':
          return await afterWrite(await graphApi.removeEntity(caseId, command.entity_id,
            command.expected_revision));
        case 'restore_entity':
          return await afterWrite(await graphApi.restoreEntity(caseId, command.entity_id,
            command.expected_revision));
        case 'add_alias':
          return await afterWrite(await graphApi.addAlias(caseId, command.entity_id, command.alias));
        case 'remove_alias':
          return await afterWrite(await graphApi.removeAlias(caseId, command.alias_id));
        case 'add_relation':
          return await afterWrite(await graphApi.addRelation(caseId, {
            from_entity_id: command.from_entity_id, to_entity_id: command.to_entity_id,
            kind: command.kind, label: command.label ?? null, credibility: command.credibility,
            directed: command.directed ?? true, attrs: (command.attrs ?? {}) as never,
            note: command.note ?? null, graph_id: command.graph_id ?? null,
            request_key: command.request_key ?? null,
          }));
        case 'update_relation': {
          const changes: Record<string, unknown> = {};
          for (const key of ['kind', 'label', 'credibility', 'directed', 'attrs', 'note'] as const) {
            if (key in command.changes) changes[key] = command.changes[key] ?? null;
          }
          return await afterWrite(await graphApi.updateRelation(caseId, command.relation_id, {
            expected_revision: command.expected_revision, changes: changes as never,
          }));
        }
        case 'remove_relation':
          return await afterWrite(await graphApi.removeRelation(caseId, command.relation_id,
            command.expected_revision));
        case 'restore_relation':
          return await afterWrite(await graphApi.restoreRelation(caseId, command.relation_id,
            command.expected_revision));
        case 'add_source': {
          const ref = 'segment_id' in command.ref
            ? { segment_id: command.ref.segment_id, material_id: null, annotation_id: null }
            : 'material_id' in command.ref
              ? { segment_id: null, material_id: command.ref.material_id, annotation_id: null }
              : { segment_id: null, material_id: null,
                annotation_id: (command.ref as { annotation_id: string }).annotation_id };
          return await afterWrite(await graphApi.addSource(caseId, {
            entity_id: command.entity_id ?? null, relation_id: command.relation_id ?? null,
            ref, proves: command.proves ?? null, request_key: command.request_key ?? null,
          }));
        }
        case 'remove_source':
          return await afterWrite(await graphApi.removeSource(caseId, command.source_id,
            command.downgrade_to ?? null));
        case 'create_graph':
          return await afterWrite(await graphApi.createGraph(caseId, {
            title: command.title, description: command.description ?? null,
            request_key: command.request_key ?? null,
          }));
        case 'update_graph': {
          const changes: Record<string, unknown> = {};
          for (const key of ['title', 'description'] as const) {
            if (key in command.changes) changes[key] = command.changes[key] ?? null;
          }
          return await afterWrite(await graphApi.updateGraph(caseId, command.graph_id, {
            expected_revision: command.expected_revision, changes: changes as never,
          }));
        }
        case 'remove_graph':
          return await afterWrite(await graphApi.removeGraph(caseId, command.graph_id,
            command.expected_revision));
        case 'add_to_graph':
          return await afterWrite(await graphApi.addToGraph(caseId, command.graph_id, {
            entity_ids: command.entity_ids ?? [], relation_ids: command.relation_ids ?? [],
          }));
        case 'remove_from_graph':
          return await afterWrite(await graphApi.removeFromGraph(caseId, command.graph_id, {
            entity_id: command.entity_id ?? null, relation_id: command.relation_id ?? null,
          }));
        case 'save_layout':
          return await afterWrite(await graphApi.saveLayout(caseId, command.graph_id, {
            nodes: (command.nodes ?? []).map((node) => ({
              node_id: last?.nodes.find((entry) => entry.id === node.node_id)?.entity_id ?? node.node_id,
              x: node.x,
              y: node.y,
            })),
            groups: (command.groups ?? []).map((group) => ({
              group_id: group.group_id, x: group.x, y: group.y,
              width: group.width ?? null, height: group.height ?? null,
            })),
          }));
        case 'set_node_group':
          return await afterWrite(await graphApi.setNodeGroup(caseId, command.graph_id,
            command.entity_id, command.group_id));
        case 'upsert_graph_group':
          return await afterWrite(await graphApi.upsertGroup(caseId, command.graph_id, {
            group_id: command.group_id ?? null,
            expected_revision: command.group_id ? revisionOf('group', command.group_id) : null,
            title: command.title, color: command.color ?? null,
            x: command.x ?? 0, y: command.y ?? 0,
            width: command.width ?? 360, height: command.height ?? 220,
            collapsed: command.collapsed ?? false, entity_ids: command.entity_ids ?? [],
          }));
        case 'remove_graph_group':
          return await afterWrite(await graphApi.removeGroup(caseId, command.group_id,
            requireRevision('group', command.group_id)));
        case 'create_snapshot':
          return await afterWrite(await graphApi.createSnapshot(caseId, command.graph_id, {
            stage_label: command.stage_label, note: command.note ?? null,
            progress_node_id: command.progress_node_id ?? null,
            acknowledge_pending: command.acknowledge_pending ?? false,
          }));
        case 'restore_snapshot':
          return await afterWrite(await graphApi.restoreSnapshot(caseId, command.snapshot_id));
        case 'add_snapshot_consent':
          return await afterWrite(await graphApi.addConsent(caseId, command.snapshot_id, {
            action: command.action, parent_consent_id: command.parent_consent_id ?? null,
            consenter: command.consenter, consented_at: command.consented_at,
            channel: command.channel, source_detail: command.source_detail,
            evidence_material_id: command.evidence_material_id ?? null,
            note: command.note ?? null,
          }));
        case 'accept_change':
          return await afterWrite(await graphApi.acceptChange(caseId, command.change_id));
        case 'accept_all':
          return await afterWrite(await graphApi.acceptAll(caseId, command.change_ids,
            command.expected_seq ?? null));
        case 'revert_change':
          return await afterWrite(await graphApi.revertChange(caseId, command.change_id));

        // --- 证据目录：接口返回目录快照，写后补读工作区以刷新编号与出处标签 ---
        case 'add_evidence_group':
          return await afterEvidence(await evidenceApi.addGroup(caseId, {
            title: command.title, purpose: command.purpose ?? null, position: null,
            request_key: command.request_key ?? null,
          }));
        case 'update_evidence_group':
          return await afterEvidence(await evidenceApi.updateGroup(caseId, command.group_id, {
            expected_revision: command.expected_revision, changes: command.changes as never,
          }));
        case 'reorder_evidence_groups':
          return await afterEvidence(await evidenceApi.reorderGroups(caseId, command.group_ids));
        case 'remove_evidence_group':
          return await afterEvidence(await evidenceApi.removeGroup(caseId, command.group_id,
            command.expected_revision));
        case 'add_evidence_item':
          return await afterEvidence(await evidenceApi.addItem(caseId, {
            group_id: command.group_id, title: command.title,
            purpose: command.purpose ?? null, position: null,
            request_key: command.request_key ?? null,
          }));
        case 'update_evidence_item':
          return await afterEvidence(await evidenceApi.updateItem(caseId, command.item_id, {
            expected_revision: command.expected_revision, changes: command.changes as never,
          }));
        case 'move_evidence_item':
          return await afterEvidence(await evidenceApi.moveItem(caseId, command.item_id, {
            to_group_id: command.to_group_id, before_item_id: command.before_item_id ?? null,
          }));
        case 'remove_evidence_item':
          return await afterEvidence(await evidenceApi.removeItem(caseId, command.item_id,
            command.expected_revision));
        case 'add_evidence_binding':
          return await afterEvidence(await evidenceApi.addBinding(caseId, {
            item_id: command.item_id, material_id: command.material_id,
            page_start: command.page_start ?? null, page_end: command.page_end ?? null,
            request_key: command.request_key ?? null,
          }));
        case 'remove_evidence_binding':
          return await afterEvidence(await evidenceApi.removeBinding(caseId, command.binding_id));
        case 'confirm_evidence_item':
          return await afterEvidence(await evidenceApi.confirmItem(caseId, command.item_id,
            command.expected_revision));
        case 'reject_evidence_item':
          return await afterEvidence(await evidenceApi.rejectItem(caseId, command.item_id,
            command.expected_revision));
        case 'confirm_evidence_binding':
          // 绑定没有 revision 列，服务端复核只按 binding_id 取行；这里传 0 与原审定契约一致。
          return await afterEvidence(await evidenceApi.confirmBinding(caseId, command.binding_id, 0));
        case 'reject_evidence_binding':
          return await afterEvidence(await evidenceApi.rejectBinding(caseId, command.binding_id, 0));
        case 'import_evidence_catalog':
          return {
            ok: false, code: 'validation',
            message: '真实案件请用「证据目录」页签的上传导入：先解析已上传的目录文件，匹配材料后再提交。',
          };
        case 'accept_evidence_change':
          return await afterEvidence(await evidenceApi.acceptChange(caseId, command.change_id));
        case 'revert_evidence_change':
          return await afterEvidence(await evidenceApi.revertChange(caseId, command.change_id));
        default:
          return { ok: false, code: 'validation', message: '未知命令' };
      }
    } catch (error) {
      return failFrom(error);
    }
  }

  return {
    load,
    rules: async () => mapRules((await graphApi.rules()).rules),
    seqs: async () => {
      const { seqs } = await progressApi.seqs(caseId);
      return { graph: seqs.graph ?? 0, evidence: seqs.evidence ?? 0 };
    },
    human: (command) => run(command),
    undo: async () => {
      const changeId = undoChangeId;
      if (!changeId) {
        return { ok: false, code: 'validation', message: '没有可撤销的最近一步。' };
      }
      const result = await run({ type: 'revert_change', change_id: changeId });
      // 撤销本身不进入撤销栈：成功后再清空，避免第二次撤销把「撤销」撤回去。
      undoChangeId = result.ok ? null : changeId;
      return result;
    },
    impact: async (target) => {
      try {
        const result = await graphApi.removalImpact(caseId, target);
        return { graphs: result.graphs.map((graph) => ({ id: graph.id, title: graph.title })) };
      } catch {
        return { graphs: [] };
      }
    },
    archive: async (archived: boolean) => {
      const overview = await casesApi.overview(caseId);
      const already = Boolean(overview.archived_at);
      if (already === archived) return;
      if (archived) await casesApi.archive(caseId, overview.revision);
      else await casesApi.unarchive(caseId, overview.revision);
    },
    catalog: () => evidenceApi.catalog(caseId),
  };
}
