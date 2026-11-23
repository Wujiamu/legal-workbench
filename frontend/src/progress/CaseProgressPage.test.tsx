/**
 * 正式案件进度页 · 关联材料（审查末节）：材料清单分页加载，不再固定只取前 200；
 * 读取失败与空列表分开呈现并可重试；可找到并关联第 201 项。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    casesApi: { ...actual.casesApi, overview: vi.fn(), archive: vi.fn(), unarchive: vi.fn() },
    materialsApi: { ...actual.materialsApi, list: vi.fn() },
    progressApi: {
      ...actual.progressApi,
      rules: vi.fn(), snapshot: vi.fn(), seqs: vi.fn(), link: vi.fn(), setActiveStage: vi.fn(),
    },
  };
});

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useParams: () => ({ caseId: 'case-1' }) };
});

import { MemoryRouter } from 'react-router';
import { casesApi, materialsApi, progressApi } from '../api/endpoints';
import { buildLitigationFixture } from './fixtures';
import { defaultRules } from './rules';
import CaseProgressPage from './CaseProgressPage';

const ALL = Array.from({ length: 250 }, (_, index) => ({
  material_id: `m-${index + 1}`,
  display_name: `TEST-ONLY 材料 ${index + 1}`,
}));

function pageOf(offset: number, limit: number) {
  return {
    items: ALL.slice(offset, offset + limit).map((item) => ({
      material_id: item.material_id, display_name: item.display_name,
    })),
    offset, limit, total: ALL.length,
  };
}

function setup(overview: Record<string, unknown> = {}) {
  const executor = buildLitigationFixture();
  const snapshot = executor.getSnapshot();
  vi.mocked(casesApi.overview).mockResolvedValue({
    title: 'TEST-ONLY 案件', revision: 0, archived_at: null, ...overview } as never);
  vi.mocked(progressApi.rules).mockResolvedValue({ rules: defaultRules } as never);
  vi.mocked(progressApi.snapshot).mockResolvedValue(snapshot as never);
  vi.mocked(progressApi.seqs).mockResolvedValue({ seqs: { progress: snapshot.seq } } as never);
  vi.mocked(progressApi.link).mockResolvedValue(snapshot as never);
  render(<MemoryRouter><CaseProgressPage /></MemoryRouter>);
  return { executor, snapshot };
}

async function openLinkDialog() {
  const item = await screen.findByRole('treeitem', { name: /庭前程序/ });
  const button = item.querySelector('.node-btn') as HTMLElement;
  fireEvent.contextMenu(button, { clientX: 10, clientY: 10 });
  fireEvent.click(screen.getByRole('menuitem', { name: '关联材料…' }));
  return screen.getByRole('dialog');
}

describe('CaseProgressPage · 关联材料分页（审查末节）', () => {
  beforeEach(() => {
    vi.mocked(materialsApi.list).mockReset();
  });

  it('首屏 200 项、显示加载更多，加载后可选并关联第 201 项', async () => {
    vi.mocked(materialsApi.list).mockImplementation(((_case: string, params: { offset: number; limit: number }) =>
      Promise.resolve(pageOf(params.offset, params.limit))) as never);
    setup();
    const dialog = await openLinkDialog();
    await waitFor(() => expect(within(dialog).getByText(/已显示 200 \/ 250 项/)).toBeTruthy());
    const select = within(dialog).getByLabelText('案内材料') as HTMLSelectElement;
    expect(select.querySelectorAll('option')).toHaveLength(201); // 占位 + 200
    expect(within(dialog).queryByRole('option', { name: 'TEST-ONLY 材料 201' })).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: '加载更多' }));
    await waitFor(() =>
      expect(within(dialog).queryByRole('option', { name: 'TEST-ONLY 材料 201' })).toBeTruthy());
    expect(select.querySelectorAll('option')).toHaveLength(251); // 占位 + 250
    expect(within(dialog).queryByRole('button', { name: '加载更多' })).toBeNull();
    // 选中第 201 项并关联
    fireEvent.change(select, { target: { value: 'm-201' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '关联' }));
    await waitFor(() => expect(progressApi.link).toHaveBeenCalledWith(
      'case-1', expect.any(String),
      expect.objectContaining({ material_id: 'm-201' })));
  });

  it('读取失败与空列表分开呈现：显示错误与重试，重试成功后出现材料', async () => {
    vi.mocked(materialsApi.list).mockRejectedValueOnce(new Error('网络中断'));
    setup();
    const dialog = await openLinkDialog();
    await waitFor(() => expect(within(dialog).getByRole('alert').textContent).toContain('材料清单读取失败'));
    expect(within(dialog).queryByText('本案暂无已登记材料。')).toBeNull();
    vi.mocked(materialsApi.list).mockImplementation(((_case: string, params: { offset: number; limit: number }) =>
      Promise.resolve(pageOf(params.offset, params.limit))) as never);
    fireEvent.click(within(dialog).getByRole('button', { name: '重试' }));
    await waitFor(() =>
      expect(within(dialog).queryByRole('option', { name: 'TEST-ONLY 材料 1' })).toBeTruthy());
    expect(within(dialog).queryByRole('alert')).toBeNull();
  });
});

describe('CaseProgressPage · 结案归档（树外按钮）', () => {
  beforeEach(() => {
    vi.mocked(casesApi.archive).mockReset();
    vi.mocked(casesApi.unarchive).mockReset();
    vi.mocked(progressApi.setActiveStage).mockReset();
    vi.mocked(materialsApi.list).mockResolvedValue(
      { items: [], offset: 0, limit: 200, total: 0 } as never);
  });

  it('确认归档调用 archive 并转为只读提示', async () => {
    setup({ revision: 3 });
    fireEvent.click(await screen.findByRole('button', { name: '结案归档' }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText(/归档后本案的进度、材料与工时变为只读/)).toBeTruthy();
    vi.mocked(casesApi.archive).mockResolvedValue({
      title: 'TEST-ONLY 案件', revision: 4, archived_at: '2026-11-20T10:00:00Z' } as never);
    fireEvent.click(within(dialog).getByRole('button', { name: '确认归档' }));
    await waitFor(() => expect(casesApi.archive).toHaveBeenCalledWith('case-1', 3));
    expect(await screen.findByText(/案件已归档（2026-11-20）/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: '结案归档' })).toBeNull();
    expect(screen.getByRole('button', { name: '取消归档' })).toBeTruthy();
  });

  it('归档后进度写入被本地拦截并提示，不发请求', async () => {
    setup({ revision: 4, archived_at: '2026-11-20T10:00:00Z' });
    const item = await screen.findByRole('treeitem', { name: /庭前程序/ });
    expect((screen.getByRole('button', { name: '＋ 添加' }) as HTMLButtonElement).disabled).toBe(true);
    const button = item.querySelector('.node-btn') as HTMLElement;
    fireEvent.contextMenu(button, { clientX: 10, clientY: 10 });
    expect((screen.getByRole('menuitem', { name: '编辑' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('menuitem', { name: '关联材料…' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('menuitem', { name: '设为进行中' }));
    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toContain('案件已归档，进度只读'));
    expect(progressApi.setActiveStage).not.toHaveBeenCalled();
  });

  it('取消归档调用 unarchive 并恢复写入入口', async () => {
    setup({ revision: 5, archived_at: '2026-11-20T10:00:00Z' });
    vi.mocked(casesApi.unarchive).mockResolvedValue({
      title: 'TEST-ONLY 案件', revision: 6, archived_at: null } as never);
    fireEvent.click(await screen.findByRole('button', { name: '取消归档' }));
    await waitFor(() => expect(casesApi.unarchive).toHaveBeenCalledWith('case-1', 5));
    await waitFor(() => expect(screen.getByRole('button', { name: '结案归档' })).toBeTruthy());
    expect(screen.queryByText(/案件已归档/)).toBeNull();
  });

  it('修订号过期（409）时刷新概览，重试用新修订号成功', async () => {
    setup({ revision: 3 });
    const { ApiError } = await import('../api/client');
    vi.mocked(casesApi.archive).mockRejectedValueOnce(
      new ApiError(409, { code: 'conflict', message: '案件已变化，请刷新后重试',
        details: {}, request_id: 'r-1' }));
    vi.mocked(casesApi.overview).mockResolvedValue({
      title: 'TEST-ONLY 案件', revision: 9, archived_at: null } as never);
    fireEvent.click(await screen.findByRole('button', { name: '结案归档' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '确认归档' }));
    await waitFor(() =>
      expect(within(screen.getByRole('dialog')).getByRole('alert').textContent)
        .toContain('请刷新后重试'));
    vi.mocked(casesApi.archive).mockResolvedValue({
      title: 'TEST-ONLY 案件', revision: 10, archived_at: '2026-11-20T10:00:00Z' } as never);
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '确认归档' }));
    await waitFor(() => expect(casesApi.archive).toHaveBeenLastCalledWith('case-1', 9));
    expect(await screen.findByText(/案件已归档（2026-11-20）/)).toBeTruthy();
  });
});
