/**
 * 真实案件「证据目录」页签：编号、待确认复核、CSV 导出与外部目录导入。
 * 导入路径覆盖「必须先选定已上传材料（import_material_id）才能提交」这一硬条件。
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    graphApi: { ...actual.graphApi, workspace: vi.fn(), rules: vi.fn() },
    progressApi: { ...actual.progressApi, snapshot: vi.fn(), seqs: vi.fn() },
    annotationsApi: { ...actual.annotationsApi, list: vi.fn() },
    evidenceApi: {
      ...actual.evidenceApi, previewImport: vi.fn(), importCatalog: vi.fn(),
      confirmItem: vi.fn(), rejectItem: vi.fn(), acceptChange: vi.fn(),
    },
    materialsApi: {
      ...actual.materialsApi, original: vi.fn(), downloadBlob: vi.fn(),
    },
  };
});

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useParams: () => ({ caseId: 'case-ev-1' }) };
});

import {
  annotationsApi, evidenceApi, graphApi, materialsApi, progressApi,
} from '../api/endpoints';
import CaseEvidencePage from './CaseEvidencePage';

const CASE = 'case-ev-1';

const workspace = vi.mocked(graphApi.workspace);
const rules = vi.mocked(graphApi.rules);
const snapshotNodes = vi.mocked(progressApi.snapshot);
const seqs = vi.mocked(progressApi.seqs);
const annotations = vi.mocked(annotationsApi.list);
const previewImport = vi.mocked(evidenceApi.previewImport);
const importCatalog = vi.mocked(evidenceApi.importCatalog);
const confirmItem = vi.mocked(evidenceApi.confirmItem);
const original = vi.mocked(materialsApi.original);
const downloadBlob = vi.mocked(materialsApi.downloadBlob);

function apiWorkspace(overrides: Record<string, unknown> = {}) {
  const base = {
    case_id: CASE, case_title: '甲公司诉乙公司', archived: false, seq: 4,
    seqs: { graph: 4, evidence: 3 },
    entities: [], aliases: [], relations: [], sources: [], graphs: [], nodes: [], edges: [],
    groups: [], snapshots: [], consents: [],
    evidence: {
      case_id: CASE, seq: 3,
      groups: [{
        id: 'eg-1', case_id: CASE, title: '权属证据', purpose: '证明股权归属', position: 0,
        created_by: 'human', updated_by: 'human', request_key: null, removed_at: null,
        revision: 0, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
      }],
      items: [
        {
          id: 'ei-1', case_id: CASE, group_id: 'eg-1', title: '股权转让协议',
          purpose: '证明转让合意', position: 0, review_state: 'confirmed',
          created_by: 'human', updated_by: 'human', request_key: null, removed_at: null,
          revision: 1, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
        },
        {
          id: 'ei-2', case_id: CASE, group_id: 'eg-1', title: '补充协议（待确认）',
          purpose: '证明补充约定', position: 1, review_state: 'pending',
          created_by: 'agent', updated_by: 'agent', request_key: 'k-1', removed_at: null,
          revision: 0, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
        },
      ],
      bindings: [{
        id: 'eb-1', case_id: CASE, item_id: 'ei-1', material_id: 'mat-1',
        page_start: 2, page_end: 3, review_state: 'confirmed',
        created_by: 'human', removed_at: null, created_at: '2026-11-20T00:00:00Z',
      }],
      materials: [
        { id: 'mat-1', display_name: '股权转让协议.pdf' },
        { id: 'mat-cat', display_name: '证据目录.xlsx' },
      ],
      numbers: { 'ei-1': '证据1-1' },
      group_numbers: { 'eg-1': '1' },
    },
    labels: {},
    pending_changes: [], changes: [], last_change_id: null, notice: null,
  };
  return { ...base, ...overrides };
}

it('刷新后显示证据目录域提案并确认正确的变更', async () => {
  const base = apiWorkspace();
  const proposal = {
    id: 'evidence-import-1', case_id: CASE, seq: 3, command: 'import_catalog',
    actor: 'agent', entry: 'mcp', diff: [],
    review_state: 'pending', reviewed_at: null, reverted_by: null,
    created_at: '2026-11-20T00:00:00Z',
  };
  workspace.mockResolvedValueOnce({
    ...base, evidence: { ...base.evidence, pending_changes: [proposal] },
  } as never);
  vi.mocked(evidenceApi.acceptChange).mockResolvedValue(base.evidence as never);
  render(<MemoryRouter><CaseEvidencePage /></MemoryRouter>);
  await userEvent.click(await screen.findByRole('button', { name: '全部确认导入' }));
  await waitFor(() => expect(evidenceApi.acceptChange).toHaveBeenCalledWith(CASE, proposal.id));
});

beforeEach(() => {
  vi.clearAllMocks();
  workspace.mockResolvedValue(apiWorkspace() as never);
  rules.mockResolvedValue({ rules: {} });
  snapshotNodes.mockResolvedValue({
    case_id: CASE, seq: 1, nodes: [], relations: [], links: [],
    pending_changes: [], changes: [], last_change_id: null,
  } as never);
  seqs.mockResolvedValue({ seqs: { graph: 4, evidence: 3 } });
  annotations.mockResolvedValue({ items: [], offset: 0, limit: 200, total: 0 } as never);
  previewImport.mockResolvedValue({
    rows: [{
      group_title: '权属证据', item_title: '股东名册',
      item_purpose: '证明股东身份', material_title: '股权转让协议.pdf',
      page_start: 5, page_end: 6,
    }],
    warnings: ['1 条草稿未绑定材料：请先匹配材料，再提交导入（确认前不参与编号）'],
  } as never);
  importCatalog.mockResolvedValue(apiWorkspace({ seq: 5 }) as never);
  confirmItem.mockResolvedValue(apiWorkspace({ seq: 5 }) as never);
  original.mockResolvedValue({ file_version_id: 'ver-7', integrity_status: 'ok' } as never);
  downloadBlob.mockResolvedValue(new Blob(['group,item']) as never);
});

function renderPage() {
  return render(<MemoryRouter><CaseEvidencePage /></MemoryRouter>);
}

describe('真实案件证据目录页', () => {
  it('显示真实编号、待确认条目与 CSV 下载入口', async () => {
    renderPage();
    await waitFor(() => { expect(screen.getByText('股权转让协议')).toBeTruthy(); });
    expect(screen.getByText('证据1-1')).toBeTruthy();
    const link = screen.getByRole('link', { name: '下载 CSV 目录' }) as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe(`/api/v1/cases/${CASE}/evidence-catalog/export`);
    // 待确认条目不参与编号，只显示「待确认」。
    const pendingRow = screen.getByText('补充协议（待确认）').closest('.graph-ev-item')!;
    // 编号位与复核徽标都会写「待确认」，这里只要求至少出现一次。
    expect(within(pendingRow as HTMLElement).getAllByText('待确认').length)
      .toBeGreaterThan(0);
    expect(within(pendingRow as HTMLElement).getByRole('button', { name: '确认条目' })).toBeTruthy();
  }, 20000);

  it('确认待确认条目后重新读取目录（编号随之刷新）', async () => {
    renderPage();
    await waitFor(() => { expect(screen.getByText('补充协议（待确认）')).toBeTruthy(); });
    const pendingRow = screen.getByText('补充协议（待确认）').closest('.graph-ev-item') as HTMLElement;
    await userEvent.click(within(pendingRow).getByRole('button', { name: '确认条目' }));
    await waitFor(() => {
      expect(confirmItem).toHaveBeenCalledWith(CASE, 'ei-2', 0);
    });
  }, 20000);

  it('目录导入：未选定 import_material_id 时不能解析，选定后可预览并提交映射', async () => {
    renderPage();
    await waitFor(() => { expect(screen.getByText('股权转让协议')).toBeTruthy(); });
    const panel = screen.getByLabelText('导入外部证据目录');
    const parseButton = within(panel).getByRole('button', { name: '解析预览' }) as HTMLButtonElement;
    expect(parseButton.disabled).toBe(true);

    await userEvent.selectOptions(
      within(panel).getByLabelText('导入材料'), 'mat-cat');
    expect(parseButton.disabled).toBe(false);
    await userEvent.click(parseButton);

    await waitFor(() => { expect(previewImport).toHaveBeenCalled(); });
    expect(original).toHaveBeenCalledWith(CASE, 'mat-cat');
    expect(downloadBlob).toHaveBeenCalledWith(CASE, 'ver-7');
    // 解析出的材料名唯一匹配到本案材料，映射自动填好。
    await waitFor(() => {
      expect(within(panel).getByText(/股东名册/)).toBeTruthy();
    });
    const submit = within(panel).getByRole('button', { name: /提交导入（1 条）/ });
    await userEvent.click(submit);
    await waitFor(() => { expect(importCatalog).toHaveBeenCalled(); });
    const body = importCatalog.mock.calls[0][1] as {
      import_material_id: string; rows: { bindings: unknown[] }[];
    };
    expect(body.import_material_id).toBe('mat-cat');
    expect(body.rows[0].bindings).toEqual([
      { material_id: 'mat-1', page_start: 5, page_end: 6 },
    ]);
  }, 20000);

  it('归档案件：目录与导入只读，CSV 仍可下载', async () => {
    workspace.mockResolvedValue(apiWorkspace({ archived: true }) as never);
    renderPage();
    await waitFor(() => { expect(screen.getByText('股权转让协议')).toBeTruthy(); });
    expect(screen.getByText(/本案已归档：证据目录只读/)).toBeTruthy();
    const panel = screen.getByLabelText('导入外部证据目录');
    expect(within(panel).getByText('案件已归档：目录导入只读。')).toBeTruthy();
    expect(screen.getByRole('link', { name: '下载 CSV 目录' })).toBeTruthy();
    const bundleBtn = screen.getByRole('button', { name: '一键装卷（ZIP）' }) as HTMLButtonElement;
    expect(bundleBtn.disabled).toBe(false);
    const edit = [...document.querySelectorAll('button')]
      .filter((button) => button.textContent === '编辑');
    expect(edit.every((button) => (button as HTMLButtonElement).disabled)).toBe(true);
  }, 20000);

  it('一键装卷（ZIP）可触发下载并在服务端拒绝时提示原因', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const createUrl = vi.fn(() => 'blob:mock-zip');
    const revokeUrl = vi.fn();
    vi.stubGlobal('URL', { ...URL, createObjectURL: createUrl, revokeObjectURL: revokeUrl });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    fetchSpy.mockResolvedValueOnce(new Response(new Blob(['zipbytes']), {
      status: 200,
      headers: {
        'Content-Type': 'application/zip',
        'Content-Disposition': "attachment; filename=\"bundle.zip\"; filename*=UTF-8''%E8%AF%81%E6%8D%AE%E5%8D%B7%E5%AE%97.zip",
      },
    }));

    renderPage();
    await waitFor(() => { expect(screen.getByText('股权转让协议')).toBeTruthy(); });
    await userEvent.click(screen.getByRole('button', { name: '一键装卷（ZIP）' }));
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        `/api/v1/cases/${CASE}/evidence-catalog/bundle`,
        expect.objectContaining({ headers: { Accept: 'application/zip, application/json' } }),
      );
      expect(createUrl).toHaveBeenCalled();
      expect(clickSpy).toHaveBeenCalled();
      expect(screen.getByText(/已生成并开始下载证据卷宗包/)).toBeTruthy();
    });

    fetchSpy.mockResolvedValueOnce(new Response(
      JSON.stringify({ message: '暂无已确认的证据条目，请先确认证据目录后再装卷' }),
      { status: 400, headers: { 'Content-Type': 'application/json' } },
    ));
    await userEvent.click(screen.getByRole('button', { name: '一键装卷（ZIP）' }));
    await waitFor(() => {
      expect(screen.getByText(/暂无已确认的证据条目，请先确认证据目录后再装卷/)).toBeTruthy();
    });

    fetchSpy.mockRestore();
    clickSpy.mockRestore();
    vi.unstubAllGlobals();
  }, 20000);
});
