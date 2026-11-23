/**
 * Material switching: title, body and every original-file action must describe
 * the same material.
 *
 * The page reuses `MaterialDetail` across an A→B switch, and the detail kept A's
 * `original` and `segments` until B's request returned.  For that window B's
 * title sat above A's preview, A's download link, A's "create working copy" and
 * A's "reparse" — all of which act on A's file version.
 */
import { render, screen, waitFor, cleanup, act, fireEvent } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    materialsApi: {
      ...actual.materialsApi,
      list: vi.fn(),
      original: vi.fn(),
      createWorkingCopy: vi.fn(),
      reparse: vi.fn(),
      classify: vi.fn(),
      rename: vi.fn(),
    },
    searchApi: { ...actual.searchApi, extractedText: vi.fn() },
  };
});

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useParams: () => ({ caseId: 'case-1' }) };
});

import { materialsApi, searchApi } from '../api/endpoints';
import type { GetOriginalOutput, MaterialSummary } from '../api/endpoints';
import { MemoryRouter } from 'react-router';
import { CaseWorkspacePage } from './CaseWorkspacePage';

const list = vi.mocked(materialsApi.list);
const original = vi.mocked(materialsApi.original);
const extractedText = vi.mocked(searchApi.extractedText);

function material(id: string, name: string): MaterialSummary {
  return {
    material_id: id,
    display_name: name,
    category: '待分类',
    status: 'ready',
    revision: 0,
    intake_id: 'i-1',
    acquisition: null,
    source_label: '虚构',
    relative_source: `虚构/${name}`,
    coverage: { total: 1, complete: 1, partial: 0, failed: 0, unsupported: 0,
                pending: 0, warnings: [] },
    created_at: '2026-01-01T00:00:00Z',
  } as MaterialSummary;
}

function originalFor(id: string, fileVersionId: string): GetOriginalOutput {
  return {
    material_id: id,
    file_version_id: fileVersionId,
    stored_object_id: `so-${id}`,
    sha256: 'a'.repeat(64),
    size: 10,
    integrity_status: 'ok',
    relative_path: `cases/x/originals/${id}/source.txt`,
    browse_url: `/api/v1/cases/case-1/file-versions/${fileVersionId}/download`,
  } as GetOriginalOutput;
}

/** A promise plus its resolver, so a test can hold a response open. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => {
  list.mockReset();
  original.mockReset();
  extractedText.mockReset();
  list.mockResolvedValue({
    items: [material('m-a', '甲材料.txt'), material('m-b', '乙材料.txt'),
            material('m-c', '丙材料.txt')],
    total: 3, case_total: 3, categories: ['待分类'],
  } as never);
  extractedText.mockResolvedValue({ segments: [] } as never);
});

afterEach(cleanup);

async function renderAndOpenFirst() {
  render(<MemoryRouter><CaseWorkspacePage /></MemoryRouter>);
  const first = await screen.findByRole('button', { name: /甲材料\.txt/ });
  await act(async () => { first.click(); });
}

describe('材料切换时的原件归属', () => {
  it('B 的原件未返回时，不得显示 A 的预览或操作', async () => {
    const held = deferred<GetOriginalOutput>();
    original.mockImplementation(((_case: string, materialId: string) => {
      if (materialId === 'm-a') return Promise.resolve(originalFor('m-a', 'fv-a'));
      return held.promise;
    }) as never);

    await renderAndOpenFirst();
    await screen.findByText(/原件校验/);
    expect(document.querySelector('iframe[title="原件预览"]')?.getAttribute('src'))
      .toContain('/file-versions/fv-a/');

    const second = await screen.findByRole('button', { name: /乙材料\.txt/ });
    await act(async () => { second.click(); });

    // Title is B, so nothing that acts on A's file may remain on screen.
    expect(screen.getByRole('heading', { name: '乙材料.txt' })).toBeTruthy();
    expect(document.querySelector('iframe[title="原件预览"]')).toBeNull();
    expect(screen.queryByText('下载原件')).toBeNull();
    expect(screen.getByRole('button', { name: '创建工作副本' })).toHaveProperty('disabled', true);
    expect(screen.getByRole('button', { name: '重新解析' })).toHaveProperty('disabled', true);

    await act(async () => { held.resolve(originalFor('m-b', 'fv-b')); });
    await waitFor(() => {
      expect(document.querySelector('iframe[title="原件预览"]')?.getAttribute('src'))
        .toContain('/file-versions/fv-b/');
    });
  });

  it('A 的迟到响应不能覆盖 C 的原件', async () => {
    const slowA = deferred<GetOriginalOutput>();
    const fastB = deferred<GetOriginalOutput>();
    original.mockImplementation(((_case: string, materialId: string) => {
      if (materialId === 'm-a') return slowA.promise;
      if (materialId === 'm-b') return fastB.promise;
      return Promise.resolve(originalFor('m-c', 'fv-c'));
    }) as never);

    render(<MemoryRouter><CaseWorkspacePage /></MemoryRouter>);
    const first = await screen.findByRole('button', { name: /甲材料\.txt/ });
    await act(async () => { first.click(); });
    const second = await screen.findByRole('button', { name: /乙材料\.txt/ });
    await act(async () => { second.click(); });
    const third = await screen.findByRole('button', { name: /丙材料\.txt/ });
    await act(async () => { third.click(); });

    await waitFor(() => {
      expect(document.querySelector('iframe[title="原件预览"]')?.getAttribute('src'))
        .toContain('/file-versions/fv-c/');
    });

    // A and B answer late; neither may take over the pane.
    await act(async () => {
      slowA.resolve(originalFor('m-a', 'fv-a'));
      fastB.resolve(originalFor('m-b', 'fv-b'));
    });
    await waitFor(() => {
      expect(document.querySelector('iframe[title="原件预览"]')?.getAttribute('src'))
        .toContain('/file-versions/fv-c/');
    });
    expect(screen.getByRole('heading', { name: '丙材料.txt' })).toBeTruthy();
  });

  it('迟到的提取文本不能贴到另一份材料上', async () => {
    original.mockImplementation(((_case: string, materialId: string) =>
      Promise.resolve(originalFor(materialId, `fv-${materialId}`))) as never);

    const slowText = deferred<{ segments: unknown[] }>();
    extractedText.mockImplementation(((_case: string, versionId: string) => {
      if (versionId === 'fv-m-a') return slowText.promise as never;
      return Promise.resolve({ segments: [] }) as never;
    }) as never);

    await renderAndOpenFirst();
    await screen.findByText(/原件校验/);
    const textTab = await screen.findByRole('button', { name: '提取文本' });
    await act(async () => { textTab.click(); });

    const second = await screen.findByRole('button', { name: /乙材料\.txt/ });
    await act(async () => { second.click(); });
    await act(async () => {
      slowText.resolve({ segments: [{ source_ref: 's-a', locator: { page: 1 },
                                      text: '甲材料的正文', method: 'native',
                                      status: 'extracted' }] });
    });

    await waitFor(() => {
      expect(screen.queryByText('甲材料的正文')).toBeNull();
    });
  });
});


describe('材料切换：复审提出的两条受控时序', () => {
  it('甲的正文先返回、乙原件后到，不得把甲正文显示在乙标题下', async () => {
    // Review repro: A's extracted text arrives before B's original.  Once B's
    // original lands, the pane is about B but still holds A's segments until B's
    // own text returns — so B's title sat above A's body.
    const slowBOriginal = deferred<GetOriginalOutput>();
    original.mockImplementation(((_case: string, materialId: string) => {
      if (materialId === 'm-b') return slowBOriginal.promise;
      return Promise.resolve(originalFor(materialId, `fv-${materialId}`));
    }) as never);

    const slowA = deferred<{ segments: unknown[] }>();
    const slowB = deferred<{ segments: unknown[] }>();
    extractedText.mockImplementation(((_case: string, versionId: string) => {
      if (versionId === 'fv-m-a') return slowA.promise as never;
      if (versionId === 'fv-m-b') return slowB.promise as never;
      return Promise.resolve({ segments: [] }) as never;
    }) as never);

    await renderAndOpenFirst();
    await screen.findByText(/原件校验/);
    await act(async () => { screen.getByRole('button', { name: '提取文本' }).click(); });

    const second = await screen.findByRole('button', { name: /乙材料\.txt/ });
    await act(async () => { second.click(); });

    // A's body lands while B's original is still in flight.
    await act(async () => {
      slowA.resolve({ segments: [{ source_ref: 's-a', locator: { page: 1 },
                                   text: '甲的正文不该出现在乙下面', method: 'native',
                                   status: 'extracted' }] });
    });
    // Now B's original arrives.  The text tab is open, so what matters is that
    // A's body is not carried over: the pane must wait for B's own text.
    await act(async () => { slowBOriginal.resolve(originalFor('m-b', 'fv-m-b')); });
    await waitFor(() => {
      expect(extractedText).toHaveBeenCalledWith('case-1', 'fv-m-b');
    });
    expect(screen.queryByText('甲的正文不该出现在乙下面')).toBeNull();

    await act(async () => {
      slowB.resolve({ segments: [{ source_ref: 's-b', locator: { page: 1 },
                                   text: '乙的正文', method: 'native',
                                   status: 'extracted' }] });
    });
    await screen.findByText('乙的正文');
  });

  it('甲的副本回包不能启用乙详情的「登记为新版本」', async () => {
    // Review repro: click "create working copy" on A, switch to B, let A's copy
    // response land — B's detail then had an enabled register button holding A's
    // working copy id.
    original.mockImplementation(((_case: string, materialId: string) =>
      Promise.resolve(originalFor(materialId, `fv-${materialId}`))) as never);

    const slowCopy = deferred<unknown>();
    const createWorkingCopy = vi.mocked(materialsApi.createWorkingCopy);
    createWorkingCopy.mockImplementation((() => slowCopy.promise) as never);

    await renderAndOpenFirst();
    await screen.findByText(/原件校验/);
    const copyButton = await screen.findByRole('button', { name: '创建工作副本' });
    await act(async () => { copyButton.click(); });

    const second = await screen.findByRole('button', { name: /乙材料\.txt/ });
    await act(async () => { second.click(); });
    await screen.findByRole('heading', { name: '乙材料.txt' });

    // A's copy lands late, with A's ids.
    await act(async () => {
      slowCopy.resolve({
        working_copy_id: 'wc-from-a', relative_path: 'cases/x/working/wc-from-a/甲（工作副本）.txt',
      });
    });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '登记为新版本' }))
        .toHaveProperty('disabled', true);
    });
    expect(screen.queryByText(/wc-from-a/)).toBeNull();
  });
});


describe('切换后仍可继续操作', () => {
  it('甲的创建请求迟到后，乙的按钮必须恢复可用', async () => {
    // Review repro: A's copy request stays in flight across the switch, its
    // completion is discarded as stale, and nothing resets `busy` — so B opened
    // with 创建工作副本 / 保存 / 重新解析 all disabled forever.
    original.mockImplementation(((_case: string, materialId: string) =>
      Promise.resolve(originalFor(materialId, `fv-${materialId}`))) as never);

    const slowCopy = deferred<unknown>();
    const createWorkingCopy = vi.mocked(materialsApi.createWorkingCopy);
    createWorkingCopy.mockImplementation((() => slowCopy.promise) as never);

    await renderAndOpenFirst();
    await screen.findByText(/原件校验/);
    await act(async () => {
      screen.getByRole('button', { name: '创建工作副本' }).click();
    });

    const second = await screen.findByRole('button', { name: /乙材料\.txt/ });
    await act(async () => { second.click(); });
    await screen.findByRole('heading', { name: '乙材料.txt' });
    await waitFor(() => {
      expect(document.querySelector('iframe[title="原件预览"]')?.getAttribute('src'))
        .toContain('/file-versions/fv-m-b/');
    });

    // A's copy lands late and must not touch B.
    await act(async () => {
      slowCopy.resolve({ working_copy_id: 'wc-from-a',
                         relative_path: 'cases/x/working/wc-from-a/甲（工作副本）.txt' });
    });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '创建工作副本' }))
        .toHaveProperty('disabled', false);
    });
    expect(screen.getByRole('button', { name: '重新解析' })).toHaveProperty('disabled', false);
    const saves = screen.getAllByRole('button', { name: '保存' });
    expect(saves.every((button) => !(button as HTMLButtonElement).disabled)).toBe(true);
    expect(screen.getByRole('button', { name: '登记为新版本' }))
      .toHaveProperty('disabled', true);
  });
});


describe('编辑响应的归属', () => {
  it('甲的分类保存迟到时，不得改写乙的修订号、提示和按钮状态', async () => {
    // Review repro: A's classify response arrived while B was mid-save; B showed
    // "分类已保存", B's revision was replaced by A's, and B's buttons went live
    // while B was still saving.
    original.mockImplementation(((_case: string, materialId: string) =>
      Promise.resolve(originalFor(materialId, `fv-${materialId}`))) as never);

    const slowA = deferred<{ revision: number }>();
    const slowB = deferred<{ revision: number }>();
    const classify = vi.mocked(materialsApi.classify);
    classify.mockImplementation(((_case: string, materialId: string) => {
      if (materialId === 'm-a') return slowA.promise;
      if (materialId === 'm-b') return slowB.promise;
      return Promise.resolve({ revision: 9 });
    }) as never);

    await renderAndOpenFirst();
    await screen.findByText(/原件校验/);
    // Start A's save.
    await act(async () => {
      screen.getAllByRole('button', { name: '保存' })[0].click();
    });

    const second = await screen.findByRole('button', { name: /乙材料\.txt/ });
    await act(async () => { second.click(); });
    await screen.findByRole('heading', { name: '乙材料.txt' });

    // Start B's save, then let A's answer land first.
    await act(async () => {
      screen.getAllByRole('button', { name: '保存' })[0].click();
    });
    await act(async () => { slowA.resolve({ revision: 7 }); });

    // A's answer must change nothing about B.
    await waitFor(() => {
      expect(screen.queryByText('分类已保存。')).toBeNull();
    });
    expect(screen.getByText(/版本修订 0/)).toBeTruthy();
    // B is still saving, so its buttons stay disabled.
    expect(screen.getAllByRole('button', { name: '保存' })[0])
      .toHaveProperty('disabled', true);

    // B's own answer is what may update B.
    await act(async () => { slowB.resolve({ revision: 1 }); });
    await waitFor(() => {
      expect(screen.getByText('分类已保存。')).toBeTruthy();
    });
    expect(screen.getByText(/版本修订 1/)).toBeTruthy();
  });
});


describe('切走后的保存仍更新列表', () => {
  it('甲的改名成功返回后，切回甲应显示新名称与新修订号', async () => {
    // Review repro: A's rename was in flight, the user switched to B, then A's
    // success landed.  The ownership guard returned early and skipped the list
    // refresh, so going back to A still showed the old name and revision 0.
    original.mockImplementation(((_case: string, materialId: string) =>
      Promise.resolve(originalFor(materialId, `fv-${materialId}`))) as never);

    const slowRename = deferred<{ revision: number }>();
    vi.mocked(materialsApi.rename).mockImplementation((() => slowRename.promise) as never);

    await renderAndOpenFirst();
    await screen.findByText(/原件校验/);

    // Rename A, then switch to B before the answer lands.
    const nameInput = screen.getAllByRole('textbox')[1];
    fireEvent.change(nameInput, { target: { value: '甲材料（改名后）.txt' } });
    await act(async () => { screen.getAllByRole('button', { name: '保存' })[1].click(); });

    const second = await screen.findByRole('button', { name: /乙材料\.txt/ });
    await act(async () => { second.click(); });
    await screen.findByRole('heading', { name: '乙材料.txt' });

    // The list the server now returns already carries A's new name and revision.
    list.mockResolvedValue({
      items: [material('m-a', '甲材料（改名后）.txt'), material('m-b', '乙材料.txt'),
              material('m-c', '丙材料.txt')],
      total: 3, case_total: 3, categories: ['待分类'],
    } as never);
    list.mockResolvedValueOnce({
      items: [material('m-a', '甲材料（改名后）.txt'), material('m-b', '乙材料.txt'),
              material('m-c', '丙材料.txt')],
      total: 3, case_total: 3, categories: ['待分类'],
    } as never);
    await act(async () => { slowRename.resolve({ revision: 1 }); });

    // B's detail must not be polluted by A's answer.
    await waitFor(() => {
      expect(screen.queryByText('显示名已保存。')).toBeNull();
    });
    expect(screen.getByRole('heading', { name: '乙材料.txt' })).toBeTruthy();

    // Going back to A shows the new name from the refreshed list.
    const back = await screen.findByRole('button', { name: /甲材料/ });
    await act(async () => { back.click(); });
    await screen.findByRole('heading', { name: '甲材料（改名后）.txt' });
  });
  it('甲改名迟到时，当前分类仍只显示该分类中的材料', async () => {
    const pending = deferred<{ revision: number }>();
    const a = { ...material('m-a', '甲材料.txt'), category: '甲分类' };
    const b = { ...material('m-b', '乙材料.txt'), category: '乙分类' };
    list.mockImplementation(async (_case, options) => ({
      items: options?.category === '乙分类' ? [b] : [a, b],
      total: options?.category === '乙分类' ? 1 : 2,
      case_total: 2, categories: ['甲分类', '乙分类'],
    } as never));
    original.mockImplementation(((_case: string, id: string) =>
      Promise.resolve(originalFor(id, `fv-${id}`))) as never);
    vi.mocked(materialsApi.rename).mockReturnValue(pending.promise as never);
    await renderAndOpenFirst();
    await screen.findByText(/原件校验/);
    await act(async () => { screen.getAllByRole('button', { name: '保存' })[1].click(); });
    await act(async () => { screen.getByRole('button', { name: '乙分类' }).click(); });
    expect(screen.queryByRole('button', { name: /甲材料/ })).toBeNull();
    await act(async () => { pending.resolve({ revision: 1 }); });
    expect(screen.getByRole('button', { name: '乙分类' }).getAttribute('aria-current')).toBe('true');
    expect(screen.queryByRole('button', { name: /甲材料/ })).toBeNull();
  });
});
