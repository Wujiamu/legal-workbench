/** 快照 → 视图模型：纯函数，不含业务规则，大纲与图形视图共用。 */
import type { ProgressNode, ProgressSnapshot } from './types';

export interface TreeNode {
  node: ProgressNode;
  children: TreeNode[];
  level: number;
}

export function buildTree(nodes: ProgressNode[]): TreeNode[] {
  const active = nodes.filter((node) => node.removed_at === null);
  const byParent = new Map<string | null, ProgressNode[]>();
  for (const node of active) {
    const list = byParent.get(node.parent_id) ?? [];
    list.push(node);
    byParent.set(node.parent_id, list);
  }
  for (const list of byParent.values()) list.sort((a, b) => a.position - b.position);
  function walk(parentId: string | null, level: number): TreeNode[] {
    return (byParent.get(parentId) ?? []).map((node) => ({
      node, children: walk(node.id, level + 1), level,
    }));
  }
  return walk(null, 1);
}

export function flattenTree(tree: TreeNode[]): TreeNode[] {
  const flat: TreeNode[] = [];
  function walk(items: TreeNode[]) {
    for (const item of items) {
      flat.push(item);
      walk(item.children);
    }
  }
  walk(tree);
  return flat;
}

/** 节点“待确认”= 存在触及该节点、状态为 pending 的变更记录（6.1）。 */
export function pendingNodeIds(snapshot: ProgressSnapshot): Set<string> {
  const ids = new Set<string>();
  for (const change of snapshot.pending_changes) {
    for (const item of change.diff) {
      if (item.target === 'node') ids.add(item.id);
    }
  }
  return ids;
}

/**
 * Agent 徽标状态（用户反馈第七轮）：小框只写「Agent」，颜色表认可状态。
 * pending（黄）优先于 accepted（绿）；撤回与人工节点无徽标。
 * 需要全部变更历史时传 changes；只有快照时用 pending_changes（仅黄）。
 */
export function agentBadgeState(snapshot: ProgressSnapshot, nodeId: string,
                                allChanges?: ProgressSnapshot['pending_changes']): 'pending' | 'accepted' | null {
  const changes = allChanges ?? snapshot.pending_changes;
  let accepted = false;
  for (const change of changes) {
    if (change.actor !== 'agent') continue;
    if (!change.diff.some((item) => item.target === 'node' && item.id === nodeId)) continue;
    if (change.review_state === 'pending') return 'pending';
    if (change.review_state === 'accepted') accepted = true;
  }
  return accepted ? 'accepted' : null;
}

export function linkKindLabel(link: { material_id: string | null; analysis_id: string | null; annotation_id: string | null }): string {
  if (link.material_id) return '材料';
  if (link.analysis_id) return '分析';
  return '标注';
}

/** 收起是视图状态，不入库（K13）。以下工具供两种视图共用。 */

export function activeChildCount(nodes: ProgressNode[], parentId: string): number {
  return nodes.filter((node) => node.parent_id === parentId && node.removed_at === null).length;
}

export function nodeById(nodes: ProgressNode[]): Map<string, ProgressNode> {
  return new Map(nodes.map((node) => [node.id, node]));
}

/** 返回最近的被收起祖先；节点自身被收起不算（自身仍显示为紧凑卡片）。 */
export function collapsedAncestorId(node: ProgressNode, byId: Map<string, ProgressNode>,
                                    collapsed: Set<string>): string | null {
  let cursor = node.parent_id;
  while (cursor) {
    if (collapsed.has(cursor)) return cursor;
    cursor = byId.get(cursor)?.parent_id ?? null;
  }
  return null;
}

/** 把隐藏节点改挂到最近的可见祖先（收起节点本身）。 */
export function remapToVisible(id: string, byId: Map<string, ProgressNode>,
                               collapsed: Set<string>): string {
  let cursor: string | null = id;
  while (cursor) {
    const node = byId.get(cursor);
    if (!node) return id;
    if (collapsedAncestorId(node, byId, collapsed) === null) return cursor;
    cursor = node.parent_id;
  }
  return id;
}

/** 默认收起：第 2 层起有子节点的分组（主线程序保持展开）。 */
export function defaultCollapsedIds(nodes: ProgressNode[]): Set<string> {
  return new Set(
    flattenTree(buildTree(nodes))
      .filter((item) => item.level >= 2 && item.children.length > 0)
      .map((item) => item.node.id),
  );
}