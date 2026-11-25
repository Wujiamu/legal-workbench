/**
 * 关系图工作区视图（可复用）：原型页与真实案件页共用同一套界面。
 *
 * 视图只接收快照、规则与命令入口，不关心数据来自内存还是 HTTP；
 * 所有命令都可能返回 Promise：提交期间禁用按钮，等服务端响应后再关闭弹窗或显示错误。
 * 选中对象在快照刷新后若已不存在会被清空，避免旧响应对着一个已删除的主体/关系继续操作。
 *
 * 真实案件页需要额外注入：
 * - onOpenSource：用 SourcePreview 打开真实材料版本与页码（原型默认打开合成原文弹窗）；
 * - loadImpact：删除前的影响面（由服务端返回；缺失时不显示伪造数量）；
 * - onArchive：案件级归档/取消归档（P4 走 casesApi）。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { chartsApi } from '../api/endpoints';
import { downloadTextFile } from '../claims/exportClaimsSvg';
import { AppBrand, ThemeSwitcher } from '../components/BrandMark';
import { ChartExportDialog } from '../components/ChartExportDialog';
import type { ApprovedIssue, ChartExportFormat, ChartExportSettings } from '../components/ChartExportDialog';
import { EntityDiscoveryDialog } from './EntityDiscoveryDialog';
import { EvidenceCatalogPanel } from './EvidenceCatalogPanel';
import { GraphAuditDrawer } from './GraphAuditDrawer';
import { GraphCanvas } from './GraphCanvas';
import type { LayoutPayload, Selection } from './GraphCanvas';
import { GraphChangeReview } from './GraphChangeReview';
import {
  AddToGraphDialog, ArchiveDialog, DeleteDialog, EntityDialog, GraphDialog, GroupDialog,
  RelationDialog, SourcePreviewDialog,
} from './GraphDialogs';
import type { RelationDialogPrefill } from './GraphDialogs';
import { InspectorPanel } from './InspectorPanel';
import { SnapshotDrawer } from './SnapshotDrawer';
import { TableImportDialog } from './TableImportDialog';
import { buildGraphSvg, downloadPngFile, downloadSvgFile, exportFileName } from './exportSvg';
import type { GraphExportProjection } from './exportSvg';
import { autoLayout } from './layout';
import {
  buildCanvasModel, buildSnapshotCanvasModel, buildSnapshotPayload, graphsActive,
  pendingChangesInGraph,
} from './model';
import { findPaths } from './paths';
import type {
  CanvasGroup, CaseEntity, CaseRelation, CommandResult, GraphCommandRunner, GraphRules,
  GraphSnapshot, GraphSource, RelationGraph, SourceOptionSearch,
} from './types';
import './graph.css';

type EntityDialogState = { mode: 'add' | 'edit'; entity?: CaseEntity } | null;
type RelationDialogState = {
  mode: 'add' | 'edit';
  relation?: CaseRelation;
  prefill?: RelationDialogPrefill;
} | null;
type DeleteTarget = { kind: 'entity' | 'relation' | 'graph'; id: string; name: string; revision: number } | null;

function formatExportTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  }).format(date);
}

function inferEntityKind(name: string): string {
  return /(?:有限公司|股份有限公司|有限责任公司|合伙企业|合伙|公司|银行|支行|分行|法院|仲裁委员会|事务所)$/.test(name)
    ? 'org'
    : 'person';
}

function entityName(ws: GraphSnapshot, id: string): string {
  return ws.entities.find((entity) => entity.id === id)?.name ?? '（已删除）';
}

export interface GraphWorkspaceProps {
  snapshot: GraphSnapshot;
  rules: GraphRules;
  run: GraphCommandRunner;
  runAgent?: GraphCommandRunner;
  undo?: () => Promise<CommandResult>;
  notice?: string;
  error?: string;
  onDismissNotice?: () => void;
  onDismissError?: () => void;
  /** 页头副标题，例如「TEST-ONLY 合成案卷」。 */
  section?: string;
  /** 页头右侧附加操作（如返回案件工作表）。 */
  headerAction?: ReactNode;
  /** 案件级统一导航栏；原型页不传。 */
  caseNav?: ReactNode;
  /** 原型演示控制；真实案件页不传。 */
  demo?: ReactNode;
  /** 出处点击：默认打开合成原文；真实案件页传入 SourcePreview 打开逻辑。 */
  onOpenSource?: (source: GraphSource) => void;
  /** 真实案件页的解析段检索（出处选择器）；原型不传。 */
  sourceOptions?: SourceOptionSearch;
  /** 真实案件页的证据目录导入面板（上传预览 + 材料映射）；原型不传。 */
  evidenceImport?: ReactNode;
  /** 真实 CSV 导出地址；缺失时隐藏导出按钮。 */
  exportHref?: string;
  /** 真实案件自动装卷（ZIP）下载地址；缺失时隐藏。 */
  bundleHref?: string;
  /** 删除主体/关系前的影响面；返回 null 表示数据源未提供。 */
  loadImpact?: (target: { entity_id?: string; relation_id?: string })
    => Promise<{ id: string; title: string }[] | null>;
  /** 案件级归档开关；缺失时隐藏归档按钮。 */
  onArchive?: (archived: boolean) => Promise<void>;
  /** 视图内产生的错误（自动排版、导出）交给工作区统一提示。 */
  onLocalError?: (message: string) => void;
  /** 后台刷新失败的明确提示（保留在上方，直到重试成功或用户重试）。 */
  refreshError?: string;
  onRetryRefresh?: () => void;
  /** 真实案件页 UUID；传入后启用三态导出、防漏审计、共现秒查、初筛消歧与表格映射。 */
  caseId?: string;
  /** 打开真实原件/片段预览。 */
  onOpenSourcePreview?: (info: {
    fileVersionId: string;
    title: string;
    page?: number;
    segmentId?: string;
  }) => void;
  /** 表格导入等批量服务端提交后的工作区刷新回调。 */
  onReload?: () => Promise<void>;
  /** 视图内状态提示回调。 */
  onLocalNotice?: (message: string) => void;
}

export function GraphWorkspace({
  snapshot: ws, rules, run, runAgent, undo, notice = '', error = '',
  onDismissNotice, onDismissError, section, headerAction, caseNav, demo, onOpenSource, loadImpact,
  onArchive, onLocalError, onLocalNotice, sourceOptions, evidenceImport, exportHref, bundleHref,
  refreshError = '', onRetryRefresh, caseId, onOpenSourcePreview, onReload,
}: GraphWorkspaceProps) {
  const readOnly = ws.archived;

  const [graphId, setGraphId] = useState<string>(() => ws.graphs[0]?.id ?? '');
  const [selection, setSelection] = useState<Selection>(null);
  const [credibilityFilter, setCredibilityFilter] = useState<Set<string>>(
    () => new Set(rules.credibility.map((rule) => rule.value)));
  const [kindFilter, setKindFilter] = useState('all');
  const [pathFrom, setPathFrom] = useState('');
  const [pathTo, setPathTo] = useState('');
  const [pathActive, setPathActive] = useState(false);
  const [algorithm, setAlgorithm] = useState<'stress' | 'layered'>('stress');
  const [layoutBusy, setLayoutBusy] = useState(false);

  const [entityDialog, setEntityDialog] = useState<EntityDialogState>(null);
  const [relationDialog, setRelationDialog] = useState<RelationDialogState>(null);
  const [graphDialog, setGraphDialog] = useState<{ mode: 'add' | 'rename' } | null>(null);
  const [addToGraphOpen, setAddToGraphOpen] = useState(false);
  const [groupDialog, setGroupDialog] = useState<{ group: CanvasGroup | null } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget>(null);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [archiveError, setArchiveError] = useState('');
  const [sourceId, setSourceId] = useState<string | null>(null);
  const [snapshotOpen, setSnapshotOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState<{ initialSnapshotId?: string } | null>(null);
  const [auditOpen, setAuditOpen] = useState(false);
  const [auditRefresh, setAuditRefresh] = useState(0);
  const [discoveryOpen, setDiscoveryOpen] = useState(false);
  const [tableImportOpen, setTableImportOpen] = useState(false);

  const activeGraph: RelationGraph | null = ws.graphs.find((graph) =>
    graph.id === graphId && graph.removed_at === null) ?? graphsActive(ws)[0] ?? null;
  const graphKey = activeGraph?.id ?? '';
  const model = useMemo(() => (activeGraph
    ? buildCanvasModel(ws, activeGraph.id)
    : { nodes: [], edges: [], groups: [] }), [ws, activeGraph?.id]);

  // 选中对象在刷新后消失时清空，避免继续对旧对象提交命令。
  useEffect(() => {
    if (!selection) return;
    if (selection.type === 'entity') {
      const exists = ws.entities.some((entity) => entity.id === selection.id && entity.removed_at === null);
      if (!exists) setSelection(null);
    } else {
      const exists = ws.relations.some((relation) => relation.id === selection.id && relation.removed_at === null);
      if (!exists) setSelection(null);
    }
  }, [ws.entities, ws.relations, selection]);

  const pathSearch = useMemo(() => (pathFrom && pathTo && pathFrom !== pathTo
    ? findPaths(model.edges, pathFrom, pathTo, rules.max_path_depth, rules.max_paths)
    : null), [model.edges, pathFrom, pathTo, rules.max_path_depth, rules.max_paths]);

  const undoRef = undo;
  useEffect(() => {
    if (!undoRef) return;
    function onKey(event: KeyboardEvent) {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'z' || event.shiftKey) return;
      const tag = (event.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (document.querySelector('dialog[open]')) return;
      event.preventDefault();
      void undoRef!();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [undoRef]);

  function switchGraph(id: string) {
    setGraphId(id);
    setSelection(null);
    setPathFrom('');
    setPathTo('');
    setPathActive(false);
  }

  function openSource(sourceId: string) {
    if (onOpenSource) {
      const source = ws.sources.find((item) => item.id === sourceId);
      if (source) onOpenSource(source);
      return;
    }
    setSourceId(sourceId);
  }

  function handleSaveLayout(payload: LayoutPayload) {
    if (!activeGraph) return;
    void run({ type: 'save_layout', graph_id: activeGraph.id, ...payload });
  }

  // 回调保持稳定引用：GraphCanvas 的节点数组依赖它们，异步刷新时不应重建节点、
  // 否则拖拽中的位置会被快照刷新重置。
  const toggleGroup = useCallback((group: CanvasGroup) => {
    if (!activeGraph) return;
    void run({
      type: 'upsert_graph_group', graph_id: activeGraph.id, group_id: group.id,
      title: group.title, color: group.color, collapsed: !group.collapsed,
    });
  }, [activeGraph, run]);

  const editGroup = useCallback((group: CanvasGroup) => setGroupDialog({ group }), []);

  async function handleAutoLayout() {
    if (!activeGraph) return;
    setLayoutBusy(true);
    try {
      const result = await autoLayout(model, algorithm);
      const nodes = [...result.nodes.entries()].flatMap(([entityId, position]) => {
        const node = ws.nodes.find((item) => item.graph_id === activeGraph.id
          && item.entity_id === entityId);
        return node ? [{ node_id: node.id, x: position.x, y: position.y }] : [];
      });
      const groups = [...result.groups.entries()].map(([id, rect]) => ({
        group_id: id, x: rect.x, y: rect.y, width: rect.width, height: rect.height,
      }));
      const saved = await run({ type: 'save_layout', graph_id: activeGraph.id, nodes, groups });
      if (!saved.ok) onLocalError?.(saved.message);
    } catch (cause) {
      // 排版在浏览器内完成，失败与后端无关。
      onLocalError?.(`自动排版失败：${cause instanceof Error ? cause.message : String(cause)}`);
    } finally {
      setLayoutBusy(false);
    }
  }

  const filteredModel = useMemo(() => ({
    ...model,
    edges: model.edges.filter((edge) => credibilityFilter.has(edge.credibility)
      && (kindFilter === 'all' || kindFilter === edge.kind)),
  }), [model, credibilityFilter, kindFilter]);

  function handleExportSvg() {
    if (!activeGraph) return;
    try {
      const { svg } = buildGraphSvg({
        title: activeGraph.title,
        caseTitle: ws.case_title,
        model: filteredModel,
        rules,
        note: ws.archived ? '案件已归档 · 只读导出' : undefined,
      });
      downloadSvgFile(exportFileName(activeGraph.title, 'svg'), svg);
    } catch (cause) {
      onLocalError?.(`导出失败：${cause instanceof Error ? cause.message : String(cause)}`);
    }
  }

  async function handleExportPng() {
    if (!activeGraph) return;
    try {
      const { svg, width, height } = buildGraphSvg({
        title: activeGraph.title,
        caseTitle: ws.case_title,
        model: filteredModel,
        rules,
      });
      await downloadPngFile(exportFileName(activeGraph.title, 'png'), svg, width, height);
    } catch (cause) {
      onLocalError?.(`导出失败：${cause instanceof Error ? cause.message : String(cause)}`);
    }
  }

  async function handleFreezeForExport(): Promise<string> {
    if (!activeGraph) throw new Error('当前没有选中的关系图');
    const pendingCount = pendingChangesInGraph(ws, activeGraph.id).length;
    const result = await run({
      type: 'create_snapshot',
      graph_id: activeGraph.id,
      stage_label: '对外导出冻结',
      note: null,
      progress_node_id: null,
      acknowledge_pending: pendingCount > 0 ? true : undefined,
    });
    if (!result.ok) throw new Error(result.message);
    const created = result.snapshot.snapshots
      .filter((item) => item.graph_id === activeGraph.id)
      .sort((a, b) => b.version_no - a.version_no)[0];
    if (!created) throw new Error('快照冻结后未找到新快照');
    return created.id;
  }

  function handleLocateTarget(targetId: string) {
    if (ws.entities.some((item) => item.id === targetId && item.removed_at === null)) {
      setSelection({ type: 'entity', id: targetId });
      setExportOpen(null);
      return;
    }
    if (ws.relations.some((item) => item.id === targetId && item.removed_at === null)) {
      setSelection({ type: 'relation', id: targetId });
      setExportOpen(null);
    }
  }

  async function handleDownloadTriState(
    format: ChartExportFormat,
    settings: ChartExportSettings,
    _preview: unknown,
    approved: ApprovedIssue[],
    showWorkingLabels: boolean,
    signal?: AbortSignal,
  ): Promise<void> {
    if (!activeGraph || !caseId) return;
    if (settings.profile === 'working') {
      const { svg, width, height } = buildGraphSvg({
        title: activeGraph.title,
        caseTitle: ws.case_title,
        model: filteredModel,
        rules,
        showWorkingLabels,
        note: ws.archived ? '案件已归档 · 只读导出' : undefined,
      });
      if (signal?.aborted) return;
      if (format === 'svg') {
        downloadSvgFile(exportFileName(activeGraph.title, 'svg'), svg);
      } else if (format === 'png') {
        await downloadPngFile(exportFileName(activeGraph.title, 'png'), svg, width, height);
      }
      return;
    }

    if (format === 'md' || format === 'csv') {
      const text = await chartsApi.citationTableText(caseId, {
        chart_type: 'graph',
        target_id: activeGraph.id,
        profile: settings.profile,
        snapshot_id: settings.snapshotId || null,
        excluded_suspected: settings.excludedSuspected,
        format,
        waivers: approved,
      }, signal);
      if (signal?.aborted) return;
      const baseName = exportFileName(activeGraph.title, 'svg').replace(/\.svg$/i, '');
      downloadTextFile(
        `${baseName}-引用索引.${format}`,
        format === 'csv' ? `\ufeff${text}` : text,
        format === 'csv' ? 'text/csv;charset=utf-8' : 'text/markdown;charset=utf-8',
      );
      return;
    }

    const selectedSnapshot = settings.snapshotId
      ? ws.snapshots.find((item) => item.id === settings.snapshotId) ?? null
      : null;
    if (settings.snapshotId && !selectedSnapshot) {
      throw new Error('选中的冻结快照在当前工作区中不存在，请刷新后重试。');
    }

    const table = await chartsApi.citationTableJson(caseId, {
      chart_type: 'graph',
      target_id: activeGraph.id,
      profile: settings.profile,
      snapshot_id: settings.snapshotId || null,
      excluded_suspected: settings.excludedSuspected,
      waivers: approved,
    }, signal);
    if (signal?.aborted) return;
    const baseModel = selectedSnapshot
      ? buildSnapshotCanvasModel(selectedSnapshot)
      : buildSnapshotCanvasModel({
        payload: buildSnapshotPayload(ws, activeGraph.id, { excludePending: true }),
      });
    const heldIds = new Set(
      table.assertions.filter((item) => item.held_for_proof).map((item) => item.target_id),
    );
    const projectedModel = heldIds.size === 0
      ? baseModel
      : {
        ...baseModel,
        edges: baseModel.edges.map((edge) => (
          heldIds.has(edge.id)
            ? { ...edge, label: edge.label ? `${edge.label}（待举证主张）` : '待举证主张' }
            : edge
        )),
      };
    const projection: GraphExportProjection = {
      profile: settings.profile,
      includedIds: new Set(table.assertions.map((item) => item.target_id)),
      references: table.rows.map((row) => ({
        targetId: row.target_id,
        index: row.index,
        label: `${row.evidence_number}${row.location ? ` ${row.location}` : ''}`,
      })),
      version: table.chart_meta.snapshot_version != null
        ? `快照 v${table.chart_meta.snapshot_version}`
        : undefined,
      notice: [
        table.chart_meta.contains_suspected ? '含待查/待举证项，导出前请人工核对' : null,
        settings.profile === 'court'
          ? '当庭呈递版 · 仅含已确认内容与正式引用'
          : '客户沟通版 · 已过滤内部备注与未确认项',
      ].filter(Boolean).join(' · '),
    };
    const { svg, width, height } = buildGraphSvg({
      title: table.chart_meta.title || activeGraph.title,
      caseTitle: ws.case_title,
      model: projectedModel,
      rules,
      generatedAt: formatExportTime(table.chart_meta.generated_at),
      projection,
    });
    if (format === 'svg') {
      downloadSvgFile(exportFileName(activeGraph.title, 'svg'), svg);
    } else {
      await downloadPngFile(exportFileName(activeGraph.title, 'png'), svg, width, height);
    }
  }

  async function handleAddOrphanEntity(name: string, segmentId?: string): Promise<void> {
    if (!activeGraph) return;
    let res = await run({
      type: 'add_entity',
      kind: inferEntityKind(name),
      name,
      roles: [],
      note: null,
      graph_id: activeGraph.id,
    });
    if (!res.ok) throw new Error(res.message);
    if (segmentId) {
      const snap = res.snapshot;
      const lastChange = snap.last_change_id
        ? snap.changes.find((c) => c.id === snap.last_change_id)
        : undefined;
      const createdId = lastChange?.diff.find((d) => d.target === 'entity' && d.before === null)?.id
        ?? snap.entities.find((e) => e.removed_at === null && e.name === name)?.id;
      if (createdId) {
        res = await run({
          type: 'add_source',
          entity_id: createdId,
          ref: { segment_id: segmentId },
          proves: '全文识别孤儿机构',
        });
        if (!res.ok) throw new Error(res.message);
      }
    }
    setAuditRefresh((value) => value + 1);
  }

  const confirmDelete = useCallback(async (): Promise<CommandResult> => {
    if (!deleteTarget) return { ok: false, code: 'validation', message: '没有待删除的对象。' };
    const result = await run(deleteTarget.kind === 'entity'
      ? { type: 'remove_entity', entity_id: deleteTarget.id, expected_revision: deleteTarget.revision }
      : deleteTarget.kind === 'relation'
        ? { type: 'remove_relation', relation_id: deleteTarget.id, expected_revision: deleteTarget.revision }
        : { type: 'remove_graph', graph_id: deleteTarget.id, expected_revision: deleteTarget.revision });
    if (result.ok) {
      setDeleteTarget(null);
      setSelection(null);
      if (deleteTarget.kind === 'graph') {
        const next = graphsActive(result.snapshot)[0];
        if (next) switchGraph(next.id);
      }
    }
    return result;
  }, [deleteTarget, run]);

  async function confirmArchive() {
    if (!onArchive) return;
    setArchiveBusy(true);
    setArchiveError('');
    try {
      await onArchive(!ws.archived);
      setArchiveOpen(false);
    } catch (cause) {
      setArchiveError(cause instanceof Error ? cause.message : '归档操作失败');
    } finally {
      setArchiveBusy(false);
    }
  }

  const graphSnapshots = activeGraph
    ? ws.snapshots.filter((item) => item.graph_id === activeGraph.id) : [];

  return (
    <div className="graph-prototype">
      <header className="app-bar"><div className="app-bar-inner">
        <AppBrand />
        <span className="app-section">{section ?? '案件关系图'}</span>
        {headerAction}
        <ThemeSwitcher />
      </div></header>
      {caseNav ?? null}
      <main className="graph-main">
        <div className="page-heading graph-heading">
          <div>
            <h1>{ws.case_title}</h1>
            <p className="muted graph-subtitle">
              一案多图：主体、别名、关系与出处属于案件；每张图各自保存纳入的节点与布局。
            </p>
          </div>
          <div className="dashboard-actions">
            {onArchive ? (
              <button type="button" className="archive-btn" onClick={() => {
                setArchiveError('');
                setArchiveOpen(true);
              }}>
                {ws.archived ? '取消归档' : '结案归档'}
              </button>
            ) : null}
            <button type="button" className="graph-snapshot-open"
              onClick={() => setSnapshotOpen(true)}>
              版本快照 · 客户同意（{graphSnapshots.length}）
            </button>
          </div>
        </div>

        <div className="graph-tabs" role="tablist" aria-label="关系图">
          {graphsActive(ws).map((graph) => (
            <button key={graph.id} type="button" role="tab"
              aria-selected={graph.id === activeGraph?.id}
              onClick={() => switchGraph(graph.id)}>
              {graph.title}
              <span className="muted">（{ws.nodes.filter((node) => node.graph_id === graph.id).length}）</span>
            </button>
          ))}
          <button type="button" className="graph-tab-add" disabled={readOnly}
            onClick={() => setGraphDialog({ mode: 'add' })}>＋ 新建图</button>
          {activeGraph ? (
            <>
              <button type="button" disabled={readOnly}
                onClick={() => setGraphDialog({ mode: 'rename' })}>重命名</button>
              <button type="button" disabled={readOnly}
                onClick={() => setDeleteTarget({
                  kind: 'graph', id: activeGraph.id, name: activeGraph.title,
                  revision: activeGraph.revision,
                })}>删除本图</button>
            </>
          ) : null}
        </div>
        {activeGraph?.description ? (
          <p className="muted graph-description">{activeGraph.description}</p>
        ) : null}

        {ws.archived ? (
          <p role="status" className="graph-archived">
            本案已归档：关系图、证据目录、快照与客户同意全部只读；查看与导出仍可用。
          </p>
        ) : null}

        {demo ? (
          <section className="graph-demo" aria-label="原型演示控制">
            <strong>演示控制</strong>
            {demo}
          </section>
        ) : null}

        {refreshError ? (
          <p role="alert" className="graph-error">
            刷新失败：{refreshError}
            {onRetryRefresh ? <button type="button" onClick={onRetryRefresh}>重试刷新</button> : null}
          </p>
        ) : null}
        {notice ? (
          <p role="status" className="graph-notice">{notice}
            <button type="button" onClick={onDismissNotice}>知道了</button>
          </p>
        ) : null}
        {error ? (
          <p role="alert" className="graph-error">{error}
            <button type="button" onClick={onDismissError}>关闭</button>
          </p>
        ) : null}

        <div className="graph-layout">
          <section className="graph-canvas-panel" aria-label="关系图画布与工具">
            <div className="graph-toolbar">
              <fieldset className="graph-filter">
                <legend>可信状态（{credibilityFilter.size}/{rules.credibility.length}）</legend>
                {rules.credibility.map((rule) => (
                  <label key={rule.value} className="graph-checkbox">
                    <input type="checkbox" checked={credibilityFilter.has(rule.value)}
                      onChange={(event) => {
                        const checked = event.currentTarget.checked;
                        setCredibilityFilter((current) => {
                          const next = new Set(current);
                          if (checked) next.add(rule.value);
                          else next.delete(rule.value);
                          return next;
                        });
                      }} />
                    <span className="graph-legend-line" aria-hidden="true"
                      style={{
                        borderTopColor: rule.stroke,
                        borderTopStyle: rule.dash ? 'dashed' : 'solid',
                        borderTopWidth: Math.max(2, rule.width),
                      }} />
                    {rule.label}
                  </label>
                ))}
              </fieldset>
              <label className="graph-field-inline">关系类型
                <select value={kindFilter} onChange={(event) => setKindFilter(event.target.value)}>
                  <option value="all">全部类型</option>
                  {rules.relation_kinds.map((kind) => (
                    <option key={kind.value} value={kind.value}>{kind.label}</option>
                  ))}
                </select>
              </label>
              <label className="graph-field-inline">定位主体
                <select value="" onChange={(event) => {
                  if (event.target.value) setSelection({ type: 'entity', id: event.target.value });
                }}>
                  <option value="">选择…</option>
                  {model.nodes.map((node) => (
                    <option key={node.id} value={node.id}>{node.name}</option>
                  ))}
                </select>
              </label>
              <div className="graph-path">
                <label className="graph-field-inline">路径起点
                  <select aria-label="路径起点" value={pathFrom} onChange={(event) => {
                    setPathFrom(event.target.value); setPathActive(false);
                  }}>
                    <option value="">选择…</option>
                    {model.nodes.map((node) => (
                      <option key={node.id} value={node.id}>{node.name}</option>
                    ))}
                  </select>
                </label>
                <label className="graph-field-inline">路径终点
                  <select aria-label="路径终点" value={pathTo} onChange={(event) => {
                    setPathTo(event.target.value); setPathActive(false);
                  }}>
                    <option value="">选择…</option>
                    {model.nodes.map((node) => (
                      <option key={node.id} value={node.id}>{node.name}</option>
                    ))}
                  </select>
                </label>
                <button type="button" disabled={!pathFrom || !pathTo || pathFrom === pathTo}
                  onClick={() => setPathActive(true)}>高亮路径</button>
                <button type="button" onClick={() => {
                  setPathActive(false); setPathFrom(''); setPathTo('');
                }}>清除</button>
              </div>
              <div className="graph-layout-tools">
                <select aria-label="自动排版算法" value={algorithm}
                  onChange={(event) => setAlgorithm(event.target.value as 'stress' | 'layered')}>
                  <option value="stress">网状（stress）</option>
                  <option value="layered">层级（layered）</option>
                </select>
                <button type="button" disabled={layoutBusy || readOnly} onClick={() => void handleAutoLayout()}>
                  {layoutBusy ? '排版中…' : '自动排版'}
                </button>
              </div>
              <div className="graph-export">
                {caseId ? (
                  <button type="button"
                    disabled={model.nodes.length === 0 && graphSnapshots.length === 0}
                    onClick={() => setExportOpen({})}>
                    三态导出…
                  </button>
                ) : (
                  <>
                    <button type="button" disabled={model.nodes.length === 0} onClick={handleExportSvg}>
                      导出 SVG
                    </button>
                    <button type="button" disabled={model.nodes.length === 0} onClick={() => void handleExportPng()}>
                      导出 PNG
                    </button>
                  </>
                )}
              </div>
              {caseId ? (
                <div className="graph-assist-tools">
                  <button type="button" onClick={() => setAuditOpen(true)}>防漏与暗线审计</button>
                  <button type="button" disabled={readOnly} onClick={() => setDiscoveryOpen(true)}>
                    初筛主体与别名
                  </button>
                  <button type="button" disabled={readOnly || !activeGraph} onClick={() => setTableImportOpen(true)}>
                    表格映射出边
                  </button>
                </div>
              ) : null}
              <div className="graph-add-tools">
                <button type="button" disabled={readOnly}
                  onClick={() => setAddToGraphOpen(true)}>加入本图…</button>
                <button type="button" disabled={readOnly}
                  onClick={() => setGroupDialog({ group: null })}>＋ 分组</button>
                <button type="button" disabled={readOnly}
                  onClick={() => setEntityDialog({ mode: 'add' })}>＋ 主体</button>
                <button type="button" disabled={readOnly}
                  onClick={() => setRelationDialog({ mode: 'add' })}>＋ 关系</button>
              </div>
            </div>

            <details className="graph-legend-details">
              <summary>线型图例</summary>
              <ul className="graph-legend" aria-label="线型图例">
                {rules.credibility.map((rule) => (
                  <li key={rule.value}>
                    <span className="graph-legend-line" aria-hidden="true"
                      style={{
                        borderTopColor: rule.stroke,
                        borderTopStyle: rule.dash ? 'dashed' : 'solid',
                        borderTopWidth: Math.max(2, rule.width),
                      }} />
                    <strong>{rule.label}</strong>
                    <span className="muted">{rule.hint}</span>
                  </li>
                ))}
              </ul>
            </details>

            {pathActive && pathSearch ? (
              <div className="graph-path-result" role="status">
                {pathSearch.paths.length === 0 ? (
                  <p>两个主体之间没有连通路径。</p>
                ) : (
                  <>
                    <p>
                      找到 {pathSearch.paths.length} 条连通路径
                      {pathSearch.truncated ? '（已达上限，仅显示部分）' : ''}：
                    </p>
                    <ol>
                      {pathSearch.paths.map((path) => (
                        <li key={path.edgeIds.join('|')}>
                          {path.nodes.map((id) => entityName(ws, id)).join(' → ')}
                        </li>
                      ))}
                    </ol>
                  </>
                )}
              </div>
            ) : null}

            <GraphCanvas
              model={model}
              rules={rules}
              graphId={graphKey}
              selected={selection}
              onSelect={setSelection}
              readOnly={readOnly}
              filters={{ credibility: credibilityFilter, kind: kindFilter }}
              highlight={pathActive && pathSearch
                ? { nodeIds: pathSearch.nodeIds, edgeIds: pathSearch.edgeIds } : null}
              onSaveLayout={handleSaveLayout}
              onToggleGroup={toggleGroup}
              onEditGroup={editGroup}
            />
          </section>

          <aside className="graph-side">
            <GraphChangeReview ws={ws} rules={rules} readOnly={readOnly} onCommand={run} />
            <InspectorPanel
              ws={ws} rules={rules} graphId={graphKey} selection={selection} readOnly={readOnly}
              run={run} onSelect={setSelection} onOpenSource={openSource}
              searchSources={sourceOptions}
              caseId={caseId}
              pathFrom={pathFrom}
              pathTo={pathTo}
              onOpenCooccurrenceSource={onOpenSourcePreview}
              onCreateRelationFromCooccurrence={(info) => {
                setRelationDialog({
                  mode: 'add',
                  prefill: {
                    fromEntityId: info.fromEntityId,
                    toEntityId: info.toEntityId,
                    amountYuan: info.amount ? info.amount.replace(/[^\d.]/g, '') : undefined,
                    dateStart: info.date && /^\d{4}-\d{2}-\d{2}$/.test(info.date) ? info.date : undefined,
                    initialSources: [{ ref: { segment_id: info.segmentId } }],
                  },
                });
              }}
              onEditEntity={(entity) => setEntityDialog({ mode: 'edit', entity })}
              onEditRelation={(relation) => setRelationDialog({ mode: 'edit', relation })}
              onDeleteEntity={(entity) => setDeleteTarget({
                kind: 'entity', id: entity.id, name: entity.name, revision: entity.revision,
              })}
              onDeleteRelation={(relation) => setDeleteTarget({
                kind: 'relation', id: relation.id,
                name: `${entityName(ws, relation.from_entity_id)} → ${entityName(ws, relation.to_entity_id)}`,
                revision: relation.revision,
              })}
            />
          </aside>
        </div>

        {caseId ? (
          <details className="graph-evidence-fold">
            <summary>
              案卷证据目录（编号唯一来源 · 共 {ws.evidence.items.filter((item) => item.removed_at === null).length} 条）
            </summary>
            <EvidenceCatalogPanel ws={ws} readOnly={readOnly} run={run}
              exportHref={exportHref} bundleHref={bundleHref} importSlot={evidenceImport}
              runAgent={runAgent ?? (async () => ({
                ok: false as const, code: 'read_only' as const,
                message: '当前入口不提供 Agent 写入。',
              }))} />
          </details>
        ) : (
          <EvidenceCatalogPanel ws={ws} readOnly={readOnly} run={run}
            exportHref={exportHref} bundleHref={bundleHref} importSlot={evidenceImport}
            runAgent={runAgent ?? (async () => ({
              ok: false as const, code: 'read_only' as const,
              message: '当前入口不提供 Agent 写入。',
            }))} />
        )}
      </main>

      {entityDialog ? (
        <EntityDialog mode={entityDialog.mode} entity={entityDialog.entity} rules={rules}
          graphId={graphKey} onClose={() => setEntityDialog(null)} onSubmit={run} />
      ) : null}
      {relationDialog && activeGraph ? (
        <RelationDialog mode={relationDialog.mode} relation={relationDialog.relation}
          prefill={relationDialog.prefill}
          ws={ws} rules={rules} graphId={activeGraph.id}
          onClose={() => setRelationDialog(null)} onSubmit={run} />
      ) : null}
      {graphDialog ? (
        <GraphDialog mode={graphDialog.mode}
          graph={graphDialog.mode === 'rename' && activeGraph ? {
            id: activeGraph.id, title: activeGraph.title,
            description: activeGraph.description, revision: activeGraph.revision,
          } : undefined}
          onClose={() => setGraphDialog(null)}
          onSubmit={async (command) => {
            const result = await run(command);
            if (result.ok && command.type === 'create_graph') {
              const created = [...result.snapshot.graphs]
                .filter((graph) => graph.removed_at === null)
                .sort((a, b) => b.position - a.position)[0];
              if (created) switchGraph(created.id);
            }
            return result;
          }} />
      ) : null}
      {addToGraphOpen && activeGraph ? (
        <AddToGraphDialog ws={ws} rules={rules} graphId={activeGraph.id}
          onClose={() => setAddToGraphOpen(false)} onSubmit={run} />
      ) : null}
      {groupDialog && activeGraph ? (
        <GroupDialog group={groupDialog.group} ws={ws} graphId={activeGraph.id}
          onClose={() => setGroupDialog(null)} onSubmit={run} />
      ) : null}
      {deleteTarget ? (
        <DeleteDialog kind={deleteTarget.kind} name={deleteTarget.name}
          loadImpact={loadImpact
            ? () => loadImpact(deleteTarget.kind === 'entity'
              ? { entity_id: deleteTarget.id } : { relation_id: deleteTarget.id })
            : undefined}
          onClose={() => setDeleteTarget(null)} onConfirm={confirmDelete} />
      ) : null}
      {archiveOpen && onArchive ? (
        <ArchiveDialog archived={ws.archived} busy={archiveBusy} error={archiveError}
          onClose={() => setArchiveOpen(false)} onConfirm={confirmArchive} />
      ) : null}
      {sourceId ? (
        <SourcePreviewDialog ws={ws} sourceId={sourceId} onClose={() => setSourceId(null)} />
      ) : null}
      {activeGraph ? (
        <SnapshotDrawer open={snapshotOpen} onClose={() => setSnapshotOpen(false)}
          ws={ws} rules={rules} graphId={activeGraph.id} readOnly={readOnly} run={run}
          onExportSnapshot={caseId ? (snapId) => {
            setSnapshotOpen(false);
            setExportOpen({ initialSnapshotId: snapId });
          } : undefined} />
      ) : null}
      {exportOpen && caseId && activeGraph ? (
        <ChartExportDialog
          title={`「${activeGraph.title}」`}
          snapshots={graphSnapshots.map((item) => ({
            id: item.id, label: `v${item.version_no} · ${item.stage_label}`,
          }))}
          initialSnapshotId={exportOpen.initialSnapshotId ?? ''}
          readOnly={readOnly}
          onClose={() => setExportOpen(null)}
          onFreeze={handleFreezeForExport}
          onLocate={handleLocateTarget}
          preflight={(settings, signal) => chartsApi.preflight(caseId, {
            chart_type: 'graph',
            target_id: activeGraph.id,
            profile: settings.profile,
            snapshot_id: settings.snapshotId || null,
            excluded_suspected: settings.excludedSuspected,
          }, signal)}
          download={handleDownloadTriState}
        />
      ) : null}
      {caseId && activeGraph ? (
        <GraphAuditDrawer
          open={auditOpen}
          onClose={() => setAuditOpen(false)}
          caseId={caseId}
          graphId={activeGraph.id}
          readOnly={readOnly}
          refreshToken={auditRefresh}
          onOpenSourcePreview={onOpenSourcePreview ?? (() => {})}
          onAddOrphanEntity={handleAddOrphanEntity}
          onCreateAffiliateRelation={(prefill) => {
            setAuditOpen(false);
            setRelationDialog({
              mode: 'add',
              prefill: {
                fromEntityId: prefill.fromEntityId,
                toEntityId: prefill.toEntityId,
                kind: 'affiliate',
                credibility: 'suspected',
                label: prefill.label,
                initialSources: prefill.segmentIds.map((id) => ({
                  ref: { segment_id: id },
                  proves: prefill.label,
                })),
              },
            });
          }}
        />
      ) : null}
      {caseId && activeGraph ? (
        <EntityDiscoveryDialog
          open={discoveryOpen}
          onClose={() => setDiscoveryOpen(false)}
          caseId={caseId}
          graphId={activeGraph.id}
          ws={ws}
          rules={rules}
          readOnly={readOnly}
          run={run}
          onOpenSourcePreview={onOpenSourcePreview}
        />
      ) : null}
      {caseId && activeGraph ? (
        <TableImportDialog
          open={tableImportOpen}
          onClose={() => setTableImportOpen(false)}
          caseId={caseId}
          graphId={activeGraph.id}
          rules={rules}
          readOnly={readOnly}
          onCommitted={(result) => {
            setTableImportOpen(false);
            setAuditRefresh((value) => value + 1);
            onLocalNotice?.(
              `表格已写入当前关系图：新建主体 ${result.created_entities} 个、新建关系 ${result.created_relations} 条、补挂出处 ${result.attached_sources} 条。`,
            );
            void onReload?.();
          }}
        />
      ) : null}
    </div>
  );
}
