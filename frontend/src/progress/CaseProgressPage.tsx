/**
 * P4 真实案件进度页：复用 P1 的全部视图组件（大纲/图形/编辑/复核），
 * 数据来自后端 progress 服务（progressApi），不再使用内存演示数据。
 *
 * 与原型页的差异：
 * - 快照与规则从 HTTP 读取；写命令走 progressApi，返回的服务端快照整体替换本地态。
 * - 变化序号轮询（10 秒）与回到前台检查；迟到响应丢弃。
 * - 冲突（409）保留草稿并提示刷新。
 * - 撤销栈在客户端按逆命令实现（与原型同口径），落库仍是一条新命令。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useParams } from 'react-router';
import { describeError } from '../api/client';
import { casesApi, materialsApi, progressApi } from '../api/endpoints';
import type { ProgressSnapshot as ApiSnapshot } from '../api/endpoints';
import { ChangeReviewPanel } from './ChangeReviewPanel';
import { CaseTimePanel } from './CaseTimePanel';
import { GraphView } from './GraphView';
import { mapSnapshot } from './httpExecutor';
import { NodeEditorDialog } from './NodeEditorDialog';
import { OutlineView } from './OutlineView';
import { kindLabel } from './rules';
import { activeChildCount, defaultCollapsedIds } from './tree';
import type {
  CommandResult, ProgressCommand, ProgressNode, ProgressRules, ProgressSnapshot,
} from './types';
import { createUndoStacks } from './undo';
import type { UndoZone } from './undo';
import './progress.css';

type EditorState =
  | { mode: 'add'; parentId: string | null; kind: string }
  | { mode: 'edit'; node: ProgressNode }
  | null;

export default function CaseProgressPage() {
  const { caseId = '' } = useParams();
  return <CaseProgress key={caseId} caseId={caseId} />;
}

function CaseProgress({ caseId }: { caseId: string }) {
  const [caseTitle, setCaseTitle] = useState('');
  const [rules, setRules] = useState<ProgressRules | null>(null);
  const [snapshot, setSnapshot] = useState<ProgressSnapshot | null>(null);
  const [loadError, setLoadError] = useState('');
  const [view, setView] = useState<'outline' | 'graph'>('outline');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [collapsedIds, setCollapsedIds] = useState<Set<string>>(new Set());
  const [editor, setEditor] = useState<EditorState>(null);
  const [moveNode, setMoveNode] = useState<ProgressNode | null>(null);
  // 审查：网页关系/关联/恢复入口
  const [relationFrom, setRelationFrom] = useState<ProgressNode | null>(null);
  const [linkNode, setLinkNode] = useState<ProgressNode | null>(null);
  const [materials, setMaterials] = useState<{ id: string; title: string }[]>([]);
  const [materialsTotal, setMaterialsTotal] = useState(0);
  const [materialsError, setMaterialsError] = useState('');
  const [materialsLoading, setMaterialsLoading] = useState(false);
  const [notice, setNotice] = useState('');
  const [conflict, setConflict] = useState('');
  // 结案归档：案件级操作，放在树状图之外（用户裁定）。归档后进度/工时只读。
  const [archivedAt, setArchivedAt] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [showTime, setShowTime] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [archiveError, setArchiveError] = useState('');
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [zone, setZone] = useState<UndoZone>('graph');
  const [, setUndoTick] = useState(0);
  const undoRef = useRef(createUndoStacks());
  const seqRef = useRef(-1);
  const tokenRef = useRef(0);

  const applySnapshot = useCallback((api: ApiSnapshot) => {
    const next = mapSnapshot(api);
    if (seqRef.current >= 0 && next.seq !== seqRef.current) setNotice('进度数据已更新');
    seqRef.current = next.seq;
    setSnapshot(next);
  }, []);

  const reload = useCallback(() => {
    const my = ++tokenRef.current;
    progressApi.snapshot(caseId).then((api) => {
      if (my === tokenRef.current) applySnapshot(api);
    }).catch((cause: unknown) => {
      if (my === tokenRef.current) setLoadError(describeError(cause));
    });
  }, [caseId, applySnapshot]);

  // 审查末节：关联材料分页加载（不再固定只取前 200），失败与空列表分开呈现、可重试。
  const loadMaterials = useCallback(async (offset: number, append: boolean) => {
    setMaterialsLoading(true);
    setMaterialsError('');
    try {
      const out = await materialsApi.list(caseId, { offset, limit: 200 });
      const items = out.items.map((m) => ({ id: m.material_id, title: m.display_name }));
      setMaterials((prev) => (append ? [...prev, ...items] : items));
      setMaterialsTotal(out.total);
    } catch (cause) {
      setMaterialsError(describeError(cause));
    } finally {
      setMaterialsLoading(false);
    }
  }, [caseId]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([casesApi.overview(caseId), progressApi.rules(), progressApi.snapshot(caseId)])
      .then(([overview, rulesOut, snap]) => {
        if (cancelled) return;
        setCaseTitle(overview.title);
        setArchivedAt(overview.archived_at ?? null);
        setRevision(overview.revision ?? 0);
        setRules(rulesOut.rules as unknown as ProgressRules);
        applySnapshot(snap);
        setCollapsedIds(defaultCollapsedIds(mapSnapshot(snap).nodes));
      })
      .catch((cause: unknown) => { if (!cancelled) setLoadError(describeError(cause)); });
    // 审查：关联材料对话框需要案内材料清单（分页加载）
    void loadMaterials(0, false);
    return () => { cancelled = true; };
  }, [caseId, applySnapshot, loadMaterials]);

  // 轮询变化序号 + 回到前台检查
  useEffect(() => {
    const check = () => {
      progressApi.seqs(caseId).then((out) => {
        if (out.seqs.progress !== seqRef.current) reload();
      }).catch(() => { /* 静默，下次再试 */ });
    };
    const timer = window.setInterval(check, 10000);
    const onVisible = () => { if (document.visibilityState === 'visible') check(); };
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('focus', check);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('focus', check);
    };
  }, [caseId, reload]);

  const execute = useCallback(async (command: ProgressCommand): Promise<CommandResult> => {
    // 归档后进度只读：服务端同样拒绝，这里先给出即时反馈。
    if (archivedAt) {
      return { ok: false, code: 'validation', message: '案件已归档，进度只读；如需修改请先取消归档' };
    }
    try {
      const api = await sendCommand(caseId, command);
      const next = mapSnapshot(api);
      tokenRef.current += 1;
      // 审查 09：只接受序号前进的响应，倒序送达的旧写响应不回退已显示状态。
      if (next.seq < seqRef.current) {
        return { ok: true, snapshot: next };
      }
      seqRef.current = next.seq;
      setSnapshot(next);
      setConflict('');
      setLoadError('');
      // 审查 03：撤销以服务端本次变更记录为准（revert_change 按全部影响还原），
      // 不再用前后快照猜测新建节点或只还原目标阶段。
      if (command.type === 'accept_all') {
        const shown = new Set(command.change_ids ?? []);
        for (const change of next.changes) {
          if (change.review_state === 'accepted' && (shown.size === 0 || shown.has(change.id))) {
            undoRef.current.pushReview(change.id);
          }
        }
      } else if (command.type === 'accept_change') {
        undoRef.current.pushReview(command.change_id);
      } else if (command.type !== 'revert_change') {
        // 审查 N3/R3-3：严格按服务端返回的本次变更 ID 入栈；缺失则不入栈，
        // 不退回「最后一条同型 human 变更」猜测（并发下会错认）。
        if (next.last_change_id) {
          undoRef.current.pushHuman('graph', { type: 'revert_change', change_id: next.last_change_id });
        }
      }
      setUndoTick((value) => value + 1);
      return { ok: true, snapshot: next };
    } catch (cause: unknown) {
      const { ApiError } = await import('../api/client');
      if (cause instanceof ApiError) {
        const code = cause.code === 'conflict' ? 'conflict'
          : cause.code === 'case_mismatch' ? 'case_mismatch'
          : cause.code === 'not_found' ? 'not_found' : 'validation';
        if (code === 'conflict') { setConflict(cause.message); reload(); }
        return { ok: false, code, message: cause.message };
      }
      return { ok: false, code: 'validation', message: '无法连接本地服务' };
    }
  }, [caseId, reload, archivedAt]);

  // 审查 02：视图组件直接拿到本次命令的 Promise，等待真正完成后再关闭/保留草稿。
  const asyncExecute = useCallback((command: ProgressCommand): Promise<CommandResult> => execute(command), [execute]);

  async function undoZone(target: UndoZone) {
    const stack = undoRef.current;
    if (stack.depth(target) === 0) return;
    // 审查 03：等待撤销命令真正完成，成功后才出栈；失败保留栈项并提示。
    const result = await stack.undoAsync(target, (cmd) => execute(cmd));
    if (!result.ok && result.message) setConflict(result.message);
    setUndoTick((value) => value + 1);
    return result;
  }

  // 结案归档/取消归档：案件级 CAS，成功后刷新归档态与修订号。
  async function changeArchive(next: boolean) {
    setArchiveBusy(true);
    setArchiveError('');
    try {
      const overview = next
        ? await casesApi.archive(caseId, revision)
        : await casesApi.unarchive(caseId, revision);
      setArchivedAt(overview.archived_at ?? null);
      setRevision(overview.revision);
      setArchiveOpen(false);
      setConflict('');
      reload();
    } catch (cause) {
      setArchiveError(describeError(cause));
      // 修订号过期（409）时刷新一次案件概览，用户重试即可成功。
      const { ApiError } = await import('../api/client');
      if (cause instanceof ApiError && cause.code === 'conflict') {
        casesApi.overview(caseId).then((overview) => {
          setArchivedAt(overview.archived_at ?? null);
          setRevision(overview.revision ?? 0);
        }).catch(() => { /* 保留原提示 */ });
      }
    } finally {
      setArchiveBusy(false);
    }
  }

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'z' || event.shiftKey) return;
      const tag = (event.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      event.preventDefault();
      undoZone(zone);
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  const stages = useMemo(
    () => (snapshot?.nodes ?? []).filter((n) => n.kind === 'stage' && n.removed_at === null),
    [snapshot]);
  const moveCandidates = useMemo(() => {
    if (!moveNode || !snapshot || !rules) return [];
    const forbidden = new Set<string>([moveNode.id]);
    let changed = true;
    while (changed) {
      changed = false;
      for (const node of snapshot.nodes) {
        if (node.parent_id && forbidden.has(node.parent_id) && !forbidden.has(node.id)) {
          forbidden.add(node.id); changed = true;
        }
      }
    }
    return snapshot.nodes.filter((n) => n.removed_at === null && !forbidden.has(n.id)
      && (rules.containment[n.kind] ?? []).includes(moveNode.kind));
  }, [moveNode, snapshot, rules]);

  if (loadError) return <p className="progress-error" role="alert" style={{ margin: 24 }}>错误：{loadError}</p>;
  if (!snapshot || !rules) return <p className="progress-loading">加载中…</p>;

  function toggleCollapse(nodeId: string) {
    setCollapsedIds((current) => {
      const next = new Set(current);
      if (next.has(nodeId)) next.delete(nodeId); else next.add(nodeId);
      return next;
    });
  }
  function collapseAll() {
    setCollapsedIds(new Set(snapshot!.nodes
      .filter((n) => n.removed_at === null && activeChildCount(snapshot!.nodes, n.id) > 0)
      .map((n) => n.id)));
  }

  return (
    <div className="progress-prototype">
      <header className="app-bar"><div className="app-bar-inner">
        <span className="app-brand">法律工作台</span>
        <span className="app-section">办案进度 · {caseTitle}</span>
      </div></header>
      <main className="progress-main">
        <div className="page-heading">
          <h1>{caseTitle}</h1>
          <div className="dashboard-actions">
            <Link to={`/cases/${caseId}?view=overview`} className="time-open-btn">返回案件</Link>
            <button type="button" className="time-open-btn" onClick={() => setShowTime(true)}>工时</button>
            {archivedAt ? null : (
              <button type="button" onClick={() => { setArchiveError(''); setArchiveOpen(true); }}>
                结案归档
              </button>
            )}
            <div className="progress-view-switch" role="group" aria-label="视图切换">
              <button type="button" aria-pressed={view === 'outline'} onClick={() => setView('outline')}>大纲视图</button>
              <button type="button" aria-pressed={view === 'graph'} onClick={() => setView('graph')}>图形视图</button>
            </div>
          </div>
        </div>
        {archivedAt ? (
          <p role="status" className="progress-notice">
            案件已归档（{archivedAt.slice(0, 10)}），进度与工时只读。
            <button type="button" disabled={archiveBusy} onClick={() => void changeArchive(false)}>取消归档</button>
          </p>
        ) : null}
        {notice ? <p role="status" className="progress-notice">{notice}
          <button type="button" onClick={() => setNotice('')}>知道了</button></p> : null}
        {conflict ? <p role="alert" className="progress-error">错误：{conflict}
          <button type="button" onClick={() => setConflict('')}>关闭</button></p> : null}
        <div className="progress-layout">
          <section className={`progress-view${zone === 'graph' ? ' zone-focused' : ''}`} aria-label="进度视图"
            onMouseDown={() => setZone('graph')} onFocus={() => setZone('graph')}>
            <div className="progress-collapse-tools">
              <button type="button" onClick={() => setCollapsedIds(new Set())}>展开全部</button>
              <button type="button" onClick={collapseAll}>收起全部</button>
              {undoRef.current.depth('graph') > 0 && archivedAt === null ? (
                <button type="button" onClick={() => undoZone('graph')} title="撤销本区域最近一步（⌘Z）">
                  撤销（{undoRef.current.depth('graph')}）
                </button>
              ) : null}
              <span className="muted">序号 seq={snapshot.seq} · ⌘Z 撤销高亮区域</span>
            </div>
            {view === 'outline' ? (
              <OutlineView snapshot={snapshot} rules={rules} selectedId={selectedId}
                collapsedIds={collapsedIds} readOnly={archivedAt !== null} onToggleCollapse={toggleCollapse}
                onSelect={setSelectedId} onCommand={asyncExecute}
                onAddRequest={(parentId, kind) => setEditor({ mode: 'add', parentId, kind })}
                onEditRequest={(node) => setEditor({ mode: 'edit', node })}
                onMoveRequest={(node) => setMoveNode(node)}
                onRelationRequest={(node) => setRelationFrom(node)}
                onLinkRequest={(node) => setLinkNode(node)}
                onRestoreRequest={(node) => void asyncExecute({ type: 'restore_node', node_id: node.id, expected_revision: node.revision })} />
            ) : (
              <GraphView snapshot={snapshot} rules={rules} selectedId={selectedId} readOnly={archivedAt !== null} onSelect={setSelectedId}
                collapsedIds={collapsedIds} onToggleCollapse={toggleCollapse}
                onCommand={asyncExecute}
                onEditRequest={(node) => setEditor({ mode: 'edit', node })}
                onMoveRequest={(node) => setMoveNode(node)}
                onAddRequest={(parentId, kind) => setEditor({ mode: 'add', parentId, kind })} />
            )}
          </section>
          <aside className="progress-aside">
            <ChangeReviewPanel snapshot={snapshot} rules={rules} readOnly={archivedAt !== null} onCommand={asyncExecute}
              focused={zone === 'review'} onFocusZone={() => setZone('review')}
              undoDepth={undoRef.current.depth('review')} onUndo={() => undoZone('review')} />
          </aside>
        </div>
      </main>
      {editor ? (
        <NodeEditorDialog mode={editor.mode}
          node={editor.mode === 'edit' ? editor.node : undefined}
          kind={editor.mode === 'add' ? editor.kind : undefined}
          parentId={editor.mode === 'add' ? editor.parentId : undefined}
          rules={rules} onClose={() => setEditor(null)} onSubmit={asyncExecute} />
      ) : null}
      {moveNode ? (
        <dialog className="work-dialog" aria-labelledby="move-heading" ref={(el) => el?.showModal()}>
          <form onSubmit={async (event) => {
            event.preventDefault();
            const data = new FormData(event.currentTarget);
            const parentId = String(data.get('parent') ?? '');
            // 审查 02：等待本次移动完成，冲突时保留对话框与提示。
            const result = await execute({ type: 'move_node', node_id: moveNode.id, expected_revision: moveNode.revision,
              new_parent_id: parentId || null });
            if (result.ok) setMoveNode(null);
            else setConflict(result.message);
          }}>
            <h2 id="move-heading">移动「{moveNode.title}」</h2>
            <label htmlFor="move-parent">目标位置
              <select id="move-parent" name="parent" defaultValue="">
                <option value="">案件根级</option>
                {moveCandidates.map((node) => (
                  <option key={node.id} value={node.id}>{node.title}（{kindLabel(rules, node.kind)}）</option>
                ))}
              </select>
            </label>
            <div className="work-actions">
              <button type="button" onClick={() => setMoveNode(null)}>取消</button>
              <button type="submit">移动</button>
            </div>
          </form>
        </dialog>
      ) : null}
      {relationFrom ? (
        <dialog className="work-dialog" aria-labelledby="relation-heading" ref={(el) => el?.showModal()}>
          <form onSubmit={async (event) => {
            event.preventDefault();
            const data = new FormData(event.currentTarget);
            const toId = String(data.get('to') ?? '');
            const relationType = String(data.get('type') ?? 'relates');
            if (!toId) return;
            const result = await asyncExecute({ type: 'add_relation', from_node_id: relationFrom.id,
              to_node_id: toId, relation_type: relationType });
            if (result.ok) setRelationFrom(null);
            else setConflict(result.message);
          }}>
            <h2 id="relation-heading">从「{relationFrom.title}」建立关系</h2>
            <label htmlFor="relation-to">指向节点
              <select id="relation-to" name="to" required defaultValue="">
                <option value="" disabled>选择节点…</option>
                {snapshot.nodes.filter((n) => n.removed_at === null && n.id !== relationFrom.id).map((node) => (
                  <option key={node.id} value={node.id}>{node.title}</option>
                ))}
              </select>
            </label>
            <label htmlFor="relation-type">关系类型
              <select id="relation-type" name="type" defaultValue="relates">
                {rules.relation_types.map((entry) => (
                  <option key={entry.value} value={entry.value}>{entry.label}</option>
                ))}
              </select>
            </label>
            <div className="work-actions">
              <button type="button" onClick={() => setRelationFrom(null)}>取消</button>
              <button type="submit">建立关系</button>
            </div>
          </form>
        </dialog>
      ) : null}
      {linkNode ? (
        <dialog className="work-dialog" aria-labelledby="link-heading" ref={(el) => el?.showModal()}>
          <form onSubmit={async (event) => {
            event.preventDefault();
            const data = new FormData(event.currentTarget);
            const materialId = String(data.get('material') ?? '');
            if (!materialId) return;
            const result = await asyncExecute({ type: 'link', node_id: linkNode.id,
              target: { material_id: materialId } });
            if (result.ok) setLinkNode(null);
            else setConflict(result.message);
          }}>
            <h2 id="link-heading">为「{linkNode.title}」关联材料</h2>
            <label htmlFor="link-material">案内材料
              <select id="link-material" name="material" required defaultValue="">
                <option value="" disabled>选择材料…</option>
                {materials.map((material) => (
                  <option key={material.id} value={material.id}>{material.title}</option>
                ))}
              </select>
            </label>
            {materials.length === 0 && !materialsError ? <p className="muted">本案暂无已登记材料。</p> : null}
            {materialsError ? (
              <p className="muted" role="alert">材料清单读取失败：{materialsError}
                <button type="button" disabled={materialsLoading}
                  onClick={() => void loadMaterials(materials.length, materials.length > 0)}>重试</button>
              </p>
            ) : materials.length < materialsTotal ? (
              <p className="muted">已显示 {materials.length} / {materialsTotal} 项。
                <button type="button" disabled={materialsLoading}
                  onClick={() => void loadMaterials(materials.length, true)}>加载更多</button>
              </p>
            ) : null}
            <div className="work-actions">
              <button type="button" onClick={() => setLinkNode(null)}>取消</button>
              <button type="submit" disabled={materials.length === 0}>关联</button>
            </div>
          </form>
        </dialog>
      ) : null}
      {archiveOpen ? (
        <dialog className="work-dialog" aria-label="结案归档"
          ref={(el) => { if (el && !el.open) el.showModal(); }}>
          <form onSubmit={(event) => { event.preventDefault(); void changeArchive(true); }}>
            <h2>结案归档</h2>
            <p>归档后本案的进度、材料与工时变为只读，并从办理中列表隐藏；可随时取消归档。</p>
            {archiveError ? <p role="alert" className="progress-error">错误：{archiveError}</p> : null}
            <div className="work-actions">
              <button type="button" disabled={archiveBusy} onClick={() => setArchiveOpen(false)}>取消</button>
              <button type="submit" disabled={archiveBusy}>确认归档</button>
            </div>
          </form>
        </dialog>
      ) : null}
      {showTime ? (
        <dialog className="work-dialog" aria-label="工时"
          ref={(el) => { if (el && !el.open) el.showModal(); }}>
          <CaseTimePanel caseId={caseId} nodes={snapshot.nodes} readOnly={archivedAt !== null} />
          <div className="work-actions">
            <button type="button" onClick={() => setShowTime(false)}>关闭</button>
          </div>
        </dialog>
      ) : null}
    </div>
  );
}

async function sendCommand(caseId: string, command: ProgressCommand): Promise<ApiSnapshot> {
  switch (command.type) {
    case 'add_node':
      return progressApi.addNode(caseId, {
        parent_id: command.parent_id, kind: command.kind, title: command.title,
        position: command.position ?? null, state: command.state ?? null,
        note: command.note ?? null, date_start: command.date_start ?? null,
        date_end: command.date_end ?? null, request_key: command.request_key ?? null });
    case 'update_node': {
      // 审查 11：只发送实际提供的字段；显式 null 清空，未提供＝不动。
      const changes: Record<string, unknown> = {};
      for (const key of ['title', 'state', 'note', 'date_start', 'date_end'] as const) {
        if (key in command.changes) changes[key] = command.changes[key] ?? null;
      }
      return progressApi.updateNode(caseId, command.node_id, command.expected_revision,
        changes as Parameters<typeof progressApi.updateNode>[3]);
    }
    case 'move_node':
      return progressApi.moveNode(caseId, command.node_id, command.expected_revision,
        command.new_parent_id, command.before_node_id ?? null);
    case 'set_active_stage':
      return progressApi.setActiveStage(caseId, command.stage_id, command.expected_revision);
    case 'insert_fragment':
      return progressApi.insertFragment(caseId, command.fragment_key, command.parent_id,
        command.before_node_id ?? null);
    case 'remove_node':
      return progressApi.removeNode(caseId, command.node_id, command.expected_revision);
    case 'restore_node':
      return progressApi.restoreNode(caseId, command.node_id, command.expected_revision);
    case 'add_relation':
      return progressApi.addRelation(caseId, command.from_node_id, command.to_node_id,
        command.relation_type, command.note ?? null);
    case 'remove_relation':
      return progressApi.removeRelation(caseId, command.relation_id);
    case 'link': {
      const target = command.target;
      return progressApi.link(caseId, command.node_id, {
        material_id: 'material_id' in target ? target.material_id : null,
        analysis_id: 'analysis_id' in target ? target.analysis_id : null,
        annotation_id: 'annotation_id' in target ? target.annotation_id : null });
    }
    case 'unlink':
      return progressApi.unlink(caseId, command.link_id);
    case 'accept_change':
      return progressApi.acceptChange(caseId, command.change_id);
    case 'accept_all':
      return progressApi.acceptAll(caseId, command.change_ids, command.expected_seq ?? null);
    case 'revert_change':
      return progressApi.revertChange(caseId, command.change_id);
  }
}