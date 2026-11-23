/**
 * P1 合成案卷（全部 TEST-ONLY 虚构内容）。
 * 场景覆盖总体计划第八节：三层嵌套、并行无效程序、引起关系、
 * 业务关联、Agent 待确认变更。通过真实命令构建，不走旁路。
 */
import { createLocalExecutor } from './localExecutor';
import { defaultRules } from './rules';
import type { ProgressExecutor, ProgressNode } from './types';

export const FIXTURE_CASE_ID = 'case-demo';
export const FIXTURE_CASE_TITLE = 'TEST-ONLY 甲公司诉乙公司合同纠纷案';

export const fixtureCatalog = {
  materials: [
    { id: 'mat-1', case_id: FIXTURE_CASE_ID, title: 'TEST-ONLY 购销合同.pdf' },
    { id: 'mat-2', case_id: FIXTURE_CASE_ID, title: 'TEST-ONLY 履约凭证截图.png' },
  ],
  analyses: [
    { id: 'ana-1', case_id: FIXTURE_CASE_ID, title: 'TEST-ONLY 违约事实分析' },
  ],
  annotations: [
    { id: 'ann-1', case_id: FIXTURE_CASE_ID, title: 'TEST-ONLY 截图转录标注（待复核）' },
  ],
};

export function buildLitigationFixture(): ProgressExecutor {
  const executor = createLocalExecutor({
    caseId: FIXTURE_CASE_ID, rules: defaultRules, catalog: fixtureCatalog,
  });
  const byTitle = (title: string): ProgressNode => {
    const node = executor.getSnapshot().nodes.find((item) => item.title === title);
    if (!node) throw new Error(`夹具缺少节点：${title}`);
    return node;
  };
  const must = (result: { ok: boolean }) => {
    if (!result.ok) throw new Error('夹具命令失败');
  };

  must(executor.human({ type: 'insert_fragment', parent_id: null, fragment_key: 'litigation-default' }));
  // 一审进展：立案完成，庭前程序进行中
  const filing = byTitle('立案');
  must(executor.human({ type: 'set_active_stage', stage_id: filing.id, expected_revision: filing.revision }));
  const pretrial = byTitle('庭前程序');
  must(executor.human({ type: 'set_active_stage', stage_id: pretrial.id, expected_revision: pretrial.revision }));
  // 庭前程序下的证据交换事件与子工作（程序→阶段→事件→工作）
  must(executor.human({
    type: 'add_node', parent_id: pretrial.id, kind: 'event',
    title: '证据交换（第一次）', state: 'occurred', date_start: '2026-10-20',
  }));
  const exchange = byTitle('证据交换（第一次）');
  must(executor.human({ type: 'add_node', parent_id: exchange.id, kind: 'work', title: '补充证据清单' }));
  must(executor.human({ type: 'add_node', parent_id: exchange.id, kind: 'work', title: '质证意见' }));
  must(executor.human({ type: 'link', node_id: exchange.id, target: { material_id: 'mat-1' } }));
  must(executor.human({ type: 'link', node_id: byTitle('补充证据清单').id, target: { material_id: 'mat-2' } }));
  must(executor.human({ type: 'link', node_id: byTitle('质证意见').id, target: { analysis_id: 'ana-1' } }));
  // 通用诉讼框架：不预置专利等专门案件类型的程序（用户反馈第五轮）。
  // 并行程序与关系能力由模型支持，演示时用「＋ 添加 → 插入片段：二审」按需加入。
  // Agent 已把当前阶段改为「庭审」，等待承办人确认或撤回
  const hearing = byTitle('庭审');
  must(executor.agent({ type: 'set_active_stage', stage_id: hearing.id, expected_revision: hearing.revision }));
  return executor;
}