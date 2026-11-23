/**
 * 首页跨案期限时间轴组件测试（TEST-ONLY 合成数据，无后端）。
 *
 * 覆盖计划第一节的验收口径：默认 -7/+60、窗口切换、统一上海“今天”锚点、
 * 更早逾期汇总、完成状态默认隐藏、外部/内部样式、同日堆叠、点击跳概览。
 * 760px 窄窗布局由 CSS 媒体查询决定，jsdom 不计算布局，改由浏览器实测覆盖。
 */
import { render, screen, within, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';

import { DeadlineTimeline } from './DeadlineTimeline';
import type { DashboardDeadlineItem } from '../api/endpoints';

const TODAY = '2026-11-21';

function item(overrides: Partial<DashboardDeadlineItem> & {
  due: string; title: string;
}): DashboardDeadlineItem {
  return {
    case_id: 'case-a',
    case_title: 'TEST-ONLY 甲案',
    kind: 'external',
    completed: false,
    ...overrides,
  };
}

const ITEMS: DashboardDeadlineItem[] = [
  item({ due: '2026-09-01', title: '更早逾期两个月', case_id: 'case-old' }),
  item({ due: '2026-11-13', title: '逾期十五天', case_id: 'case-early' }),
  item({ due: '2026-11-14', title: '窗口左边界', case_id: 'case-left' }),
  item({ due: '2026-11-21', title: '今日开庭', case_id: 'case-today-a' }),
  item({ due: '2026-11-21', title: '今日内部复核', kind: 'internal', case_id: 'case-today-b' }),
  item({ due: '2026-11-25', title: '已完成送达', completed: true, case_id: 'case-done' }),
  item({ due: '2026-12-21', title: '后30天边界', case_id: 'case-30' }),
  item({ due: '2026-12-22', title: '后31天', case_id: 'case-31' }),
  item({ due: '2027-01-05', title: '后45天', case_id: 'case-45' }),
  item({ due: '2027-01-20', title: '后60天边界', case_id: 'case-60' }),
  item({ due: '2027-01-21', title: '后61天之外', case_id: 'case-61' }),
  item({ due: '2027-02-09', title: '后80天', case_id: 'case-80' }),
  item({ due: '2027-03-01', title: '后100天', case_id: 'case-100' }),
];

function renderTimeline(items: DashboardDeadlineItem[] = ITEMS, caseTotal = 7) {
  return render(<MemoryRouter>
    <DeadlineTimeline items={items} today={TODAY} caseTotal={caseTotal} />
  </MemoryRouter>);
}

afterEach(cleanup);

describe('DeadlineTimeline', () => {
  it('默认窗口为前 7 天至后 60 天，并把更早未完成期限汇总为“更早逾期”', () => {
    renderTimeline();
    expect(screen.getByText('窗口左边界')).toBeTruthy();
    expect(screen.getByText('后60天边界')).toBeTruthy();
    expect(screen.getByText('后45天')).toBeTruthy();
    expect(screen.queryByText('后61天之外')).toBeNull();
    expect(screen.queryByText('后80天')).toBeNull();
    // 左边界之前未完成：固定在“更早逾期”，不静默隐藏（含两个月前的）。
    const early = screen.getByRole('group', { name: '更早逾期 2 项' });
    expect(within(early).getByText('逾期十五天')).toBeTruthy();
    expect(within(early).getByText('更早逾期两个月')).toBeTruthy();
    // 默认按钮即“前7天~后60天”。
    expect(screen.getByRole('button', { name: '前7天~后60天' }).getAttribute('aria-pressed'))
      .toBe('true');
    expect(screen.getByText(/覆盖 7 个在办案件/)).toBeTruthy();
  });

  it('窗口切换实时更新可见期限', async () => {
    const user = userEvent.setup();
    renderTimeline();
    await user.click(screen.getByRole('button', { name: '前7天~后30天' }));
    expect(screen.getByText('后30天边界')).toBeTruthy();
    expect(screen.queryByText('后31天')).toBeNull();
    expect(screen.queryByText('后45天')).toBeNull();
    expect(screen.queryByText('后60天边界')).toBeNull();

    await user.click(screen.getByRole('button', { name: '前7天~后90天' }));
    expect(screen.getByText('后80天')).toBeTruthy();
    expect(screen.getByText('后61天之外')).toBeTruthy();
    expect(screen.queryByText('后100天')).toBeNull();

    await user.click(screen.getByRole('button', { name: '全部未完成' }));
    expect(screen.getByText('后61天之外')).toBeTruthy();
    expect(screen.getByText('后80天')).toBeTruthy();
    expect(screen.getByText('后100天')).toBeTruthy();
    // “全部未完成”没有左边界，更早的逾期项回到各自的日期列。
    expect(screen.queryByRole('group', { name: /更早逾期/ })).toBeNull();
    expect(screen.getByText('更早逾期两个月')).toBeTruthy();
  });

  it('已完成期限默认隐藏，勾选后可见且不计逾期', async () => {
    const user = userEvent.setup();
    renderTimeline();
    expect(screen.queryByText('已完成送达')).toBeNull();
    expect(screen.getByText(/另有 1 项已完成期限未显示/)).toBeTruthy();
    await user.click(screen.getByRole('checkbox', { name: '显示已完成' }));
    const done = screen.getByText('已完成送达');
    const card = done.closest('a')!;
    expect(card.className).toContain('is-completed');
    expect(card.className).not.toContain('is-overdue');
    expect(within(card).queryByText('已逾期')).toBeNull();
    await user.click(screen.getByRole('checkbox', { name: '显示已完成' }));
    expect(screen.queryByText('已完成送达')).toBeNull();
  });

  it('逾期标红用文字与样式双重表达，外部/内部区分边框', () => {
    renderTimeline();
    const overdue = screen.getByText('逾期十五天').closest('a')!;
    expect(overdue.className).toContain('is-overdue');
    expect(within(overdue).getByText('已逾期')).toBeTruthy();
    const external = screen.getByText('今日开庭').closest('a')!;
    expect(external.className).toContain('is-external');
    const internal = screen.getByText('今日内部复核').closest('a')!;
    expect(internal.className).toContain('is-internal');
    expect(within(internal).getByText('内部计划')).toBeTruthy();
  });

  it('同一天多条期限在同一日期列内纵向堆叠，并保留“今天”锚点', () => {
    renderTimeline();
    const todayColumn = screen.getByText('今日开庭').closest<HTMLElement>('li.timeline-day')!;
    expect(todayColumn.className).toContain('is-today');
    expect(within(todayColumn).getByText('今天')).toBeTruthy();
    const stacked = todayColumn.querySelectorAll('.timeline-stack > li');
    expect(stacked).toHaveLength(2);
    expect(stacked[0].textContent).toContain('今日开庭');
    expect(stacked[1].textContent).toContain('今日内部复核');
    // 日期列按真实 DOM 顺序排列：左边界 → 今天 → 后 45 天。
    const days = [...document.querySelectorAll('.timeline-day')];
    const dates = days.map((day) => day.querySelector('time')?.getAttribute('dateTime'));
    expect(dates).toEqual([...dates].sort());
    expect(dates.indexOf('2026-11-21')).toBeGreaterThan(dates.indexOf('2026-11-14'));
    expect(dates.indexOf('2026-11-21')).toBeLessThan(dates.indexOf('2027-01-05'));
  });

  it('宽屏“更早逾期”是横轨左端的独立红色列，日期列在其右侧的滚动轨道里', () => {
    renderTimeline();
    const early = screen.getByRole('group', { name: '更早逾期 2 项' });
    const track = screen.getByRole('list', { name: '按日期排列的期限' });
    const board = early.parentElement!;
    expect(board.className).toContain('timeline-board');
    expect(track.parentElement).toBe(board);
    // 红区在日期轨道之前，即同一横轨的最左端。
    expect(early.compareDocumentPosition(track) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
    // 红区内部纵向堆叠，不与日期列混排。
    const earlyList = early.querySelector('.timeline-early-list')!;
    expect(earlyList.children).toHaveLength(2);
    expect(earlyList.className).toContain('timeline-early-list');
  });

  it('更早逾期较多时全部保留在同一可滚动红列内，不散落到日期轨道', () => {
    const manyEarly = Array.from({ length: 12 }, (_, index) =>
      item({ due: `2026-09-${String(index + 1).padStart(2, '0')}`, title: `逾期第${index + 1}项` }));
    renderTimeline([...manyEarly, item({ due: '2026-11-25', title: '窗口内一项' })]);
    const early = screen.getByRole('group', { name: '更早逾期 12 项' });
    expect(early.querySelectorAll('.timeline-early-list > li')).toHaveLength(12);
    const track = screen.getByRole('list', { name: '按日期排列的期限' });
    expect(within(track).queryByText('逾期第1项')).toBeNull();
    expect(within(track).getByText('窗口内一项')).toBeTruthy();
  });

  it('没有更早逾期时不渲染红色列，轨道仍是横轨容器的唯一子块', () => {
    renderTimeline([item({ due: '2026-11-25', title: '仅窗口内' })]);
    expect(screen.queryByRole('group', { name: /更早逾期/ })).toBeNull();
    const track = screen.getByRole('list', { name: '按日期排列的期限' });
    const board = track.parentElement!;
    expect(board.className).toContain('timeline-board');
    expect(board.querySelectorAll('.timeline-early')).toHaveLength(0);
  });

  it('点击任意期限卡片跳转到对应案件概览页', () => {
    renderTimeline();
    const link = screen.getByText('后45天').closest('a')!;
    expect(link.getAttribute('href')).toBe('/cases/case-45?view=overview');
    const earlyLink = screen.getByText('逾期十五天').closest('a')!;
    expect(earlyLink.getAttribute('href')).toBe('/cases/case-early?view=overview');
  });

  it('窗口内没有期限时给出明确空状态，今天锚点不伪造内容', () => {
    renderTimeline([]);
    expect(screen.getByText('该窗口内没有未完成期限。')).toBeTruthy();
    expect(screen.getByText(/窗口内 0 项/)).toBeTruthy();
  });
});
