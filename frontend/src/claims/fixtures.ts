/**
 * P1 合成诉请数据（全部 TEST-ONLY 虚构内容，不来自业务库）。
 * 两组：对被告一主诉请（含本金/利息/违约金/律师费/已付款抵扣）、
 * 二审变更诉请（与一审重叠，默认不计入全案总计）。
 * 另含 1 条 Agent 待确认金额项，以及可选取的模拟精确值（带合成原文出处）。
 */
import { createClaimExecutor } from './localExecutor';
import type { ClaimExecutor, ClaimSource, Currency } from './types';

export const FIXTURE_CASE_ID = 'case-claims-demo';
export const FIXTURE_CASE_TITLE = 'TEST-ONLY 甲公司诉乙公司合同纠纷案';

export interface AmountCandidate {
  id: string;
  label: string;
  raw_text: string;
  /** 可填入金额输入框的字符串（元）。 */
  amount_text: string;
  currency: Currency;
  source: ClaimSource;
}

function source(segmentId: string, materialId: string, materialTitle: string,
                page: number, locator: string, excerpt: string, rawText: string): ClaimSource {
  const start = excerpt.indexOf(rawText);
  if (start < 0) throw new Error(`合成原文缺少金额写法：${rawText}`);
  return {
    segment_id: segmentId,
    material_id: materialId,
    material_title: materialTitle,
    page,
    locator,
    excerpt,
    span: { start, end: start + rawText.length },
    raw_text: rawText,
  };
}

export const fixtureCandidates: AmountCandidate[] = [
  {
    id: 'cand-contract-price',
    label: '购销合同 · 第 3 页 · 第 1 条',
    raw_text: '人民币1,280,000元',
    amount_text: '1280000.00',
    currency: 'CNY',
    source: source('seg-contract-1', 'mat-contract', 'TEST-ONLY 购销合同.pdf', 3, '第 1 条',
      '第一条 合同价款\n合同价款为人民币1,280,000元，买受人应于货物交付后三十日内一次性付清。',
      '人民币1,280,000元'),
  },
  {
    id: 'cand-interest',
    label: '购销合同 · 第 4 页 · 第 7.2 条',
    raw_text: '86,400.00元',
    amount_text: '86400.00',
    currency: 'CNY',
    source: source('seg-contract-2', 'mat-contract', 'TEST-ONLY 购销合同.pdf', 4, '第 7.2 条',
      '第七条 逾期付款\n逾期付款利息按未付金额的日万分之一点七五计算。暂计至2026年10月31日为86,400.00元。',
      '86,400.00元'),
  },
  {
    id: 'cand-penalty',
    label: '购销合同 · 第 4 页 · 第 8.1 条',
    raw_text: '256,000元',
    amount_text: '256000.00',
    currency: 'CNY',
    source: source('seg-contract-3', 'mat-contract', 'TEST-ONLY 购销合同.pdf', 4, '第 8.1 条',
      '第八条 违约责任\n任何一方逾期履行的，应按合同总价款的20%支付违约金，即256,000元。',
      '256,000元'),
  },
  {
    id: 'cand-lawyer-fee',
    label: '律师费发票 · 第 1 页',
    raw_text: '80,000.00元',
    amount_text: '80000.00',
    currency: 'CNY',
    source: source('seg-invoice-1', 'mat-invoice', 'TEST-ONLY 律师费发票.pdf', 1, '价税合计',
      '律师费发票（TEST-ONLY）\n一审律师代理费80,000.00元，已由甲公司支付。',
      '80,000.00元'),
  },
  {
    id: 'cand-paid',
    label: '收款回单 · 第 1 页',
    raw_text: '300,000元',
    amount_text: '300000.00',
    currency: 'CNY',
    source: source('seg-receipt-1', 'mat-receipt', 'TEST-ONLY 收款回单.pdf', 1, '附言',
      '收款回单（TEST-ONLY）\n乙公司于2026年9月30日付款300,000元，附言：部分货款。',
      '300,000元'),
  },
  {
    id: 'cand-interest-supplement',
    label: '补充协议 · 第 5 页 · 第 2 条',
    raw_text: '3,287.67元',
    amount_text: '3287.67',
    currency: 'CNY',
    source: source('seg-supplement-1', 'mat-supplement', 'TEST-ONLY 补充协议.pdf', 5, '第 2 条',
      '补充协议第二条\n截至2026年11月20日，新增逾期利息3,287.67元。',
      '3,287.67元'),
  },
  {
    id: 'cand-high-precision',
    label: '结算单 · 第 2 页 · 高额精度示例',
    raw_text: '123,456,789,012.34元',
    amount_text: '123456789012.34',
    currency: 'CNY',
    source: source('seg-statement-1', 'mat-statement', 'TEST-ONLY 结算单.pdf', 2, '结算总额',
      '结算单（TEST-ONLY）\n双方确认结算金额为123,456,789,012.34元（高额精度演示）。',
      '123,456,789,012.34元'),
  },
  {
    id: 'cand-usd',
    label: '跨境采购合同 · 第 1 页 · 币种示例',
    raw_text: '美元12,345.67元',
    amount_text: '12345.67',
    currency: 'USD',
    source: source('seg-usd-1', 'mat-usd', 'TEST-ONLY 跨境采购合同.pdf', 1, '第 1 条',
      '跨境采购合同（TEST-ONLY）\n合同价款为美元12,345.67元，按付款日汇率结汇。',
      '美元12,345.67元'),
  },
];

export function searchCandidates(query: string): AmountCandidate[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return fixtureCandidates;
  return fixtureCandidates.filter((candidate) =>
    [candidate.raw_text, candidate.label, candidate.source.material_title, candidate.source.excerpt]
      .some((text) => text.toLowerCase().includes(needle)));
}

export function buildClaimsFixture(): ClaimExecutor {
  const executor = createClaimExecutor({ caseId: FIXTURE_CASE_ID, caseTitle: FIXTURE_CASE_TITLE });
  const human = (command: Parameters<ClaimExecutor['human']>[0]) => {
    const result = executor.human(command);
    if (!result.ok) throw new Error(`夹具命令失败：${result.message}`);
    return result.snapshot!;
  };
  const candidate = (id: string): AmountCandidate => {
    const found = fixtureCandidates.find((entry) => entry.id === id);
    if (!found) throw new Error(`夹具缺少精确值：${id}`);
    return found;
  };

  human({ type: 'add_group', title: '对被告一主诉请', currency: 'CNY' });
  human({ type: 'add_group', title: '二审变更诉请', currency: 'CNY' });
  let snapshot = executor.getSnapshot();
  const first = snapshot.groups.find((group) => group.title === '对被告一主诉请')!;
  const second = snapshot.groups.find((group) => group.title === '二审变更诉请')!;

  const add = (groupId: string, title: string, category: 'principal' | 'interest' | 'liquidated_damages'
    | 'expense' | 'deduction' | 'other', amount: string, calcNote: string, candidateId?: string) => {
    human({
      type: 'add_item', group_id: groupId, title, category, amount,
      calc_note: calcNote,
      source: candidateId ? candidate(candidateId).source : null,
    });
  };

  add(first.id, '合同价款（本金）', 'principal', '1280000.00',
    '合同第 1 条约定价款；扣除已付款后主张余额。', 'cand-contract-price');
  add(first.id, '逾期利息（暂计至 2026-10-31）', 'interest', '86400.00',
    '以未付价款为基数，按日万分之一点七五自 2026-01-01 暂计至 2026-10-31。', 'cand-interest');
  add(first.id, '违约金', 'liquidated_damages', '256000.00',
    '合同总价款 20%，依合同第 8.1 条。', 'cand-penalty');
  add(first.id, '律师费（一审）', 'expense', '80000.00',
    '一审律师代理费，凭发票主张。', 'cand-lawyer-fee');
  add(first.id, '已付款抵扣', 'deduction', '300000.00',
    '乙公司 2026-09-30 已付 300,000 元，按减项扣除。', 'cand-paid');

  add(second.id, '一审判决后利息', 'interest', '12345.67',
    '按判决主文第二项，自 2026-11-01 起按日万分之一点七五暂计（合成演示值）。');
  add(second.id, '二审律师费', 'expense', '30000.00',
    '二审阶段律师代理费（合成演示值，未附出处）。');

  human({
    type: 'update_group', group_id: second.id, include_in_total: false,
    note: '二审请求与一审主诉请存在重叠；默认不计入全案总计，勾选后可对比重复加总。',
  });

  // Agent 已提交 1 条候选金额项，等待承办人确认或撤回；确认前不计入小计。
  executor.agent({
    type: 'add_item', group_id: first.id, title: '逾期利息（补充至 2026-11-20）',
    category: 'interest', amount: '3287.67',
    calc_note: '补充协议第 2 条：2026-11-01 至 2026-11-20 新增利息。',
    source: candidate('cand-interest-supplement').source,
  });

  return executor;
}
