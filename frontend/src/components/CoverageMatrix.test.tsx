/**
 * Wave 1C 案卷覆盖矩阵与工作区格子筛选测试（TEST-ONLY 合成数据，不连后端）。
 */
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    materialsApi: {
      ...actual.materialsApi,
      coverageMatrix: vi.fn(),
      list: vi.fn(),
    },
    searchApi: {
      ...actual.searchApi,
      indexStatus: vi.fn(),
    },
    casesApi: {
      ...actual.casesApi,
      overview: vi.fn(),
    },
  };
});

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useParams: () => ({ caseId: 'case-cov-1' }) };
});

import { casesApi, materialsApi, searchApi } from '../api/endpoints';
import type { CorpusCoverageMatrixOut, MaterialSummary } from '../api/endpoints';
import { CaseWorkspacePage } from '../pages/CaseWorkspacePage';
import { CoverageMatrix } from './CoverageMatrix';

const coverageMatrixMock = vi.mocked(materialsApi.coverageMatrix);
const listMock = vi.mocked(materialsApi.list);
const indexStatusMock = vi.mocked(searchApi.indexStatus);
const overviewMock = vi.mocked(casesApi.overview);

const CASE_ID = 'case-cov-1';

function buildMatrix(): CorpusCoverageMatrixOut {
  return {
    case_id: CASE_ID,
    graph_id: null,
    categories: ['合同', '银行流水'],
    totals: {
      total: 4,
      referenced: 1,
      parsed_unused: 1,
      partial_or_failed: 1,
      pending_or_unsupported: 1,
    },
    cells: [
      { category: '合同', status: 'referenced', count: 1, material_ids: ['m-1'] },
      { category: '合同', status: 'parsed_unused', count: 1, material_ids: ['m-2'] },
      { category: '合同', status: 'partial_or_failed', count: 0, material_ids: [] },
      { category: '合同', status: 'pending_or_unsupported', count: 0, material_ids: [] },
      { category: '银行流水', status: 'referenced', count: 0, material_ids: [] },
      { category: '银行流水', status: 'parsed_unused', count: 0, material_ids: [] },
      { category: '银行流水', status: 'partial_or_failed', count: 1, material_ids: ['m-3'] },
      { category: '银行流水', status: 'pending_or_unsupported', count: 1, material_ids: ['m-4'] },
    ],
    materials: [
      {
        material_id: 'm-1', display_name: 'TEST-ONLY 借款合同.pdf', category: '合同',
        status: 'referenced', file_version_id: 'fv-1',
        coverage: { complete: 2, partial: 0, failed: 0, pending: 0, unsupported: 0 },
        reference_kinds: ['graph'],
      },
      {
        material_id: 'm-2', display_name: 'TEST-ONLY 补充协议.pdf', category: '合同',
        status: 'parsed_unused', file_version_id: 'fv-2',
        coverage: { complete: 1, partial: 0, failed: 0, pending: 0, unsupported: 0 },
        reference_kinds: [],
      },
      {
        material_id: 'm-3', display_name: 'TEST-ONLY 扫描流水.pdf', category: '银行流水',
        status: 'partial_or_failed', file_version_id: 'fv-3',
        coverage: { complete: 1, partial: 0, failed: 1, pending: 0, unsupported: 0 },
        reference_kinds: [],
      },
      {
        material_id: 'm-4', display_name: 'TEST-ONLY 附件包.zip', category: '银行流水',
        status: 'pending_or_unsupported', file_version_id: 'fv-4',
        coverage: { complete: 0, partial: 0, failed: 0, pending: 0, unsupported: 1 },
        reference_kinds: [],
      },
    ],
  };
}

function makeMaterial(id: string, name: string, category: string): MaterialSummary {
  return {
    material_id: id,
    display_name: name,
    category,
    revision: 0,
    intake_id: 'i-1',
    acquisition: null,
    source_label: '上传',
    relative_source: `TEST-ONLY/${name}`,
    status: 'ready',
    coverage: { complete: 1, partial: 0, failed: 0, pending: 0, unsupported: 0 },
    created_at: '2026-11-22T00:00:00Z',
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  coverageMatrixMock.mockResolvedValue(buildMatrix());
  indexStatusMock.mockResolvedValue({ complete: 4, partial: 0, failed: 1, pending: 0, unsupported: 1 });
  overviewMock.mockResolvedValue({
    case_id: CASE_ID, title: 'TEST-ONLY 案卷', description: null, revision: 0,
    created_at: '2026-11-22T00:00:00Z', archived_at: null, stage: null, next_action: null,
    deadlines: [], active_stage: null,
  });
});

afterEach(cleanup);

describe('CoverageMatrix', () => {
  it('折叠面板展示汇总数字、四档色调单元格，点击触发筛选与反选', async () => {
    const user = userEvent.setup();
    const onSelectCell = vi.fn();

    const { rerender } = render(
      <CoverageMatrix caseId={CASE_ID} activeFilter={null} onSelectCell={onSelectCell} />,
    );

    const summary = await screen.findByText(/共 4 份 · 已引用 1 · 未引用 1 · 异常\/告警 1 · 待解析 1/);
    expect(summary).toBeTruthy();

    await user.click(summary);
    const table = screen.getByRole('table');
    expect(within(table).getByRole('columnheader', { name: '已入图/已引用' })).toBeTruthy();
    expect(within(table).getByRole('columnheader', { name: '已解析未引用' })).toBeTruthy();
    expect(within(table).getByRole('columnheader', { name: 'OCR告警/失败' })).toBeTruthy();
    expect(within(table).getByRole('columnheader', { name: '未解析/不支持' })).toBeTruthy();

    const refBtn = within(table).getByRole('button', { name: '合同 已入图/已引用 1 份' });
    const unusedBtn = within(table).getByRole('button', { name: '合同 已解析未引用 1 份' });
    const warnBtn = within(table).getByRole('button', { name: '银行流水 OCR告警/失败 1 份' });
    const pendingBtn = within(table).getByRole('button', { name: '银行流水 未解析/不支持 1 份' });
    expect(refBtn.className).toContain('is-referenced');
    expect(unusedBtn.className).toContain('is-unused');
    expect(warnBtn.className).toContain('is-warn');
    expect(pendingBtn.className).toContain('is-pending');

    await user.click(unusedBtn);
    expect(onSelectCell).toHaveBeenLastCalledWith({ category: '合同', status: 'parsed_unused' });

    rerender(
      <CoverageMatrix
        caseId={CASE_ID}
        activeFilter={{ category: '合同', status: 'parsed_unused' }}
        onSelectCell={onSelectCell}
      />,
    );
    expect(within(table).getByRole('button', { name: '合同 已解析未引用 1 份' }).getAttribute('aria-pressed'))
      .toBe('true');

    await user.click(within(table).getByRole('button', { name: '合同 已解析未引用 1 份' }));
    expect(onSelectCell).toHaveBeenLastCalledWith({ category: null, status: null });

    await user.click(within(table).getByRole('button', { name: '全部 OCR告警/失败 1 份' }));
    expect(onSelectCell).toHaveBeenLastCalledWith({ category: null, status: 'partial_or_failed' });
  });

  it('接口失败时静默不渲染，不抛出异常', async () => {
    coverageMatrixMock.mockRejectedValueOnce(new Error('network error'));
    const { container } = render(
      <CoverageMatrix caseId={CASE_ID} onSelectCell={vi.fn()} />,
    );
    await waitFor(() => expect(coverageMatrixMock).toHaveBeenCalledTimes(1));
    expect(container.firstChild).toBeNull();
  });

  it('切换 caseId 时中止并丢弃旧案迟到响应', async () => {
    const slowA = deferred<CorpusCoverageMatrixOut>();
    const fastB = deferred<CorpusCoverageMatrixOut>();
    coverageMatrixMock.mockImplementation((id) => (
      id === 'case-a' ? slowA.promise : fastB.promise
    ));

    const { rerender } = render(<CoverageMatrix caseId="case-a" onSelectCell={vi.fn()} />);
    rerender(<CoverageMatrix caseId="case-b" onSelectCell={vi.fn()} />);

    await act(async () => {
      fastB.resolve({
        ...buildMatrix(),
        case_id: 'case-b',
        totals: { total: 9, referenced: 9, parsed_unused: 0, partial_or_failed: 0, pending_or_unsupported: 0 },
      });
    });
    expect(await screen.findByText(/共 9 份 · 已引用 9/)).toBeTruthy();

    await act(async () => {
      slowA.resolve({
        ...buildMatrix(),
        case_id: 'case-a',
        totals: { total: 1, referenced: 0, parsed_unused: 1, partial_or_failed: 0, pending_or_unsupported: 0 },
      });
    });
    expect(screen.getByText(/共 9 份 · 已引用 9/)).toBeTruthy();
    expect(screen.queryByText(/共 1 份/)).toBeNull();
  });

  it('在 CaseWorkspacePage 中点击矩阵格子筛选材料并可一键清除', async () => {
    const user = userEvent.setup();
    const allMaterials = [
      makeMaterial('m-1', 'TEST-ONLY 借款合同.pdf', '合同'),
      makeMaterial('m-2', 'TEST-ONLY 补充协议.pdf', '合同'),
      makeMaterial('m-3', 'TEST-ONLY 扫描流水.pdf', '银行流水'),
      makeMaterial('m-4', 'TEST-ONLY 附件包.zip', '银行流水'),
    ];
    listMock.mockImplementation(async (_caseId, params) => {
      const items = allMaterials.filter((item) => (params.category ? item.category === params.category : true));
      return {
        items,
        offset: params.offset,
        limit: params.limit,
        total: items.length,
        case_total: allMaterials.length,
        categories: ['合同', '银行流水'],
      };
    });

    render(<MemoryRouter><CaseWorkspacePage /></MemoryRouter>);
    await screen.findByText('TEST-ONLY 借款合同.pdf');

    await user.click(await screen.findByText(/共 4 份 · 已引用 1/));
    await user.click(screen.getByRole('button', { name: '合同 已解析未引用 1 份' }));

    await waitFor(() => {
      expect(screen.getByText('TEST-ONLY 补充协议.pdf')).toBeTruthy();
      expect(screen.queryByText('TEST-ONLY 借款合同.pdf')).toBeNull();
    });
    expect(screen.getByRole('button', { name: '清除格子筛选' })).toBeTruthy();

    await user.click(screen.getByRole('button', { name: '清除格子筛选' }));
    await waitFor(() => {
      expect(screen.getByText('TEST-ONLY 借款合同.pdf')).toBeTruthy();
      expect(screen.getByText('TEST-ONLY 扫描流水.pdf')).toBeTruthy();
    });
  });

  it('传入内联 onMatrixLoaded 回调且父组件重渲染时不重复拉取矩阵', async () => {
    const { rerender } = render(
      <CoverageMatrix caseId={CASE_ID} activeFilter={null} onSelectCell={vi.fn()} onMatrixLoaded={() => {}} />,
    );
    await screen.findByText(/共 4 份 · 已引用 1/);
    expect(coverageMatrixMock).toHaveBeenCalledTimes(1);

    rerender(
      <CoverageMatrix
        caseId={CASE_ID}
        activeFilter={{ category: '合同', status: 'referenced' }}
        onSelectCell={vi.fn()}
        onMatrixLoaded={() => {}}
      />,
    );
    expect(coverageMatrixMock).toHaveBeenCalledTimes(1);
  });
});
