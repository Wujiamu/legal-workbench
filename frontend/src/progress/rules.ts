/**
 * 首版规则配置（K2/K3/K4/K6）。
 * 与后端 progress_rules.py 的首版口径一致；P2 以后由 GET /progress/rules 提供，
 * 这里保留为原型与组件测试的默认值。新增种类、层级或片段只改这份配置。
 */
import type { ProgressRules, TitlePresets } from './types';

const stageStates = [
  { value: 'not_started', label: '未开始' },
  { value: 'in_progress', label: '进行中' },
  { value: 'completed', label: '已完成' },
  { value: 'skipped', label: '已跳过' },
];

export const defaultRules: ProgressRules = {
  kinds: {
    program: {
      label: '程序',
      states: [
        { value: 'not_started', label: '未开始' },
        { value: 'in_progress', label: '进行中' },
        { value: 'completed', label: '已完成' },
      ],
      default_state: 'not_started',
    },
    stage: { label: '阶段', states: stageStates, default_state: 'not_started' },
    event: {
      label: '事件',
      states: [
        { value: 'planned', label: '计划' },
        { value: 'occurred', label: '已发生' },
        { value: 'cancelled', label: '取消' },
      ],
      default_state: 'planned',
    },
    work: {
      label: '工作',
      states: [
        { value: 'pending', label: '未完成' },
        { value: 'done', label: '已完成' },
        { value: 'cancelled', label: '取消' },
      ],
      default_state: 'pending',
    },
  },
  containment: {
    case: ['program', 'stage'],
    program: ['program', 'stage', 'event', 'work'],
    stage: ['work', 'event'],
    event: ['work'],
    work: ['work'],
  },
  relation_types: [
    { value: 'causes', label: '引起' },
    { value: 'relates', label: '关联' },
  ],
  fragments: {
    'litigation-default': {
      label: '诉讼默认',
      version: 1,
      roots: [
        { kind: 'stage', title: '诉前准备' },
        {
          kind: 'program', title: '一审',
          children: [
            { kind: 'stage', title: '立案' },
            { kind: 'stage', title: '庭前程序' },
            { kind: 'stage', title: '庭审' },
            { kind: 'stage', title: '裁判' },
          ],
        },
        // 结案归档是案件级操作（树外按钮），不是进度节点（用户反馈第四轮）。
      ],
    },
    /* 以下片段内部阶段为草稿，P1 原型时请承办人过目（总体计划 Q1）。 */
    'second-instance': {
      label: '二审',
      version: 1,
      roots: [{
        kind: 'program', title: '二审',
        children: [
          { kind: 'stage', title: '上诉' },
          { kind: 'stage', title: '二审审理' },
          { kind: 'stage', title: '二审裁判' },
        ],
      }],
    },
    'remand': {
      label: '发回重审',
      version: 1,
      roots: [{
        kind: 'program', title: '发回重审',
        children: [
          { kind: 'stage', title: '重审立案' },
          { kind: 'stage', title: '重审庭前程序' },
          { kind: 'stage', title: '重审庭审' },
          { kind: 'stage', title: '重审裁判' },
        ],
      }],
    },
    'retrial': {
      label: '再审',
      version: 1,
      roots: [{
        kind: 'program', title: '再审',
        children: [
          { kind: 'stage', title: '再审申请' },
          { kind: 'stage', title: '再审审查' },
          { kind: 'stage', title: '再审审理' },
          { kind: 'stage', title: '再审裁判' },
        ],
      }],
    },
    'enforcement': {
      label: '执行',
      version: 1,
      roots: [{
        kind: 'program', title: '执行',
        children: [
          { kind: 'stage', title: '执行申请' },
          { kind: 'stage', title: '执行立案' },
          { kind: 'stage', title: '执行实施' },
          { kind: 'stage', title: '执行结案' },
        ],
      }],
    },
  },
  default_fragment: 'litigation-default',
  agent_commands: [
    'add_node', 'update_node', 'move_node', 'set_active_stage', 'insert_fragment',
    'remove_node', 'add_relation', 'remove_relation', 'link', 'unlink',
  ],
  // 通用诉讼的常见工作与事件；承办人确认后可增删（用户反馈第六轮）。
  title_presets: {
    work: [
      '证据整理', '证据清单起草', '质证意见起草', '起诉状起草', '答辩状起草',
      '代理词起草', '法律检索', '案例检索', '阅卷', '当事人沟通',
      '出庭准备', '保全申请准备', '鉴定申请准备', '和解方案评估', '执行申请准备',
    ],
    event: [
      '证据交换', '庭前会议', '开庭', '谈话', '询问', '质证',
      '调解', '和解谈判', '宣判', '送达', '保全执行', '鉴定听证',
    ],
  },
};

/** 新建节点标题预设；无预设的种类返回空数组（自由输入）。 */
export function titlePresets(rules: ProgressRules, kind: string): string[] {
  return (rules.title_presets as TitlePresets | undefined)?.[kind] ?? [];
}

export function kindLabel(rules: ProgressRules, kind: string): string {
  return rules.kinds[kind]?.label ?? kind;
}

export function stateLabel(rules: ProgressRules, kind: string, state: string): string {
  return rules.kinds[kind]?.states.find((entry) => entry.value === state)?.label ?? state;
}

export function relationLabel(rules: ProgressRules, type: string): string {
  return rules.relation_types.find((entry) => entry.value === type)?.label ?? type;
}