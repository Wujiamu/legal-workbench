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
    casesApi: {
      ...actual.casesApi,
      dashboard: vi.fn(),
      deadlines: vi.fn(),
      list: vi.fn(),
      updateMetadata: vi.fn(),
      create: vi.fn(),
    },
  };
});

import { casesApi } from '../api/endpoints';
import type { CaseOverview, DashboardDeadlinesOutput } from '../api/endpoints';
import { CasesPage } from './CasesPage';

const dashboard = vi.mocked(casesApi.dashboard);
const deadlines = vi.mocked(casesApi.deadlines);
const listCases = vi.mocked(casesApi.list);
const updateMetadata = vi.mocked(casesApi.updateMetadata);
const createCase = vi.mocked(casesApi.create);

function overview(overrides: Partial<CaseOverview> & { case_id: string; title: string }): CaseOverview {
  return {
    description: null,
    case_number: null,
    plaintiff: null,
    defendant: null,
    work_status: 'normal',
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
    expect(screen.getByRole('button', { name: /最近期限/ })).toBeTruthy();
    expect(screen.getByText('庭审准备')).toBeTruthy();
    expect(screen.getByText('共 51 个案件')).toBeTruthy();
    expect(screen.getByRole('button', { name: '下一页' })).toBeTruthy();
  });

  it('支持按关键词/办理状态/期限筛选、列头排序及行内切换办理状态', async () => {
    const rich = overview({
      case_id: 'case-rich-1',
      title: 'TEST-ONLY 货款案',
      case_number: '(2026)沪0115民初888号',
      plaintiff: '甲公司',
      defendant: '乙厂',
      work_status: 'need_materials',
      revision: 2,
    });
    dashboard.mockResolvedValue({ items: [rich], offset: 0, limit: 50, total: 1 });
    deadlines.mockResolvedValue(TIMELINE);
    updateMetadata.mockResolvedValue({ ...rich, work_status: 'need_sync', revision: 3 });

    const user = userEvent.setup();
    render(<MemoryRouter><CasesPage /></MemoryRouter>);

    await screen.findByText('(2026)沪0115民初888号');
    expect(screen.getByText('甲公司')).toBeTruthy();
    expect(screen.getByText('乙厂')).toBeTruthy();

    // 行内变更办理状态走 CAS updateMetadata
    await user.selectOptions(screen.getByLabelText('办理状态 TEST-ONLY 货款案'), 'need_sync');
    await waitFor(() => {
      expect(updateMetadata).toHaveBeenCalledWith('case-rich-1', {
        expected_revision: 2,
        case_number: '(2026)沪0115民初888号',
        plaintiff: '甲公司',
        defendant: '乙厂',
        work_status: 'need_sync',
      });
    });

    // 状态筛选与排序触发服务端查询参数
    await user.selectOptions(screen.getByLabelText('按办理状态筛选'), 'need_materials');
    await waitFor(() => {
      expect(dashboard).toHaveBeenLastCalledWith(
        0, 50, expect.any(AbortSignal),
        expect.objectContaining({ work_status: 'need_materials', sort_by: 'next_due', sort_dir: 'asc' }),
      );
    });

    await user.click(screen.getByRole('button', { name: /办理状态/ }));
    await waitFor(() => {
      expect(dashboard).toHaveBeenLastCalledWith(
        0, 50, expect.any(AbortSignal),
        expect.objectContaining({ sort_by: 'work_status', sort_dir: 'asc' }),
      );
    });

    await user.click(screen.getByRole('button', { name: '重置' }));
    await waitFor(() => {
      expect(dashboard).toHaveBeenLastCalledWith(0, 50, expect.any(AbortSignal), undefined);
    });
  });

  it('新建案件可携带选填案号、原被告与办理状态', async () => {
    dashboard.mockResolvedValue({ items: [], offset: 0, limit: 50, total: 0 });
    deadlines.mockResolvedValue(TIMELINE);
    createCase.mockResolvedValue({ case_id: 'created-1', revision: 0 });

    const user = userEvent.setup();
    render(<MemoryRouter><CasesPage /></MemoryRouter>);
    await screen.findByText('还没有案件');

    await user.click(screen.getByRole('button', { name: '＋ 新建案件' }));
    const dlg = screen.getByRole('dialog');
    await user.type(within(dlg).getByLabelText('案件名称'), '新案测试');
    await user.type(within(dlg).getByLabelText('案号（选填）'), '(2026)苏05民初12号');
    await user.type(within(dlg).getByLabelText('原告 / 申请人（选填）'), '原告张三');
    await user.type(within(dlg).getByLabelText('被告 / 被申请人（选填）'), '被告李四');
    await user.selectOptions(within(dlg).getByLabelText('办理状态'), 'need_materials');
    await user.click(within(dlg).getByRole('button', { name: '创建案件' }));

    await waitFor(() => {
      expect(createCase).toHaveBeenCalledWith('新案测试', undefined, {
        case_number: '(2026)苏05民初12号',
        plaintiff: '原告张三',
        defendant: '被告李四',
        work_status: 'need_materials',
      });
    });
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
    expect(listCases).toHaveBeenCalledWith(0, 50, expect.any(AbortSignal), true, undefined);

    await user.click(screen.getByRole('button', { name: '返回在办案件' }));
    await screen.findByRole('link', { name: 'TEST-ONLY 甲案' });
  });
});
