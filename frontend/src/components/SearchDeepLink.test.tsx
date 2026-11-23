/**
 * T17: a search hit must be addressable, not just clickable.
 *
 * The selected excerpt lived only in React state, so a reload lost it and
 * "copy" handed out a bare UUID that could not be opened.  The ref now travels
 * in the URL, and the copy button produces a link that reopens the excerpt.
 */
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    searchApi: {
      ...actual.searchApi,
      search: vi.fn(),
      readSource: vi.fn(),
    },
    analysisApi: { ...actual.analysisApi, list: vi.fn().mockResolvedValue({ items: [] }) },
  };
});

import { analysisApi, searchApi } from '../api/endpoints';
import type { ReadSourceOutput, SearchHit, SearchOutput } from '../api/endpoints';
import { SearchPage } from './SearchPanel';

const search = vi.mocked(searchApi.search);
const readSource = vi.mocked(searchApi.readSource);
void analysisApi;

function hit(ref: string, text: string): SearchHit {
  return {
    source_type: 'original',
    context: text,
    match_start: 0,
    match_end: 4,
    source: {
      source_ref: ref,
      material_id: 'm-1',
      file_version_id: 'fv-1',
      parse_run_id: 'r-1',
      locator: { page: 1 },
      method: 'native',
    },
  } as unknown as SearchHit;
}

function result(hits: SearchHit[]): SearchOutput {
  return {
    hits, total: hits.length, offset: 0, limit: 20, mode_used: 'literal',
    coverage: { total: 1, complete: 1, partial: 0, failed: 0, unsupported: 0,
                pending: 0, warnings: [] },
  } as SearchOutput;
}

function payload(ref: string, text: string): ReadSourceOutput {
  return {
    source_ref: ref, material_id: 'm-1', file_version_id: 'fv-1', parse_run_id: 'r-1',
    locator: { page: 1 }, text, method: 'native', acquisition: null, display_name: null, source_label: null, relative_source: null, is_original: true, context_before: '', context_after: '',
  };
}

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{location.pathname + location.search}</span>;
}

function renderPage(initial = '/cases/case-1') {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <SearchPage caseId="case-1" onOpenAnalysis={vi.fn()} />
      <LocationProbe />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  search.mockReset();
  readSource.mockReset();
  readSource.mockImplementation(((caseId: string, ref: string) =>
    Promise.resolve(payload(ref, `正文 ${ref}`))) as never);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('检索命中的可寻址性', () => {
  it('组合输入按回车时不检索，完成输入后可用回车提交', async () => {
    search.mockResolvedValue(result([]));
    const user = userEvent.setup();
    renderPage();

    const query = screen.getByRole('textbox', { name: '检索词' });
    await user.type(query, '合同');
    fireEvent.compositionStart(query);
    fireEvent.keyDown(query, { key: 'Enter', isComposing: true, keyCode: 229 });
    expect(search).not.toHaveBeenCalled();

    fireEvent.compositionEnd(query, { data: '合同' });
    await user.keyboard('{Enter}');
    expect(search).toHaveBeenCalledTimes(1);
    expect(search.mock.calls[0]?.[1]).toMatchObject({ query: '合同' });
  });

  it('选中命中后把 source_ref 写进地址栏', async () => {
    search.mockResolvedValue(result([hit('ref-a', '甲 正文'), hit('ref-b', '乙 正文')]));
    renderPage();

    await userEvent.type(screen.getByPlaceholderText(/例如：合同/), '正文');
    await userEvent.click(screen.getByRole('button', { name: '检索' }));
    const buttons = await screen.findAllByRole('button', { name: '查看上下文' });
    await userEvent.click(buttons[1]);

    await waitFor(() => {
      expect(screen.getByTestId('location').textContent).toContain('ref=ref-b');
    });
    await screen.findByText('正文 ref-b');
  });

  it('带 ref 参数打开时直接显示该片段，不需要先检索', async () => {
    renderPage('/cases/case-1?ref=ref-c');

    await waitFor(() => expect(readSource).toHaveBeenCalledWith(
      'case-1', 'ref-c', 200, expect.anything()));
    await screen.findByText('正文 ref-c');
    expect(search).not.toHaveBeenCalled();
  });

  it('复制的是可打开的来源链接，不是裸 UUID', async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, 'writeText');
    renderPage('/cases/case-1?ref=ref-d');

    await screen.findByText('正文 ref-d');
    await user.click(screen.getByRole('button', { name: '复制来源链接' }));

    await waitFor(() => expect(writeText).toHaveBeenCalled());
    const copied = writeText.mock.calls[0][0] as string;
    expect(copied).toContain('ref=ref-d');
    expect(copied.startsWith('http')).toBe(true);
    expect(copied).not.toBe('ref-d');
  });
});
