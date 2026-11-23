/**
 * 图形视图（总体计划 6.4 / K10）：React Flow 自定义节点 + elkjs 嵌套布局。
 * 布局在 Worker 中计算；Worker 不可用时退回主线程（jsdom 测试路径）。
 * 与大纲视图共享选中状态与交互口径：右键菜单、双击编辑、悬浮边缘加号。
 */
import { Background, Controls, Handle, Position, ReactFlow, ReactFlowProvider, useNodesInitialized, useNodesState, useReactFlow } from '@xyflow/react';
import type { NodeProps, NodeTypes } from '@xyflow/react';
import { useEffect, useMemo, useRef, useState } from 'react';
import '@xyflow/react/dist/style.css';
import { applyElkLayout, buildElkGraph, buildGraphModel } from './graphModel';
import type { GraphModel, GraphNode, ProgressCardData } from './graphModel';
import { ContextMenu, kindMenuItems, nearestEdge, nodeMenuItems } from './interactions';
import type { Edge } from './interactions';
import type { CommandResult, ProgressCommand, ProgressNode, ProgressRules, ProgressSnapshot } from './types';

/** 悬浮加号：附着在鼠标靠近的边上，点击打开种类菜单。 */
function EdgePlus({ card }: { card: ProgressCardData }) {
  const [edge, setEdge] = useState<Edge | null>(null);
  const allowed = card.allowedKinds ?? [];
  if (allowed.length === 0) return null;
  return (
    <>
      {edge ? (
        <span className={`edge-plus edge-${edge}`} role="button" tabIndex={-1}
          aria-label={`为 ${card.node.title} 添加子节点`}
          onMouseDown={(event) => event.stopPropagation()}
          onClick={(event) => {
            event.stopPropagation();
            const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
            card.onPlus?.(card.node.id, rect.left + rect.width / 2, rect.top + rect.height / 2);
          }}>＋</span>
      ) : null}
      <span className="edge-plus-sensor" aria-hidden="true"
        onMouseMove={(event) => {
          const parent = (event.currentTarget as HTMLElement).parentElement;
          if (!parent) return;
          setEdge(nearestEdge(event.clientX, event.clientY, parent.getBoundingClientRect()));
        }}
        onMouseLeave={() => setEdge(null)} />
    </>
  );
}

function ProgressCard({ data, selected }: NodeProps) {
  const card = data as ProgressCardData;
  return (
    <div className={selected ? 'progress-card selected' : 'progress-card'}>
      <Handle type="target" position={Position.Left} />
      {card.collapsed ? (
        <button type="button" className="progress-toggle"
          aria-label={`展开 ${card.node.title}`} aria-expanded={false}
          onClick={(event) => { event.stopPropagation(); card.onToggle?.(card.node.id); }}>▸</button>
      ) : null}
      <span className="progress-kind">{card.kindLabel}</span>
      <span className="progress-title">{card.node.title}</span>
      <span className={`progress-state state-${card.node.state}`}>{card.stateLabel}</span>
      {card.collapsed ? <span className="progress-collapsed-count">{card.childCount} 项</span> : null}
      {card.agentBadge ? (
        <span className={`agent-badge agent-${card.agentBadge}`}
          title={card.agentBadge === 'pending' ? 'Agent 修改待确认（黄）' : 'Agent 修改已确认（绿）'}>Agent</span>
      ) : null}
      <EdgePlus card={card} />
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function ProgressGroup({ data, selected }: NodeProps) {
  const card = data as ProgressCardData;
  return (
    <div className={selected ? 'progress-group selected' : 'progress-group'}>
      <Handle type="target" position={Position.Left} />
      <header>
        <button type="button" className="progress-toggle"
          aria-label={`收起 ${card.node.title}`} aria-expanded={true}
          onClick={(event) => { event.stopPropagation(); card.onToggle?.(card.node.id); }}>▾</button>
        <span className="progress-kind">{card.kindLabel}</span>
        <span className="progress-title">{card.node.title}</span>
        <span className={`progress-state state-${card.node.state}`}>{card.stateLabel}</span>
        {card.agentBadge ? (
          <span className={`agent-badge agent-${card.agentBadge}`}
            title={card.agentBadge === 'pending' ? 'Agent 修改待确认（黄）' : 'Agent 修改已确认（绿）'}>Agent</span>
        ) : null}
        <EdgePlus card={card} />
      </header>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes: NodeTypes = { progress: ProgressCard, progressGroup: ProgressGroup };

/** 节点完成测量后，用 elk 坐标直接计算视口居中，不依赖 RF 内部边界。 */
function FitWhenReady({ layoutKey, model, container }: {
  layoutKey: number;
  model: GraphModel | null;
  container: React.RefObject<HTMLDivElement | null>;
}) {
  const initialized = useNodesInitialized();
  const { setViewport } = useReactFlow();
  useEffect(() => {
    if (!initialized || layoutKey === 0 || !model) return;
    const fit = () => {
      const element = container.current;
      if (!element || model.nodes.length === 0) return;
      const byId = new Map(model.nodes.map((node) => [node.id, node]));
      const absolute = (node: GraphNode): { x: number; y: number } => {
        let { x, y } = node.position;
        let parentId = node.parentId;
        while (parentId) {
          const parent = byId.get(parentId);
          if (!parent) break;
          x += parent.position.x;
          y += parent.position.y;
          parentId = parent.parentId;
        }
        return { x, y };
      };
      let minX = Infinity; let minY = Infinity; let maxX = -Infinity; let maxY = -Infinity;
      for (const node of model.nodes) {
        const origin = absolute(node);
        const width = (node.style?.width as number | undefined) ?? 132;
        const height = (node.style?.height as number | undefined) ?? 52;
        minX = Math.min(minX, origin.x);
        minY = Math.min(minY, origin.y);
        maxX = Math.max(maxX, origin.x + width);
        maxY = Math.max(maxY, origin.y + height);
      }
      const boundsWidth = maxX - minX;
      const boundsHeight = maxY - minY;
      const viewportWidth = element.clientWidth;
      const viewportHeight = element.clientHeight;
      const zoom = Math.min(viewportWidth / (boundsWidth * 1.12), viewportHeight / (boundsHeight * 1.12), 1);
      void setViewport({
        x: (viewportWidth - boundsWidth * zoom) / 2 - minX * zoom,
        y: (viewportHeight - boundsHeight * zoom) / 2 - minY * zoom,
        zoom,
      }, { duration: 200 });
    };
    const first = setTimeout(fit, 60);
    const second = setTimeout(fit, 500);
    return () => { clearTimeout(first); clearTimeout(second); };
  }, [initialized, layoutKey, model, container, setViewport]);
  return null;
}

export function GraphView({ snapshot, rules, selectedId, readOnly = false, onSelect, collapsedIds = new Set(), onToggleCollapse, onCommand, onEditRequest, onMoveRequest, onAddRequest }: {
  snapshot: ProgressSnapshot;
  rules: ProgressRules;
  selectedId: string | null;
  readOnly?: boolean;
  onSelect(nodeId: string): void;
  collapsedIds?: Set<string>;
  onToggleCollapse?: (nodeId: string) => void;
  onCommand?: (command: ProgressCommand) => CommandResult | Promise<CommandResult>;
  onEditRequest?: (node: ProgressNode) => void;
  onMoveRequest?: (node: ProgressNode) => void;
  onAddRequest?: (parentId: string | null, kind: string) => void;
}) {
  const model = useMemo(() => buildGraphModel(snapshot, rules, collapsedIds), [snapshot, rules, collapsedIds]);
  const [laid, setLaid] = useState<GraphModel | null>(null);
  const [layoutKey, setLayoutKey] = useState(0);
  const [menu, setMenu] = useState<{ x: number; y: number; node: ProgressNode } | null>(null);
  const [kindMenu, setKindMenu] = useState<{ x: number; y: number; parentId: string } | null>(null);
  const [error, setError] = useState('');

  async function run(command: ProgressCommand) {
    if (readOnly) return;
    const result = await onCommand?.(command);
    setError(result && !result.ok ? result.message : '');
  }

  useEffect(() => {
    let cancelled = false;
    const elkGraph = buildElkGraph(snapshot, collapsedIds);
    let worker: Worker | null = null;
    try {
      worker = new Worker(new URL('./layout.worker.ts', import.meta.url), { type: 'module' });
    } catch {
      worker = null; // jsdom 等环境无 Worker，退回主线程
    }
    const finish = (result: Parameters<typeof applyElkLayout>[1]) => {
      if (!cancelled) {
        setLaid(applyElkLayout(model, result));
        setLayoutKey((key) => key + 1);
      }
    };
    // 主线程回退按需加载 elk，避免与 Worker 重复打包。
    // 审查 08：布局失败时向用户说明并保留切回大纲的入口，不静默空白。
    const failLayout = (cause: unknown) => {
      if (cancelled) return;
      setError(`图形布局计算失败：${cause instanceof Error ? cause.message : String(cause)}。可切回大纲视图继续操作。`);
    };
    const fallback = () => {
      void import('./layout').then(({ computeLayout }) => computeLayout(elkGraph)).then(finish).catch(failLayout);
    };
    if (worker) {
      const active = worker;
      active.onmessage = (event) => { finish(event.data); active.terminate(); };
      active.onerror = () => { active.terminate(); fallback(); };
      active.postMessage(elkGraph);
    } else {
      fallback();
    }
    return () => { cancelled = true; worker?.terminate(); };
  }, [snapshot, model, collapsedIds]);

  // 受控节点：尺寸测量变更经 onNodesChange 回写，nodesInitialized 才能成立。
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<GraphNode>([]);
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    setRfNodes((laid?.nodes ?? []).map((node) => ({
      ...node,
      selected: node.id === selectedId,
      data: {
        ...node.data,
        onToggle: onToggleCollapse,
        allowedKinds: readOnly ? [] : (rules.containment[node.data.node.kind] ?? []),
        onPlus: (nodeId: string, x: number, y: number) => {
          if (!readOnly) setKindMenu({ x, y, parentId: nodeId });
        },
      },
    })));
  }, [laid, selectedId, readOnly, onToggleCollapse, rules, setRfNodes]);

  const menuNode = menu?.node;
  const menuHasChildren = menuNode
    ? snapshot.nodes.some((item) => item.parent_id === menuNode.id && item.removed_at === null)
    : false;

  return (
    <ReactFlowProvider>
      <div className="progress-graph" role="application" aria-label="办案进度图形视图" ref={container}>
        {error ? <p role="alert" className="progress-error graph-error">错误：{error}</p> : null}
        <ReactFlow nodes={rfNodes} edges={laid?.edges ?? []} nodeTypes={nodeTypes}
          onNodesChange={onNodesChange} nodesDraggable={false} zoomOnDoubleClick={false}
          minZoom={0.15} maxZoom={2} nodesConnectable={false}
          onNodeClick={(_, node) => onSelect(node.id)}
          onNodeContextMenu={(event, node) => {
            if (readOnly || (!onCommand && !onEditRequest)) return;
            event.preventDefault();
            setMenu({ x: event.clientX, y: event.clientY, node: (node.data as ProgressCardData).node });
          }}
          onNodeDoubleClick={(_, node) => { if (!readOnly) onEditRequest?.((node.data as ProgressCardData).node); }}>
          <Background gap={24} />
          <Controls showInteractive={false} />
          <FitWhenReady layoutKey={layoutKey} model={laid} container={container} />
        </ReactFlow>
        {menu && menuNode ? (
          <ContextMenu x={menu.x} y={menu.y} label="节点操作"
            items={nodeMenuItems(menuNode, {
              hasChildren: menuHasChildren,
              collapsed: collapsedIds.has(menuNode.id),
              onSetActive: () => run({ type: 'set_active_stage', stage_id: menuNode.id, expected_revision: menuNode.revision }),
              onEdit: () => onEditRequest?.(menuNode),
              onMove: () => onMoveRequest?.(menuNode),
              onToggle: () => onToggleCollapse?.(menuNode.id),
              onRemove: () => run({ type: 'remove_node', node_id: menuNode.id, expected_revision: menuNode.revision }),
            })}
            onClose={() => setMenu(null)} />
        ) : null}
        {kindMenu ? (
          <ContextMenu x={kindMenu.x} y={kindMenu.y} label="添加子节点"
            items={kindMenuItems(rules, snapshot.nodes.find((node) => node.id === kindMenu.parentId)?.kind ?? '',
              (kind) => onAddRequest?.(kindMenu.parentId, kind))}
            onClose={() => setKindMenu(null)} />
        ) : null}
      </div>
    </ReactFlowProvider>
  );
}