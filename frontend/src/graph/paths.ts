/**
 * 双主体路径高亮：在两张图当前可见的关系上求所有简单路径。
 * 图规模小（首版 50 主体 / 100 关系），用带深度上限的 DFS；
 * 深度与条数上限来自规则配置，避免密集图卡死界面。
 */

export interface PathEdge {
  id: string;
  source: string;
  target: string;
}

export interface PathResult {
  nodes: string[];
  edgeIds: string[];
}

export interface PathSearch {
  paths: PathResult[];
  /** 所有路径上出现过的节点 / 边，供画布高亮。 */
  nodeIds: Set<string>;
  edgeIds: Set<string>;
  /** 达到上限被截断时为 true，界面应提示只显示部分路径。 */
  truncated: boolean;
}

export function findPaths(edges: PathEdge[], from: string, to: string,
                          maxDepth = 8, limit = 20): PathSearch {
  const empty: PathSearch = { paths: [], nodeIds: new Set(), edgeIds: new Set(), truncated: false };
  if (!from || !to || from === to) return empty;
  const adjacency = new Map<string, { edgeId: string; other: string }[]>();
  for (const edge of edges) {
    if (edge.source === edge.target) continue;
    adjacency.set(edge.source, [...(adjacency.get(edge.source) ?? []), { edgeId: edge.id, other: edge.target }]);
    adjacency.set(edge.target, [...(adjacency.get(edge.target) ?? []), { edgeId: edge.id, other: edge.source }]);
  }
  if (!adjacency.has(from) || !adjacency.has(to)) return empty;
  // 先按节点 id 排序，保证同一图上路径顺序稳定（测试与界面一致）。
  for (const list of adjacency.values()) list.sort((a, b) => a.other.localeCompare(b.other));
  const paths: PathResult[] = [];
  const seen = new Set<string>();
  let truncated = false;

  function walk(node: string, visited: Set<string>, pathNodes: string[], pathEdges: string[]): void {
    if (truncated) return;
    if (paths.length >= limit) { truncated = true; return; }
    if (node === to) {
      const key = [...pathEdges].sort().join('|');
      if (!seen.has(key)) {
        seen.add(key);
        paths.push({ nodes: [...pathNodes], edgeIds: [...pathEdges] });
      }
      return;
    }
    if (pathEdges.length >= maxDepth) return;
    for (const step of adjacency.get(node) ?? []) {
      if (visited.has(step.other)) continue;
      visited.add(step.other);
      pathNodes.push(step.other);
      pathEdges.push(step.edgeId);
      walk(step.other, visited, pathNodes, pathEdges);
      pathEdges.pop();
      pathNodes.pop();
      visited.delete(step.other);
    }
  }

  walk(from, new Set([from]), [from], []);
  const nodeIds = new Set<string>();
  const edgeIds = new Set<string>();
  for (const path of paths) {
    path.nodes.forEach((id) => nodeIds.add(id));
    path.edgeIds.forEach((id) => edgeIds.add(id));
  }
  return { paths, nodeIds, edgeIds, truncated };
}
