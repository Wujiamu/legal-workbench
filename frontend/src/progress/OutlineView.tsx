/**
 * 大纲视图（横向树，用户反馈第三轮）：节点即按钮。
 * 单击选中、双击编辑、右键菜单（设为进行中/编辑/移至/收起/删除）；
 * 悬浮加号附着在鼠标靠近的边上，点击按规则列出子节点种类。
 * 菜单项完全由规则配置驱动；视图本身不含业务规则。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { ContextMenu, kindMenuItems, nearestEdge, nodeMenuItems } from './interactions';
import type { Edge } from './interactions';
import { kindLabel, relationLabel, stateLabel } from './rules';
import { agentBadgeState, buildTree, flattenTree, linkKindLabel, pendingNodeIds } from './tree';
import type { TreeNode } from './tree';
import type { CommandResult, ProgressCommand, ProgressNode, ProgressRules, ProgressSnapshot } from './types';

export function OutlineView({ snapshot, rules, selectedId, collapsedIds, readOnly = false, onToggleCollapse, onSelect, onCommand, onAddRequest, onEditRequest, onMoveRequest, onRelationRequest, onLinkRequest, onRestoreRequest }: {
  snapshot: ProgressSnapshot;
  rules: ProgressRules;
  selectedId: string | null;
  collapsedIds: Set<string>;
  readOnly?: boolean;
  onToggleCollapse(nodeId: string): void;
  onSelect(nodeId: string): void;
  onCommand(command: ProgressCommand): CommandResult | Promise<CommandResult>;
  onAddRequest(parentId: string | null, kind: string): void;
  onEditRequest(node: ProgressNode): void;
  onMoveRequest(node: ProgressNode): void;
  /** 审查：网页关系/关联/恢复入口（正式页提供，原型可不传）。 */
  onRelationRequest?(node: ProgressNode): void;
  onLinkRequest?(node: ProgressNode): void;
  onRestoreRequest?(node: ProgressNode): void;
}) {
  const tree = useMemo(() => buildTree(snapshot.nodes), [snapshot.nodes]);
  const flat = useMemo(() => flattenTree(tree), [tree]);
  const pending = useMemo(() => pendingNodeIds(snapshot), [snapshot]);
  const linksByNode = useMemo(() => {
    const map = new Map<string, ProgressSnapshot['links']>();
    for (const link of snapshot.links) {
      map.set(link.node_id, [...(map.get(link.node_id) ?? []), link]);
    }
    return map;
  }, [snapshot.links]);
  const [focusId, setFocusId] = useState<string | null>(flat[0]?.node.id ?? null);
  const [error, setError] = useState('');
  const [menu, setMenu] = useState<{ x: number; y: number; node: ProgressNode } | null>(null);
  const [kindMenu, setKindMenu] = useState<{ x: number; y: number; parentId: string } | null>(null);
  const [rootMenu, setRootMenu] = useState<{ x: number; y: number } | null>(null);
  const [hoverEdge, setHoverEdge] = useState<{ nodeId: string; edge: Edge } | null>(null);
  // 审查：已删除节点回收站（buildTree 过滤 removed_at，需单独入口才能恢复）。
  const [trashOpen, setTrashOpen] = useState(false);
  const trashDialog = useRef<HTMLDialogElement>(null);
  const removedNodes = snapshot.nodes.filter((node) => node.removed_at !== null);
  useEffect(() => {
    if (readOnly) {
      setMenu(null);
      setKindMenu(null);
      setRootMenu(null);
      setHoverEdge(null);
    }
  }, [readOnly]);
  useEffect(() => {
    const el = trashDialog.current;
    if (!el) return;
    if (trashOpen && !el.open) el.showModal();
    if (!trashOpen && el.open) el.close();
  }, [trashOpen]);

  async function run(command: ProgressCommand) {
    const result = await onCommand(command);
    setError(result.ok ? '' : result.message);
  }

  function currentFocusId(): string | null {
    // 以实际焦点为准：jsdom 与真实浏览器的 onFocus 时序不一致。
    const active = document.activeElement as HTMLElement | null;
    if (active?.id.startsWith('progress-node-')) return active.id.slice('progress-node-'.length);
    return focusId;
  }

  function focusNode(nodeId: string) {
    setFocusId(nodeId);
    document.getElementById(`progress-node-${nodeId}`)?.focus();
  }

  function siblingsOf(node: ProgressNode): ProgressNode[] {
    return snapshot.nodes
      .filter((item) => item.removed_at === null && item.parent_id === node.parent_id)
      .sort((a, b) => a.position - b.position);
  }

  function menuItemsFor(node: ProgressNode) {
    const hasChildren = snapshot.nodes.some((item) => item.parent_id === node.id && item.removed_at === null);
    return nodeMenuItems(node, {
      hasChildren,
      collapsed: collapsedIds.has(node.id),
      removed: node.removed_at !== null,
      readOnly,
      onSetActive: () => run({ type: 'set_active_stage', stage_id: node.id, expected_revision: node.revision }),
      onEdit: () => onEditRequest(node),
      onMove: () => onMoveRequest(node),
      onToggle: () => onToggleCollapse(node.id),
      onRemove: () => run({ type: 'remove_node', node_id: node.id, expected_revision: node.revision }),
      onAddRelation: onRelationRequest ? () => onRelationRequest(node) : undefined,
      onLinkMaterial: onLinkRequest ? () => onLinkRequest(node) : undefined,
      onRestore: onRestoreRequest ? () => onRestoreRequest(node) : undefined,
    });
  }

  function renderNode(item: TreeNode) {
    const { node } = item;
    const allowed = rules.containment[node.kind] ?? [];
    const isPending = pending.has(node.id);
    const links = linksByNode.get(node.id) ?? [];
    const hasChildren = item.children.length > 0;
    const isCollapsed = collapsedIds.has(node.id);
    const plusHere = !readOnly && hoverEdge?.nodeId === node.id && allowed.length > 0;
    return (
      <div key={node.id} id={`progress-node-${node.id}`} role="treeitem"
        aria-level={item.level} aria-selected={selectedId === node.id}
        aria-expanded={hasChildren ? !isCollapsed : undefined}
        aria-label={node.title}
        tabIndex={focusId === node.id ? 0 : -1}
        className="h-node"
        onFocus={() => setFocusId(node.id)}>
        <div className="h-node-main">
          <button type="button"
            className={selectedId === node.id ? 'node-btn selected' : 'node-btn'}
            aria-label={node.title}
            onClick={() => { setFocusId(node.id); onSelect(node.id); }}
            onDoubleClick={() => { if (!readOnly) onEditRequest(node); }}
            onContextMenu={(event) => {
              event.preventDefault();
              setMenu({ x: event.clientX, y: event.clientY, node });
            }}
            onMouseMove={(event) => {
              if (readOnly || allowed.length === 0) return;
              setHoverEdge({
                nodeId: node.id,
                edge: nearestEdge(event.clientX, event.clientY, event.currentTarget.getBoundingClientRect()),
              });
            }}
            onMouseLeave={() => setHoverEdge((current) => current?.nodeId === node.id ? null : current)}>
            {hasChildren ? (
              <span className="node-collapse" role="button" tabIndex={-1}
                aria-label={isCollapsed ? `展开 ${node.title}` : `收起 ${node.title}`}
                aria-expanded={!isCollapsed}
                onClick={(event) => { event.stopPropagation(); onToggleCollapse(node.id); }}>
                {isCollapsed ? '▸' : '▾'}
              </span>
            ) : null}
            <span className="progress-kind">{kindLabel(rules, node.kind)}</span>
            <span className="progress-title">{node.title}</span>
            <span className={`progress-state state-${node.state}`}>{stateLabel(rules, node.kind, node.state)}</span>
            {isCollapsed ? <span className="progress-collapsed-count">{item.children.length} 项</span> : null}
            {node.date_start || node.date_end ? (
              <span className="progress-dates">{node.date_start ?? '…'} → {node.date_end ?? '…'}</span>
            ) : null}
            {(() => {
              const badge = agentBadgeState(snapshot, node.id, snapshot.changes);
              return badge ? (
                <span className={`agent-badge agent-${badge}`}
                  title={badge === 'pending' ? 'Agent 修改待确认（黄）' : 'Agent 修改已确认（绿）'}>Agent</span>
              ) : null;
            })()}
            {links.map((link) => (
              <span key={link.id} className="progress-link">{linkKindLabel(link)}：{link.display_name}</span>
            ))}
            {plusHere ? (
              <span className={`edge-plus edge-${hoverEdge!.edge}`} role="button" tabIndex={-1}
                aria-label={`为 ${node.title} 添加子节点`}
                onMouseDown={(event) => event.stopPropagation()}
                onClick={(event) => {
                  event.stopPropagation();
                  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
                  setKindMenu({ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, parentId: node.id });
                }}>＋</span>
            ) : null}
          </button>
          {/* 审查 R3-2：解除关联按钮移出节点 <button>，避免 button 嵌套 button（HTML 规范/可访问性）。 */}
          {links.length > 0 ? (
            <span className="h-node-links">
              {links.map((link) => (
                <button key={link.id} type="button" className="progress-link-remove"
                  disabled={readOnly}
                  aria-label={`解除关联 ${link.display_name}`}
                  onClick={(event) => { event.stopPropagation(); void run({ type: 'unlink', link_id: link.id }); }}>
                  × {link.display_name}
                </button>
              ))}
            </span>
          ) : null}
        </div>
        {hasChildren && !isCollapsed ? (
          <div role="group" className="h-children">{item.children.map(renderNode)}</div>
        ) : null}
      </div>
    );
  }

  const fragments = Object.entries(rules.fragments).filter(([key]) => key !== rules.default_fragment);
  const rootItems = [
    ...kindMenuItems(rules, 'case', (kind) => onAddRequest(null, kind)),
    { label: '', onSelect: () => {}, separator: true },
    ...fragments.map(([key, fragment]) => ({
      label: `插入片段：${fragment.label}`,
      onSelect: () => run({ type: 'insert_fragment', parent_id: null, fragment_key: key }),
    })),
  ];

  return (
    <div className="progress-outline">
      {error ? <p role="alert" className="progress-error">错误：{error}</p> : null}
      <div className="progress-outline-toolbar">
        <button type="button" disabled={readOnly} onClick={(event) => {
          if (readOnly) return;
          setRootMenu({ x: event.clientX, y: event.clientY });
        }}>＋ 添加</button>
        {onRestoreRequest && removedNodes.length > 0 ? (
          <button type="button" onClick={() => setTrashOpen(true)}>已删除节点（{removedNodes.length}）</button>
        ) : null}
        <span className="muted">单击选中 · 双击编辑 · 右键菜单 · 悬浮节点边缘出现加号</span>
      </div>
      {trashOpen ? (
        <dialog ref={trashDialog} className="progress-editor" aria-labelledby="trash-heading"
          onCancel={(event) => { event.preventDefault(); setTrashOpen(false); }}>
          <h2 id="trash-heading">已删除节点</h2>
          <p className="muted">软删除的节点保留在此，可恢复。恢复后回到原父级末尾。</p>
          <ul className="progress-review-list">
            {removedNodes.map((node) => (
              <li key={node.id} className="progress-review-item">
                <header><strong>{node.title}</strong><span className="muted">{kindLabel(rules, node.kind)}</span></header>
                <div className="work-actions">
                  <button type="button" disabled={readOnly} onClick={() => { if (!readOnly) onRestoreRequest?.(node); }}>恢复</button>
                </div>
              </li>
            ))}
          </ul>
          <div className="work-actions">
            <button type="button" onClick={() => setTrashOpen(false)}>关闭</button>
          </div>
        </dialog>
      ) : null}
      <div role="tree" aria-label="办案进度大纲" className="h-tree"
        onKeyDown={(event) => {
          const current = flat.find((item) => item.node.id === currentFocusId());
          if (!current) return;
          const siblings = siblingsOf(current.node);
          const index = siblings.findIndex((node) => node.id === current.node.id);
          if (event.key === 'ArrowDown' && index < siblings.length - 1) {
            event.preventDefault(); focusNode(siblings[index + 1].id);
          }
          if (event.key === 'ArrowUp' && index > 0) {
            event.preventDefault(); focusNode(siblings[index - 1].id);
          }
          if (event.key === 'ArrowRight') {
            event.preventDefault();
            if (current.children.length > 0 && collapsedIds.has(current.node.id)) {
              onToggleCollapse(current.node.id);
            } else if (current.children.length > 0) {
              focusNode(current.children[0].node.id);
            }
          }
          if (event.key === 'ArrowLeft') {
            event.preventDefault();
            if (current.children.length > 0 && !collapsedIds.has(current.node.id)) {
              onToggleCollapse(current.node.id);
            } else if (current.node.parent_id) {
              focusNode(current.node.parent_id);
            }
          }
          if (event.key === 'Home' && flat.length > 0) { event.preventDefault(); focusNode(flat[0].node.id); }
          if (event.key === 'End' && flat.length > 0) { event.preventDefault(); focusNode(flat[flat.length - 1].node.id); }
        }}>
        {tree.map(renderNode)}
      </div>
      {/* 审查：人工主动移除关系的网页入口（列出本案有效关系，可移除）。 */}
      {snapshot.relations.some((relation) => relation.removed_at === null) ? (
        <details className="progress-relations">
          <summary>关系（{snapshot.relations.filter((relation) => relation.removed_at === null).length}）</summary>
          <ul>
            {snapshot.relations.filter((relation) => relation.removed_at === null).map((relation) => {
              const from = snapshot.nodes.find((node) => node.id === relation.from_node_id)?.title ?? '未知节点';
              const to = snapshot.nodes.find((node) => node.id === relation.to_node_id)?.title ?? '未知节点';
              return (
                <li key={relation.id}>
                  {from} —{relationLabel(rules, relation.type)}→ {to}
                  <button type="button" disabled={readOnly} onClick={() => void run({ type: 'remove_relation', relation_id: relation.id })}>移除</button>
                </li>
              );
            })}
          </ul>
        </details>
      ) : null}
      {menu ? (
        <ContextMenu x={menu.x} y={menu.y} label="节点操作"
          items={menuItemsFor(menu.node)} onClose={() => setMenu(null)} />
      ) : null}
      {kindMenu ? (
        <ContextMenu x={kindMenu.x} y={kindMenu.y} label="添加子节点"
          items={kindMenuItems(rules, snapshot.nodes.find((node) => node.id === kindMenu.parentId)?.kind ?? '',
            (kind) => onAddRequest(kindMenu.parentId, kind))}
          onClose={() => setKindMenu(null)} />
      ) : null}
      {rootMenu ? (
        <ContextMenu x={rootMenu.x} y={rootMenu.y} label="根级添加"
          items={rootItems} onClose={() => setRootMenu(null)} />
      ) : null}
    </div>
  );
}