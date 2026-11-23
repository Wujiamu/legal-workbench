/**
 * 工时面板（P2 持久化）：计时/补录/删除全部走 timeApi；归档只读。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    timeApi: { day: vi.fn(), start: vi.fn(), stop: vi.fn(), add: vi.fn(), remove: vi.fn() },
  };
});

import { timeApi } from '../api/endpoints';
import { CaseTimePanel, localToday } from './CaseTimePanel';
import type { ProgressNode } from './types';

const NODES = [
  { id: 'n1', title: '起草起诉状', removed_at: null },
  { id: 'n2', title: '已移除节点', removed_at: '2026-01-01T00:00:00Z' },
] as unknown as ProgressNode[];

function entry(overrides: Record<string, unknown>) {
  return {
    id: 'e1', case_id: 'case-1', node_id: 'n1', node_title: '起草起诉状',
    work_date: localToday(), hours: 0, source: 'timer', started_at: null,
    stopped_at: null, note: null, created_at: '2026-11-20T00:00:00Z',
    ...overrides,
  };
}

function dayOf(entries: Record<string, unknown>[], summary: Record<string, unknown>[] = []) {
  return {
    work_date: localToday(),
    entries,
    total_hours: entries.reduce((sum, item) => sum + Number(item.hours ?? 0), 0),
    summary,
  };
}

function setup(readOnly = false) {
  render(<CaseTimePanel caseId="case-1" nodes={NODES} readOnly={readOnly} />);
}

describe('CaseTimePanel · 工时持久化', () => {
  beforeEach(() => {
    vi.mocked(timeApi.day).mockReset();
    vi.mocked(timeApi.start).mockReset();
    vi.mocked(timeApi.stop).mockReset();
    vi.mocked(timeApi.add).mockReset();
    vi.mocked(timeApi.remove).mockReset();
  });

  it('开始计时后显示运行中记录，停止时调用 stop', async () => {
    vi.mocked(timeApi.day)
      .mockResolvedValueOnce(dayOf([]) as never)
      .mockResolvedValue(dayOf([entry({ started_at: new Date().toISOString() })]) as never);
    vi.mocked(timeApi.start).mockResolvedValue(entry({}) as never);
    vi.mocked(timeApi.stop).mockResolvedValue(entry({ hours: 0.25, stopped_at: '2026-11-20T01:00:00Z' }) as never);
    setup();
    await waitFor(() => expect(timeApi.day).toHaveBeenCalledWith('case-1', localToday()));
    expect(screen.queryByRole('option', { name: '已移除节点' })).toBeNull();
    fireEvent.change(screen.getByLabelText('节点'), { target: { value: 'n1' } });
    fireEvent.click(screen.getByRole('button', { name: '开始计时' }));
    await waitFor(() => expect(timeApi.start).toHaveBeenCalledWith(
      'case-1', { node_id: 'n1', node_title: '起草起诉状' }));
    const stop = await screen.findByRole('button', { name: '停止（按实际）' });
    fireEvent.click(stop);
    await waitFor(() => expect(timeApi.stop).toHaveBeenCalledWith('case-1', 'e1'));
  });

  it('补录 0.25h 写入服务端，日视图显示合计并可删除', async () => {
    vi.mocked(timeApi.day)
      .mockResolvedValueOnce(dayOf([]) as never)
      .mockResolvedValue(dayOf([entry({ source: 'manual', hours: 0.25 })],
        [{ title: '起草起诉状', hours: 0.25, entries: 1 }]) as never);
    vi.mocked(timeApi.add).mockResolvedValue(entry({ source: 'manual', hours: 0.25 }) as never);
    vi.mocked(timeApi.remove).mockResolvedValue(entry({ source: 'manual', hours: 0.25 }) as never);
    setup();
    await waitFor(() => expect(timeApi.day).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText('节点'), { target: { value: 'n1' } });
    fireEvent.click(screen.getByRole('button', { name: '0.25h' }));
    await waitFor(() => expect(timeApi.add).toHaveBeenCalledWith('case-1', {
      node_id: 'n1', node_title: '起草起诉状', hours: 0.25, work_date: localToday() }));
    fireEvent.click(screen.getByRole('button', { name: '日视图' }));
    const table = await screen.findByRole('table');
    expect(within(table).getByText('起草起诉状')).toBeTruthy();
    expect(within(table).getByText('0.25h')).toBeTruthy();
    fireEvent.click(within(table).getByRole('button', { name: '删除' }));
    await waitFor(() => expect(timeApi.remove).toHaveBeenCalledWith('case-1', 'e1'));
  });

  it('归档后所有写操作禁用并提示只读', async () => {
    vi.mocked(timeApi.day).mockResolvedValue(dayOf([entry({ started_at: new Date().toISOString() })]) as never);
    setup(true);
    expect(screen.getByText('案件已归档，工时只读。')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('节点'), { target: { value: 'n1' } });
    expect((screen.getByRole('button', { name: '开始计时' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '0.25h' }) as HTMLButtonElement).disabled).toBe(true);
    const stop = await screen.findByRole('button', { name: '停止（按实际）' });
    expect((stop as HTMLButtonElement).disabled).toBe(true);
    expect(timeApi.start).not.toHaveBeenCalled();
  });

  it('补录条目（stopped_at 为空）不算进行中的计时', async () => {
    vi.mocked(timeApi.day).mockResolvedValue(dayOf([
      entry({ id: 'm-1', source: 'manual', hours: 0.25, stopped_at: null, started_at: null }),
    ]) as never);
    setup();
    await waitFor(() => expect(timeApi.day).toHaveBeenCalled());
    expect(await screen.findByText('没有进行中的计时。')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '停止（按实际）' })).toBeNull();
  });
});
