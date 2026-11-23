import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    materialsApi: {
      ...actual.materialsApi,
      list: vi.fn(),
    },
    tableImportApi: {
      ...actual.tableImportApi,
      previewMaterial: vi.fn(),
      previewUpload: vi.fn(),
      compile: vi.fn(),
      commit: vi.fn(),
    },
  };
});

import { ApiError } from '../api/client';
import { materialsApi, tableImportApi } from '../api/endpoints';
import type { MaterialSummary, TableCommitOut, TableCompileOut, TablePreviewOut } from '../api/endpoints';
import { TableImportDialog } from './TableImportDialog';
import { defaultRules } from './rules';

const listMaterials = vi.mocked(materialsApi.list);
const previewMaterial = vi.mocked(tableImportApi.previewMaterial);
const previewUpload = vi.mocked(tableImportApi.previewUpload);
const compileTable = vi.mocked(tableImportApi.compile);
const commitTable = vi.mocked(tableImportApi.commit);

const CASE_ID = 'case-table-1';
const GRAPH_ID = 'graph-table-1';

function makeMaterial(id: string, displayName: string): MaterialSummary {
  return {
    material_id: id,
    display_name: displayName,
    category: 'evidence',
    revision: 1,
    intake_id: 'intake-1',
    source_label: displayName,
    relative_source: displayName,
    status: 'ready',
    acquisition: null,
    coverage: {
      case_id: CASE_ID,
      total_pages: 1,
      parsed_pages: 1,
      ocr_pages: 0,
      failed_pages: 0,
      unreadable_pages: 0,
      indexed_segments: 1,
      status: 'complete',
    } as never,
    created_at: '2026-11-22T00:00:00Z',
  };
}

function samplePreview(): TablePreviewOut {
  return {
    sheets: ['银行流水', '汇总'],
    selected_sheet: '银行流水',
    header_row: 1,
    headers: ['交易日期', '对方名称', '收支方向', '金额', '摘要'],
    sample_rows: [
      ['2025-01-05', '乙科技有限公司', '支出', '100000.00', '货款'],
      ['2025-01-08', '丙贸易行', '收入', '23456.78', '回款'],
    ],
    total_rows: 4,
  };
}

function sampleCompile(): TableCompileOut {
  return {
    case_id: CASE_ID,
    material_id: 'mat-xlsx',
    selected_sheet: '银行流水',
    processed_rows: 4,
    new_entities: [
      { name: '乙科技有限公司', entity_kind: 'org' },
      { name: '丙贸易行', entity_kind: 'org' },
    ],
    compiled_edges: [
      {
        from_name: '甲投资有限公司',
        to_name: '乙科技有限公司',
        from_entity_id: 'ent-1',
        to_entity_id: null,
        existing_relation_id: null,
        relation_kind: 'fund_flow',
        label: '货款',
        amount_cents: 1234567,
        currency: 'CNY',
        share_pct: null,
        date_start: '2025-01-05',
        date_end: '2025-02-01',
        tx_count: 2,
        row_numbers: [2, 3],
        proves: '银行流水 第2-3行（共2笔）',
      },
      {
        from_name: '丙贸易行',
        to_name: '甲投资有限公司',
        from_entity_id: null,
        to_entity_id: 'ent-1',
        existing_relation_id: 'rel-existing-1',
        relation_kind: 'fund_flow',
        label: '回款',
        amount_cents: 2345678,
        currency: 'CNY',
        share_pct: null,
        date_start: '2025-01-08',
        date_end: '2025-01-08',
        tx_count: 1,
        row_numbers: [5],
        proves: '银行流水 第5行',
      },
    ],
    skipped_rows: [
      {
        row_number: 4,
        reason: 'invalid_amount',
        raw_row: ['2025-01-12', '丁公司', '支出', '待定', '异常行'],
      },
    ],
  };
}

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
});

describe('TableImportDialog', () => {
  it('完整三步流程：选案卷表格预览 -> 常量+方向+金额列映射 -> 核对汇总与异常行 -> 提交', async () => {
    listMaterials.mockResolvedValue({
      items: [
        makeMaterial('mat-pdf', '起诉状.pdf'),
        makeMaterial('mat-xlsx', 'A公司流水.xlsx'),
      ],
      offset: 0,
      limit: 200,
      total: 2,
      case_total: 2,
      categories: [],
    });
    previewMaterial.mockResolvedValue(samplePreview());
    compileTable.mockResolvedValue(sampleCompile());
    const commitOut: TableCommitOut = {
      created_entities: 2,
      created_relations: 1,
      attached_sources: 2,
      skipped_rows: sampleCompile().skipped_rows,
      workspace: {} as never,
    };
    commitTable.mockResolvedValue(commitOut);

    const onCommitted = vi.fn();
    const onClose = vi.fn();

    render(
      <TableImportDialog
        open
        onClose={onClose}
        caseId={CASE_ID}
        graphId={GRAPH_ID}
        rules={defaultRules}
        readOnly={false}
        onCommitted={onCommitted}
      />,
    );

    const dialog = await screen.findByRole('dialog', { name: '从表格列映射生成关系' });
    await waitFor(() => {
      expect(listMaterials).toHaveBeenCalledWith(CASE_ID, { offset: 0, limit: 200 }, expect.any(AbortSignal));
    });

    // 表格材料在分组中被高亮显示。
    const matSelect = within(dialog).getByLabelText(/已导入本案的材料/);
    expect(matSelect.textContent).toContain('[表格] A公司流水.xlsx');
    await userEvent.selectOptions(matSelect, 'mat-xlsx');

    await waitFor(() => {
      expect(previewMaterial).toHaveBeenCalledWith(
        CASE_ID,
        { material_id: 'mat-xlsx', sheet_name: null, header_row: 1 },
        expect.any(AbortSignal),
      );
    });

    expect(within(dialog).getByText('共 4 行数据（预览前 2 行）')).toBeTruthy();
    const previewTable = within(dialog).getByRole('table', { name: /列结构与样例数据预览/ });
    expect(previewTable.textContent).toContain('乙科技有限公司');

    await userEvent.click(within(dialog).getByRole('button', { name: '下一步：列映射配置' }));

    // 步骤 2：配置起点常量、终点列、方向列、金额列、标签列。
    const fromGroup = within(dialog).getByRole('group', { name: /起点方式/ });
    await userEvent.click(within(fromGroup).getByLabelText('固定常量主体'));
    const fromConstInput = within(fromGroup).getByLabelText('起点固定主体名称');
    await userEvent.type(fromConstInput, '甲投资有限公司');

    // 验证输入法组词回车不误触发 compile。
    const allowed = fireEvent.keyDown(fromConstInput, { key: 'Enter', isComposing: true, keyCode: 229 });
    expect(allowed).toBe(false);
    expect(compileTable).not.toHaveBeenCalled();

    const toGroup = within(dialog).getByRole('group', { name: /终点方式/ });
    await userEvent.selectOptions(within(toGroup).getByLabelText('终点列'), '1');
    await userEvent.selectOptions(within(dialog).getByLabelText('收支方向列（可选）'), '2');
    await userEvent.selectOptions(within(dialog).getByLabelText('金额列（可选）'), '3');
    await userEvent.selectOptions(within(dialog).getByLabelText('日期列（可选）'), '0');
    await userEvent.selectOptions(within(dialog).getByLabelText('标签列（可选）'), '4');

    await userEvent.click(within(dialog).getByRole('button', { name: '生成预览' }));

    await waitFor(() => {
      expect(compileTable).toHaveBeenCalledWith(
        CASE_ID,
        {
          material_id: 'mat-xlsx',
          graph_id: GRAPH_ID,
          sheet_name: '银行流水',
          header_row: 1,
          from_col: null,
          from_constant: '甲投资有限公司',
          to_col: 1,
          to_constant: null,
          direction_col: 2,
          relation_kind: 'fund_flow',
          amount_col: 3,
          currency_col: null,
          currency: 'CNY',
          share_pct_col: null,
          date_col: 0,
          label_col: 4,
          filter_min_amount: null,
          aggregate_by_pair: true,
        },
        expect.any(AbortSignal),
      );
    });

    // 步骤 3：核对摘要、新建主体、金额格式化、状态徽标与跳过异常行警告表。
    expect(
      await within(dialog).findByText('处理数据行 4 · 将新建主体 2 个 · 生成/关联边 2 条 · 跳过异常行 1 条'),
    ).toBeTruthy();

    const edgesTable = within(dialog).getByRole('table', { name: /拟写入关系汇总预览/ });
    expect(edgesTable.textContent).toContain('甲投资有限公司 → 乙科技有限公司');
    expect(edgesTable.textContent).toContain('12,345.67 CNY');
    expect(edgesTable.textContent).toContain('2025-01-05 ~ 2025-02-01');
    expect(edgesTable.textContent).toContain('新建关系');
    expect(edgesTable.textContent).toContain('已有关系补出处');

    const skippedSection = within(dialog).getByLabelText('跳过异常行警告');
    const skippedTable = within(skippedSection).getByRole('table');
    expect(skippedTable.textContent).toContain('第 4 行');
    expect(skippedTable.textContent).toContain('金额格式无效');
    expect(skippedTable.textContent).toContain('丁公司');

    await userEvent.click(within(dialog).getByRole('button', { name: '确认写入当前关系图' }));

    await waitFor(() => {
      expect(commitTable).toHaveBeenCalledWith(
        CASE_ID,
        expect.objectContaining({
          material_id: 'mat-xlsx',
          graph_id: GRAPH_ID,
          from_constant: '甲投资有限公司',
          to_col: 1,
        }),
        expect.any(AbortSignal),
      );
    });
    expect(onCommitted).toHaveBeenCalledWith(commitOut);
    expect(onClose).toHaveBeenCalled();
  });

  it('临时上传文件可预览表结构，但禁止直接进入写入步骤；只读归档禁用确认写入', async () => {
    listMaterials.mockResolvedValue({
      items: [makeMaterial('mat-xlsx', 'A公司流水.xlsx')],
      offset: 0,
      limit: 200,
      total: 1,
      case_total: 1,
      categories: [],
    });
    previewUpload.mockResolvedValue(samplePreview());
    previewMaterial.mockResolvedValue(samplePreview());
    compileTable.mockResolvedValue(sampleCompile());

    render(
      <TableImportDialog
        open
        onClose={vi.fn()}
        caseId={CASE_ID}
        graphId={GRAPH_ID}
        rules={defaultRules}
        readOnly
        onCommitted={vi.fn()}
      />,
    );

    const dialog = await screen.findByRole('dialog');
    const fileInput = within(dialog).getByLabelText(/或上传本地表格仅作预览/) as HTMLInputElement;
    const csvFile = new File(['a,b\n1,2'], 'temp.csv', { type: 'text/csv' });
    await userEvent.upload(fileInput, csvFile);

    await waitFor(() => {
      expect(previewUpload).toHaveBeenCalled();
    });
    expect(within(dialog).getByText(/仅预览模式/)).toBeTruthy();
    const nextBtn = within(dialog).getByRole('button', { name: '下一步：列映射配置' }) as HTMLButtonElement;
    expect(nextBtn.disabled).toBe(true);

    // 改选案卷材料并走完预览后，只读模式下提交按钮依然禁用。
    await userEvent.selectOptions(within(dialog).getByLabelText(/已导入本案的材料/), 'mat-xlsx');
    await waitFor(() => expect(nextBtn.disabled).toBe(false));
    await userEvent.click(nextBtn);

    const fromGroup = within(dialog).getByRole('group', { name: /起点方式/ });
    await userEvent.selectOptions(within(fromGroup).getByLabelText('起点列'), '1');
    const toGroup = within(dialog).getByRole('group', { name: /终点方式/ });
    await userEvent.selectOptions(within(toGroup).getByLabelText('终点列'), '2');
    await userEvent.click(within(dialog).getByRole('button', { name: '生成预览' }));

    const commitBtn = await within(dialog).findByRole('button', { name: '确认写入当前关系图' }) as HTMLButtonElement;
    expect(commitBtn.disabled).toBe(true);
    expect(within(dialog).getByText(/本案已归档/)).toBeTruthy();
  });

  it('支持切换工作表/表头行、股权比例与最小金额过滤、空结果禁用写入、提交失败保留错误并支持取消中止', async () => {
    let listSignal: AbortSignal | undefined;
    listMaterials.mockImplementationOnce((_cid, _params, signal) => {
      listSignal = signal;
      return new Promise(() => {});
    });

    const { unmount } = render(
      <TableImportDialog
        open
        onClose={vi.fn()}
        caseId={CASE_ID}
        graphId={GRAPH_ID}
        rules={defaultRules}
        readOnly={false}
        onCommitted={vi.fn()}
      />,
    );
    expect(listSignal?.aborted).toBe(false);
    unmount();
    expect(listSignal?.aborted).toBe(true);

    listMaterials.mockResolvedValue({
      items: [makeMaterial('mat-eq', '股权结构表.xlsx')],
      offset: 0,
      limit: 200,
      total: 1,
      case_total: 1,
      categories: [],
    });
    previewMaterial.mockResolvedValue({
      sheets: ['总表', '股权明细'],
      selected_sheet: '总表',
      header_row: 1,
      headers: ['股东', '被投企业', '持股比例', '认缴金额'],
      sample_rows: [['张三', '甲公司', '60%', '600000']],
      total_rows: 1,
    });

    const onCommitted = vi.fn();
    const onClose = vi.fn();

    render(
      <TableImportDialog
        open
        onClose={onClose}
        caseId={CASE_ID}
        graphId={GRAPH_ID}
        rules={defaultRules}
        readOnly={false}
        onCommitted={onCommitted}
      />,
    );

    const dialog = await screen.findByRole('dialog');
    await userEvent.selectOptions(await within(dialog).findByLabelText(/已导入本案的材料/), 'mat-eq');
    await waitFor(() => expect(previewMaterial).toHaveBeenCalledTimes(1));

    previewMaterial.mockResolvedValueOnce({
      sheets: ['总表', '股权明细'],
      selected_sheet: '股权明细',
      header_row: 2,
      headers: ['股东', '被投企业', '持股比例', '认缴金额'],
      sample_rows: [['张三', '甲公司', '60%', '600000']],
      total_rows: 1,
    });

    await userEvent.selectOptions(within(dialog).getByLabelText('工作表（Sheet）'), '股权明细');
    const headerInput = within(dialog).getByLabelText(/表头所在行号/);
    await userEvent.clear(headerInput);
    await userEvent.type(headerInput, '2');

    await waitFor(() => {
      expect(previewMaterial).toHaveBeenLastCalledWith(
        CASE_ID,
        { material_id: 'mat-eq', sheet_name: '股权明细', header_row: 2 },
        expect.any(AbortSignal),
      );
    });

    await userEvent.click(within(dialog).getByRole('button', { name: '下一步：列映射配置' }));

    const fromGroup = within(dialog).getByRole('group', { name: /起点方式/ });
    await userEvent.selectOptions(within(fromGroup).getByLabelText('起点列'), '0');
    const toGroup = within(dialog).getByRole('group', { name: /终点方式/ });
    await userEvent.selectOptions(within(toGroup).getByLabelText('终点列'), '1');
    await userEvent.selectOptions(within(dialog).getByLabelText('关系类型'), 'equity_control');
    await userEvent.selectOptions(within(dialog).getByLabelText('金额列（可选）'), '3');
    await userEvent.selectOptions(within(dialog).getByLabelText('持股比例列（可选）'), '2');
    await userEvent.type(within(dialog).getByLabelText('最小金额过滤（可选）'), '100000');
    await userEvent.click(within(dialog).getByLabelText(/按起止主体对汇总/));

    // 先模拟返回 0 条边，确认写入按钮禁用。
    compileTable.mockResolvedValueOnce({
      case_id: CASE_ID,
      material_id: 'mat-eq',
      selected_sheet: '股权明细',
      new_entities: [],
      compiled_edges: [],
      skipped_rows: [],
      processed_rows: 1,
    });
    await userEvent.click(within(dialog).getByRole('button', { name: '生成预览' }));
    const disabledCommitBtn = await within(dialog).findByRole('button', { name: '确认写入当前关系图' }) as HTMLButtonElement;
    expect(disabledCommitBtn.disabled).toBe(true);

    // 返回修改后重新生成预览，带出持股比例 60%。
    await userEvent.click(within(dialog).getByRole('button', { name: '上一步：修改映射' }));
    compileTable.mockResolvedValueOnce({
      case_id: CASE_ID,
      material_id: 'mat-eq',
      selected_sheet: '股权明细',
      new_entities: [{ name: '张三', entity_kind: 'person' }],
      compiled_edges: [{
        from_name: '张三',
        to_name: '甲公司',
        from_entity_id: null,
        to_entity_id: 'ent-a',
        existing_relation_id: null,
        relation_kind: 'equity_control',
        label: null,
        amount_cents: 60000000,
        currency: 'CNY',
        share_pct: 60,
        date_start: null,
        date_end: null,
        tx_count: 1,
        row_numbers: [3],
        proves: '股权明细 第3行',
      }],
      skipped_rows: [],
      processed_rows: 1,
    });

    await userEvent.click(within(dialog).getByRole('button', { name: '生成预览' }));
    await waitFor(() => {
      expect(compileTable).toHaveBeenLastCalledWith(
        CASE_ID,
        expect.objectContaining({
          sheet_name: '股权明细',
          header_row: 2,
          relation_kind: 'equity_control',
          share_pct_col: 2,
          filter_min_amount: '100000',
          aggregate_by_pair: false,
        }),
        expect.any(AbortSignal),
      );
    });
    expect(within(dialog).getByText('60%')).toBeTruthy();

    commitTable.mockRejectedValueOnce(new ApiError(409, {
      code: 'conflict',
      message: 'conflict',
      details: {},
      request_id: 'req-1',
    }));
    await userEvent.click(within(dialog).getByRole('button', { name: '确认写入当前关系图' }));

    const alert = await within(dialog).findByRole('alert');
    expect(alert.textContent).toContain('记录已被其他操作修改');
    expect(onCommitted).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('自动识别币种列并在步骤 3 展示同对多币种不混并提示', async () => {
    listMaterials.mockResolvedValue({
      items: [makeMaterial('mat-xlsx', '多币种往来台账.xlsx')],
      offset: 0,
      limit: 200,
      total: 1,
      case_total: 1,
      categories: [],
    });
    previewMaterial.mockResolvedValue({
      sheets: ['往来明细'],
      selected_sheet: '往来明细',
      header_row: 1,
      headers: ['交易日期', '付款方', '收款方', '交易金额', '币种'],
      sample_rows: [
        ['2025-03-18', '启航建工', '华辰智造', '3500000', 'CNY'],
        ['2025-08-05', '启航建工', '华辰智造', '50000', 'USD'],
      ],
      total_rows: 2,
    });
    compileTable.mockResolvedValue({
      case_id: CASE_ID,
      material_id: 'mat-xlsx',
      selected_sheet: '往来明细',
      new_entities: [],
      compiled_edges: [
        {
          from_name: '启航建工',
          to_name: '华辰智造',
          from_entity_id: 'e-1',
          to_entity_id: 'e-2',
          existing_relation_id: null,
          relation_kind: 'fund_flow',
          label: null,
          amount_cents: 350000000,
          currency: 'CNY',
          share_pct: null,
          date_start: '2025-03-18',
          date_end: '2025-03-18',
          tx_count: 1,
          row_numbers: [2],
          proves: '往来明细 第2行',
        },
        {
          from_name: '启航建工',
          to_name: '华辰智造',
          from_entity_id: 'e-1',
          to_entity_id: 'e-2',
          existing_relation_id: null,
          relation_kind: 'fund_flow',
          label: null,
          amount_cents: 5000000,
          currency: 'USD',
          share_pct: null,
          date_start: '2025-08-05',
          date_end: '2025-08-05',
          tx_count: 1,
          row_numbers: [3],
          proves: '往来明细 第3行',
        },
      ],
      skipped_rows: [],
      processed_rows: 2,
    });

    render(
      <TableImportDialog
        open
        onClose={vi.fn()}
        caseId={CASE_ID}
        graphId={GRAPH_ID}
        rules={defaultRules}
        readOnly={false}
        onCommitted={vi.fn()}
      />,
    );

    const dialog = await screen.findByRole('dialog');
    await userEvent.selectOptions(within(dialog).getByLabelText(/已导入本案的材料/), 'mat-xlsx');
    await waitFor(() => expect(within(dialog).getByText('共 2 行数据（预览前 2 行）')).toBeTruthy());
    await userEvent.click(within(dialog).getByRole('button', { name: '下一步：列映射配置' }));

    const currencySelect = within(dialog).getByLabelText('币种列（可选）') as HTMLSelectElement;
    expect(currencySelect.value).toBe('4');

    const fromGroup = within(dialog).getByRole('group', { name: /起点方式/ });
    const toGroup = within(dialog).getByRole('group', { name: /终点方式/ });
    await userEvent.selectOptions(within(fromGroup).getByLabelText('起点列'), '1');
    await userEvent.selectOptions(within(toGroup).getByLabelText('终点列'), '2');
    await userEvent.selectOptions(within(dialog).getByLabelText('金额列（可选）'), '3');
    await userEvent.click(within(dialog).getByRole('button', { name: '生成预览' }));

    await waitFor(() => {
      expect(compileTable).toHaveBeenLastCalledWith(
        CASE_ID,
        expect.objectContaining({
          amount_col: 3,
          currency_col: 4,
        }),
        expect.any(AbortSignal),
      );
    });
    expect(await within(dialog).findByText(/同一主体对存在多种币种，预览已按币种拆分而不混算/)).toBeTruthy();
  });
});
