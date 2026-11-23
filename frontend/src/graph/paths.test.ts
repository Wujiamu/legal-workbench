import { describe, expect, it } from 'vitest';
import { findPaths } from './paths';

const edges = [
  { id: 'e1', source: 'a', target: 'b' },
  { id: 'e2', source: 'b', target: 'c' },
  { id: 'e3', source: 'a', target: 'd' },
  { id: 'e4', source: 'd', target: 'c' },
  { id: 'e5', source: 'b', target: 'd' },
  { id: 'e6', source: 'c', target: 'e' },
];

describe('双主体路径高亮', () => {
  it('返回两主体间所有简单路径（关系按无向处理）', () => {
    const result = findPaths(edges, 'a', 'c');
    const signatures = result.paths.map((path) => [...path.edgeIds].sort().join('+')).sort();
    expect(signatures).toEqual(['e1+e2', 'e1+e4+e5', 'e2+e3+e5', 'e3+e4'].sort());
    expect(result.nodeIds).toEqual(new Set(['a', 'b', 'c', 'd']));
    expect(result.edgeIds).toEqual(new Set(['e1', 'e2', 'e3', 'e4', 'e5']));
    expect(result.truncated).toBe(false);
  });

  it('同主体、未知主体与孤立主体返回空结果', () => {
    expect(findPaths(edges, 'a', 'a').paths).toEqual([]);
    expect(findPaths(edges, 'a', 'zzz').paths).toEqual([]);
    expect(findPaths([], 'a', 'b').paths).toEqual([]);
  });

  it('深度与条数上限可截断并标记', () => {
    const result = findPaths(edges, 'a', 'e', 3, 1);
    expect(result.paths.length).toBe(1);
    expect(result.truncated).toBe(true);
  });
});
