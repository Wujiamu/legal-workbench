/**
 * Wave 1C 防漏审计抽屉（覆盖缺口 / 孤儿机构 / 强指纹碰撞）及 Wave 1B Inspector 共现秒查测试（TEST-ONLY 合成数据）。
 */
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    graphAssistApi: {
      ...actual.graphAssistApi,
      audit: vi.fn(),
      cooccurrence: vi.fn(),
    },
  };
});

import { graphAssistApi } from '../api/endpoints';
import type { CooccurrenceOut, GraphAuditOut } from '../api/endpoints';
import { GraphAuditDrawer } from './GraphAuditDrawer';
import { InspectorPanel } from './InspectorPanel';
import { defaultRules } from './rules';
import type { GraphSnapshot } from './types';

const auditMock = vi.mocked(graphAssistApi.audit);
const cooccurrenceMock = vi.mocked(graphAssistApi.cooccurrence);

const CASE_ID = 'case-audit-1';
const GRAPH_ID = 'graph-audit-1';

function buildAudit(overrides: Partial<GraphAuditOut> = {}): GraphAuditOut {
  return {
    case_id: CASE_ID,
    graph_id: GRAPH_ID,
    coverage: {
      case_id: CASE_ID,
      graph_id: GRAPH_ID,
      categories: ['合同', '银行流水'],
      totals: {
        total: 4,
        referenced: 2,
        parsed_unused: 1,
        partial_or_failed: 1,
        pending_or_unsupported: 0,
      },
      cells: [
        { category: '合同', status: 'referenced', count: 2, material_ids: ['m-1', 'm-2'] },
        { category: '合同', status: 'parsed_unused', count: 1, material_ids: ['m-3'] },
        { category: '银行流水', status: 'partial_or_failed', count: 1, material_ids: ['m-4'] },
      ],
      materials: [
        {
          material_id: 'm-1', display_name: 'TEST-ONLY 借款合同.pdf', category: '合同',
          status: 'referenced', file_version_id: 'fv-1',
          coverage: { complete: 2, partial: 0, failed: 0, pending: 0, unsupported: 0 },
          reference_kinds: ['graph'],
        },
        {
          material_id: 'm-3', display_name: 'TEST-ONLY 补充承诺函.pdf', category: '合同',
          status: 'parsed_unused', file_version_id: 'fv-3',
          coverage: { complete: 1, partial: 0, failed: 0, pending: 0, unsupported: 0 },
          reference_kinds: [],
        },
        {
          material_id: 'm-4', display_name: 'TEST-ONLY 模糊流水.pdf', category: '银行流水',
          status: 'partial_or_failed', file_version_id: 'fv-4',
          coverage: { complete: 1, partial: 1, failed: 1, pending: 0, unsupported: 0 },
          reference_kinds: [],
        },
      ],
    },
    orphan_orgs: [
      {
        name: 'TEST-ONLY 关联保理有限公司',
        mention_count: 5,
        material_count: 2,
        material_ids: ['m-3', 'm-4'],
        sample_segments: [
          {
            segment_id: 'seg-org-1',
            material_id: 'm-3',
            material_title: 'TEST-ONLY 补充承诺函.pdf',
            file_version_id: 'fv-3',
            page: 2,
            excerpt: '由 TEST-ONLY 关联保理有限公司 提供连带担保。',
          },
        ],
      },
    ],
    identifier_bridges: [
      {
        kind: 'bank_account',
        value: '6222021001122334455',
        material_count: 2,
        entity_ids: ['ent-a', 'ent-b'],
        occurrences: [
          {
            segment_id: 'seg-br-1',
            material_id: 'm-1',
            material_title: 'TEST-ONLY 借款合同.pdf',
            file_version_id: 'fv-1',
            page: 3,
            excerpt: '甲方指定收款账号 6222021001122334455',
            matched_entity_ids: ['ent-a'],
            matched_entity_names: ['甲公司'],
          },
          {
            segment_id: 'seg-br-2',
            material_id: 'm-4',
            material_title: 'TEST-ONLY 模糊流水.pdf',
            file_version_id: 'fv-4',
            page: 1,
            excerpt: '转入户名乙公司 账号 6222021001122334455',
            matched_entity_ids: ['ent-b'],
            matched_entity_names: ['乙公司'],
          },
        ],
      },
    ],
    ...overrides,
  };
}

function buildSnapshot(): GraphSnapshot {
  return {
    case_id: CASE_ID,
    case_title: 'TEST-ONLY 审计案',
    archived: false,
    seq: 1,
    seqs: { graph: 1, evidence: 0 },
    entities: [
      {
        id: 'ent-a', case_id: CASE_ID, kind: 'org', name: '甲公司', roles: ['plaintiff'],
        note: null, created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
      },
      {
        id: 'ent-b', case_id: CASE_ID, kind: 'org', name: '乙公司', roles: ['defendant'],
        note: null, created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
      },
      {
        id: 'ent-c', case_id: CASE_ID, kind: 'org', name: '丙公司', roles: [],
        note: null, created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
      },
    ],
    aliases: [],
    relations: [
      {
        id: 'rel-1', case_id: CASE_ID, from_entity_id: 'ent-a', to_entity_id: 'ent-b',
        directed: true, kind: 'fund_flow', label: '资金往来', credibility: 'suspected',
        attrs: {}, note: null, created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
      },
    ],
    sources: [],
    graphs: [
      {
        id: GRAPH_ID, case_id: CASE_ID, title: '主图', description: null, position: 0,
        created_by: 'human', updated_by: 'human', request_key: null, removed_at: null,
        revision: 0, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
      },
    ],
    nodes: [],
    edges: [],
    groups: [],
    snapshots: [],
    consents: [],
    evidence: { groups: [], items: [], bindings: [] },
    materials: [],
    segments: [],
    annotations: [],
    progress_nodes: [],
    labels: {},
    evidence_numbers: {},
    pending_changes: [],
    changes: [],
    last_change_id: null,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  auditMock.mockResolvedValue(buildAudit());
});

afterEach(cleanup);

describe('GraphAuditDrawer', () => {
  it('关闭时不发起请求；打开后加载审计并支持三个页签与操作回调', async () => {
    const user = userEvent.setup();
    const onOpenSourcePreview = vi.fn();
    const onAddOrphanEntity = vi.fn().mockResolvedValue(undefined);
    const onCreateAffiliateRelation = vi.fn();

    const { rerender } = render(
      <GraphAuditDrawer
        open={false}
        onClose={vi.fn()}
        caseId={CASE_ID}
        graphId={GRAPH_ID}
        readOnly={false}
        onOpenSourcePreview={onOpenSourcePreview}
        onAddOrphanEntity={onAddOrphanEntity}
        onCreateAffiliateRelation={onCreateAffiliateRelation}
      />,
    );
    expect(auditMock).not.toHaveBeenCalled();

    rerender(
      <GraphAuditDrawer
        open
        onClose={vi.fn()}
        caseId={CASE_ID}
        graphId={GRAPH_ID}
        readOnly={false}
        onOpenSourcePreview={onOpenSourcePreview}
        onAddOrphanEntity={onAddOrphanEntity}
        onCreateAffiliateRelation={onCreateAffiliateRelation}
      />,
    );

    const dialog = await screen.findByRole('dialog', { name: '防漏审计' });
    // 原生 dialog 使用 showModal，不重复写 role="dialog" 或 aria-modal。
    expect(dialog.getAttribute('role')).toBeNull();
    expect(dialog.getAttribute('aria-modal')).toBeNull();
    expect(auditMock).toHaveBeenCalledWith(CASE_ID, { graph_id: GRAPH_ID }, expect.any(AbortSignal));

    // Tab 1: 覆盖缺口
    expect(await within(dialog).findByText(/共 4 份 · 已引用 2 · 未引用 1 · 异常\/告警 1 · 待解析 0/)).toBeTruthy();
    expect(within(dialog).getByText('TEST-ONLY 补充承诺函.pdf')).toBeTruthy();
    expect(within(dialog).getByText('TEST-ONLY 模糊流水.pdf')).toBeTruthy();
    expect(within(dialog).queryByText('TEST-ONLY 借款合同.pdf')).toBeNull();

    await user.click(within(dialog).getAllByRole('button', { name: '查看原文' })[0]);
    expect(onOpenSourcePreview).toHaveBeenCalledWith({
      fileVersionId: 'fv-3',
      title: 'TEST-ONLY 补充承诺函.pdf',
    });

    // Tab 2: 孤儿机构
    await user.click(within(dialog).getByRole('tab', { name: /孤儿机构/ }));
    expect(within(dialog).getByText('TEST-ONLY 关联保理有限公司')).toBeTruthy();
    expect(within(dialog).getByText(/提及 5 次 · 涉及 2 份材料/)).toBeTruthy();

    await user.click(within(dialog).getByRole('button', { name: '查看原文' }));
    expect(onOpenSourcePreview).toHaveBeenLastCalledWith({
      fileVersionId: 'fv-3',
      title: 'TEST-ONLY 补充承诺函.pdf',
      page: 2,
      segmentId: 'seg-org-1',
    });

    await user.click(within(dialog).getByRole('button', { name: '添加为案件主体' }));
    await waitFor(() => expect(onAddOrphanEntity).toHaveBeenCalledWith('TEST-ONLY 关联保理有限公司', 'seg-org-1'));
    await waitFor(() => expect(auditMock).toHaveBeenCalledTimes(2));

    // Tab 3: 强指纹碰撞
    await user.click(within(dialog).getByRole('tab', { name: /强指纹碰撞/ }));
    expect(within(dialog).getByText(/银行账号：6222021001122334455/)).toBeTruthy();
    expect(within(dialog).getByText('匹配主体：甲公司')).toBeTruthy();
    expect(within(dialog).getByText('匹配主体：乙公司')).toBeTruthy();

    await user.click(within(dialog).getByRole('button', { name: '创建关联关系' }));
    expect(onCreateAffiliateRelation).toHaveBeenCalledWith({
      fromEntityId: 'ent-a',
      toEntityId: 'ent-b',
      label: '共用银行账号 6222021001122334455',
      segmentIds: ['seg-br-1', 'seg-br-2'],
    });
  });

  it('只读模式禁用写按钮，切换 graphId 时丢弃旧响应', async () => {
    const user = userEvent.setup();
    const slowG1 = deferred<GraphAuditOut>();
    const fastG2 = deferred<GraphAuditOut>();
    auditMock.mockImplementation((_caseId, params) => (
      params?.graph_id === 'g-1' ? slowG1.promise : fastG2.promise
    ));

    const { rerender } = render(
      <GraphAuditDrawer
        open
        onClose={vi.fn()}
        caseId={CASE_ID}
        graphId="g-1"
        readOnly
        onOpenSourcePreview={vi.fn()}
        onAddOrphanEntity={vi.fn()}
        onCreateAffiliateRelation={vi.fn()}
      />,
    );

    rerender(
      <GraphAuditDrawer
        open
        onClose={vi.fn()}
        caseId={CASE_ID}
        graphId="g-2"
        readOnly
        onOpenSourcePreview={vi.fn()}
        onAddOrphanEntity={vi.fn()}
        onCreateAffiliateRelation={vi.fn()}
      />,
    );

    await act(async () => {
      fastG2.resolve(buildAudit({
        graph_id: 'g-2',
        orphan_orgs: [{
          name: '新图孤儿机构有限公司', mention_count: 2, material_count: 1,
          material_ids: ['m-3'], sample_segments: [],
        }],
      }));
    });
    expect(await screen.findByText(/孤儿机构（1）/)).toBeTruthy();

    await act(async () => {
      slowG1.resolve(buildAudit({
        graph_id: 'g-1',
        orphan_orgs: [{
          name: '旧图孤儿机构有限公司', mention_count: 9, material_count: 3,
          material_ids: ['m-3'], sample_segments: [],
        }],
      }));
    });

    await user.click(screen.getByRole('tab', { name: /孤儿机构/ }));
    expect(screen.getByText('新图孤儿机构有限公司')).toBeTruthy();
    expect(screen.queryByText('旧图孤儿机构有限公司')).toBeNull();
    expect((screen.getByRole('button', { name: '添加为案件主体' }) as HTMLButtonElement).disabled).toBe(true);

    await user.click(screen.getByRole('tab', { name: /强指纹碰撞/ }));
    expect((screen.getByRole('button', { name: '创建关联关系' }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('InspectorPanel · 全案共现秒查', () => {
  const cooccurResult: CooccurrenceOut = {
    entity_a_terms: ['甲公司', '甲方'],
    entity_b_terms: ['乙公司', '乙方'],
    hits: [
      {
        kind: 'segment',
        fragments: [
          {
            location: {
              source_ref: 'seg-hit-1',
              material_id: 'mat-1',
              file_version_id: 'fv-1',
              parse_run_id: 'run-1',
              locator: { page: 5 },
              text: '2026年04月01日甲公司向乙公司付款 120000 元。',
              method: 'native',
            },
            citation: {
              labels: ['证据1-1'],
              text: '证据1-1 第5页',
              material_id: 'mat-1',
              material_title: '转账凭证.pdf',
              page: 5,
              text_excerpt: null,
              source_ref: 'seg-hit-1',
              file_version_id: 'fv-1',
            },
            exact_values: [
              {
                kind: 'amount',
                raw_text: '120000元',
                canonical_value: { value: '120000', currency: 'CNY' },
                amount: '120000',
                currency: 'CNY',
              },
              {
                kind: 'date',
                raw_text: '2026年04月01日',
                canonical_value: { date: '2026-04-01' },
                amount: null,
                currency: null,
              },
            ],
            match_terms: ['甲公司', '乙公司'],
          },
        ],
      },
      {
        kind: 'page',
        fragments: [
          {
            location: {
              source_ref: 'seg-hit-2',
              material_id: 'mat-2',
              file_version_id: 'fv-2',
              parse_run_id: 'run-2',
              locator: { page: 2 },
              text: '同页左侧提及丙公司。',
              method: 'native',
            },
            citation: {
              labels: [],
              text: '明细表.pdf（未编号）',
              material_id: 'mat-2',
              material_title: '明细表.pdf',
              page: 2,
              text_excerpt: null,
              source_ref: 'seg-hit-2',
              file_version_id: 'fv-2',
            },
            exact_values: [],
            match_terms: ['甲公司'],
          },
        ],
      },
    ],
  };

  it('选中关系时展示词条、命中标签、查看原件与「挂为关系出处」', async () => {
    const user = userEvent.setup();
    const ws = buildSnapshot();
    const run = vi.fn().mockResolvedValue({ ok: true, snapshot: ws });
    const onOpenSource = vi.fn();
    cooccurrenceMock.mockResolvedValue(cooccurResult);

    render(
      <InspectorPanel
        ws={ws}
        rules={defaultRules}
        graphId={GRAPH_ID}
        selection={{ type: 'relation', id: 'rel-1' }}
        readOnly={false}
        run={run}
        onSelect={vi.fn()}
        onOpenSource={vi.fn()}
        onEditEntity={vi.fn()}
        onEditRelation={vi.fn()}
        onDeleteEntity={vi.fn()}
        onDeleteRelation={vi.fn()}
        caseId={CASE_ID}
        onOpenCooccurrenceSource={onOpenSource}
      />,
    );

    await user.click(screen.getByRole('button', { name: '全案共现秒查' }));
    await waitFor(() => expect(cooccurrenceMock).toHaveBeenCalledWith(
      CASE_ID,
      { entity_a_id: 'ent-a', entity_b_id: 'ent-b', scope: 'originals' },
      expect.any(AbortSignal),
    ));

    expect(await screen.findByText(/甲公司 词条：甲公司、甲方/)).toBeTruthy();
    expect(screen.getByText(/乙公司 词条：乙公司、乙方/)).toBeTruthy();
    expect(screen.getByText('同段命中')).toBeTruthy();
    expect(screen.getByText('同页/同表命中')).toBeTruthy();
    expect(screen.getByText('金额：120000元')).toBeTruthy();
    expect(screen.getByText('日期：2026年04月01日')).toBeTruthy();

    await user.click(screen.getAllByRole('button', { name: '查看原件' })[0]);
    expect(onOpenSource).toHaveBeenCalledWith({
      fileVersionId: 'fv-1',
      title: '转账凭证.pdf',
      page: 5,
      segmentId: 'seg-hit-1',
    });

    await user.click(screen.getAllByRole('button', { name: '挂为关系出处' })[0]);
    expect(run).toHaveBeenCalledWith({
      type: 'add_source',
      relation_id: 'rel-1',
      ref: { segment_id: 'seg-hit-1' },
      proves: '2026年04月01日甲公司向乙公司付款 120000 元。',
    });
  });

  it('未选中对象且设置路径双主体时提供「以本段建关系」，切换目标时中止旧请求', async () => {
    const user = userEvent.setup();
    const ws = buildSnapshot();
    const onCreateRelation = vi.fn();
    const slowAB = deferred<CooccurrenceOut>();
    cooccurrenceMock.mockImplementation((_caseId, params) => (
      params.entity_b_id === 'ent-b' ? slowAB.promise : Promise.resolve(cooccurResult)
    ));

    const { rerender } = render(
      <InspectorPanel
        ws={ws}
        rules={defaultRules}
        graphId={GRAPH_ID}
        selection={null}
        readOnly={false}
        run={vi.fn()}
        onSelect={vi.fn()}
        onOpenSource={vi.fn()}
        onEditEntity={vi.fn()}
        onEditRelation={vi.fn()}
        onDeleteEntity={vi.fn()}
        onDeleteRelation={vi.fn()}
        caseId={CASE_ID}
        pathFrom="ent-a"
        pathTo="ent-b"
        onCreateRelationFromCooccurrence={onCreateRelation}
      />,
    );

    await user.click(screen.getByRole('button', { name: '全案共现秒查' }));
    const firstSignal = cooccurrenceMock.mock.calls[0]?.[2] as AbortSignal;
    expect(firstSignal.aborted).toBe(false);

    rerender(
      <InspectorPanel
        ws={ws}
        rules={defaultRules}
        graphId={GRAPH_ID}
        selection={null}
        readOnly={false}
        run={vi.fn()}
        onSelect={vi.fn()}
        onOpenSource={vi.fn()}
        onEditEntity={vi.fn()}
        onEditRelation={vi.fn()}
        onDeleteEntity={vi.fn()}
        onDeleteRelation={vi.fn()}
        caseId={CASE_ID}
        pathFrom="ent-a"
        pathTo="ent-c"
        onCreateRelationFromCooccurrence={onCreateRelation}
      />,
    );
    expect(firstSignal.aborted).toBe(true);

    await user.click(screen.getByRole('button', { name: '全案共现秒查' }));
    await screen.findByText('同段命中');
    await user.click(screen.getAllByRole('button', { name: '以本段建关系' })[0]);
    expect(onCreateRelation).toHaveBeenCalledWith({
      fromEntityId: 'ent-a',
      toEntityId: 'ent-c',
      segmentId: 'seg-hit-1',
      amount: '120000',
      date: '2026-04-01',
    });
  });

  it('共现检索加载中点击收起后立即解锁 loading，允许再次触发检索', async () => {
    const user = userEvent.setup();
    const ws = buildSnapshot();
    const pending = deferred<CooccurrenceOut>();
    cooccurrenceMock
      .mockImplementationOnce(() => pending.promise)
      .mockResolvedValueOnce(cooccurResult);

    render(
      <InspectorPanel
        ws={ws}
        rules={defaultRules}
        graphId={GRAPH_ID}
        selection={{ type: 'relation', id: 'rel-1' }}
        readOnly={false}
        run={vi.fn()}
        onSelect={vi.fn()}
        onOpenSource={vi.fn()}
        onEditEntity={vi.fn()}
        onEditRelation={vi.fn()}
        onDeleteEntity={vi.fn()}
        onDeleteRelation={vi.fn()}
        caseId={CASE_ID}
      />,
    );

    await user.click(screen.getByRole('button', { name: '全案共现秒查' }));
    const busyBtn = screen.getByRole('button', { name: '检索共现中…' }) as HTMLButtonElement;
    expect(busyBtn.disabled).toBe(true);

    await user.click(screen.getByRole('button', { name: '收起' }));
    const resetBtn = screen.getByRole('button', { name: '全案共现秒查' }) as HTMLButtonElement;
    expect(resetBtn.disabled).toBe(false);

    await user.click(resetBtn);
    await screen.findByText('同段命中');
    expect(cooccurrenceMock).toHaveBeenCalledTimes(2);
  });
});
