/**
 * 图形视图模型（总体计划 6.4）：与大纲视图读同一快照。
 * 包含关系 → 复合节点嵌套；非包含关系 → 独立边（虚线 + 类型标签）。
 * 布局坐标不入库，由 elkjs 计算（K10）。
 */
import type { Edge, Node } from '@xyflow/react';
import type { ElkNode } from 'elkjs/lib/elk-api';
import { kindLabel, relationLabel, stateLabel } from './rules';
import { activeChildCount, agentBadgeState, collapsedAncestorId, nodeById, pendingNodeIds, remapToVisible } from './tree';
import type { ProgressNode, ProgressRules, ProgressSnapshot } from './types';

export interface ProgressCardData {
  node: ProgressNode;
  pending: boolean;
  /** Agent 徽标：pending 黄 / accepted 绿 / null 无（用户反馈第七轮）。 */
  agentBadge: 'pending' | 'accepted' | null;
  kindLabel: string;
  stateLabel: string;
  collapsed?: boolean;
  childCount?: number;
  onToggle?: (nodeId: string) => void;
  allowedKinds?: string[];
  onPlus?: (nodeId: string, x: number, y: number) => void;
  [key: string]: unknown;
}

export type GraphNode = Node<ProgressCardData>;
export type GraphEdge = Edge<{ kind: 'relation' }>;

export interface GraphModel {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export function buildGraphModel(snapshot: ProgressSnapshot, rules: ProgressRules,
                                collapsedIds: Set<string> = new Set()): GraphModel {
  const active = snapshot.nodes.filter((node) => node.removed_at === null);
  const byId = nodeById(active);
  const visible = active.filter((node) => collapsedAncestorId(node, byId, collapsedIds) === null);
  const pending = pendingNodeIds(snapshot);
  const parentIds = new Set(visible.map((node) => node.parent_id).filter((id): id is string => id !== null));
  // React Flow 要求父节点在数组中先于子节点（adoptUserNodes 顺序解析 parentId，
  // 父在后时子节点按根级定位并告警）。后端快照按创建序返回，子节点可能先于父，
  // 这里按树先序重排；同级保持 position 顺序。
  const byParent = new Map<string | null, ProgressNode[]>();
  for (const node of visible) {
    byParent.set(node.parent_id, [...(byParent.get(node.parent_id) ?? []), node]);
  }
  for (const list of byParent.values()) list.sort((a, b) => a.position - b.position);
  const ordered: ProgressNode[] = [];
  function walk(node: ProgressNode) {
    ordered.push(node);
    for (const child of byParent.get(node.id) ?? []) walk(child);
  }
  for (const root of byParent.get(null) ?? []) walk(root);
  const nodes: GraphNode[] = ordered.map((node) => {
    const isCollapsed = collapsedIds.has(node.id) && activeChildCount(active, node.id) > 0;
    return {
      id: node.id,
      type: !isCollapsed && parentIds.has(node.id) ? 'progressGroup' : 'progress',
      position: { x: 0, y: 0 },
      parentId: node.parent_id ?? undefined,
      extent: node.parent_id ? 'parent' : undefined,
      data: {
        node, pending: pending.has(node.id),
        agentBadge: agentBadgeState(snapshot, node.id, snapshot.changes),
        kindLabel: kindLabel(rules, node.kind),
        stateLabel: stateLabel(rules, node.kind, node.state),
        collapsed: isCollapsed,
        childCount: activeChildCount(active, node.id),
      },
    };
  });
  const visibleIds = new Set(visible.map((node) => node.id));
  const edges: GraphEdge[] = [];
  for (const relation of snapshot.relations.filter((item) => item.removed_at === null)) {
    // 端点被收起时改挂到收起的祖先节点，避免悬空边。
    const source = remapToVisible(relation.from_node_id, byId, collapsedIds);
    const target = remapToVisible(relation.to_node_id, byId, collapsedIds);
    if (source === target || !visibleIds.has(source) || !visibleIds.has(target)) continue;
    edges.push({
      id: relation.id,
      source,
      target,
      type: 'smoothstep',
      label: relationLabel(rules, relation.type),
      style: { strokeDasharray: '6 4', stroke: '#8a5a00' },
      data: { kind: 'relation' },
    });
  }
  return { nodes, edges };
}

function leafSize(node: ProgressNode, collapsed = false): { width: number; height: number } {
  if (collapsed) {
    // 收起卡片含展开按钮、「N 项」与可能的待确认徽标，预留单行宽度与高度。
    return { width: Math.min(340, Math.max(180, node.title.length * 14 + 186)), height: 64 };
  }
  return { width: Math.min(280, Math.max(132, node.title.length * 14 + 56)), height: 52 };
}

/** elk 输入：程序内阶段横排，阶段下工作纵排（6.4）；收起节点按叶子处理。 */
export function buildElkGraph(snapshot: ProgressSnapshot,
                              collapsedIds: Set<string> = new Set()): ElkNode {
  const active = snapshot.nodes.filter((node) => node.removed_at === null);
  const byId = nodeById(active);
  const visible = active.filter((node) => collapsedAncestorId(node, byId, collapsedIds) === null);
  const visibleIds = new Set(visible.map((node) => node.id));
  const byParent = new Map<string | null, ProgressNode[]>();
  for (const node of visible) {
    byParent.set(node.parent_id, [...(byParent.get(node.parent_id) ?? []), node]);
  }
  for (const list of byParent.values()) list.sort((a, b) => a.position - b.position);
  function walk(node: ProgressNode): ElkNode {
    const children = (byParent.get(node.id) ?? []).map(walk);
    const isCollapsed = collapsedIds.has(node.id);
    const size = children.length > 0 ? {} : leafSize(node, isCollapsed);
    return {
      id: node.id,
      ...size,
      layoutOptions: children.length > 0
        ? {
            'elk.direction': node.kind === 'program' ? 'RIGHT' : 'DOWN',
            'elk.padding': '[top=46,left=16,bottom=16,right=16]',
            'elk.spacing.nodeNode': '20',
          }
        : undefined,
      children: children.length > 0 ? children : undefined,
    };
  }
  // 顺序链边：只约束布局先后（阶段横排、工作纵排），不作为渲染边。
  const sequenceEdges: { id: string; sources: string[]; targets: string[] }[] = [];
  for (const list of byParent.values()) {
    for (let i = 1; i < list.length; i += 1) {
      sequenceEdges.push({
        id: `seq-${list[i - 1].id}-${list[i].id}`,
        sources: [list[i - 1].id], targets: [list[i].id],
      });
    }
  }
  return {
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.hierarchyHandling': 'SEPARATE_CHILDREN',
      'elk.padding': '[top=20,left=20,bottom=20,right=20]',
      'elk.spacing.nodeNode': '28',
      'elk.layered.spacing.nodeNodeBetweenLayers': '40',
    },
    children: (byParent.get(null) ?? []).map(walk),
    edges: [
      // 审查 08：布局边与渲染边用同一可见端点集合；端点已删除的关系不送布局器，
      // 否则 elk 报 "Referenced shape does not exist" 导致整页无节点。库中关系保留供恢复。
      ...snapshot.relations
        .filter((relation) => relation.removed_at === null)
        .map((relation) => ({
          id: relation.id,
          sources: [remapToVisible(relation.from_node_id, byId, collapsedIds)],
          targets: [remapToVisible(relation.to_node_id, byId, collapsedIds)],
        }))
        .filter((edge) => edge.sources[0] !== edge.targets[0]
          && visibleIds.has(edge.sources[0]) && visibleIds.has(edge.targets[0])),
      ...sequenceEdges,
    ],
  };
}

interface ElkLayoutNode {
  id: string;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  children?: ElkLayoutNode[];
}

/** 把 elk 结果映射回 React Flow 节点；子节点坐标相对父节点。 */
export function applyElkLayout(model: GraphModel, elk: ElkLayoutNode): GraphModel {
  const layout = new Map<string, ElkLayoutNode>();
  function walk(item: ElkLayoutNode) {
    layout.set(item.id, item);
    (item.children ?? []).forEach(walk);
  }
  walk(elk);
  const nodes = model.nodes.map((node) => {
    const placed = layout.get(node.id);
    if (!placed) return node;
    const isGroup = (placed.children ?? []).length > 0;
    return {
      ...node,
      position: { x: placed.x ?? 0, y: placed.y ?? 0 },
      style: isGroup
        ? { width: placed.width, height: placed.height }
        : { width: placed.width, height: placed.height },
    };
  });
  return { nodes, edges: model.edges };
}