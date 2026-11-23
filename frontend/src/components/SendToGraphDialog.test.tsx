/**
 * Wave 1B 送入关系图弹窗及入口测试（TEST-ONLY 合成数据，不连后端）。
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    graphApi: {
      ...actual.graphApi,
      rules: vi.fn(),
      workspace: vi.fn(),
      addRelation: vi.fn(),
      addSource: vi.fn(),
      updateRelation: vi.fn(),
    },
    graphAssistApi: {
      ...actual.graphAssistApi,
      segmentHints: vi.fn(),
    },
    searchApi: {
      ...actual.searchApi,
      readSource: vi.fn(),
    },
    annotationsApi: {
      ...actual.annotationsApi,
      list: vi.fn(),
    },
  };
});

import { ApiError } from '../api/client';
import { annotationsApi, graphApi, graphAssistApi, searchApi } from '../api/endpoints';
import type { GraphWorkspaceOut, SegmentHintsOut } from '../api/endpoints';
import { AnnotationPanel } from './Annotations';
import { SourcePanel } from './SearchPanel';
import { SendToGraphDialog } from './SendToGraphDialog';
import { SourcePreview } from './SourcePreview';

const rulesMock = vi.mocked(graphApi.rules);
const workspaceMock = vi.mocked(graphApi.workspace);
const addRelationMock = vi.mocked(graphApi.addRelation);
const addSourceMock = vi.mocked(graphApi.addSource);
const updateRelationMock = vi.mocked(graphApi.updateRelation);
const segmentHintsMock = vi.mocked(graphAssistApi.segmentHints);
const readSourceMock = vi.mocked(searchApi.readSource);
const annotationsListMock = vi.mocked(annotationsApi.list);

const CASE_ID = 'case-send-1';

function buildWorkspace(overrides: Partial<GraphWorkspaceOut> = {}): GraphWorkspaceOut {
  return {
    case_id: CASE_ID,
    case_title: 'TEST-ONLY 甲乙借款纠纷',
    archived: false,
    seq: 2,
    seqs: { graph: 2, evidence: 0 },
    entities: [
      {
        id: 'ent-1', case_id: CASE_ID, kind: 'person', name: '张三', roles: [],
        note: null, created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
      },
      {
        id: 'ent-2', case_id: CASE_ID, kind: 'org', name: '甲公司', roles: [],
        note: null, created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
      },
      {
        id: 'ent-3', case_id: CASE_ID, kind: 'org', name: '乙公司', roles: [],
        note: null, created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
      },
    ],
    aliases: [],
    relations: [],
    sources: [],
    graphs: [
      {
        id: 'graph-removed', case_id: CASE_ID, title: '已删图', description: null, position: 0,
        created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: '2026-11-21T00:00:00Z', revision: 0,
        created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-21T00:00:00Z',
      },
      {
        id: 'graph-1', case_id: CASE_ID, title: '主关系图', description: null, position: 1,
        created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0,
        created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
      },
      {
        id: 'graph-2', case_id: CASE_ID, title: '资金流向图', description: null, position: 2,
        created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0,
        created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
      },
    ],
    nodes: [],
    edges: [],
    groups: [],
    snapshots: [],
    consents: [],
    evidence: {
      case_id: CASE_ID, seq: 0, groups: [], items: [], bindings: [],
      materials: [], numbers: {}, group_numbers: {},
      pending_changes: [], changes: [], last_change_id: null, notice: null,
    },
    labels: {},
    pending_changes: [],
    changes: [],
    last_change_id: null,
    notice: null,
    ...overrides,
  };
}

function buildHints(): SegmentHintsOut {
  return {
    fragment: {
      location: {
        source_ref: 'seg-1',
        material_id: 'mat-1',
        file_version_id: 'fv-1',
        parse_run_id: 'run-1',
        locator: { page: 2 },
        text: '2026年03月15日，甲公司向乙公司转账 500000 元。',
        method: 'native',
      },
      citation: {
        labels: ['证据1-1'],
        text: '证据1-1 第2页',
        material_id: 'mat-1',
        material_title: '银行回单.pdf',
        page: 2,
        text_excerpt: '甲公司向乙公司转账',
        source_ref: 'seg-1',
        file_version_id: 'fv-1',
      },
      exact_values: [
        {
          kind: 'amount',
          raw_text: '500000元',
          canonical_value: { value: '500000', currency: 'CNY' },
          amount: '500000',
          currency: 'CNY',
        },
        {
          kind: 'date',
          raw_text: '2026年03月15日',
          canonical_value: { date: '2026-03-15' },
          amount: null,
          currency: null,
        },
      ],
      match_terms: ['甲公司', '乙公司'],
    },
    entities: [
      { entity_id: 'ent-2', name: '甲公司', match_terms: ['甲公司'] },
      { entity_id: 'ent-3', name: '乙公司', match_terms: ['乙公司'] },
    ],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  rulesMock.mockResolvedValue({ rules: {} });
  workspaceMock.mockResolvedValue(buildWorkspace());
  segmentHintsMock.mockResolvedValue(buildHints());
});

afterEach(cleanup);

describe('SendToGraphDialog', () => {
  it('预填首个有效图、匹配主体、金额、日期与证明要点，并按三步法提交「有证据」关系', async () => {
    const user = userEvent.setup();
    const createdWs = buildWorkspace({
      relations: [{
        id: 'rel-new', case_id: CASE_ID, from_entity_id: 'ent-2', to_entity_id: 'ent-3',
        directed: true, kind: 'fund_flow', label: null, credibility: 'suspected',
        attrs: { share_pct: null, amount_cents: 50000000, currency: 'CNY', date_start: '2026-03-15', date_end: null },
        note: null, created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0, created_at: '2026-11-22T00:00:00Z', updated_at: '2026-11-22T00:00:00Z',
      }],
      last_change_id: 'chg-1',
      changes: [{
        id: 'chg-1', case_id: CASE_ID, seq: 3, command: 'add_relation',
        actor: 'human', entry: 'local-web',
        diff: [{ target: 'relation', id: 'rel-new', before: null, after: { id: 'rel-new' }, before_rev: null, after_rev: 0 }],
        review_state: 'none', reviewed_at: null, reverted_by: null, created_at: '2026-11-22T00:00:00Z',
      }],
    });
    const sourcedWs = buildWorkspace({
      relations: createdWs.relations,
    });
    const finalWs = buildWorkspace({
      relations: [{ ...createdWs.relations[0], credibility: 'evidenced', revision: 1 }],
    });

    addRelationMock.mockResolvedValue(createdWs);
    addSourceMock.mockResolvedValue(sourcedWs);
    updateRelationMock.mockResolvedValue(finalWs);

    const onClose = vi.fn();
    const onCreated = vi.fn();

    render(
      <SendToGraphDialog
        caseId={CASE_ID}
        segmentId="seg-1"
        excerpt="2026年03月15日，甲公司向乙公司转账 500000 元。"
        onClose={onClose}
        onCreated={onCreated}
      />,
    );

    const dialog = screen.getByRole('dialog', { name: '送入关系图' });
    await within(dialog).findByText(/已识别本段主体：甲公司、乙公司/);

    expect((within(dialog).getByLabelText('目标关系图') as HTMLSelectElement).value).toBe('graph-1');
    expect((within(dialog).getByLabelText('起点主体') as HTMLSelectElement).value).toBe('ent-2');
    expect((within(dialog).getByLabelText('终点主体') as HTMLSelectElement).value).toBe('ent-3');
    expect((within(dialog).getByLabelText('金额（元，可选）') as HTMLInputElement).value).toBe('500000');
    expect((within(dialog).getByLabelText('发生日期（可选）') as HTMLInputElement).value).toBe('2026-03-15');
    expect((within(dialog).getByLabelText('证明要点（可选）') as HTMLTextAreaElement).value)
      .toContain('甲公司向乙公司转账');

    await user.selectOptions(within(dialog).getByLabelText('关系类型'), 'fund_flow');
    await user.click(within(dialog).getByRole('button', { name: '送入关系图' }));

    await waitFor(() => expect(addRelationMock).toHaveBeenCalledTimes(1));
    expect(addRelationMock).toHaveBeenCalledWith(CASE_ID, expect.objectContaining({
      from_entity_id: 'ent-2',
      to_entity_id: 'ent-3',
      kind: 'fund_flow',
      credibility: 'suspected',
      graph_id: 'graph-1',
      attrs: expect.objectContaining({
        amount_cents: 50000000,
        currency: 'CNY',
        date_start: '2026-03-15',
      }),
    }));
    expect(addSourceMock).toHaveBeenCalledWith(CASE_ID, expect.objectContaining({
      relation_id: 'rel-new',
      ref: { segment_id: 'seg-1', material_id: null, annotation_id: null },
    }));
    expect(updateRelationMock).toHaveBeenCalledWith(CASE_ID, 'rel-new', {
      expected_revision: 0,
      changes: { credibility: 'evidenced' },
    });
    expect(onCreated).toHaveBeenCalledWith(finalWs);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('标注模式直接用 annotation_id 且选「待查 / 存疑」时不调用 updateRelation', async () => {
    const user = userEvent.setup();
    const createdWs = buildWorkspace({
      relations: [{
        id: 'rel-ann', case_id: CASE_ID, from_entity_id: 'ent-1', to_entity_id: 'ent-2',
        directed: true, kind: 'equity_control', label: null, credibility: 'suspected',
        attrs: { share_pct: null, amount_cents: null, currency: null, date_start: null, date_end: null },
        note: null, created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0, created_at: '2026-11-22T00:00:00Z', updated_at: '2026-11-22T00:00:00Z',
      }],
    });
    addRelationMock.mockResolvedValue(createdWs);
    addSourceMock.mockResolvedValue(createdWs);

    const onClose = vi.fn();
    render(
      <SendToGraphDialog
        caseId={CASE_ID}
        annotationId="ann-1"
        excerpt="张三系甲公司实控人"
        onClose={onClose}
      />,
    );

    const dialog = screen.getByRole('dialog', { name: '送入关系图' });
    await within(dialog).findByLabelText('起点主体');
    expect(segmentHintsMock).not.toHaveBeenCalled();

    await user.selectOptions(within(dialog).getByLabelText('可信状态'), 'suspected');
    await user.click(within(dialog).getByRole('button', { name: '送入关系图' }));

    await waitFor(() => expect(addSourceMock).toHaveBeenCalledTimes(1));
    expect(addSourceMock).toHaveBeenCalledWith(CASE_ID, expect.objectContaining({
      relation_id: 'rel-ann',
      ref: { segment_id: null, material_id: null, annotation_id: 'ann-1' },
    }));
    expect(updateRelationMock).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('归档案件禁用提交、同主体拦截、输入法组词回车不提交，重复关系错误走 describeError', async () => {
    const user = userEvent.setup();
    workspaceMock.mockResolvedValueOnce(buildWorkspace({ archived: true }));
    const { unmount } = render(
      <SendToGraphDialog caseId={CASE_ID} segmentId="seg-1" onClose={vi.fn()} />,
    );
    const archivedDialog = screen.getByRole('dialog', { name: '送入关系图' });
    expect(await within(archivedDialog).findByText(/本案已归档/)).toBeTruthy();
    expect((within(archivedDialog).getByRole('button', { name: '送入关系图' }) as HTMLButtonElement).disabled)
      .toBe(true);
    unmount();

    addRelationMock.mockRejectedValueOnce(new ApiError(400, {
      code: 'invalid_input',
      message: '这两个主体之间已有同类关系',
      details: {},
      request_id: 'req-1',
    }));
    render(<SendToGraphDialog caseId={CASE_ID} segmentId="seg-1" onClose={vi.fn()} />);
    const dialog = screen.getByRole('dialog', { name: '送入关系图' });
    await within(dialog).findByText(/已识别本段主体/);

    // 输入法组词回车不得触发提交
    const labelInput = within(dialog).getByLabelText('边标签（可选）');
    fireEvent.keyDown(labelInput, { key: 'Enter', isComposing: true, keyCode: 229 });
    expect(addRelationMock).not.toHaveBeenCalled();

    // 同一主体不允许提交
    await user.selectOptions(within(dialog).getByLabelText('终点主体'), 'ent-2');
    expect(within(dialog).getByText('起点主体与终点主体不能相同。')).toBeTruthy();
    expect((within(dialog).getByRole('button', { name: '送入关系图' }) as HTMLButtonElement).disabled)
      .toBe(true);

    // 改回不同主体后触发服务端报错 -> describeError 展示
    await user.selectOptions(within(dialog).getByLabelText('终点主体'), 'ent-3');
    await user.click(within(dialog).getByRole('button', { name: '送入关系图' }));
    const alert = await within(dialog).findByRole('alert');
    expect(alert.textContent).toContain('输入内容不合法，请检查后重试。');
  });

  it('SourcePreview、SearchPanel 与 Annotations 均挂载送入关系图入口且受归档状态约束', async () => {
    const user = userEvent.setup();

    // 1. SourcePreview 带 segmentId 时出现按钮，点击可打开 SendToGraphDialog
    const preview = render(
      <SourcePreview
        caseId={CASE_ID}
        versionId="fv-1"
        name="合同.pdf"
        page={1}
        defaultOpen
        segmentId="seg-1"
      />,
    );
    await user.click(screen.getByRole('button', { name: '送入关系图' }));
    expect(await screen.findByRole('dialog', { name: '送入关系图' })).toBeTruthy();
    preview.unmount();

    // 2. SourcePanel（SearchPanel）在加载完成后出现按钮；归档时禁用
    readSourceMock.mockResolvedValue({
      source_ref: 'seg-1', material_id: 'mat-1', file_version_id: 'fv-1',
      parse_run_id: 'run-1', locator: { page: 1 }, text: '测试原文片段',
      method: 'native', acquisition: null, display_name: '合同.pdf',
      source_label: null, relative_source: null, is_original: true,
      context_before: '', context_after: '',
    });
    const searchView = render(
      <MemoryRouter>
        <SourcePanel caseId={CASE_ID} sourceRef="seg-1" archived />
      </MemoryRouter>,
    );
    const sendBtn = await screen.findByRole('button', { name: '送入关系图' });
    expect((sendBtn as HTMLButtonElement).disabled).toBe(true);
    searchView.unmount();

    // 3. Annotations 详情展示按钮并传入 annotationId
    annotationsListMock.mockResolvedValue({
      items: [{
        annotation_id: 'ann-9',
        case_id: CASE_ID,
        file_version_id: 'fv-1',
        display_name: '回单.pdf',
        page: 1,
        kind: 'interpretation',
        author: 'human',
        body: '备注内容',
        reviewed_text: null,
        revision: 0,
        created_at: '2026-11-22T00:00:00Z',
      }],
      offset: 0,
      limit: 30,
      total: 1,
    });
    render(<AnnotationPanel caseId={CASE_ID} />);
    await user.click(await screen.findByRole('button', { name: /回单\.pdf/ }));
    await user.click(screen.getByRole('button', { name: '送入关系图' }));
    expect(await screen.findByRole('dialog', { name: '送入关系图' })).toBeTruthy();
  });

  it('三步写入局部失败后保留进度且重试不重复 addRelation', async () => {
    const user = userEvent.setup();
    const createdWs = buildWorkspace({
      relations: [{
        id: 'rel-resume', case_id: CASE_ID, from_entity_id: 'ent-2', to_entity_id: 'ent-3',
        directed: true, kind: 'fund_flow', label: null, credibility: 'suspected',
        attrs: { share_pct: null, amount_cents: null, currency: 'CNY', date_start: null, date_end: null },
        note: null, created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0, created_at: '2026-11-22T00:00:00Z', updated_at: '2026-11-22T00:00:00Z',
      }],
      last_change_id: 'chg-r',
      changes: [{
        id: 'chg-r', case_id: CASE_ID, seq: 3, command: 'add_relation',
        actor: 'human', entry: 'local-web',
        diff: [{ target: 'relation', id: 'rel-resume', before: null, after: { id: 'rel-resume' }, before_rev: null, after_rev: 0 }],
        review_state: 'none', reviewed_at: null, reverted_by: null, created_at: '2026-11-22T00:00:00Z',
      }],
    });
    const sourcedWs = buildWorkspace({ relations: createdWs.relations });
    const finalWs = buildWorkspace({
      relations: [{ ...createdWs.relations[0], credibility: 'evidenced', revision: 1 }],
    });

    addRelationMock.mockResolvedValueOnce(createdWs);
    addSourceMock
      .mockRejectedValueOnce(new Error('source timeout'))
      .mockResolvedValueOnce(sourcedWs);
    updateRelationMock
      .mockRejectedValueOnce(new Error('revision mismatch'))
      .mockResolvedValueOnce(finalWs);

    const onClose = vi.fn();
    const onCreated = vi.fn();
    render(
      <SendToGraphDialog
        caseId={CASE_ID}
        segmentId="seg-1"
        excerpt="借款 100 万元"
        onClose={onClose}
        onCreated={onCreated}
      />,
    );

    const dialog = screen.getByRole('dialog', { name: '送入关系图' });
    await within(dialog).findByText(/已识别本段主体/);

    // 1st click: addRelation succeeds, addSource fails
    await user.click(within(dialog).getByRole('button', { name: '送入关系图' }));
    const err1 = await within(dialog).findByRole('alert');
    expect(err1.textContent).toContain('挂载出处失败');
    expect(addRelationMock).toHaveBeenCalledTimes(1);

    // 2nd click: skips addRelation, retries addSource -> succeeds, then updateRelation fails
    await user.click(within(dialog).getByRole('button', { name: '送入关系图' }));
    const err2 = await within(dialog).findByRole('alert');
    expect(err2.textContent).toContain('升级「有证据」状态失败');
    expect(addRelationMock).toHaveBeenCalledTimes(1);
    expect(addSourceMock).toHaveBeenCalledTimes(2);

    // 3rd click: skips both addRelation and addSource, retries updateRelation -> succeeds
    await user.click(within(dialog).getByRole('button', { name: '送入关系图' }));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(addRelationMock).toHaveBeenCalledTimes(1);
    expect(addSourceMock).toHaveBeenCalledTimes(2);
    expect(updateRelationMock).toHaveBeenCalledTimes(2);
    expect(onCreated).toHaveBeenCalledWith(finalWs);
  });
});
