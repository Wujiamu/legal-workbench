/** elkjs 布局计算：主线程与 Worker 共用同一实现。 */
import ELK from 'elkjs/lib/elk.bundled.js';
import type { ElkNode } from 'elkjs/lib/elk-api';

const elk = new ELK();

export function computeLayout(graph: ElkNode): Promise<ElkNode> {
  return elk.layout(graph);
}