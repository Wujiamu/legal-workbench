/**
 * 关系图画布：React Flow 自定义主体节点 + 分组框 + 四档线型边。
 * - 主体/分组可拖拽，拖拽结束由页面通过 save_layout 持久化到本图；
 * - 四档线型与颜色来自规则配置（graph_rules 同构），不在组件里写死；
 * - 分组框可收起/展开（收起时隐藏组内主体与相连边）；
 * - 选中主体/关系、路径高亮、筛选与快照对比高亮都通过 props 传入，工作稿与快照共用。
 */
import {
  Background, Controls, EdgeLabelRenderer, Handle, Position, ReactFlow, ReactFlowProvider,
  getBezierPath, useNodesState, useReactFlow,
} from '@xyflow/react';
import type { Edge, EdgeProps, Node, NodeProps, NodeTypes, EdgeTypes } from '@xyflow/react';
import { MarkerType } from '@xyflow/react';
import { useEffect, useMemo } from 'react';
import '@xyflow/react/dist/style.css';
import { credibilityRule, entityKindLabel, relationKindLabel, roleLabel } from './rules';
import type {
  CanvasEdge, CanvasGroup, CanvasModel, CanvasNode, CredibilityRule, GraphRules,
} from './types';
import type { DiffHighlight } from './model';

export type Selection = { type: 'entity'; id: string } | { type: 'relation'; id: string } | null;

export interface LayoutPayload {
  nodes?: { node_id: string; x: number; y: number }[];
  groups?: { group_id: string; x: number; y: number; width?: number; height?: number }[];
}

export interface CanvasFilters {
  credibility: Set<string>;
  kind: string;
}

interface EntityData {
  node: CanvasNode;
  kindLabel: string;
  rolesLabel: string;
  sourceText: string;
  selected: boolean;
  dimmed: boolean;
  diffState: 'added' | 'changed' | 'moved' | null;
  onSelect: (id: string) => void;
  [key: string]: unknown;
}

interface GroupData {
  group: CanvasGroup;
  memberCount: number;
  dimmed: boolean;
  readOnly: boolean;
  onToggle: () => void;
  onEdit: () => void;
  [key: string]: unknown;
}

interface RelationData {
  edge: CanvasEdge;
  rule: CredibilityRule;
  kindLabel: string;
  sourceText: string;
  curveOffset: number;
  selected: boolean;
  dimmed: boolean;
  diffState: 'added' | 'changed' | null;
  onSelect: (id: string) => void;
  [key: string]: unknown;
}

function EntityCard({ data, selected }: NodeProps) {
  const card = data as EntityData;
  return (
    <div
      className={`graph-node${selected || card.selected ? ' selected' : ''}`
        + `${card.dimmed ? ' dimmed' : ''}${card.diffState ? ` diff-${card.diffState}` : ''}`}
      role="button" tabIndex={0}
      aria-label={`${card.node.name}，${card.kindLabel}${card.rolesLabel ? `，${card.rolesLabel}` : ''}`}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          card.onSelect(card.node.id);
        }
      }}
    >
      <Handle type="target" position={Position.Left} />
      <div className="graph-node-head">
        <span className="graph-node-name">{card.node.name}</span>
        {card.node.pending ? <span className="agent-badge agent-pending" title="Agent 修改待确认">Agent</span> : null}
        {!card.node.pending && card.node.accepted ? (
          <span className="agent-badge agent-accepted" title="Agent 修改已确认">Agent</span>
        ) : null}
      </div>
      <div className="graph-node-meta">{card.kindLabel}{card.rolesLabel ? ` · ${card.rolesLabel}` : ''}</div>
      {card.node.aliases.length > 0 ? (
        <div className="graph-node-alias">别名：{card.node.aliases.join('、')}</div>
      ) : null}
      <div className={`graph-node-source${card.node.source_count > 0 ? '' : ' empty'}`}>
        {card.node.source_count > 0 ? `出处 ${card.node.source_count} · ${card.sourceText}` : '无出处'}
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function GroupCard({ data }: NodeProps) {
  const card = data as GroupData;
  return (
    <div className={`graph-group-box${card.dimmed ? ' dimmed' : ''}`}
      style={{ background: card.group.color ?? 'var(--surface-subtle)' }}>
      <header className="graph-group-head">
        <button type="button" className="nodrag graph-group-toggle"
          aria-expanded={!card.group.collapsed}
          aria-label={`${card.group.collapsed ? '展开' : '收起'}分组 ${card.group.title}`}
          onClick={(event) => { event.stopPropagation(); card.onToggle(); }}>
          {card.group.collapsed ? '▸' : '▾'}
        </button>
        <span className="graph-group-title">{card.group.title}</span>
        <span className="graph-group-count">{card.memberCount} 项{card.group.collapsed ? ' · 已收起' : ''}</span>
        {!card.readOnly ? (
          <button type="button" className="nodrag graph-group-edit"
            onClick={(event) => { event.stopPropagation(); card.onEdit(); }}>编辑</button>
        ) : null}
      </header>
      {card.group.collapsed ? (
        <p className="graph-group-hint">组内主体与关系已收起，展开后恢复显示。</p>
      ) : null}
    </div>
  );
}

export function assignPairCurveOffsets<T extends { source: string; target: string }>(
  edges: T[],
): Map<T, number> {
  const pairBuckets = new Map<string, T[]>();
  for (const edge of edges) {
    const pairKey = edge.source < edge.target
      ? `${edge.source}::${edge.target}`
      : `${edge.target}::${edge.source}`;
    const list = pairBuckets.get(pairKey) ?? [];
    list.push(edge);
    pairBuckets.set(pairKey, list);
  }
  const offsets = new Map<T, number>();
  for (const group of pairBuckets.values()) {
    group.forEach((edge, slotIndex) => {
      const canonicalOffset = group.length > 1
        ? (slotIndex - (group.length - 1) / 2) * 160
        : 0;
      offsets.set(edge, canonicalOffset * (edge.source < edge.target ? 1 : -1));
    });
  }
  return offsets;
}

export function curvedPairPath(
  sourceX: number,
  sourceY: number,
  targetX: number,
  targetY: number,
  curveOffset: number,
): [string, number, number] {
  const dx = targetX - sourceX;
  const dy = targetY - sourceY;
  const len = Math.hypot(dx, dy);
  if (curveOffset === 0 || len < 1) {
    const [fallbackPath, fallbackX, fallbackY] = getBezierPath({
      sourceX,
      sourceY,
      sourcePosition: Position.Right,
      targetX,
      targetY,
      targetPosition: Position.Left,
    });
    return [fallbackPath, fallbackX, fallbackY];
  }
  const mx = (sourceX + targetX) / 2;
  const my = (sourceY + targetY) / 2;
  const nx = -dy / len;
  const ny = dx / len;
  const cx = mx + nx * curveOffset;
  const cy = my + ny * curveOffset;
  return [
    `M ${sourceX},${sourceY} Q ${cx},${cy} ${targetX},${targetY}`,
    mx + 0.5 * nx * curveOffset,
    my + 0.5 * ny * curveOffset,
  ];
}

function RelationEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition,
  targetPosition, data, markerEnd, selected }: EdgeProps) {
  const edgeData = data as RelationData;
  const [path, labelX, labelY] = edgeData.curveOffset
    ? curvedPairPath(sourceX, sourceY, targetX, targetY, edgeData.curveOffset)
    : getBezierPath({
      sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition,
    });
  const rule = edgeData.rule;
  const dimmed = edgeData.dimmed;
  const stroke = rule.stroke;
  return (
    <>
      <path
        id={id}
        d={path}
        fill="none"
        className={`graph-edge-path${selected || edgeData.selected ? ' selected' : ''}`
          + `${dimmed ? ' dimmed' : ''}${edgeData.diffState ? ` diff-${edgeData.diffState}` : ''}`}
        style={{
          stroke, strokeWidth: rule.width,
          strokeDasharray: rule.dash || undefined,
          opacity: dimmed ? 0.16 : 1,
        }}
        markerEnd={markerEnd}
      />
      <path d={path} fill="none" stroke="transparent" strokeWidth={18} className="graph-edge-hit"
        onClick={(event) => { event.stopPropagation(); edgeData.onSelect(edgeData.edge.relation_id); }} />
      <EdgeLabelRenderer>
        <button
          type="button"
          className={`graph-edge-label${selected || edgeData.selected ? ' selected' : ''}`
            + `${dimmed ? ' dimmed' : ''}${edgeData.diffState ? ` diff-${edgeData.diffState}` : ''}`}
          style={{
            transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            borderColor: stroke,
          }}
          aria-label={`${edgeData.kindLabel}${edgeData.edge.label ? ` ${edgeData.edge.label}` : ''}，`
            + `${rule.label}，出处：${edgeData.sourceText}`}
          onClick={(event) => { event.stopPropagation(); edgeData.onSelect(edgeData.edge.relation_id); }}
        >
          <span className="graph-edge-main" style={{ color: stroke }}>
            {edgeData.edge.credibility === 'ruled_out' ? '✕ ' : ''}
            {edgeData.kindLabel}{edgeData.edge.label ? ` · ${edgeData.edge.label}` : ''}
          </span>
          <span className="graph-edge-sub">{edgeData.sourceText}</span>
          {edgeData.edge.pending ? <span className="agent-badge agent-pending">Agent</span> : null}
          {!edgeData.edge.pending && edgeData.edge.accepted ? (
            <span className="agent-badge agent-accepted">Agent</span>
          ) : null}
        </button>
      </EdgeLabelRenderer>
    </>
  );
}

const nodeTypes: NodeTypes = { graphEntity: EntityCard, graphGroup: GroupCard };
const edgeTypes: EdgeTypes = { graphRelation: RelationEdge };

export function GraphCanvas({
  model, rules, graphId, selected, onSelect, readOnly = false, filters, highlight,
  diff, onSaveLayout, onToggleGroup, onEditGroup, emptyHint,
}: {
  model: CanvasModel;
  rules: GraphRules;
  graphId: string;
  selected: Selection;
  onSelect: (selection: Selection) => void;
  readOnly?: boolean;
  filters?: CanvasFilters;
  highlight?: { nodeIds: Set<string>; edgeIds: Set<string> } | null;
  diff?: DiffHighlight | null;
  onSaveLayout?: (payload: LayoutPayload) => void;
  onToggleGroup?: (group: CanvasGroup) => void;
  onEditGroup?: (group: CanvasGroup) => void;
  emptyHint?: string;
}) {
  const collapsed = useMemo(() => new Set(model.groups
    .filter((group) => group.collapsed).map((group) => group.id)), [model.groups]);
  const visibleNodes = useMemo(() => model.nodes.filter((node) =>
    !node.group_id || !collapsed.has(node.group_id)), [model.nodes, collapsed]);
  const visibleIds = useMemo(() => new Set(visibleNodes.map((node) => node.id)), [visibleNodes]);
  const groupById = useMemo(() => new Map(model.groups.map((group) => [group.id, group])),
    [model.groups]);

  const rfNodes = useMemo(() => {
    const list: Node[] = [];
    for (const group of model.groups) {
      const memberCount = model.nodes.filter((node) => node.group_id === group.id).length;
      const dimmed = Boolean(highlight && ![...highlight.nodeIds].some((id) => {
        const node = model.nodes.find((item) => item.id === id);
        return node?.group_id === group.id;
      }));
      list.push({
        id: `group:${group.id}`,
        type: 'graphGroup',
        position: { x: group.x, y: group.y },
        style: { width: group.width, height: group.height },
        data: {
          group, memberCount, dimmed,
          readOnly: readOnly ?? false,
          onToggle: () => onToggleGroup?.(group),
          onEdit: () => onEditGroup?.(group),
        } satisfies GroupData,
        draggable: !readOnly,
        selectable: false,
        zIndex: 0,
      });
    }
    for (const node of visibleNodes) {
      const group = node.group_id ? groupById.get(node.group_id) : null;
      const dimmed = Boolean(highlight && !highlight.nodeIds.has(node.id));
      list.push({
        id: node.id,
        type: 'graphEntity',
        parentId: group ? `group:${group.id}` : undefined,
        extent: group ? 'parent' : undefined,
        position: group
          ? { x: node.x - group.x, y: node.y - group.y }
          : { x: node.x, y: node.y },
        data: {
          node,
          kindLabel: entityKindLabel(rules, node.kind),
          rolesLabel: node.roles.map((role) => roleLabel(rules, role)).join('、'),
          sourceText: node.source_labels.join('、') || '无',
          selected: selected?.type === 'entity' && selected.id === node.id,
          dimmed,
          diffState: diff
            ? diff.addedNodes.has(node.id) ? 'added'
              : diff.changedNodes.has(node.id) ? 'changed'
                : diff.movedNodes.has(node.id) ? 'moved' : null
            : null,
          onSelect: (id: string) => onSelect({ type: 'entity', id }),
        } satisfies EntityData,
        draggable: !readOnly,
        zIndex: 2,
      });
    }
    return list;
  }, [model, rules, selected, readOnly, highlight, diff, visibleNodes, groupById,
    onSelect, onToggleGroup, onEditGroup]);

  const rfEdges = useMemo(() => {
    const visibleEdges = model.edges
      .filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target))
      .filter((edge) => !filters || (filters.credibility.has(edge.credibility)
        && (filters.kind === 'all' || filters.kind === edge.kind)));
    const curveOffsets = assignPairCurveOffsets(visibleEdges);
    return visibleEdges.map((edge) => {
      const curveOffset = curveOffsets.get(edge) ?? 0;
      const rule = credibilityRule(rules, edge.credibility);
      const dimmed = Boolean(highlight && !highlight.edgeIds.has(edge.relation_id));
      return {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: 'graphRelation',
        data: {
          edge, rule,
          kindLabel: relationKindLabel(rules, edge.kind),
          sourceText: edge.source_labels.join('、') || '无出处',
          curveOffset,
          selected: selected?.type === 'relation' && selected.id === edge.relation_id,
          dimmed,
          diffState: diff
            ? diff.addedEdges.has(edge.relation_id) ? 'added'
              : diff.changedEdges.has(edge.relation_id) ? 'changed' : null
            : null,
          onSelect: (id: string) => onSelect({ type: 'relation', id }),
        } satisfies RelationData,
        markerEnd: edge.directed
          ? { type: MarkerType.ArrowClosed, color: rule.stroke, width: 18, height: 18 }
          : undefined,
        zIndex: 1,
      } as Edge;
    });
  }, [model.edges, rules, visibleIds, filters, highlight, diff, selected, onSelect]);

  const [nodesState, setNodes, onNodesChange] = useNodesState<Node>([]);
  useEffect(() => { setNodes(rfNodes); }, [rfNodes, setNodes]);

  function handleDragStop(_event: unknown, node: Node) {
    if (!onSaveLayout) return;
    if (node.id.startsWith('group:')) {
      const groupId = node.id.slice('group:'.length);
      const group = groupById.get(groupId);
      if (!group) return;
      const deltaX = node.position.x - group.x;
      const deltaY = node.position.y - group.y;
      if (deltaX === 0 && deltaY === 0) return;
      const members = model.nodes.filter((item) => item.group_id === groupId);
      onSaveLayout({
        groups: [{ group_id: groupId, x: node.position.x, y: node.position.y }],
        nodes: members.map((member) => ({
          node_id: member.node_id, x: member.x + deltaX, y: member.y + deltaY,
        })),
      });
      return;
    }
    const canvasNode = model.nodes.find((item) => item.id === node.id);
    if (!canvasNode) return;
    const group = canvasNode.group_id ? groupById.get(canvasNode.group_id) : null;
    const absolute = group
      ? { x: group.x + node.position.x, y: group.y + node.position.y }
      : node.position;
    if (absolute.x === canvasNode.x && absolute.y === canvasNode.y) return;
    onSaveLayout({ nodes: [{ node_id: canvasNode.node_id, x: absolute.x, y: absolute.y }] });
  }

  return (
    <div className={`graph-canvas${readOnly ? ' read-only' : ''}`}
      role="application" aria-label={readOnly ? '只读关系图画布' : '关系图画布'}>
      {model.nodes.length === 0 ? (
        <p className="graph-empty">{emptyHint ?? '本图还没有主体。可点「加入本图」从案件池挑选，或新建主体。'}</p>
      ) : null}
      <ReactFlowProvider>
        <ReactFlow
          key={graphId}
          nodes={nodesState}
          edges={rfEdges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodesChange={onNodesChange}
          nodesDraggable={!readOnly}
          nodesConnectable={false}
          elementsSelectable
          zoomOnDoubleClick={false}
          minZoom={0.12}
          maxZoom={1.6}
          fitView
          fitViewOptions={{ padding: 0.22, maxZoom: 1 }}
          onNodeClick={(_event, node) => {
            if (node.id.startsWith('group:')) return;
            onSelect({ type: 'entity', id: node.id });
          }}
          onPaneClick={() => onSelect(null)}
          onNodeDragStop={handleDragStop}
        >
          <Background gap={26} />
          <Controls showInteractive={false} />
          <FitOnGraph graphId={graphId} nodeCount={model.nodes.length} />
        </ReactFlow>
      </ReactFlowProvider>
    </div>
  );
}

/** 节点是异步测量后才可 fitView；切换图或首次加载后重新适配视口。 */
function FitOnGraph({ graphId, nodeCount }: { graphId: string; nodeCount: number }) {
  const { fitView } = useReactFlow();
  useEffect(() => {
    if (nodeCount === 0) return;
    const fit = () => { void fitView({ padding: 0.22, maxZoom: 1, duration: 160 }); };
    const first = window.setTimeout(fit, 80);
    const second = window.setTimeout(fit, 420);
    return () => { window.clearTimeout(first); window.clearTimeout(second); };
  }, [graphId, nodeCount, fitView]);
  return null;
}
