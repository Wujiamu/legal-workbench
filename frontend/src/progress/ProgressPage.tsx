/**
 * P1 交互原型页：两种视图 + 内存执行器 + 合成案卷，演示总体计划第八节场景。
 * 结案归档是案件级操作（树外按钮），不是进度节点；归档后进度只读（演示口径）。
 * P4 接真实接口时，本页换成案件工作区的进度页签。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { AppBrand, ThemeSwitcher } from '../components/BrandMark';
import { ChangeReviewPanel } from './ChangeReviewPanel';
import { buildLitigationFixture, FIXTURE_CASE_TITLE } from './fixtures';
import { GraphView } from './GraphView';
import { NodeEditorDialog } from './NodeEditorDialog';
import { OutlineView } from './OutlineView';
import { kindLabel } from './rules';
import { TimePanel } from './TimePanel';
import { createTimeLog } from './timeLog';
import { activeChildCount, defaultCollapsedIds } from './tree';
import type { CommandResult, ProgressCommand, ProgressExecutor, ProgressNode } from './types';
import { createUndoStacks, inverseOf } from './undo';
import type { UndoZone } from './undo';
import { useCaseProgress } from './useCaseProgress';
import { undoShortcutLabel } from '../ui/shortcuts';
import './progress.css';

type EditorState =
  | { mode: 'add'; parentId: string | null; kind: string }
  | { mode: 'edit'; node: ProgressNode }
  | null;

export default function ProgressPrototypePage() {
  const [executor, setExecutor] = useState<ProgressExecutor>(() => buildLitigationFixture());
  const [view, setView] = useState<'outline' | 'graph'>('outline');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [collapsedIds, setCollapsedIds] = useState<Set<string>>(
    () => defaultCollapsedIds(executor.getSnapshot().nodes));
  const [editor, setEditor] = useState<EditorState>(null);
  const [moveNode, setMoveNode] = useState<ProgressNode | null>(null);
  const [agentStageId, setAgentStageId] = useState('');
  const [archived, setArchived] = useState(false);
  const [archiveDialog, setArchiveDialog] = useState<'archive' | 'unarchive' | null>(null);
  // 撤销：按区域分栈 + 焦点高亮（用户反馈第七轮）
  const undoRef = useRef(createUndoStacks());
  const timeLogRef = useRef(createTimeLog({ caseId: 'case-demo' }));
  const [zone, setZone] = useState<UndoZone>('graph');
  const [undoTick, setUndoTick] = useState(0);
  const [timeTick, setTimeTick] = useState(0);
  const [timeOpen, setTimeOpen] = useState(false);
  const { snapshot, rules, notice, conflict, execute, refresh, dismissNotice, dismissConflict }
    = useCaseProgress(executor);

  // 归档后进度只读：命令在入口层被拒，各视图自行显示错误（演示口径）。
  // 人工命令成功即构造逆命令入 graph 栈；accept_all 把每条确认入 review 栈。
  function guardedExecute(command: ProgressCommand): CommandResult {
    if (archived) {
      return { ok: false, code: 'validation', message: '案件已归档，进度为只读（演示口径）' };
    }
    const before = snapshot;
    const result = execute(command);
    if (result.ok && before) {
      if (command.type === 'accept_all') {
        for (const change of before.pending_changes) undoRef.current.pushReview(change.id);
      } else if (command.type === 'accept_change') {
        undoRef.current.pushReview(command.change_id);
      } else if (command.type !== 'revert_change') {
        const targetId = 'node_id' in command ? command.node_id
          : command.type === 'set_active_stage' ? command.stage_id : null;
        const oldNode = targetId ? before.nodes.find((node) => node.id === targetId) ?? null : null;
        const createdId = command.type === 'add_node'
          ? result.snapshot.nodes.find((node) => !before.nodes.some((old) => old.id === node.id))?.id
          : undefined;
        const inverse = inverseOf(command, oldNode, createdId);
        if (inverse) {
          // 逆命令的修订号用执行后的最新值（新建节点取新节点自身的修订号）
          const inverseTarget = 'node_id' in inverse ? inverse.node_id : null;
          if ('expected_revision' in inverse && inverseTarget) {
            const fresh = result.snapshot.nodes.find((node) => node.id === inverseTarget);
            if (fresh) (inverse as { expected_revision: number }).expected_revision = fresh.revision;
          }
          undoRef.current.pushHuman('graph', inverse);
        }
      }
      setUndoTick((value) => value + 1);
    }
    return result;
  }

  function undoZone(target: UndoZone) {
    const result = undoRef.current.undo(target, executor);
    if (result.ok) refresh();
    setUndoTick((value) => value + 1);
    return result;
  }

  // ⌘Z / Ctrl+Z 只撤销当前焦点区域；输入框内不拦截（保留原生文本撤销）。
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
    () => (snapshot?.nodes ?? []).filter((node) => node.kind === 'stage' && node.removed_at === null),
    [snapshot],
  );
  const moveCandidates = useMemo(() => {
    if (!moveNode || !snapshot) return [];
    const forbidden = new Set<string>([moveNode.id]);
    // 排除自身与子孙，避免成环候选
    let changed = true;
    while (changed) {
      changed = false;
      for (const node of snapshot.nodes) {
        if (node.parent_id && forbidden.has(node.parent_id) && !forbidden.has(node.id)) {
          forbidden.add(node.id);
          changed = true;
        }
      }
    }
    return snapshot.nodes.filter((node) => node.removed_at === null && !forbidden.has(node.id)
      && (rules.containment[node.kind] ?? []).includes(moveNode.kind));
  }, [moveNode, snapshot, rules]);

  if (!snapshot) return <p className="progress-loading">加载中…</p>;

  const undoKey = undoShortcutLabel();
  const selected = snapshot.nodes.find((node) => node.id === selectedId) ?? null;

  function toggleCollapse(nodeId: string) {
    setCollapsedIds((current) => {
      const next = new Set(current);
      if (next.has(nodeId)) next.delete(nodeId);
      else next.add(nodeId);
      return next;
    });
  }

  function collapseAll() {
    setCollapsedIds(new Set(snapshot!.nodes
      .filter((node) => node.removed_at === null && activeChildCount(snapshot!.nodes, node.id) > 0)
      .map((node) => node.id)));
  }

  function simulateAgentStage() {
    const stage = stages.find((node) => node.id === agentStageId) ?? stages[0];
    if (!stage) return;
    const result = executor.agent({
      type: 'set_active_stage', stage_id: stage.id, expected_revision: stage.revision,
    });
    if (result.ok) refresh();
  }

  function simulateAgentWork() {
    const parent = selected ?? stages.find((node) => node.title === '庭前程序') ?? stages[0];
    if (!parent) return;
    const result = executor.agent({
      type: 'add_node', parent_id: parent.id, kind: 'work',
      title: `Agent 建议工作 ${new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`,
      request_key: `agent-work-${Date.now()}`,
    });
    if (result.ok) refresh();
  }

  return (
    <div className="progress-prototype">
      <header className="app-bar"><div className="app-bar-inner">
        <AppBrand />
        <span className="app-section">办案进度原型 · TEST-ONLY 合成案卷</span>
        <ThemeSwitcher />
      </div></header>
      <main className="progress-main">
        <div className="page-heading">
          <h1>{FIXTURE_CASE_TITLE}</h1>
          <div className="dashboard-actions">
            <button type="button" className="archive-btn"
              onClick={() => setArchiveDialog(archived ? 'unarchive' : 'archive')}>
              {archived ? '取消归档' : '结案归档'}
            </button>
            <div className="progress-view-switch" role="group" aria-label="视图切换">
              <button type="button" aria-pressed={view === 'outline'} onClick={() => setView('outline')}>大纲视图</button>
              <button type="button" aria-pressed={view === 'graph'} onClick={() => setView('graph')}>图形视图</button>
            </div>
            <button type="button" className="time-open-btn" onClick={() => setTimeOpen(true)}>
              工时{timeLogRef.current.running().length > 0 ? ` · 计时中 ${timeLogRef.current.running().length}` : ''}
            </button>
          </div>
        </div>
        {archived ? (
          <p role="status" className="progress-archived">
            本案已归档（演示状态，未写入数据库）：进度只读。正式归档语义（案件状态、列表过滤、写保护）在 P2 由案件域实现。
          </p>
        ) : null}
        <section className="progress-demo" aria-label="原型演示控制">
          <strong>演示控制</strong>
          <label htmlFor="agent-stage">Agent 设置当前阶段
            <select id="agent-stage" value={agentStageId} onChange={(event) => setAgentStageId(event.target.value)}>
              <option value="">选择阶段…</option>
              {stages.map((stage) => <option key={stage.id} value={stage.id}>{stage.title}</option>)}
            </select>
          </label>
          <button type="button" disabled={archived} onClick={simulateAgentStage}>模拟 Agent 改阶段</button>
          <button type="button" disabled={archived} onClick={simulateAgentWork}>模拟 Agent 加工作</button>
          <button type="button" onClick={() => {
            const fresh = buildLitigationFixture();
            setExecutor(fresh);
            setCollapsedIds(defaultCollapsedIds(fresh.getSnapshot().nodes));
            setSelectedId(null); setEditor(null); setMoveNode(null); setArchived(false);
            undoRef.current.clear();
            timeLogRef.current = createTimeLog({ caseId: 'case-demo' });
            setUndoTick((value) => value + 1); setTimeTick((value) => value + 1);
          }}>重置演示</button>
          <span className="muted">序号 seq={snapshot.seq} · 待确认 {snapshot.pending_changes.length}</span>
        </section>
        {notice ? <p role="status" className="progress-notice">{notice}
          <button type="button" onClick={dismissNotice}>知道了</button></p> : null}
        {conflict ? <p role="alert" className="progress-error">错误：{conflict}
          <button type="button" onClick={dismissConflict}>关闭</button></p> : null}
        <div className="progress-layout">
          <section className={`progress-view${zone === 'graph' ? ' zone-focused' : ''}`} aria-label="进度视图"
            onMouseDown={() => setZone('graph')} onFocus={() => setZone('graph')}>
            <div className="progress-collapse-tools">
              <button type="button" onClick={() => setCollapsedIds(new Set())}>展开全部</button>
              <button type="button" onClick={collapseAll}>收起全部</button>
              {undoRef.current.depth('graph') > 0 ? (
                <button type="button" onClick={() => undoZone('graph')} title={`撤销本区域最近一步（${undoKey}）`}>
                  撤销（{undoRef.current.depth('graph')}）
                </button>
              ) : null}
              <span className="muted">收起状态两种视图共享，不写入数据 · {undoKey} 撤销高亮区域</span>
            </div>
            {view === 'outline' ? (
              <OutlineView snapshot={snapshot} rules={rules} selectedId={selectedId}
                collapsedIds={collapsedIds} onToggleCollapse={toggleCollapse}
                onSelect={setSelectedId} onCommand={guardedExecute}
                onAddRequest={(parentId, kind) => setEditor({ mode: 'add', parentId, kind })}
                onEditRequest={(node) => setEditor({ mode: 'edit', node })}
                onMoveRequest={(node) => setMoveNode(node)} />
            ) : (
              <GraphView snapshot={snapshot} rules={rules} selectedId={selectedId} onSelect={setSelectedId}
                collapsedIds={collapsedIds} onToggleCollapse={toggleCollapse}
                onCommand={guardedExecute}
                onEditRequest={(node) => setEditor({ mode: 'edit', node })}
                onMoveRequest={(node) => setMoveNode(node)}
                onAddRequest={(parentId, kind) => setEditor({ mode: 'add', parentId, kind })} />
            )}
          </section>
          <aside className="progress-aside">
            <ChangeReviewPanel snapshot={snapshot} rules={rules} onCommand={guardedExecute}
              focused={zone === 'review'} onFocusZone={() => setZone('review')}
              undoDepth={undoRef.current.depth('review')} onUndo={() => undoZone('review')} />
          </aside>
        </div>
      </main>
      {timeOpen ? (
        <dialog className="work-dialog time-dialog" aria-labelledby="time-heading"
          ref={(element) => { if (element && !element.open) element.showModal(); }}>
          <div className="time-dialog-head">
            <h2 id="time-heading">工时记录</h2>
            <button type="button" onClick={() => setTimeOpen(false)}>关闭</button>
          </div>
          <TimePanel log={timeLogRef.current} snapshot={snapshot} readOnly={archived}
            onChanged={() => setTimeTick((value) => value + 1)} />
        </dialog>
      ) : null}
      {editor ? (
        <NodeEditorDialog mode={editor.mode}
          node={editor.mode === 'edit' ? editor.node : undefined}
          kind={editor.mode === 'add' ? editor.kind : undefined}
          parentId={editor.mode === 'add' ? editor.parentId : undefined}
          rules={rules} onClose={() => setEditor(null)} onSubmit={guardedExecute} />
      ) : null}
      {moveNode ? (
        <dialog className="work-dialog" aria-labelledby="move-heading" ref={(element) => element?.showModal()}>
          <form onSubmit={(event) => {
            event.preventDefault();
            const data = new FormData(event.currentTarget);
            const parentId = String(data.get('parent') ?? '');
            const result = guardedExecute({
              type: 'move_node', node_id: moveNode.id, expected_revision: moveNode.revision,
              new_parent_id: parentId || null,
            });
            if (result.ok) setMoveNode(null);
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
      {archiveDialog ? (
        <dialog className="work-dialog" aria-labelledby="archive-heading" ref={(element) => element?.showModal()}>
          <form onSubmit={(event) => {
            event.preventDefault();
            setArchived(archiveDialog === 'archive');
            setArchiveDialog(null);
          }}>
            <h2 id="archive-heading">{archiveDialog === 'archive' ? '结案归档' : '取消归档'}</h2>
            <p className="muted">
              {archiveDialog === 'archive'
                ? '归档后本案进度转为只读，案件工作表不再列为在办。原型仅演示该状态，不写入数据库。'
                : '取消归档后本案恢复可编辑，重新列为在办。原型仅演示该状态，不写入数据库。'}
            </p>
            <div className="work-actions">
              <button type="button" onClick={() => setArchiveDialog(null)}>取消</button>
              <button type="submit">{archiveDialog === 'archive' ? '确认归档' : '确认取消归档'}</button>
            </div>
          </form>
        </dialog>
      ) : null}
    </div>
  );
}