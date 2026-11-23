/**
 * elkjs 自动排版（仅人工点击「自动排版」时运行）。
 * 与办案进度不同，关系图坐标按图持久化（D1）：这里只计算建议坐标，
 * 由页面通过 save_layout 命令写入，并保留分组框（按成员包围盒重算）。
 */
import ELK from 'elkjs/lib/elk.bundled.js';
import type { ElkNode } from 'elkjs/lib/elk-api';
import type { CanvasModel } from './types';

export const NODE_WIDTH = 188;
export const NODE_HEIGHT = 108;
const GROUP_HEADER = 44;
const GROUP_PADDING = 24;

const elk = new ELK();

export interface LayoutResult {
  nodes: Map<string, { x: number; y: number }>;
  groups: Map<string, { x: number; y: number; width: number; height: number }>;
}

interface ElkChild {
  id: string;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
}

/**
 * stress：无层级倾向，适合股权/资金等网状结构；layered：从左到右，适合控制链。
 * 收起的分组不参与排版（成员仍保留原坐标）。
 */
export async function autoLayout(model: CanvasModel,
                                 algorithm: 'stress' | 'layered'): Promise<LayoutResult> {
  const collapsed = new Set(model.groups.filter((group) => group.collapsed).map((group) => group.id));
  const visible = model.nodes.filter((node) => !node.group_id || !collapsed.has(node.group_id));
  const visibleIds = new Set(visible.map((node) => node.id));
  const graph: ElkNode = {
    id: 'root',
    layoutOptions: {
      'elk.algorithm': algorithm,
      'elk.spacing.nodeNode': '110',
      'elk.layered.spacing.nodeNodeBetweenLayers': '240',
      'elk.stress.desiredEdgeLength': '340',
      'elk.padding': '[top=72,left=40,bottom=40,right=40]',
      ...(algorithm === 'layered' ? { 'elk.direction': 'RIGHT' } : {}),
    },
    children: visible.map((node) => ({ id: node.id, width: NODE_WIDTH, height: NODE_HEIGHT })),
    edges: model.edges
      .filter((edge) => edge.source !== edge.target
        && visibleIds.has(edge.source) && visibleIds.has(edge.target))
      .map((edge, index) => ({
        id: `layout-edge-${index}`,
        sources: [edge.source],
        targets: [edge.target],
      })),
  };
  const laid = await elk.layout(graph);
  const positions = new Map<string, { x: number; y: number }>();
  for (const child of (laid.children ?? []) as ElkChild[]) {
    if (child.x === undefined || child.y === undefined) continue;
    positions.set(child.id, { x: Math.round(child.x), y: Math.round(child.y) });
  }
  const groups = new Map<string, { x: number; y: number; width: number; height: number }>();
  for (const group of model.groups) {
    const members = model.nodes.filter((node) => node.group_id === group.id)
      .map((node) => positions.get(node.id))
      .filter((position): position is { x: number; y: number } => Boolean(position));
    if (members.length === 0) continue;
    const minX = Math.min(...members.map((position) => position.x));
    const minY = Math.min(...members.map((position) => position.y));
    const maxX = Math.max(...members.map((position) => position.x));
    const maxY = Math.max(...members.map((position) => position.y));
    groups.set(group.id, {
      x: Math.max(0, Math.round(minX - GROUP_PADDING)),
      y: Math.max(0, Math.round(minY - GROUP_HEADER - GROUP_PADDING / 2)),
      width: Math.round(maxX - minX + NODE_WIDTH + GROUP_PADDING * 2),
      height: Math.round(maxY - minY + NODE_HEIGHT + GROUP_HEADER + GROUP_PADDING * 2),
    });
  }
  return { nodes: positions, groups };
}
