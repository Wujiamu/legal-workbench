/** 布局 Worker（K10）：elkjs 在 Worker 内计算，避免阻塞界面。 */
import { computeLayout } from './layout';
import type { ElkNode } from 'elkjs/lib/elk-api';

addEventListener('message', async (event: MessageEvent<ElkNode>) => {
  const result = await computeLayout(event.data);
  (globalThis as unknown as { postMessage(message: unknown): void }).postMessage(result);
});