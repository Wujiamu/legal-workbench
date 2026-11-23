/**
 * R05: the material list must reach past the first 50 rows.
 *
 * The page always requested offset=0&limit=50 and built the category sidebar
 * from that single page, so material 51 was unreachable and a category that only
 * appeared later never showed up.
 */
import { act, render, screen, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    materialsApi: { ...actual.materialsApi, list: vi.fn() },
    searchApi: { ...actual.searchApi, indexStatus: vi.fn() },
  };
});

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  // Only the params are stubbed; real routing context comes from MemoryRouter.
  return { ...actual, useParams: () => ({ caseId: 'case-1' }) };
});

import { materialsApi, searchApi } from '../api/endpoints';
import type { MaterialSummary } from '../api/endpoints';
import { MemoryRouter } from 'react-router';
import { CaseWorkspacePage } from './CaseWorkspacePage';

function renderPage() {
  return render(<MemoryRouter><CaseWorkspacePage /></MemoryRouter>);
}

const list = vi.mocked(materialsApi.list);
const indexStatus = vi.mocked(searchApi.indexStatus);

const TOTAL = 120;

function materialAt(index: number): MaterialSummary {
  const category = index < 50 ? '合同' : index < 100 ? '证据' : '仅在末页';
  return {
    material_id: `m-${index}`,
    display_name: `虚构材料${index + 1}.txt`,
    category,
    revision: 0,
    intake_id: 'i-1',
    acquisition: null,
    source_label: '上传',
    relative_source: `虚构/${index + 1}.txt`,
    status: 'ready',
    coverage: { complete: 1, partial: 0, failed: 0, pending: 0, unsupported: 0 },
    created_at: '2026-11-11T00:00:00Z',
  };
}

/** Every category in the case, independent of the requested page. */
const ALL_CATEGORIES = ['合同', '仅在末页', '证据'];

function page(offset: number, limit: number, category?: string) {
  const pool = Array.from({ length: TOTAL }, (_, i) => materialAt(i))
    .filter((item) => (category ? item.category === category : true));
  return Promise.resolve({
    items: pool.slice(offset, offset + limit),
    offset,
    limit,
    total: pool.length,
    case_total: TOTAL,
    categories: ALL_CATEGORIES,
  });
}

afterEach(cleanup);

describe('material list pagination', () => {
  beforeEach(() => {
    list.mockReset();
    list.mockImplementation((_case, params) => page(params.offset, params.limit, params.category));
    indexStatus.mockResolvedValue({ complete: TOTAL, partial: 0, failed: 0, pending: 0,
                                    unsupported: 0 });
  });

  it('reaches materials past the first page', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('虚构材料1.txt')).toBeTruthy());

    // Row 51 exists only on page 2.
    expect(screen.queryByText('虚构材料51.txt')).toBeNull();
    await userEvent.click(screen.getByText('下一页'));
    await waitFor(() => expect(screen.getByText('虚构材料51.txt')).toBeTruthy());

    await userEvent.click(screen.getByText('下一页'));
    await waitFor(() => expect(screen.getByText('虚构材料101.txt')).toBeTruthy());

    // Third page is the last one: no further page to advance to.
    const next = screen.getByText('下一页') as HTMLButtonElement;
    expect(next.disabled).toBe(true);
  });

  it('keeps the new page when the old response arrives after navigation', async () => {
    let releaseOld!: () => void;
    const oldPage = new Promise<void>((resolve) => { releaseOld = resolve; });
    const secondPage = await page(50, 50);
    const thirdPage = await page(100, 50);
    // Go to page 3 before the response for page 2 arrives.
    list.mockImplementation(async (_case, params) => {
      if (params.offset === 0) return page(0, 50);
      if (params.offset === 50) {
        await oldPage;
        return secondPage;
      }
      return thirdPage;
    });
    renderPage();
    await screen.findByText('虚构材料1.txt');
    await userEvent.click(screen.getByText('下一页'));
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(
      'case-1', expect.objectContaining({ offset: 50 }), expect.any(AbortSignal)));
    await userEvent.click(screen.getByText('下一页'));
    await screen.findByText('虚构材料101.txt');
    await act(async () => { releaseOld(); });
    expect(screen.getByText('虚构材料101.txt')).toBeTruthy();
    expect(screen.queryByText('虚构材料51.txt')).toBeNull();
  });

  it('shows the case-wide total, not the current page size', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText(`全部（${TOTAL}）`)).toBeTruthy());
    // The filtered list shows only 50 rows, yet the case total stays 120.
    expect(list).toHaveBeenCalledWith('case-1',
      expect.objectContaining({ offset: 0, limit: 50 }), expect.anything());
  });

  it('keeps a category that only appears on a later page', async () => {
    renderPage();
    // "仅在末页" only exists on page 3, but must be in the sidebar from the start.
    await waitFor(() => expect(screen.getByText('仅在末页')).toBeTruthy());
    expect(screen.queryByText('虚构材料101.txt')).toBeNull();

    await userEvent.click(screen.getByText('仅在末页'));
    await waitFor(() => expect(screen.getByText('虚构材料101.txt')).toBeTruthy());
    // Filtering resets to the first page of the filtered set.
    expect(list).toHaveBeenLastCalledWith('case-1',
      expect.objectContaining({ category: '仅在末页', offset: 0 }), expect.anything());
  });

  it('returns to the whole case after a category filter', async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText('证据')).toBeTruthy());
    await userEvent.click(screen.getByText('证据'));
    await waitFor(() => expect(screen.getByText('虚构材料51.txt')).toBeTruthy());

    await userEvent.click(screen.getByText(`全部（${TOTAL}）`));
    await waitFor(() => expect(screen.getByText('虚构材料1.txt')).toBeTruthy());
    expect(list).toHaveBeenLastCalledWith('case-1',
      expect.objectContaining({ offset: 0 }), expect.anything());
    expect(list.mock.lastCall?.[1].category).toBeUndefined();
  });
});


describe('来源链接直达', () => {
  it('带 ref 参数打开时默认进入检索页签', async () => {
    // A `?ref=` link must land on the search tab; otherwise the excerpt only
    // appears after the user clicks the tab by hand.
    render(
      <MemoryRouter initialEntries={['/cases/case-1?ref=ref-x']}>
        <CaseWorkspacePage />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByPlaceholderText(/HT-001/)).toBeTruthy();
    });
  });
});
