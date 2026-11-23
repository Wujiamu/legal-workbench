/**
 * 首版规则配置：主体类型、诉讼角色、关系类型、四档可信状态、快照阶段、
 * 同意方式与 Agent 命令白名单。与后端 graph_rules.py 的首版口径一致；
 * 新增类型只改这份配置，不改视图组件（子计划审核清单 1）。
 */
import type { CredibilityRule, GraphRules, Option } from './types';

const option = (value: string, label: string): Option => ({ value, label });

export const credibilityRules: CredibilityRule[] = [
  {
    value: 'evidenced', label: '有证据', badge: '实线',
    dash: '', stroke: '#2f5d50', width: 2.2,
    hint: '至少关联一条有效出处',
  },
  {
    value: 'suspected', label: '待查 / 存疑', badge: '红粗虚线',
    dash: '10 5', stroke: '#b3261e', width: 3.4,
    hint: '怀疑存在，尚无证据',
  },
  {
    value: 'disputed', label: '对方主张 / 有争议', badge: '橙点线',
    dash: '2 6', stroke: '#c2650a', width: 2.6,
    hint: '对方主张，我方不认可',
  },
  {
    value: 'ruled_out', label: '已排除', badge: '灰删除线',
    dash: '4 4', stroke: '#8b8f8b', width: 1.6,
    hint: '查证不成立，保留备查',
  },
];

export const defaultRules: GraphRules = {
  entity_kinds: [
    option('person', '自然人'),
    option('org', '法人或组织'),
    option('agency', '机关'),
    option('court', '法院'),
    option('other', '其他'),
  ],
  roles: [
    option('plaintiff', '原告'),
    option('defendant', '被告'),
    option('third_party', '第三人'),
    option('witness', '证人'),
    option('outsider', '案外人'),
    option('legal_rep', '法定代表人'),
    option('controller', '实际控制人'),
    option('shareholder', '股东'),
    option('guarantor', '担保人'),
    option('creditor', '债权人'),
    option('debtor', '债务人'),
  ],
  relation_kinds: [
    option('equity_control', '股权 / 控制'),
    option('office_rep', '任职 / 代表'),
    option('contract', '合同相对方'),
    option('guarantee', '担保'),
    option('debt', '债权债务'),
    option('fund_flow', '资金往来'),
    option('agency', '代理'),
    option('kinship', '亲属'),
    option('affiliate', '关联'),
    option('other', '其他'),
  ],
  credibility: credibilityRules,
  // Q4 待用户看原型后确认是否增删。
  snapshot_stages: ['初次沟通', '方案沟通', '起诉前', '庭前', '庭后', '二审'],
  consent_channels: [
    option('meeting', '会议'),
    option('phone', '电话'),
    option('wechat', '微信'),
    option('email', '邮件'),
    option('written', '书面'),
    option('other', '其他'),
  ],
  // 冻结/恢复、客户同意、确认/撤回绝不在白名单（D3）。
  agent_commands: [
    'add_entity', 'update_entity', 'add_alias',
    'add_relation', 'update_relation', 'add_source',
    'create_graph', 'update_graph', 'add_to_graph',
    'add_evidence_group', 'add_evidence_item', 'add_evidence_binding',
    'update_evidence_item', 'import_evidence_catalog',
  ],
  max_path_depth: 8,
  max_paths: 20,
};

export function credibilityRule(rules: GraphRules, value: string): CredibilityRule {
  return rules.credibility.find((entry) => entry.value === value)
    ?? { value, label: value, badge: value, dash: '4 4', stroke: '#8b8f8b', width: 1.6, hint: '' };
}

export function optionLabel(options: Option[], value: string | null | undefined): string {
  if (!value) return '';
  return options.find((entry) => entry.value === value)?.label ?? value;
}

export function entityKindLabel(rules: GraphRules, kind: string): string {
  return optionLabel(rules.entity_kinds, kind);
}

export function roleLabel(rules: GraphRules, role: string): string {
  return optionLabel(rules.roles, role);
}

export function relationKindLabel(rules: GraphRules, kind: string): string {
  return optionLabel(rules.relation_kinds, kind);
}

export function channelLabel(rules: GraphRules, channel: string): string {
  return optionLabel(rules.consent_channels, channel);
}

export function credibilityLabel(rules: GraphRules, value: string): string {
  return credibilityRule(rules, value).label;
}
