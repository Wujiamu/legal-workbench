import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    graphAssistApi: {
      ...actual.graphAssistApi,
      entityCandidates: vi.fn(),
    },
  };
});

import { graphAssistApi } from '../api/endpoints';
import type { EntityCandidateOut } from '../api/endpoints';
import { EntityDiscoveryDialog } from './EntityDiscoveryDialog';
import { defaultRules } from './rules';
import type { CaseEntity, CommandResult, GraphCommand, GraphSnapshot } from './types';

const entityCandidates = vi.mocked(graphAssistApi.entityCandidates);

const CASE_ID = 'case-disc-1';
const GRAPH_ID = 'graph-disc-1';

function makeEntity(id: string, name: string, kind = 'org'): CaseEntity {
  return {
    id,
    case_id: CASE_ID,
    kind,
    name,
    roles: [],
    note: null,
    created_by: 'human',
    updated_by: 'human',
    request_key: null,
    removed_at: null,
    revision: 0,
    created_at: '2026-11-22T00:00:00Z',
    updated_at: '2026-11-22T00:00:00Z',
  };
}

function makeWorkspace(entities: CaseEntity[] = []): GraphSnapshot {
  return {
    case_id: CASE_ID,
    case_title: '测试买卖合同纠纷',
    archived: false,
    seq: 1,
    seqs: { graph: 1, evidence: 0 },
    entities,
    aliases: [],
    relations: [],
    sources: [],
    graphs: [{
      id: GRAPH_ID,
      case_id: CASE_ID,
      title: '主图',
      description: null,
      position: 0,
      created_by: 'human',
      updated_by: 'human',
      request_key: null,
      removed_at: null,
      revision: 0,
      created_at: '2026-11-22T00:00:00Z',
      updated_at: '2026-11-22T00:00:00Z',
    }],
    nodes: [],
    edges: [],
    groups: [],
    snapshots: [],
    consents: [],
    evidence: { groups: [], items: [], bindings: [] },
    materials: [
      { id: 'mat-1', case_id: CASE_ID, title: '民事起诉状.pdf', kind: 'pdf', page_count: 5 },
      { id: 'mat-2', case_id: CASE_ID, title: '购销合同.pdf', kind: 'pdf', page_count: 8 },
    ],
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

function sampleResponse(): EntityCandidateOut {
  return {
    case_id: CASE_ID,
    candidates: [
      {
        name: '北京星辰科技有限公司',
        entity_kind: 'org',
        suggested_roles: ['plaintiff'],
        candidate_aliases: ['星辰科技', '甲方'],
        mention_count: 6,
        existing_entity_id: null,
        existing_entity_name: null,
        sources: [
          {
            segment_id: 'seg-101',
            material_id: 'mat-1',
            material_title: '民事起诉状.pdf',
            file_version_id: 'ver-101',
            page: 1,
            excerpt: '原告：北京星辰科技有限公司（以下简称“星辰科技”）。',
          },
        ],
      },
      {
        name: '上海蓝海贸易有限公司',
        entity_kind: 'org',
        suggested_roles: ['defendant'],
        candidate_aliases: ['蓝海贸易'],
        mention_count: 4,
        existing_entity_id: 'ent-existing-1',
        existing_entity_name: '上海蓝海贸易有限公司',
        sources: [
          {
            segment_id: 'seg-102',
            material_id: 'mat-2',
            material_title: '购销合同.pdf',
            file_version_id: 'ver-102',
            page: 2,
            excerpt: '乙方：上海蓝海贸易有限公司（简称蓝海贸易）。',
          },
        ],
      },
    ],
  };
}

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
});

describe('EntityDiscoveryDialog', () => {
  it('open=false 时不渲染弹窗且不发请求', () => {
    const run = vi.fn();
    render(
      <EntityDiscoveryDialog
        open={false}
        onClose={vi.fn()}
        caseId={CASE_ID}
        graphId={GRAPH_ID}
        ws={makeWorkspace()}
        rules={defaultRules}
        readOnly={false}
        run={run}
      />,
    );
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(entityCandidates).not.toHaveBeenCalled();
  });

  it('打开后加载候选表格、支持查看原文与按材料筛选', async () => {
    entityCandidates.mockResolvedValue(sampleResponse());
    const onOpenSourcePreview = vi.fn();
    const run = vi.fn();

    render(
      <EntityDiscoveryDialog
        open
        onClose={vi.fn()}
        caseId={CASE_ID}
        graphId={GRAPH_ID}
        ws={makeWorkspace([makeEntity('ent-existing-1', '上海蓝海贸易有限公司')])}
        rules={defaultRules}
        readOnly={false}
        run={run}
        onOpenSourcePreview={onOpenSourcePreview}
      />,
    );

    const dialog = await screen.findByRole('dialog', { name: '程序化初筛主体与别名消歧台' });
    expect(entityCandidates).toHaveBeenCalledWith(CASE_ID, [], expect.any(AbortSignal));

    const region = await within(dialog).findByRole('region', { name: /候选主体与别名清单/ });
    expect(region.getAttribute('tabindex')).toBe('0');
    const table = within(region).getByRole('table');
    expect(table.textContent).toContain('北京星辰科技有限公司');
    expect(table.textContent).toContain('6 次提及');
    expect(table.textContent).toContain('上海蓝海贸易有限公司');

    await userEvent.click(within(table).getAllByRole('button', { name: '查看原文' })[0]);
    expect(onOpenSourcePreview).toHaveBeenCalledWith({
      fileVersionId: 'ver-101',
      title: '民事起诉状.pdf',
      page: 1,
    });

    await userEvent.click(within(dialog).getByLabelText('购销合同.pdf'));
    await waitFor(() => {
      expect(entityCandidates).toHaveBeenLastCalledWith(CASE_ID, ['mat-2'], expect.any(AbortSignal));
    });
  });

  it('支持新建主体（改名+勾选别名+挂接出处）、合并已有主体及优雅忽略重复别名错误', async () => {
    entityCandidates.mockResolvedValue({
      case_id: CASE_ID,
      candidates: [
        sampleResponse().candidates[0],
        {
          name: '海蓝公司',
          entity_kind: 'org',
          suggested_roles: [],
          candidate_aliases: ['海蓝'],
          mention_count: 2,
          existing_entity_id: null,
          existing_entity_name: null,
          sources: [],
        },
        sampleResponse().candidates[1],
      ],
    });

    let currentWs = makeWorkspace([makeEntity('ent-existing-1', '上海蓝海贸易有限公司')]);
    const calls: GraphCommand[] = [];
    const run = vi.fn(async (cmd: GraphCommand): Promise<CommandResult> => {
      calls.push(cmd);
      if (cmd.type === 'add_entity') {
        const created = makeEntity('ent-new-9', cmd.name, cmd.kind);
        currentWs = { ...currentWs, seq: currentWs.seq + 1, entities: [...currentWs.entities, created] };
        return { ok: true, snapshot: currentWs };
      }
      if (cmd.type === 'add_alias' && cmd.alias === '甲方') {
        // 模拟重复别名错误：应被忽略，不阻断后续操作。
        return { ok: false, code: 'validation', message: '该别名已存在' };
      }
      return { ok: true, snapshot: { ...currentWs, seq: currentWs.seq + 1 } };
    });

    render(
      <EntityDiscoveryDialog
        open
        onClose={vi.fn()}
        caseId={CASE_ID}
        graphId={GRAPH_ID}
        ws={currentWs}
        rules={defaultRules}
        readOnly={false}
        run={run}
      />,
    );

    const dialog = await screen.findByRole('dialog');
    const tbody = within(dialog).getAllByRole('rowgroup')[1];
    const rows = within(tbody).getAllByRole('row');

    // 第一行：新建主体，修改名称并保留两个别名（其中“甲方”将触发重复别名并被优雅跳过）。
    const nameInput = within(rows[0]).getByLabelText('写入名称');
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, '北京星辰科技公司');

    // 第二行：选择合并为已有主体别名，目标选 ent-existing-1，取消勾选“海蓝”只留主名“海蓝公司”。
    await userEvent.selectOptions(within(rows[1]).getByLabelText('操作'), 'merge');
    await userEvent.selectOptions(within(rows[1]).getByLabelText('目标已有主体'), 'ent-existing-1');
    await userEvent.click(within(rows[1]).getByLabelText('海蓝'));

    // 第三行：默认 merge_existing，保留已勾选别名“蓝海贸易”。
    await userEvent.click(within(dialog).getByRole('button', { name: '批量写入关系图' }));

    await waitFor(() => {
      expect(within(dialog).getByText(/已完成批量写入：新建主体 1 个、新增别名 3 条、挂接出处 1 条/)).toBeTruthy();
    });

    expect(calls).toEqual([
      {
        type: 'add_entity',
        kind: 'org',
        name: '北京星辰科技公司',
        roles: ['plaintiff'],
        graph_id: GRAPH_ID,
      },
      { type: 'add_alias', entity_id: 'ent-new-9', alias: '星辰科技' },
      { type: 'add_alias', entity_id: 'ent-new-9', alias: '甲方' },
      { type: 'add_source', entity_id: 'ent-new-9', ref: { segment_id: 'seg-101' } },
      { type: 'add_alias', entity_id: 'ent-existing-1', alias: '海蓝公司' },
      { type: 'add_alias', entity_id: 'ent-existing-1', alias: '蓝海贸易' },
    ]);
  });

  it('只读归档禁用批量写入；中文输入法组词回车不触发表单提交', async () => {
    entityCandidates.mockResolvedValue({
      case_id: CASE_ID,
      candidates: [sampleResponse().candidates[0]],
    });
    const run = vi.fn();

    const { rerender } = render(
      <EntityDiscoveryDialog
        open
        onClose={vi.fn()}
        caseId={CASE_ID}
        graphId={GRAPH_ID}
        ws={makeWorkspace()}
        rules={defaultRules}
        readOnly
        run={run}
      />,
    );

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/本案已归档/)).toBeTruthy();
    const submitBtn = within(dialog).getByRole('button', { name: '批量写入关系图' }) as HTMLButtonElement;
    expect(submitBtn.disabled).toBe(true);

    rerender(
      <EntityDiscoveryDialog
        open
        onClose={vi.fn()}
        caseId={CASE_ID}
        graphId={GRAPH_ID}
        ws={makeWorkspace()}
        rules={defaultRules}
        readOnly={false}
        run={run}
      />,
    );

    const nameInput = await within(dialog).findByLabelText('写入名称');
    const allowed = fireEvent.keyDown(nameInput, { key: 'Enter', isComposing: true, keyCode: 229 });
    expect(allowed).toBe(false);
    expect(run).not.toHaveBeenCalled();
  });

  it('遇到非重复类写错误时中止批处理并显示错误提示', async () => {
    entityCandidates.mockResolvedValue({
      case_id: CASE_ID,
      candidates: [sampleResponse().candidates[0]],
    });
    const run = vi.fn(async (): Promise<CommandResult> => ({
      ok: false,
      code: 'conflict',
      message: '案件版本冲突，请刷新后重试',
    }));

    render(
      <EntityDiscoveryDialog
        open
        onClose={vi.fn()}
        caseId={CASE_ID}
        graphId={GRAPH_ID}
        ws={makeWorkspace()}
        rules={defaultRules}
        readOnly={false}
        run={run}
      />,
    );

    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: '批量写入关系图' }));

    const alert = await within(dialog).findByRole('alert');
    expect(alert.textContent).toContain('案件版本冲突');
    expect(run).toHaveBeenCalledTimes(1);
  });

  it('全部设为忽略时禁用批量提交；支持修改主体类型与取消勾选主名也记为别名；卸载时中止请求', async () => {
    let capturedSignal: AbortSignal | undefined;
    entityCandidates.mockImplementationOnce((_cid, _mids, signal) => {
      capturedSignal = signal;
      return new Promise(() => {});
    });

    const { unmount } = render(
      <EntityDiscoveryDialog
        open
        onClose={vi.fn()}
        caseId={CASE_ID}
        graphId={GRAPH_ID}
        ws={makeWorkspace()}
        rules={defaultRules}
        readOnly={false}
        run={vi.fn()}
      />,
    );
    expect(capturedSignal?.aborted).toBe(false);
    unmount();
    expect(capturedSignal?.aborted).toBe(true);

    entityCandidates.mockResolvedValueOnce({
      case_id: CASE_ID,
      candidates: [
        {
          name: '某科技研究院',
          entity_kind: 'person',
          suggested_roles: [],
          candidate_aliases: ['科技院'],
          mention_count: 3,
          existing_entity_id: null,
          existing_entity_name: null,
          sources: [],
        },
        {
          name: '蓝海关联方',
          entity_kind: 'org',
          suggested_roles: [],
          candidate_aliases: ['蓝海分部'],
          mention_count: 2,
          existing_entity_id: null,
          existing_entity_name: null,
          sources: [],
        },
      ],
    });

    let wsState = makeWorkspace([makeEntity('ent-existing-1', '上海蓝海贸易有限公司')]);
    const calls: GraphCommand[] = [];
    const run = vi.fn(async (cmd: GraphCommand): Promise<CommandResult> => {
      calls.push(cmd);
      if (cmd.type === 'add_entity') {
        const created = makeEntity('ent-new-org', cmd.name, cmd.kind);
        wsState = { ...wsState, seq: wsState.seq + 1, entities: [...wsState.entities, created] };
        return { ok: true, snapshot: wsState };
      }
      return { ok: true, snapshot: { ...wsState, seq: wsState.seq + 1 } };
    });

    render(
      <EntityDiscoveryDialog
        open
        onClose={vi.fn()}
        caseId={CASE_ID}
        graphId={GRAPH_ID}
        ws={wsState}
        rules={defaultRules}
        readOnly={false}
        run={run}
      />,
    );

    const dialog = await screen.findByRole('dialog');
    const tbody = within(dialog).getAllByRole('rowgroup')[1];
    const rows = within(tbody).getAllByRole('row');

    // 先把两行都设为忽略 -> 批量提交按钮必须禁用。
    await userEvent.selectOptions(within(rows[0]).getByLabelText('操作'), 'ignore');
    await userEvent.selectOptions(within(rows[1]).getByLabelText('操作'), 'ignore');
    const submitBtn = within(dialog).getByRole('button', { name: '批量写入关系图' }) as HTMLButtonElement;
    expect(submitBtn.disabled).toBe(true);

    // 恢复第一行为新建并把类型从自然人改选为法人或组织。
    await userEvent.selectOptions(within(rows[0]).getByLabelText('操作'), 'create');
    await userEvent.selectOptions(within(rows[0]).getByLabelText('类型'), 'org');

    // 恢复第二行为合并为已有主体，并取消勾选“主名也记为别名”，只保留别名“蓝海分部”。
    await userEvent.selectOptions(within(rows[1]).getByLabelText('操作'), 'merge');
    await userEvent.selectOptions(within(rows[1]).getByLabelText('目标已有主体'), 'ent-existing-1');
    await userEvent.click(within(rows[1]).getByLabelText('主名也记为别名'));

    expect(submitBtn.disabled).toBe(false);
    await userEvent.click(submitBtn);

    await waitFor(() => {
      expect(within(dialog).getByText(/已完成批量写入：新建主体 1 个、新增别名 2 条、挂接出处 0 条/)).toBeTruthy();
    });

    expect(calls).toEqual([
      {
        type: 'add_entity',
        kind: 'org',
        name: '某科技研究院',
        roles: [],
        graph_id: GRAPH_ID,
      },
      { type: 'add_alias', entity_id: 'ent-new-org', alias: '科技院' },
      { type: 'add_alias', entity_id: 'ent-existing-1', alias: '蓝海分部' },
    ]);
  });
});
