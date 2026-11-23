/**
 * 首页接线回归（TEST-ONLY 合成响应，无后端）：时间轴与案件工作表并存，
 * 时间轴数据来自独立接口，不受案件表 50 条分页影响；原有表格行为保留。
 */
import { render, screen, waitFor, cleanup, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    casesApi: { ...actual.casesApi, dashboard: vi.fn(), deadlines: vi.fn(), list: vi.fn() },
  };
});

import { casesApi } from '../api/endpoints';
import type { CaseOverview, DashboardDeadlinesOutput } from '../api/endpoints';
import { CasesPage } from './CasesPage';

const dashboard = vi.mocked(casesApi.dashboard);
const deadlines = vi.mocked(casesApi.deadlines);
const listCases = vi.mocked(casesApi.list);

function overview(overrides: Partial<CaseOverview> & { case_id: string; title: string }): CaseOverview {
  return {
    description: null,
    revision: 0,
    created_at: '2026-11-01T02:00:00Z',
    archived_at: null,
    stage: null,
    next_action: null,
    deadlines: [],
    active_stage: null,
    active_stages: [],
    pending_work: [],
    ...overrides,
  };
}

const PAGE = {
  items: [overview({
    case_id: 'case-page-1', title: 'TEST-ONLY 甲案', stage: '庭审准备',
    deadlines: [{ title: '提交证据', due: '2026-11-24', kind: 'external' as const, completed: false }],
  })],
  offset: 0, limit: 50, total: 51,
};

const TIMELINE: DashboardDeadlinesOutput = {
  as_of: '2026-11-21',
  case_total: 51,
  items: [
    { case_id: 'case-page-1', case_title: 'TEST-ONLY 甲案', title: '提交证据',
      due: '2026-11-24', kind: 'external', completed: false },
    // 只在时间轴出现：模拟第 2 页（offset 50）案件的期限，页面表格拿不到。
    { case_id: 'case-page-51', case_title: 'TEST-ONLY 后页案件', title: '二审开庭',
      due: '2027-01-08', kind: 'external', completed: false },
    { case_id: 'case-page-1', case_title: 'TEST-ONLY 甲案', title: '更早的逾期事项',
      due: '2026-10-30', kind: 'internal', completed: false },
  ],
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('CasesPage 期限时间轴接线', () => {
  it('表格与时间轴同时渲染，时间轴覆盖分页之外的在办案件', async () => {
    dashboard.mockResolvedValue(PAGE);
    deadlines.mockResolvedValue(TIMELINE);
    render(<MemoryRouter><CasesPage /></MemoryRouter>);

    await screen.findByRole('link', { name: 'TEST-ONLY 甲案' });
    const timeline = await screen.findByRole('region', { name: '跨案期限时间轴' });
    expect(within(timeline).getByText('今天')).toBeTruthy();
    // 第 2 页案件的期限出现在时间轴，而表格里没有这个案件。
    expect(within(timeline).getByText('TEST-ONLY 后页案件')).toBeTruthy();
    const table = screen.getByRole('table');
    expect(within(table).queryByText('TEST-ONLY 后页案件')).toBeNull();
    expect(within(table).getAllByRole('row')).toHaveLength(2);
    // 更早的未完成期限进入“更早逾期”，不被隐藏。
    expect(within(timeline).getByRole('group', { name: '更早逾期 1 项' })).toBeTruthy();
    // 原表格行为保留：表头、当前节点、最近期限、分页（total 51 > 50）。
    expect(screen.getByRole('columnheader', { name: '最近期限' })).toBeTruthy();
    expect(screen.getByText('庭审准备')).toBeTruthy();
    expect(screen.getByText('共 51 个案件')).toBeTruthy();
    expect(screen.getByRole('button', { name: '下一页' })).toBeTruthy();
  });

  it('切换窗口只影响时间轴，不重新请求案件表', async () => {
    dashboard.mockResolvedValue(PAGE);
    deadlines.mockResolvedValue(TIMELINE);
    const user = userEvent.setup();
    render(<MemoryRouter><CasesPage /></MemoryRouter>);
    await screen.findByRole('link', { name: 'TEST-ONLY 甲案' });
    const callsAfterLoad = dashboard.mock.calls.length;
    await user.click(screen.getByRole('button', { name: '前7天~后30天' }));
    expect(screen.queryByText('TEST-ONLY 后页案件')).toBeNull();
    expect(dashboard.mock.calls.length).toBe(callsAfterLoad);
    expect(screen.getByRole('link', { name: 'TEST-ONLY 甲案' })).toBeTruthy();
  });

  it('时间轴接口失败不影响案件表，并提供重试', async () => {
    dashboard.mockResolvedValue(PAGE);
    deadlines.mockRejectedValue(new Error('offline'));
    render(<MemoryRouter><CasesPage /></MemoryRouter>);
    await screen.findByRole('link', { name: 'TEST-ONLY 甲案' });
    await waitFor(() => expect(screen.getByText(/无法连接本地服务/)).toBeTruthy());
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy();
  });

  it('首屏不展示原型入口，支持切换查看并找回已归档案件', async () => {
    dashboard.mockResolvedValue(PAGE);
    deadlines.mockResolvedValue(TIMELINE);
    listCases.mockResolvedValue({
      items: [overview({
        case_id: 'case-archived-1',
        title: 'TEST-ONLY 已归档测试案',
        archived_at: '2026-11-23T03:58:16Z',
      })],
      offset: 0,
      limit: 50,
      total: 1,
    });
    const user = userEvent.setup();
    render(<MemoryRouter><CasesPage /></MemoryRouter>);

    await screen.findByRole('link', { name: 'TEST-ONLY 甲案' });
    expect(screen.queryByRole('link', { name: /原型/ })).toBeNull();

    await user.click(screen.getByRole('button', { name: '查看已归档' }));
    await screen.findByRole('link', { name: 'TEST-ONLY 已归档测试案' });
    expect(screen.getByText('已归档 2026-11-23')).toBeTruthy();
    expect(screen.getByText('共 1 个已归档案件')).toBeTruthy();
    expect(listCases).toHaveBeenCalledWith(0, 50, expect.any(AbortSignal), true);

    await user.click(screen.getByRole('button', { name: '返回在办案件' }));
    await screen.findByRole('link', { name: 'TEST-ONLY 甲案' });
  });
});
