/**
 * P1 合成案卷（全部 TEST-ONLY 虚构内容，不接业务数据）。
 * 覆盖：一案两图共享主体/关系、四档可信状态、分组、证据目录与重排、
 * 已冻结旧快照（编号已漂移、含「客户部分同意」备注）、4 条 Agent 待确认变更。
 * 通过真实命令构建，不走旁路；命令失败即抛错，避免夹具静默缺数据。
 */
import { createLocalGraphExecutor } from './localExecutor';
import { defaultRules } from './rules';
import type { GraphCommand, GraphExecutor, GraphSnapshot } from './types';

export const FIXTURE_CASE_ID = 'case-graph-demo';
export const FIXTURE_CASE_TITLE = 'TEST-ONLY 甲公司诉乙公司股权与担保纠纷案';

export const fixtureMaterials = [
  { id: 'mat-contract', case_id: FIXTURE_CASE_ID, title: 'TEST-ONLY 股权转让协议.pdf', kind: 'pdf' as const, page_count: 12 },
  { id: 'mat-registry', case_id: FIXTURE_CASE_ID, title: 'TEST-ONLY 工商登记档案.pdf', kind: 'pdf' as const, page_count: 30 },
  { id: 'mat-bank', case_id: FIXTURE_CASE_ID, title: 'TEST-ONLY 银行流水（节选）.xlsx', kind: 'sheet' as const, page_count: 8 },
  { id: 'mat-chat', case_id: FIXTURE_CASE_ID, title: 'TEST-ONLY 微信聊天记录截图.png', kind: 'image' as const, page_count: 3 },
  { id: 'mat-board', case_id: FIXTURE_CASE_ID, title: 'TEST-ONLY 股东会决议.docx', kind: 'doc' as const, page_count: 4 },
  { id: 'mat-supplement', case_id: FIXTURE_CASE_ID, title: 'TEST-ONLY 补充协议.pdf', kind: 'pdf' as const, page_count: 2 },
];

export const fixtureSegments = [
  {
    id: 'seg-registry-3', case_id: FIXTURE_CASE_ID, material_id: 'mat-registry', page: 3,
    text: 'TEST-ONLY 工商档案第 3 页：丙投资合伙企业（有限合伙）认缴出资 450 万元，占甲公司注册资本 45%。',
  },
  {
    id: 'seg-contract-2', case_id: FIXTURE_CASE_ID, material_id: 'mat-contract', page: 2,
    text: 'TEST-ONLY 股权转让协议第 2 页：丙投资受让甲公司 45% 股权，转让价款 450 万元，于协议签署后 30 日内支付。',
  },
  {
    id: 'seg-bank-4', case_id: FIXTURE_CASE_ID, material_id: 'mat-bank', page: 4,
    text: 'TEST-ONLY 流水第 4 页：乙公司向甲公司付款 120 万元，摘要「货款」。',
  },
  {
    id: 'seg-bank-6', case_id: FIXTURE_CASE_ID, material_id: 'mat-bank', page: 6,
    text: 'TEST-ONLY 流水第 6 页：乙公司向丙投资付款 30 万元，摘要「往来款」。',
  },
  {
    id: 'seg-chat-2', case_id: FIXTURE_CASE_ID, material_id: 'mat-chat', page: 2,
    text: 'TEST-ONLY 微信第 2 页：乙公司法定代表人回复「担保的事我认，等货款结清再谈」。',
  },
  {
    id: 'seg-supplement-1', case_id: FIXTURE_CASE_ID, material_id: 'mat-supplement', page: 1,
    text: 'TEST-ONLY 补充协议第 1 页：王五代甲公司签署本补充协议，权限至 2026 年 12 月 31 日。',
  },
  {
    id: 'seg-board-2', case_id: FIXTURE_CASE_ID, material_id: 'mat-board', page: 2,
    text: 'TEST-ONLY 股东会决议第 2 页：同意甲公司为乙公司货款债务提供保证。',
  },
];

export const fixtureAnnotations = [
  {
    id: 'ann-chat-3', case_id: FIXTURE_CASE_ID, material_id: 'mat-chat', page: 3,
    text: 'TEST-ONLY 截图转录：王五称「补充协议是我代签的」。',
  },
];

export const fixtureProgressNodes = [
  { value: 'pn-filing', label: '立案' },
  { value: 'pn-pretrial', label: '庭前程序' },
  { value: 'pn-hearing', label: '开庭' },
];

function requireOk(result: ReturnType<GraphExecutor['human']>, label: string): GraphSnapshot {
  if (!result.ok) throw new Error(`夹具命令失败（${label}）：${result.message}`);
  return result.snapshot;
}

export function buildGraphFixture(): GraphExecutor {
  const executor = createLocalGraphExecutor({
    caseId: FIXTURE_CASE_ID,
    caseTitle: FIXTURE_CASE_TITLE,
    rules: defaultRules,
    materials: fixtureMaterials,
    segments: fixtureSegments,
    annotations: fixtureAnnotations,
    progressNodes: fixtureProgressNodes,
  });
  const human = (command: GraphCommand) => requireOk(executor.human(command), command.type);
  const agent = (command: GraphCommand) => requireOk(executor.agent(command), `agent:${command.type}`);
  const entityId = (name: string): string => {
    const entity = executor.getSnapshot().entities.find((item) => item.name === name);
    if (!entity) throw new Error(`夹具缺少主体：${name}`);
    return entity.id;
  };
  const relationId = (label: string): string => {
    const relation = executor.getSnapshot().relations.find((item) => item.label === label);
    if (!relation) throw new Error(`夹具缺少关系：${label}`);
    return relation.id;
  };
  const revisionOfRelation = (id: string): number =>
    executor.getSnapshot().relations.find((item) => item.id === id)?.revision ?? 0;
  const itemId = (title: string): string => {
    const item = executor.getSnapshot().evidence.items.find((entry) => entry.title === title);
    if (!item) throw new Error(`夹具缺少证据条目：${title}`);
    return item.id;
  };
  const groupId = (title: string): string => {
    const group = executor.getSnapshot().evidence.groups.find((entry) => entry.title === title);
    if (!group) throw new Error(`夹具缺少证据分组：${title}`);
    return group.id;
  };

  // 1. 两张命名视图（一案多图，共享案件池）
  const graphEquity = human({ type: 'create_graph', title: '股权与实控关系', description: '接案梳理：主体与持股结构' });
  const equityGraphId = graphEquity.graphs.find((graph) => graph.title === '股权与实控关系')!.id;
  const graphFlow = human({ type: 'create_graph', title: '履约与资金往来', description: '货款、担保与资金流向' });
  const flowGraphId = graphFlow.graphs.find((graph) => graph.title === '履约与资金往来')!.id;

  // 2. 案件主体池（含别名与多角色）
  const entitySpecs: { name: string; kind: string; roles: string[]; note: string; aliases: string[] }[] = [
    {
      name: 'TEST-ONLY 甲公司', kind: 'org', roles: ['plaintiff', 'creditor'],
      note: '本案原告，注册资本 1000 万元。', aliases: ['甲贸易有限公司', '甲公司（旧名）'],
    },
    {
      name: 'TEST-ONLY 乙公司', kind: 'org', roles: ['defendant', 'debtor'],
      note: '本案被告，拖欠货款。', aliases: ['乙实业'],
    },
    {
      name: 'TEST-ONLY 丙投资合伙企业', kind: 'org', roles: ['third_party', 'shareholder'],
      note: '甲公司股东，认缴 45%。', aliases: ['丙投资'],
    },
    {
      name: 'TEST-ONLY 张三', kind: 'person', roles: ['legal_rep', 'controller'],
      note: '乙公司法定代表人，实际控制人。', aliases: ['张某'],
    },
    {
      name: 'TEST-ONLY 李四', kind: 'person', roles: ['witness', 'outsider'],
      note: '乙公司董事，曾参与磋商。', aliases: [],
    },
    {
      name: 'TEST-ONLY 王五', kind: 'person', roles: ['outsider'],
      note: '代甲公司签署补充协议。', aliases: ['老王'],
    },
  ];
  for (const spec of entitySpecs) {
    human({
      type: 'add_entity', kind: spec.kind, name: spec.name,
      roles: spec.roles, note: spec.note,
    });
    for (const alias of spec.aliases) {
      human({ type: 'add_alias', entity_id: entityId(spec.name), alias });
    }
  }

  // 3. 案件关系池：四档可信状态 + 一条未编号出处
  const createRelation = (
    from: string, to: string, kind: string, label: string,
    credibility: 'suspected' | 'disputed' | 'ruled_out',
    attrs: Record<string, unknown>, note: string | null,
    source?: { ref: { segment_id: string }; proves: string },
  ): string => {
    human({
      type: 'add_relation', from_entity_id: entityId(from), to_entity_id: entityId(to),
      kind, label, credibility, directed: true, attrs, note,
    });
    const id = relationId(label);
    if (source) {
      human({ type: 'add_source', relation_id: id, ref: source.ref, proves: source.proves });
    }
    return id;
  };
  const relEquityZhang = createRelation(
    'TEST-ONLY 张三', 'TEST-ONLY 丙投资合伙企业', 'equity_control', '持股 60%',
    'suspected', { share_pct: 60 }, '工商档案显示张三为丙投资执行事务合伙人。',
    { ref: { segment_id: 'seg-registry-3' }, proves: '证明张三通过丙投资控制甲公司' },
  );
  human({
    type: 'update_relation', relation_id: relEquityZhang,
    expected_revision: revisionOfRelation(relEquityZhang), changes: { credibility: 'evidenced' },
  });
  const relEquityBing = createRelation(
    'TEST-ONLY 丙投资合伙企业', 'TEST-ONLY 甲公司', 'equity_control', '持股 45%',
    'suspected', { share_pct: 45 }, '股权转让协议与工商档案相互印证。',
    { ref: { segment_id: 'seg-contract-2' }, proves: '证明丙投资持有甲公司 45% 股权' },
  );
  human({
    type: 'update_relation', relation_id: relEquityBing,
    expected_revision: revisionOfRelation(relEquityBing), changes: { credibility: 'evidenced' },
  });
  const relGuarantee = createRelation(
    'TEST-ONLY 乙公司', 'TEST-ONLY 甲公司', 'guarantee', '货款保证',
    'suspected', { amount_cents: 120_000_000, currency: 'CNY' },
    '乙公司法定代表人微信中承认担保，尚未取得书面保证合同。',
  );
  createRelation(
    'TEST-ONLY 甲公司', 'TEST-ONLY 乙公司', 'debt', '货款 120 万元',
    'disputed', { amount_cents: 120_000_000, currency: 'CNY' },
    '我方主张已交付货物；对方主张质量不合格。',
    { ref: { segment_id: 'seg-bank-4' }, proves: '证明对方已支付部分货款并确认欠款' },
  );
  createRelation(
    'TEST-ONLY 乙公司', 'TEST-ONLY 丙投资合伙企业', 'fund_flow', '往来款 30 万元',
    'ruled_out', { amount_cents: 30_000_000, currency: 'CNY' },
    '经核对系关联方正常往来，与本案货款无关。',
    { ref: { segment_id: 'seg-bank-6' }, proves: '排除该笔款项与本案的关联' },
  );
  createRelation(
    'TEST-ONLY 李四', 'TEST-ONLY 乙公司', 'office_rep', '董事',
    'suspected', {}, '工商登记显示李四为董事，待调取任职文件。',
  );
  createRelation(
    'TEST-ONLY 王五', 'TEST-ONLY 张三', 'kinship', '兄弟',
    'suspected', {}, '据当事人陈述为兄弟关系，待核实户籍。',
  );
  const relAgency = createRelation(
    'TEST-ONLY 王五', 'TEST-ONLY 甲公司', 'agency', '代理签约',
    'suspected', {}, '补充协议由王五代签，授权文件尚未收入目录。',
    { ref: { segment_id: 'seg-supplement-1' }, proves: '证明王五代甲公司签署补充协议' },
  );
  human({
    type: 'update_relation', relation_id: relAgency,
    expected_revision: revisionOfRelation(relAgency), changes: { credibility: 'evidenced' },
  });

  // 4. 图成员（共享事实，各自布局）
  human({
    type: 'add_to_graph', graph_id: equityGraphId,
    entity_ids: [
      entityId('TEST-ONLY 张三'), entityId('TEST-ONLY 丙投资合伙企业'),
      entityId('TEST-ONLY 甲公司'), entityId('TEST-ONLY 乙公司'), entityId('TEST-ONLY 李四'),
    ],
    relation_ids: [
      relEquityZhang, relEquityBing, relGuarantee,
      relationId('董事'),
    ],
  });
  human({
    type: 'add_to_graph', graph_id: flowGraphId,
    entity_ids: [
      entityId('TEST-ONLY 甲公司'), entityId('TEST-ONLY 乙公司'),
      entityId('TEST-ONLY 丙投资合伙企业'), entityId('TEST-ONLY 王五'),
      entityId('TEST-ONLY 张三'),
    ],
    relation_ids: [
      relGuarantee, relationId('货款 120 万元'), relationId('往来款 30 万元'),
      relationId('兄弟'), relAgency,
    ],
  });

  const nodeOf = (graphId: string, entityName: string): string => {
    const entity = entityId(entityName);
    const node = executor.getSnapshot().nodes.find((item) => item.graph_id === graphId
      && item.entity_id === entity);
    if (!node) throw new Error(`夹具缺少图节点：${entityName}`);
    return node.id;
  };
  human({
    type: 'save_layout', graph_id: equityGraphId,
    nodes: [
      { node_id: nodeOf(equityGraphId, 'TEST-ONLY 张三'), x: 90, y: 150 },
      { node_id: nodeOf(equityGraphId, 'TEST-ONLY 丙投资合伙企业'), x: 390, y: 150 },
      { node_id: nodeOf(equityGraphId, 'TEST-ONLY 甲公司'), x: 690, y: 150 },
      { node_id: nodeOf(equityGraphId, 'TEST-ONLY 乙公司'), x: 690, y: 390 },
      { node_id: nodeOf(equityGraphId, 'TEST-ONLY 李四'), x: 390, y: 390 },
    ],
  });
  human({
    type: 'save_layout', graph_id: flowGraphId,
    nodes: [
      { node_id: nodeOf(flowGraphId, 'TEST-ONLY 甲公司'), x: 90, y: 170 },
      { node_id: nodeOf(flowGraphId, 'TEST-ONLY 乙公司'), x: 420, y: 170 },
      { node_id: nodeOf(flowGraphId, 'TEST-ONLY 丙投资合伙企业'), x: 750, y: 170 },
      { node_id: nodeOf(flowGraphId, 'TEST-ONLY 王五'), x: 90, y: 450 },
      { node_id: nodeOf(flowGraphId, 'TEST-ONLY 张三'), x: 420, y: 450 },
    ],
  });
  human({
    type: 'upsert_graph_group', graph_id: equityGraphId, title: '实际控制结构', color: '#dbe7df',
    x: 50, y: 90, width: 600, height: 180,
    entity_ids: [entityId('TEST-ONLY 张三'), entityId('TEST-ONLY 丙投资合伙企业')],
  });
  human({
    type: 'upsert_graph_group', graph_id: flowGraphId, title: '资金往来', color: '#f0e4d2',
    x: 370, y: 100, width: 640, height: 170,
    entity_ids: [entityId('TEST-ONLY 乙公司'), entityId('TEST-ONLY 丙投资合伙企业')],
  });

  // 5. 证据目录（唯一编号来源）
  const group1 = human({
    type: 'add_evidence_group', title: '第一组：主体与股权', purpose: '证明主体资格与股权结构',
  }).evidence.groups.find((group) => group.title === '第一组：主体与股权')!.id;
  human({ type: 'add_evidence_item', group_id: group1, title: 'TEST-ONLY 公司章程', purpose: '证明甲公司主体资格与注册资本' });
  human({
    type: 'add_evidence_binding', item_id: itemId('TEST-ONLY 公司章程'),
    material_id: 'mat-registry', page_start: 1, page_end: 6,
  });
  human({ type: 'add_evidence_item', group_id: group1, title: 'TEST-ONLY 股权转让协议', purpose: '证明丙投资受让 45% 股权' });
  human({
    type: 'add_evidence_binding', item_id: itemId('TEST-ONLY 股权转让协议'),
    material_id: 'mat-contract', page_start: 1, page_end: 12,
  });
  const group2 = human({
    type: 'add_evidence_group', title: '第二组：履约与资金', purpose: '证明货款支付与担保',
  }).evidence.groups.find((group) => group.title === '第二组：履约与资金')!.id;
  human({ type: 'add_evidence_item', group_id: group2, title: 'TEST-ONLY 银行流水', purpose: '证明付款与欠款金额' });
  human({
    type: 'add_evidence_binding', item_id: itemId('TEST-ONLY 银行流水'),
    material_id: 'mat-bank', page_start: 1, page_end: 8,
  });
  human({ type: 'add_evidence_item', group_id: group2, title: 'TEST-ONLY 股东会决议', purpose: '证明担保意思形成' });
  human({
    type: 'add_evidence_binding', item_id: itemId('TEST-ONLY 股东会决议'),
    material_id: 'mat-board', page_start: 1, page_end: 4,
  });
  human({ type: 'add_evidence_group', title: '第三组：沟通记录', purpose: '证明对方承认欠款与担保' });
  human({
    type: 'add_evidence_item', group_id: groupId('第三组：沟通记录'),
    title: 'TEST-ONLY 微信记录', purpose: '证明对方确认担保与欠款',
  });
  human({
    type: 'add_evidence_binding', item_id: itemId('TEST-ONLY 微信记录'),
    material_id: 'mat-chat', page_start: 1, page_end: 3,
  });

  // 6. 已冻结旧快照（方案沟通），随后目录重排 → 快照编号漂移
  human({
    type: 'create_snapshot', graph_id: equityGraphId, stage_label: '方案沟通',
    note: '第一次向客户说明主体结构与持股关系；资金部分尚未补齐。',
    progress_node_id: 'pn-pretrial',
  });
  const snapshot = executor.getSnapshot().snapshots.find((item) => item.graph_id === equityGraphId)!;
  human({
    type: 'add_snapshot_consent', snapshot_id: snapshot.id, action: 'confirm',
    consenter: '客户 王总', consented_at: '2026-11-18T15:30', channel: 'wechat',
    source_detail: '微信语音确认，并发送截图「结构没问题，资金部分再看」。',
    evidence_material_id: 'mat-chat',
    note: '客户部分同意：仅确认主体与持股结构，资金往来部分待补。',
  });
  human({
    type: 'move_evidence_item', item_id: itemId('TEST-ONLY 股权转让协议'),
    to_group_id: group1, before_item_id: itemId('TEST-ONLY 公司章程'),
  });

  // 7. Agent 待确认：给担保补出处并升级为有证据（两图共享）
  agent({
    type: 'add_source', relation_id: relGuarantee, ref: { segment_id: 'seg-chat-2' },
    proves: '微信中乙公司法定代表人承认担保', request_key: 'agent-source-guarantee',
  });
  agent({
    type: 'update_relation', relation_id: relGuarantee,
    expected_revision: revisionOfRelation(relGuarantee), changes: { credibility: 'evidenced' },
  });
  // 8. Agent 待确认：新增主体与关系（仅第二张图）
  agent({
    type: 'add_entity', kind: 'org', name: 'TEST-ONLY 丁物流有限公司',
    roles: ['outsider'], note: '承运人，合同相对方待核。', request_key: 'agent-entity-ding',
  });
  agent({
    type: 'add_relation', from_entity_id: entityId('TEST-ONLY 丁物流有限公司'),
    to_entity_id: entityId('TEST-ONLY 乙公司'), kind: 'contract', label: '运输合同相对方',
    credibility: 'suspected', directed: true, graph_id: flowGraphId,
    note: 'Agent 从运单中识别，待人工确认。', request_key: 'agent-relation-ding',
  });
  const dingNode = executor.getSnapshot().nodes.find((node) => node.graph_id === flowGraphId
    && node.entity_id === entityId('TEST-ONLY 丁物流有限公司'));
  if (dingNode) {
    human({
      type: 'save_layout', graph_id: flowGraphId,
      nodes: [{ node_id: dingNode.id, x: 760, y: 450 }],
    });
  }

  return executor;
}
