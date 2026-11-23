/**
 * TEST-ONLY 内存数据源：把 P1 同步执行器包成 GraphWorkspaceSource。
 * 只用于 /prototype/graph 与组件测试；不读写业务库、不发起网络请求。
 * 不提供 seqs()，因此原型页不做后台轮询，行为与 P1 基线一致。
 */
import type { GraphExecutor, GraphWorkspaceSource } from './types';

export function createMemoryGraphSource(executor: GraphExecutor): GraphWorkspaceSource {
  return {
    load: () => executor.getSnapshot(),
    rules: () => executor.getRules(),
    human: (command) => executor.human(command),
    agent: (command) => executor.agent(command),
    undo: () => executor.undo(),
    impact: (target) => executor.getImpact(target),
    archive: (archived) => { executor.setArchived(archived); },
  };
}
