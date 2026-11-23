/**
 * 真实案件「诉请金额」页签：整数分、同币种候选、出处直达 SourcePreview、归档只读。
 * 金额候选按真实 GET 契约 mock，验证选取时同币种 + 整数分两个硬条件。
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    claimsApi: {
      ...actual.claimsApi, snapshot: vi.fn(), amountCandidates: vi.fn(), seq: vi.fn(),
      addItem: vi.fn(),
    },
    casesApi: { ...actual.casesApi, overview: vi.fn() },
    evidenceApi: { ...actual.evidenceApi, catalog: vi.fn() },
    annotationsApi: { ...actual.annotationsApi, list: vi.fn() },
    materialsApi: { ...actual.materialsApi, original: vi.fn() },
    searchApi: { ...actual.searchApi, readSource: vi.fn() },
  };
});

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useParams: () => ({ caseId: 'case-claims-1' }) };
});

import {
  annotationsApi, casesApi, claimsApi, evidenceApi, materialsApi, searchApi,
} from '../api/endpoints';
import CaseClaimsPage from './CaseClaimsPage';

const CASE = 'case-claims-1';
const GROUP = 'group-1';

const snapshot = vi.mocked(claimsApi.snapshot);
const amountCandidates = vi.mocked(claimsApi.amountCandidates);
const seq = vi.mocked(claimsApi.seq);
const catalog = vi.mocked(evidenceApi.catalog);
const annotations = vi.mocked(annotationsApi.list);
const readSource = vi.mocked(searchApi.readSource);
const overview = vi.mocked(casesApi.overview);
const addItem = vi.mocked(claimsApi.addItem);
const original = vi.mocked(materialsApi.original);

function apiSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    case_id: CASE, seq: 4,
    groups: [{
      id: GROUP, case_id: CASE, title: '对被告一主诉请', currency: 'CNY',
      include_in_total: true, position: 0, note: null, created_by: 'human',
      updated_by: 'human', request_key: null, removed_at: null, revision: 0,
      created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
    }],
    items: [{
      id: 'item-1', case_id: CASE, group_id: GROUP, title: '货款本金',
      category: 'principal', amount_cents: 128000000, calc_note: '合同价款',
      segment_id: 'seg-1', material_id: null, annotation_id: null, position: 0,
      created_by: 'human', updated_by: 'human', request_key: null, removed_at: null,
      revision: 1, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
    }],
    group_subtotals: { [GROUP]: 128000000 },
    totals_by_currency: [{ currency: 'CNY', total_cents: 128000000, included_group_ids: [GROUP] }],
    pending_changes: [], changes: [], pending_count: 0,
    excluded_group_count: 0, included_group_count: 1, last_change_id: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  snapshot.mockResolvedValue(apiSnapshot() as never);
  seq.mockResolvedValue({ seqs: { claims: 4 } });
  catalog.mockResolvedValue({
    case_id: CASE, seq: 0, groups: [], items: [], bindings: [],
    materials: [{ id: 'mat-1', display_name: '购销合同.pdf' }],
    numbers: {}, group_numbers: {},
  } as never);
  annotations.mockResolvedValue({ items: [], offset: 0, limit: 200, total: 0 } as never);
  readSource.mockResolvedValue({
    source_ref: 'seg-1', material_id: 'mat-1', file_version_id: 'ver-3',
    parse_run_id: 'run-1', locator: { page: 3, unit_key: '第1条' },
    text: '合同价款为人民币1,280,000元', method: 'text', display_name: '购销合同.pdf',
    context_before: '', context_after: '', acquisition: null, source_label: null,
    relative_source: null, is_original: true,
  } as never);
  overview.mockResolvedValue({
    case_id: CASE, title: '甲公司诉乙公司合同纠纷', revision: 1, archived_at: null,
  } as never);
  original.mockResolvedValue({ file_version_id: 'ver-3', integrity_status: 'ok',
    browse_url: '' } as never);
  amountCandidates.mockResolvedValue({
    case_id: CASE,
    candidates: [
      {
        exact_value_id: 'ev-cny', segment_id: 'seg-1', material_id: 'mat-1',
        material_title: '购销合同.pdf', page: 3, locator: { unit_key: '第1条' },
        raw_text: '人民币1,280,000元', currency: 'CNY', amount_cents: 128000000,
        excerpt: '合同价款为人民币1,280,000元', span: { start: 4, end: 20 },
      },
      {
        exact_value_id: 'ev-usd', segment_id: 'seg-9', material_id: 'mat-9',
        material_title: '跨境合同.pdf', page: 1, locator: {},
        raw_text: '美元12,345.67元', currency: 'USD', amount_cents: 1234567,
        excerpt: '美元12,345.67元', span: { start: 0, end: 5 },
      },
      {
        exact_value_id: 'ev-fraction', segment_id: 'seg-2', material_id: 'mat-1',
        material_title: '结算单.pdf', page: 2, locator: {},
        raw_text: '1.005元', currency: 'CNY', amount_cents: null,
        excerpt: '结算 1.005元', span: { start: 0, end: 5 },
      },
    ],
  } as never);
  addItem.mockResolvedValue(apiSnapshot({ seq: 5 }) as never);
});

function renderPage() {
  return render(<MemoryRouter><CaseClaimsPage /></MemoryRouter>);
}

describe('真实案件诉请金额页', () => {
  it('按整数分展示分组小计与全案总计', async () => {
    renderPage();
    const row = await screen.findByTestId('claim-item-row');
    expect(within(row).getByText('¥1,280,000.00')).toBeTruthy();
    expect(within(row).getByText('128,000,000 分')).toBeTruthy();
    expect(screen.getByTestId('group-subtotal').textContent).toContain('¥1,280,000.00');
    expect(screen.getByTestId('total-CNY').textContent).toContain('¥1,280,000.00');
  });

  it('候选金额走真实 GET，只列出同币种且能精确到分的值', async () => {
    renderPage();
    await screen.findByTestId('claim-item-row');
    await userEvent.click(screen.getByRole('button', { name: '＋ 添加金额项' }));
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => {
      expect(amountCandidates).toHaveBeenCalledWith(CASE, '');
    }, { timeout: 5000 });
    // 人民币候选可点选；美元与三位小数候选被隐藏并计数。
    const candidate = await within(dialog).findByText('人民币1,280,000元');
    expect(candidate).toBeTruthy();
    expect(within(dialog).queryByText('美元12,345.67元')).toBeNull();
    expect(within(dialog).getByTestId('candidates-hidden').textContent)
      .toContain('已隐藏 2 条');
    await userEvent.click(candidate.closest('button')!);
    await waitFor(() => {
      expect((within(dialog).getByLabelText('金额（元）') as HTMLInputElement).value)
        .toBe('1280000.00');
    });
    expect(within(dialog).getByTestId('amount-cents').textContent).toContain('128,000,000 分');
  }, 20000);

  it('出处点击用片段解析出的文件版本与页码打开 SourcePreview', async () => {
    renderPage();
    const row = await screen.findByTestId('claim-item-row');
    const link = within(row).getByRole('button', { name: /购销合同\.pdf/ });
    await waitFor(() => { expect(link.textContent).toContain('第 3 页'); });
    await userEvent.click(link);
    const dialog = await screen.findByRole('dialog', { name: '原件预览' });
    const image = within(dialog).getByRole('img') as HTMLImageElement;
    expect(image.getAttribute('src'))
      .toBe(`/api/v1/cases/${CASE}/file-versions/ver-3/page-preview?page=3`);
  }, 20000);

  it('归档案件只读：新增、编辑、删除与确认入口禁用', async () => {
    overview.mockResolvedValue({
      case_id: CASE, title: '甲公司诉乙公司合同纠纷', revision: 2,
      archived_at: '2026-11-21T00:00:00Z',
    } as never);
    renderPage();
    await screen.findByTestId('claim-item-row');
    expect(screen.getByText(/本案已归档：诉请金额只读/)).toBeTruthy();
    expect((screen.getByRole('button', { name: '＋ 新建分组' }) as HTMLButtonElement).disabled)
      .toBe(true);
    expect((screen.getByRole('button', { name: '＋ 添加金额项' }) as HTMLButtonElement).disabled)
      .toBe(true);
    expect((screen.getByRole('button', { name: '编辑「货款本金」' }) as HTMLButtonElement).disabled)
      .toBe(true);
  }, 20000);
});
