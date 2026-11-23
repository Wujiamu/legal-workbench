import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import type { ChartCitationTableOut, ChartPreflightOut } from '../api/endpoints';
import { chartsApi } from '../api/endpoints';
import { ClaimsPanel } from '../claims/ClaimsPanel';
import { buildClaimsFixture } from '../claims/fixtures';
import { ChartExportDialog } from './ChartExportDialog';
import type { ChartExportSettings } from './ChartExportDialog';

function makeMeta(profile: 'working' | 'client' | 'court', snapshotId: string | null = null): ChartPreflightOut['chart_meta'] {
  return {
    case_id: 'case-1',
    chart_type: 'graph',
    target_id: 'graph-1',
    profile,
    title: '股权与实控关系',
    generated_at: '2026-11-22T07:00:00Z',
    snapshot_id: snapshotId,
    snapshot_version: snapshotId ? 2 : null,
    frozen_at: snapshotId ? '2026-11-22T07:00:00Z' : null,
    digest: null,
    currency: null,
    contains_suspected: false,
  };
}

function makePreflight(
  profile: 'working' | 'client' | 'court',
  issues: ChartPreflightOut['issues'] = [],
  snapshotId: string | null = null,
): ChartPreflightOut {
  return {
    chart_meta: makeMeta(profile, snapshotId),
    issues,
  };
}

describe('ChartExportDialog · 一源三态与门禁交互', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('工作态直接启用 SVG/PNG，不请求 preflight，不显示 Markdown/CSV', async () => {
    const preflight = vi.fn<
      (settings: ChartExportSettings, signal: AbortSignal) => Promise<ChartPreflightOut>
    >();
    const download = vi.fn().mockResolvedValue(undefined);

    render(
      <ChartExportDialog
        title="股权图"
        readOnly={false}
        onClose={vi.fn()}
        preflight={preflight}
        download={download}
      />,
    );

    expect(preflight).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: '引用索引 Markdown' })).toBeNull();
    expect(screen.queryByRole('button', { name: '引用索引 CSV' })).toBeNull();
    expect(screen.queryByLabelText('图表版本')).toBeNull();

    const svgBtn = screen.getByRole('button', { name: '下载 SVG' }) as HTMLButtonElement;
    const pngBtn = screen.getByRole('button', { name: '下载 PNG' }) as HTMLButtonElement;
    expect(svgBtn.disabled).toBe(false);
    expect(pngBtn.disabled).toBe(false);

    await userEvent.click(screen.getByRole('checkbox', { name: '显示证据编号' }));
    await userEvent.click(svgBtn);

    expect(download).toHaveBeenCalledWith(
      'svg',
      { profile: 'working', snapshotId: '', excludedSuspected: [] },
      null,
      [],
      true,
      expect.any(AbortSignal),
    );
  });

  it('客户沟通态预检门禁：加载中禁用全部导出，通过后启用 SVG/PNG/MD/CSV', async () => {
    let resolvePreflight!: (value: ChartPreflightOut) => void;
    const preflight = vi.fn(
      () => new Promise<ChartPreflightOut>((resolve) => { resolvePreflight = resolve; }),
    );
    const download = vi.fn().mockResolvedValue(undefined);

    render(
      <ChartExportDialog
        title="诉请组"
        readOnly={false}
        onClose={vi.fn()}
        preflight={preflight}
        download={download}
      />,
    );

    await userEvent.selectOptions(screen.getByLabelText('导出模式'), 'client');

    expect(screen.getByRole('status').textContent).toContain('正在核对引用');
    const mdBtn = screen.getByRole('button', { name: '引用索引 Markdown' }) as HTMLButtonElement;
    const csvBtn = screen.getByRole('button', { name: '引用索引 CSV' }) as HTMLButtonElement;
    const svgBtn = screen.getByRole('button', { name: '下载 SVG' }) as HTMLButtonElement;
    const pngBtn = screen.getByRole('button', { name: '下载 PNG' }) as HTMLButtonElement;
    expect(mdBtn.disabled).toBe(true);
    expect(csvBtn.disabled).toBe(true);
    expect(svgBtn.disabled).toBe(true);
    expect(pngBtn.disabled).toBe(true);

    resolvePreflight(makePreflight('client', []));

    await waitFor(() => {
      expect(screen.getByRole('status').textContent).toContain('引用预检通过');
    });
    expect(mdBtn.disabled).toBe(false);
    expect(csvBtn.disabled).toBe(false);
    expect(svgBtn.disabled).toBe(false);
    expect(pngBtn.disabled).toBe(false);

    await userEvent.click(mdBtn);
    await userEvent.click(csvBtn);

    expect(download).toHaveBeenNthCalledWith(
      1,
      'md',
      { profile: 'client', snapshotId: '', excludedSuspected: [] },
      expect.objectContaining({ issues: [] }),
      [],
      false,
      expect.any(AbortSignal),
    );
    expect(download).toHaveBeenNthCalledWith(
      2,
      'csv',
      { profile: 'client', snapshotId: '', excludedSuspected: [] },
      expect.objectContaining({ issues: [] }),
      [],
      false,
      expect.any(AbortSignal),
    );
  });

  it('不可豁免阻断永久禁用导出；可豁免阻断须填写非空原因后方可使用并按 code:target_id 区分', async () => {
    const preflight = vi.fn().mockResolvedValue(
      makePreflight('court', [
        {
          severity: 'blocker',
          code: 'missing_source',
          target_id: 'target-1',
          message: '「违约金」缺少出处',
          can_override: true,
        },
        {
          severity: 'blocker',
          code: 'uncataloged_source',
          target_id: 'target-1',
          message: '「违约金」材料未编目',
          can_override: true,
        },
      ]),
    );
    const download = vi.fn().mockResolvedValue(undefined);
    const onLocate = vi.fn();

    render(
      <ChartExportDialog
        title="诉请组"
        readOnly={false}
        onClose={vi.fn()}
        onLocate={onLocate}
        preflight={preflight}
        download={download}
      />,
    );

    await userEvent.selectOptions(screen.getByLabelText('导出模式'), 'court');
    await screen.findByText(/「违约金」缺少出处/);

    // 定位按钮回调 target_id
    await userEvent.click(screen.getAllByRole('button', { name: '定位' })[0]);
    expect(onLocate).toHaveBeenCalledWith('target-1');

    const overrides = screen.getAllByRole('checkbox', { name: '确认此项例外' });
    expect(overrides).toHaveLength(2);

    // 勾选第一项但只填空白原因，仍禁用导出
    await userEvent.click(overrides[0]);
    const firstReason = screen.getAllByLabelText('说明例外原因')[0];
    await userEvent.type(firstReason, '   ');
    expect((screen.getByRole('button', { name: '下载 SVG' }) as HTMLButtonElement).disabled).toBe(true);

    // 填实第一项并勾选、填实第二项，同一 target_id 不同 code 互不覆盖
    await userEvent.clear(firstReason);
    await userEvent.type(firstReason, '当庭补交原件');
    expect((screen.getByRole('button', { name: '下载 SVG' }) as HTMLButtonElement).disabled).toBe(true);

    await userEvent.click(overrides[1]);
    const secondReason = screen.getAllByLabelText('说明例外原因')[1];
    await userEvent.type(secondReason, '暂以开庭笔录载明为准');

    const svgBtn = screen.getByRole('button', { name: '下载 SVG' }) as HTMLButtonElement;
    await waitFor(() => expect(svgBtn.disabled).toBe(false));

    await userEvent.click(svgBtn);
    expect(download).toHaveBeenCalledWith(
      'svg',
      { profile: 'court', snapshotId: '', excludedSuspected: [] },
      expect.any(Object),
      [
        { code: 'missing_source', target_id: 'target-1', reason: '当庭补交原件' },
        { code: 'uncataloged_source', target_id: 'target-1', reason: '暂以开庭笔录载明为准' },
      ],
      false,
      expect.any(AbortSignal),
    );
  });

  it('不可豁免阻断（can_override=false）禁用导出且不提供例外勾选', async () => {
    const preflight = vi.fn().mockResolvedValue(
      makePreflight('court', [
        {
          severity: 'blocker',
          code: 'pending_changes_present',
          target_id: 'group-1',
          message: '存在待确认变更',
          can_override: false,
        },
      ]),
    );

    render(
      <ChartExportDialog
        title="诉请组"
        readOnly={false}
        onClose={vi.fn()}
        preflight={preflight}
        download={vi.fn()}
      />,
    );

    await userEvent.selectOptions(screen.getByLabelText('导出模式'), 'court');
    await screen.findByText(/存在待确认变更/);

    expect(screen.queryByRole('checkbox', { name: /确认此项例外/ })).toBeNull();
    expect((screen.getByRole('button', { name: '下载 SVG' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '引用索引 CSV' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('当庭态 suspected_in_court 支持“排除待查关系”（自动重跑 preflight 与恢复纳入）与“标为待举证主张”', async () => {
    const preflight = vi.fn<(s: ChartExportSettings, sig: AbortSignal) => Promise<ChartPreflightOut>>(
      async (settings) => {
        const issues: ChartPreflightOut['issues'] = [
          {
            severity: 'blocker',
            code: 'missing_source',
            target_id: 'edge-other',
            message: '另一条边缺出处',
            can_override: true,
          },
        ];
        if (!settings.excludedSuspected.includes('edge-susp')) {
          issues.unshift({
            severity: 'blocker',
            code: 'suspected_in_court',
            target_id: 'edge-susp',
            message: '待查关系「代持关系」未核实',
            can_override: true,
          });
        }
        return makePreflight('court', issues, 'snap-1');
      },
    );
    const download = vi.fn().mockResolvedValue(undefined);

    render(
      <ChartExportDialog
        title="股权图"
        snapshots={[{ id: 'snap-1', label: 'v1 冻结版' }]}
        initialSnapshotId="snap-1"
        readOnly={false}
        onClose={vi.fn()}
        preflight={preflight}
        download={download}
      />,
    );

    await userEvent.selectOptions(screen.getByLabelText('导出模式'), 'court');
    await screen.findByText(/待查关系「代持关系」未核实/);

    // 先为另一条边填写例外，验证排除待查关系时不会误清空其他项的例外
    await userEvent.click(screen.getByRole('checkbox', { name: '确认此项例外' }));
    await userEvent.type(screen.getByLabelText('说明例外原因'), '对方承认');

    // 先勾选 suspected 的例外再改用排除，确认该项的 waiver 被清除
    await userEvent.click(screen.getByRole('checkbox', { name: '确认此项例外（标为待举证主张）' }));
    await userEvent.type(screen.getAllByLabelText('说明例外原因')[0], '待删除的例外');

    await userEvent.click(screen.getByRole('checkbox', { name: '从当庭导出排除此待查关系' }));

    await waitFor(() => {
      expect(preflight).toHaveBeenLastCalledWith(
        { profile: 'court', snapshotId: 'snap-1', excludedSuspected: ['edge-susp'] },
        expect.any(AbortSignal),
      );
    });

    await waitFor(() => {
      const issuesList = screen.getByRole('list', { name: '预检问题' });
      expect(within(issuesList).queryByText(/待查关系「代持关系」未核实/)).toBeNull();
    });
    const excludedRegion = screen.getByRole('region', { name: '已排除待查关系' });
    expect(within(excludedRegion).getByText('待查关系「代持关系」未核实')).toBeTruthy();
    expect(within(excludedRegion).queryByText(/edge-susp/)).toBeNull();
    expect(within(excludedRegion).getByRole('button', { name: '恢复纳入此待查关系' })).toBeTruthy();

    // 另一条边的例外保留，下载按钮可用
    const csvBtn = screen.getByRole('button', { name: '引用索引 CSV' }) as HTMLButtonElement;
    expect(csvBtn.disabled).toBe(false);
    await userEvent.click(csvBtn);

    expect(download).toHaveBeenCalledWith(
      'csv',
      { profile: 'court', snapshotId: 'snap-1', excludedSuspected: ['edge-susp'] },
      expect.any(Object),
      [{ code: 'missing_source', target_id: 'edge-other', reason: '对方承认' }],
      false,
      expect.any(AbortSignal),
    );

    // 恢复纳入后重新触发 preflight，该待查关系重新出现且先前填写的该项 waiver 已被清除
    await userEvent.click(screen.getByRole('button', { name: '恢复纳入此待查关系' }));
    await waitFor(() => {
      const issuesList = screen.getByRole('list', { name: '预检问题' });
      expect(within(issuesList).getByText(/待查关系「代持关系」未核实/)).toBeTruthy();
    });
    expect((screen.getByRole('checkbox', { name: '确认此项例外（标为待举证主张）' }) as HTMLInputElement).checked)
      .toBe(false);
  });

  it('当庭态选冻结快照与 onFreeze 流程；readOnly 只禁用“冻结当前图”，不禁用导出', async () => {
    const preflight = vi.fn().mockResolvedValue(makePreflight('court', [], 'snap-frozen'));
    const onFreeze = vi.fn().mockResolvedValue('snap-frozen');
    const download = vi.fn().mockResolvedValue(undefined);

    const { rerender } = render(
      <ChartExportDialog
        title="股权图"
        snapshots={[{ id: 'snap-old', label: 'v1' }]}
        readOnly={true}
        onClose={vi.fn()}
        onFreeze={onFreeze}
        preflight={preflight}
        download={download}
      />,
    );

    await userEvent.selectOptions(screen.getByLabelText('导出模式'), 'court');
    const freezeDisabledBtn = screen.getByRole('button', { name: '冻结当前图' }) as HTMLButtonElement;
    expect(freezeDisabledBtn.disabled).toBe(true);
    expect(preflight).not.toHaveBeenCalled();

    // readOnly 时仍可选已有快照并完成导出
    await userEvent.selectOptions(screen.getByLabelText('图表版本'), 'snap-old');
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('引用预检通过'));
    expect((screen.getByRole('button', { name: '下载 PNG' }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole('button', { name: '引用索引 Markdown' }) as HTMLButtonElement).disabled).toBe(false);

    // 可写状态下点击“冻结当前图”自动选中快照并跑 preflight
    rerender(
      <ChartExportDialog
        title="股权图"
        snapshots={[{ id: 'snap-old', label: 'v1' }]}
        readOnly={false}
        onClose={vi.fn()}
        onFreeze={onFreeze}
        preflight={preflight}
        download={download}
      />,
    );
    await userEvent.selectOptions(screen.getByLabelText('图表版本'), '');
    await userEvent.click(screen.getByRole('button', { name: '冻结当前图' }));
    expect(onFreeze).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(preflight).toHaveBeenLastCalledWith(
        { profile: 'court', snapshotId: 'snap-frozen', excludedSuspected: [] },
        expect.any(AbortSignal),
      );
    });
  });

  it('中文输入法组词期间回车被拦截（isComposing / keyCode 229），且竞态切换模式中止旧 preflight', async () => {
    let firstSignal!: AbortSignal;
    let resolveFirst!: (val: ChartPreflightOut) => void;
    const preflight = vi.fn<(s: ChartExportSettings, sig: AbortSignal) => Promise<ChartPreflightOut>>(
      (settings, sig) => {
        if (settings.profile === 'client') {
          firstSignal = sig;
          return new Promise((resolve) => { resolveFirst = resolve; });
        }
        return Promise.resolve(makePreflight('court', [
          {
            severity: 'blocker',
            code: 'missing_source',
            target_id: 't-1',
            message: '缺出处',
            can_override: true,
          },
        ]));
      },
    );

    render(
      <ChartExportDialog
        title="诉请组"
        readOnly={false}
        onClose={vi.fn()}
        preflight={preflight}
        download={vi.fn()}
      />,
    );

    await userEvent.selectOptions(screen.getByLabelText('导出模式'), 'client');
    await userEvent.selectOptions(screen.getByLabelText('导出模式'), 'court');
    expect(firstSignal.aborted).toBe(true);

    // 迟到的 client preflight 响应不能覆盖当前 court 预览
    resolveFirst(makePreflight('client', []));
    await screen.findByText(/缺出处/);

    await userEvent.click(screen.getByRole('checkbox', { name: '确认此项例外' }));
    const textarea = screen.getByLabelText('说明例外原因');
    const imeEvent = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    Object.defineProperty(imeEvent, 'isComposing', { value: true });
    textarea.dispatchEvent(imeEvent);
    expect(imeEvent.defaultPrevented).toBe(true);

    const key229Event = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    Object.defineProperty(key229Event, 'keyCode', { value: 229 });
    textarea.dispatchEvent(key229Event);
    expect(key229Event.defaultPrevented).toBe(true);

    const plainEnter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    fireEvent(textarea, plainEnter);
    expect(plainEnter.defaultPrevented).toBe(false);
  });
});

describe('ClaimsPanel · 诉请图表三态导出集成', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('归档只读案件仍可点击“导出图表…”，并完成 preflight、Markdown 与 SVG 引用导出', async () => {
    const executor = buildClaimsFixture();
    const snapshot = executor.getSnapshot();
    const group = snapshot.groups[0];
    const items = snapshot.items.filter((it) => it.group_id === group.id);
    const firstItem = { ...items[0], calc_note: 'SECRET_CLAIMS_CALC_NOTE' };
    const patchedSnapshot = {
      ...snapshot,
      items: snapshot.items.map((it) => (it.id === firstItem.id ? firstItem : it)),
    };

    const preflightSpy = vi.spyOn(chartsApi, 'preflight').mockResolvedValue({
      chart_meta: {
        case_id: snapshot.case_id,
        chart_type: 'claims',
        target_id: group.id,
        profile: 'client',
        title: group.title,
        generated_at: '2026-11-22T07:00:00Z',
        snapshot_id: null,
        snapshot_version: null,
        frozen_at: '2026-11-22T07:00:00Z',
        digest: '1234567890abcdef1234567890abcdef',
        currency: group.currency,
        contains_suspected: false,
      },
      issues: [],
    });

    const textSpy = vi.spyOn(chartsApi, 'citationTableText').mockResolvedValue('# 图示引用索引附表\n');
    const jsonTable: ChartCitationTableOut = {
      chart_meta: {
        case_id: snapshot.case_id,
        chart_type: 'claims',
        target_id: group.id,
        profile: 'client',
        title: group.title,
        generated_at: '2026-11-22T07:00:00Z',
        snapshot_id: null,
        snapshot_version: null,
        frozen_at: '2026-11-22T07:00:00Z',
        digest: '1234567890abcdef1234567890abcdef',
        currency: group.currency,
        contains_suspected: false,
      },
      assertions: items.map((item) => ({
        target_id: item.id,
        display: `${item.title} · ${group.currency} ${(item.amount_cents / 100).toFixed(2)}`,
        object_type: 'claim',
        kind: item.category,
        credibility: null,
        from_entity_id: null,
        to_entity_id: null,
        directed: false,
        x: null,
        y: null,
        roles: [],
        attrs: null,
        amount_cents: item.amount_cents,
        held_for_proof: false,
        sources: [],
      })),
      rows: [
        {
          index: 1,
          target_id: firstItem.id,
          assertion: `${firstItem.title} · CNY 1280000.00`,
          evidence_number: '证据1-1',
          evidence_name: '购销合同',
          location: '第3页',
          proves: '合同价款',
          file_name: 'contract.pdf',
          file_version_id: 'ver-1',
        },
      ],
      waivers: [],
    };
    const jsonSpy = vi.spyOn(chartsApi, 'citationTableJson').mockResolvedValue(jsonTable);

    const blobs: Blob[] = [];
    Object.defineProperty(URL, 'createObjectURL', {
      value: vi.fn((blob: Blob) => { blobs.push(blob); return 'blob:export'; }),
      configurable: true,
    });
    Object.defineProperty(URL, 'revokeObjectURL', { value: vi.fn(), configurable: true });
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    render(
      <ClaimsPanel
        snapshot={patchedSnapshot}
        onCommand={(cmd) => executor.human(cmd)}
        readOnly={true}
        caseId={snapshot.case_id}
        caseTitle={snapshot.case_title}
      />,
    );

    const exportBtn = screen.getByRole('button', { name: '导出图表…' }) as HTMLButtonElement;
    expect(exportBtn.disabled).toBe(false);
    await userEvent.click(exportBtn);

    const dialog = await screen.findByRole('dialog');
    await userEvent.selectOptions(within(dialog).getByLabelText('导出模式'), 'client');

    await waitFor(() => {
      expect(preflightSpy).toHaveBeenCalledWith(
        snapshot.case_id,
        { chart_type: 'claims', target_id: group.id, profile: 'client', snapshot_id: null },
        expect.any(AbortSignal),
      );
    });
    await within(dialog).findByText('引用预检通过');

    await userEvent.click(within(dialog).getByRole('button', { name: '引用索引 Markdown' }));
    await waitFor(() => {
      expect(textSpy).toHaveBeenCalledWith(
        snapshot.case_id,
        {
          chart_type: 'claims',
          target_id: group.id,
          profile: 'client',
          snapshot_id: null,
          format: 'md',
          waivers: [],
        },
        expect.any(AbortSignal),
      );
    });

    await userEvent.click(within(dialog).getByRole('button', { name: '下载 SVG' }));
    await waitFor(() => {
      expect(jsonSpy).toHaveBeenCalledWith(
        snapshot.case_id,
        {
          chart_type: 'claims',
          target_id: group.id,
          profile: 'client',
          snapshot_id: null,
          waivers: [],
        },
        expect.any(AbortSignal),
      );
    });

    expect(blobs).toHaveLength(2);
    const svgText = await blobs[1].text();
    expect(svgText).toContain('[1]');
    expect(svgText).toContain('[1] 证据1-1 第3页 购销合同');
    expect(svgText).not.toContain('SECRET_CLAIMS_CALC_NOTE');
  });

  it('ApiError 通过 describeError 归一提示，且 ClaimsPanel 支持定位问题金额项并关闭导出弹窗', async () => {
    const executor = buildClaimsFixture();
    const snapshot = executor.getSnapshot();
    const group = snapshot.groups[0];
    const targetItem = snapshot.items.find((it) => it.group_id === group.id)!;

    const preflightSpy = vi.spyOn(chartsApi, 'preflight')
      .mockRejectedValueOnce(new ApiError(409, {
        code: 'conflict',
        message: 'RAW_INTERNAL_DETAIL',
        details: {},
        request_id: 'req-1',
      }))
      .mockResolvedValueOnce({
        chart_meta: makeMeta('court'),
        issues: [{
          severity: 'blocker',
          code: 'missing_source',
          target_id: targetItem.id,
          message: `「${targetItem.title}」缺少出处`,
          can_override: true,
        }],
      });

    render(
      <ClaimsPanel
        snapshot={snapshot}
        onCommand={(cmd) => executor.human(cmd)}
        readOnly={false}
        caseId={snapshot.case_id}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: '导出图表…' }));
    const dialog = await screen.findByRole('dialog');

    await userEvent.selectOptions(within(dialog).getByLabelText('导出模式'), 'client');
    await within(dialog).findByRole('alert');
    expect(within(dialog).getByRole('alert').textContent).toBe('记录已被其他操作修改，请刷新后重试。');
    expect(within(dialog).getByRole('alert').textContent).not.toContain('RAW_INTERNAL_DETAIL');

    await userEvent.selectOptions(within(dialog).getByLabelText('导出模式'), 'court');
    await within(dialog).findByText(new RegExp(`「${targetItem.title}」缺少出处`));
    expect(preflightSpy).toHaveBeenCalledTimes(2);

    await userEvent.click(within(dialog).getByRole('button', { name: '定位' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    const row = screen.getAllByTestId('claim-item-row')
      .find((entry) => entry.getAttribute('data-item-id') === targetItem.id)!;
    expect(row.getAttribute('data-located')).toBe('true');
  });
});
