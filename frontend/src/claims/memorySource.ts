/**
 * TEST-ONLY 内存数据源：把 P1 诉请金额同步执行器包成 ClaimWorkspaceSource。
 * 只用于 /prototype/claims 与组件测试；不读写业务库、不发起网络请求。
 * 不提供 seq()，因此原型页不做后台轮询，行为与 P1 基线一致。
 */
import type { ClaimExecutor, ClaimWorkspaceSource } from './types';

export function createMemoryClaimSource(executor: ClaimExecutor): ClaimWorkspaceSource {
  return {
    load: () => executor.getSnapshot(),
    human: (command) => executor.human(command),
    agent: (command) => executor.agent(command),
  };
}
