/**
 * Wave 2B：通用表格「列映射」确定性出边分步弹窗。
 *
 * 1) 选表与预览：选择案卷已导入的 .xlsx / .csv 材料（或临时上传仅预览结构），挑选工作表与表头行；
 * 2) 列映射配置：起点/终点列或常量、收支方向列、关系类型、金额/币种/过滤、持股比例、日期、标签与聚合；
 * 3) 核对确定性汇总与跳过异常行 -> 确认写入当前关系图。
 */
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { describeError } from '../api/client';
import { materialsApi, tableImportApi } from '../api/endpoints';
import type {
  MaterialSummary, Schemas, TableCommitOut, TableCompileOut, TablePreviewOut,
} from '../api/endpoints';
import { guardComposition, useCompositionGuard } from './GraphDialogs';
import { entityKindLabel, relationKindLabel } from './rules';
import type { GraphRules } from './types';

type TableCompileInput = Schemas['TableCompileInput'];
type Step = 1 | 2 | 3;

const SKIP_REASON_LABELS: Record<string, string> = {
  missing_source_entity: '缺少起点主体',
  missing_target_entity: '缺少终点主体',
  entity_name_too_long: '主体名称过长',
  invalid_amount: '金额格式无效',
  invalid_share_pct: '持股比例无效（须在 0–100 之间）',
  same_source_and_target: '起点与终点主体相同',
  existing_ruled_out: '与已排除关系冲突（保留人工排除结论）',
  invalid_currency: '币种格式无效',
  currency_conflict_same_pair: '同向同类型关系已存在另一币种（未混并金额）',
};

export interface TableImportDialogProps {
  open: boolean;
  onClose: () => void;
  caseId: string;
  graphId: string;
  rules: GraphRules;
  readOnly: boolean;
  onCommitted: (result: TableCommitOut) => void;
}

function isSpreadsheetMaterial(material: MaterialSummary): boolean {
  return /\.(xlsx|csv)$/i.test(material.display_name)
    || /\.(xlsx|csv)$/i.test(material.relative_source);
}

function formatAmountCents(amountCents: number | null, currency: string | null): string {
  if (amountCents == null) return '—';
  const formatted = (amountCents / 100).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return currency ? `${formatted} ${currency}` : formatted;
}

function formatDateRange(start: string | null, end: string | null): string {
  if (start && end) return start === end ? start : `${start} ~ ${end}`;
  return start ?? end ?? '—';
}

function formatSkipReason(reason: string): string {
  return SKIP_REASON_LABELS[reason] ?? reason;
}

export function TableImportDialog(props: TableImportDialogProps) {
  if (!props.open) return null;
  return <TableImportDialogInner key={props.caseId} {...props} />;
}

function TableImportDialogInner({
  onClose, caseId, graphId, rules, readOnly, onCommitted,
}: TableImportDialogProps) {
  const titleId = useId();
  const previewCaptionId = useId();
  const edgesCaptionId = useId();
  const skippedCaptionId = useId();

  const dialogRef = useRef<HTMLDialogElement>(null);
  const stepHeadingRef = useRef<HTMLHeadingElement>(null);
  const actionAbortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  const activeCaseRef = useRef(caseId);
  activeCaseRef.current = caseId;
  const didStepTransitionRef = useRef(false);
  const imeGuard = useCompositionGuard();
  const commitKeyRef = useRef<{ spec: TableCompileInput; key: string } | null>(null);

  const defaultRelationKind = useMemo(() => (
    rules.relation_kinds.some((kind) => kind.value === 'fund_flow')
      ? 'fund_flow'
      : (rules.relation_kinds[0]?.value ?? 'fund_flow')
  ), [rules.relation_kinds]);

  const [step, setStep] = useState<Step>(1);

  // Step 1: materials & preview
  const [materials, setMaterials] = useState<MaterialSummary[]>([]);
  const [materialsTotal, setMaterialsTotal] = useState<number | null>(null);
  const [materialsLoading, setMaterialsLoading] = useState(true);
  const [materialsError, setMaterialsError] = useState('');
  const [onlyTables, setOnlyTables] = useState(false);

  const [sourceMode, setSourceMode] = useState<'material' | 'upload'>('material');
  const [materialId, setMaterialId] = useState('');
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [requestedSheet, setRequestedSheet] = useState<string | null>(null);
  const [headerRowInput, setHeaderRowInput] = useState('1');
  const [previewRefreshNonce, setPreviewRefreshNonce] = useState(0);
  const [preview, setPreview] = useState<TablePreviewOut | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState('');

  // Step 2: column mapping config
  const [fromMode, setFromMode] = useState<'col' | 'constant'>('col');
  const [fromCol, setFromCol] = useState('');
  const [fromConstant, setFromConstant] = useState('');
  const [toMode, setToMode] = useState<'col' | 'constant'>('col');
  const [toCol, setToCol] = useState('');
  const [toConstant, setToConstant] = useState('');
  const [directionCol, setDirectionCol] = useState('');
  const [relationKind, setRelationKind] = useState(defaultRelationKind);
  const [amountCol, setAmountCol] = useState('');
  const [currencyCol, setCurrencyCol] = useState('');
  const [currency, setCurrency] = useState('CNY');
  const [filterMinAmount, setFilterMinAmount] = useState('');
  const [sharePctCol, setSharePctCol] = useState('');
  const [dateCol, setDateCol] = useState('');
  const [labelCol, setLabelCol] = useState('');
  const [aggregateByPair, setAggregateByPair] = useState(true);

  // Step 2/3 execution state
  const [compileResult, setCompileResult] = useState<TableCompileOut | null>(null);
  const [compiledSpec, setCompiledSpec] = useState<TableCompileInput | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');

  useEffect(() => {
    mountedRef.current = true;
    const element = dialogRef.current;
    if (element && !element.open) element.showModal();
    return () => {
      mountedRef.current = false;
      actionAbortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (!didStepTransitionRef.current) return;
    stepHeadingRef.current?.focus();
  }, [step]);

  useEffect(() => {
    const controller = new AbortController();
    setMaterialsLoading(true);
    setMaterialsError('');

    void materialsApi.list(caseId, { offset: 0, limit: 200 }, controller.signal).then(
      (page) => {
        if (controller.signal.aborted || !mountedRef.current || activeCaseRef.current !== caseId) return;
        setMaterials(page.items);
        setMaterialsTotal(page.total > page.items.length ? page.total : null);
      },
      (cause: unknown) => {
        if (controller.signal.aborted || !mountedRef.current || activeCaseRef.current !== caseId) return;
        setMaterialsError(describeError(cause) || '加载案件材料列表失败。');
      },
    ).finally(() => {
      if (!controller.signal.aborted && mountedRef.current && activeCaseRef.current === caseId) {
        setMaterialsLoading(false);
      }
    });

    return () => controller.abort();
  }, [caseId]);

  const parsedHeaderRow = useMemo(() => {
    const trimmed = headerRowInput.trim();
    if (!trimmed) return null;
    const num = Number(trimmed);
    if (!Number.isInteger(num) || num < 1 || num > 100) return null;
    return num;
  }, [headerRowInput]);

  useEffect(() => {
    if (sourceMode === 'material' && !materialId) {
      setPreview(null);
      setPreviewError('');
      setPreviewLoading(false);
      return;
    }
    if (sourceMode === 'upload' && !uploadFile) {
      setPreview(null);
      setPreviewError('');
      setPreviewLoading(false);
      return;
    }
    if (parsedHeaderRow === null) {
      setPreviewError('表头行号须为 1 到 100 之间的整数。');
      setPreviewLoading(false);
      return;
    }

    const controller = new AbortController();
    setPreviewLoading(true);
    setPreviewError('');

    const request = sourceMode === 'material'
      ? tableImportApi.previewMaterial(
        caseId,
        { material_id: materialId, sheet_name: requestedSheet, header_row: parsedHeaderRow },
        controller.signal,
      )
      : (() => {
        const form = new FormData();
        form.append('file', uploadFile!);
        if (requestedSheet) form.append('sheet_name', requestedSheet);
        form.append('header_row', String(parsedHeaderRow));
        return tableImportApi.previewUpload(caseId, form, controller.signal);
      })();

    void request.then(
      (output) => {
        if (controller.signal.aborted || !mountedRef.current || activeCaseRef.current !== caseId) return;
        setPreview(output);
        const detectedCurrencyIdx = output.headers.findIndex((header) => /币种|货币|currency/i.test(header));
        setCurrencyCol((current) => (current === '' && detectedCurrencyIdx >= 0 ? String(detectedCurrencyIdx) : current));
      },
      (cause: unknown) => {
        if (controller.signal.aborted || !mountedRef.current || activeCaseRef.current !== caseId) return;
        setPreview(null);
        setPreviewError(describeError(cause) || '解析表格预览失败。');
      },
    ).finally(() => {
      if (!controller.signal.aborted && mountedRef.current && activeCaseRef.current === caseId) {
        setPreviewLoading(false);
      }
    });

    return () => controller.abort();
  }, [caseId, sourceMode, materialId, uploadFile, requestedSheet, parsedHeaderRow, previewRefreshNonce]);

  const tableMaterials = useMemo(
    () => materials.filter(isSpreadsheetMaterial),
    [materials],
  );
  const otherMaterials = useMemo(
    () => materials.filter((item) => !isSpreadsheetMaterial(item)),
    [materials],
  );

  function resetColumnMapping() {
    setFromCol('');
    setToCol('');
    setDirectionCol('');
    setAmountCol('');
    setCurrencyCol('');
    setSharePctCol('');
    setDateCol('');
    setLabelCol('');
    setCompileResult(null);
    setCompiledSpec(null);
    setActionError('');
  }

  function goToStep(next: Step) {
    didStepTransitionRef.current = true;
    setActionError('');
    setStep(next);
  }

  function handleSelectMaterial(nextMaterialId: string) {
    setSourceMode('material');
    setMaterialId(nextMaterialId);
    setRequestedSheet(null);
    setHeaderRowInput('1');
    setPreview(null);
    resetColumnMapping();
  }

  function handleUploadChange(file: File | null) {
    if (!file) return;
    setSourceMode('upload');
    setUploadFile(file);
    setRequestedSheet(null);
    setHeaderRowInput('1');
    setPreview(null);
    setCompileResult(null);
    setCompiledSpec(null);
    setActionError('');
  }

  function buildCompileSpec(): TableCompileInput | { error: string } {
    if (!materialId) {
      return { error: '请先选择已导入本案的表格材料，再生成映射预览。' };
    }
    if (parsedHeaderRow === null) {
      return { error: '表头行号须在 1 到 100 之间。' };
    }
    const parsedFromCol = fromMode === 'col' && fromCol !== '' ? Number(fromCol) : null;
    const trimmedFromConst = fromMode === 'constant' ? fromConstant.trim() : '';
    if (parsedFromCol === null && !trimmedFromConst) {
      return { error: '必须指定关系起点列或填写起点固定主体名称。' };
    }
    const parsedToCol = toMode === 'col' && toCol !== '' ? Number(toCol) : null;
    const trimmedToConst = toMode === 'constant' ? toConstant.trim() : '';
    if (parsedToCol === null && !trimmedToConst) {
      return { error: '必须指定关系终点列或填写终点固定主体名称。' };
    }
    const parsedAmountCol = amountCol !== '' ? Number(amountCol) : null;
    const trimmedMinAmount = filterMinAmount.trim();
    if (trimmedMinAmount && parsedAmountCol === null) {
      return { error: '指定最小金额过滤条件时必须映射金额列。' };
    }

    return {
      material_id: materialId,
      graph_id: graphId,
      sheet_name: requestedSheet ?? preview?.selected_sheet ?? null,
      header_row: parsedHeaderRow,
      from_col: parsedFromCol,
      from_constant: trimmedFromConst || null,
      to_col: parsedToCol,
      to_constant: trimmedToConst || null,
      direction_col: directionCol !== '' ? Number(directionCol) : null,
      relation_kind: relationKind,
      amount_col: parsedAmountCol,
      currency_col: currencyCol !== '' ? Number(currencyCol) : null,
      currency: currency.trim() || 'CNY',
      share_pct_col: sharePctCol !== '' ? Number(sharePctCol) : null,
      date_col: dateCol !== '' ? Number(dateCol) : null,
      label_col: labelCol !== '' ? Number(labelCol) : null,
      filter_min_amount: trimmedMinAmount || null,
      aggregate_by_pair: aggregateByPair,
    };
  }

  async function handleCompile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;

    const specOrError = buildCompileSpec();
    if ('error' in specOrError) {
      setActionError(specOrError.error);
      return;
    }

    actionAbortRef.current?.abort();
    const controller = new AbortController();
    actionAbortRef.current = controller;

    setBusy(true);
    setActionError('');
    try {
      const output = await tableImportApi.compile(caseId, specOrError, controller.signal);
      if (controller.signal.aborted || !mountedRef.current || activeCaseRef.current !== caseId) return;
      setCompileResult(output);
      setCompiledSpec(specOrError);
      commitKeyRef.current = null;
      goToStep(3);
    } catch (cause: unknown) {
      if (controller.signal.aborted || !mountedRef.current || activeCaseRef.current !== caseId) return;
      setActionError(describeError(cause) || '生成列映射预览失败。');
    } finally {
      if (!controller.signal.aborted && mountedRef.current && activeCaseRef.current === caseId) {
        setBusy(false);
      }
    }
  }

  async function handleCommit() {
    if (readOnly || busy || !compileResult || !compiledSpec || compileResult.compiled_edges.length === 0) {
      return;
    }

    actionAbortRef.current?.abort();
    const controller = new AbortController();
    actionAbortRef.current = controller;

    setBusy(true);
    setActionError('');
    try {
      if (!commitKeyRef.current || commitKeyRef.current.spec !== compiledSpec) {
        commitKeyRef.current = {
          spec: compiledSpec,
          key: `table-import-${caseId}-${graphId}-${Date.now()}`,
        };
      }
      const result = await tableImportApi.commit(
        caseId,
        {
          ...compiledSpec,
          graph_id: graphId,
          request_key: commitKeyRef.current.key,
        },
        controller.signal,
      );
      if (controller.signal.aborted || !mountedRef.current || activeCaseRef.current !== caseId) return;
      onCommitted(result);
      onClose();
    } catch (cause: unknown) {
      if (controller.signal.aborted || !mountedRef.current || activeCaseRef.current !== caseId) return;
      setActionError(describeError(cause) || '写入当前关系图失败。');
    } finally {
      if (!controller.signal.aborted && mountedRef.current && activeCaseRef.current === caseId) {
        setBusy(false);
      }
    }
  }

  const headers = preview?.headers ?? [];
  const sampleRows = preview?.sample_rows.slice(0, 20) ?? [];
  const headerOptions = headers.map((header, idx) => ({
    value: String(idx),
    label: `列 ${idx + 1}：${header}`,
  }));

  return (
    <dialog
      ref={dialogRef}
      className="work-dialog graph-snapshot-dialog graph-table-dialog"
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <header className="graph-snapshot-head">
        <div>
          <h2 id={titleId}>从表格列映射生成关系</h2>
          <p className="muted">
            通用 XLSX / CSV 列映射确定性聚合：核对汇总结果与异常行后再写入当前关系图，并保留源表格材料出处。
          </p>
        </div>
        <button type="button" autoFocus disabled={busy} onClick={onClose}>关闭</button>
      </header>

      <div style={{ padding: '14px 18px', overflow: 'auto', maxHeight: 'calc(94vh - 84px)', display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 14 }}>
        <nav aria-label="导入步骤">
          <ol
            style={{
              display: 'flex',
              flexWrap: 'wrap',
              gap: '6px 16px',
              listStyle: 'none',
              margin: 0,
              padding: '8px 12px',
              background: 'var(--warm)',
              border: '1px solid var(--line)',
              borderRadius: 8,
              fontSize: 13,
            }}
          >
            <li aria-current={step === 1 ? 'step' : undefined} style={{ fontWeight: step === 1 ? 700 : 400, color: step === 1 ? 'var(--accent)' : 'var(--muted)' }}>
              1. 选表与预览
            </li>
            <li aria-current={step === 2 ? 'step' : undefined} style={{ fontWeight: step === 2 ? 700 : 400, color: step === 2 ? 'var(--accent)' : 'var(--muted)' }}>
              2. 列映射配置
            </li>
            <li aria-current={step === 3 ? 'step' : undefined} style={{ fontWeight: step === 3 ? 700 : 400, color: step === 3 ? 'var(--accent)' : 'var(--muted)' }}>
              3. 核对汇总与写入
            </li>
          </ol>
        </nav>

        {readOnly ? (
          <p role="status" className="graph-archived">
            本案已归档：仅支持选表与映射预览，不能确认写入当前关系图。
          </p>
        ) : null}

        {step === 1 ? (
          <section aria-label="步骤 1：选表与预览" style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 12, minWidth: 0 }}>
            <h3 ref={stepHeadingRef} tabIndex={-1} style={{ margin: 0, fontSize: 16 }}>
              步骤 1：选择表格与预览结构
            </h3>

            <form
              onSubmit={(event) => {
                event.preventDefault();
                if (sourceMode === 'material' && materialId && preview && !previewLoading) {
                  goToStep(2);
                }
              }}
              {...imeGuard}
              style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 10, minWidth: 0 }}
            >
              <div className="graph-form-row">
                <div>
                  <label>
                    已导入本案的材料（共 {tableMaterials.length} 份 .xlsx / .csv）
                    <select
                      disabled={materialsLoading || busy}
                      value={sourceMode === 'material' ? materialId : ''}
                      onChange={(event) => handleSelectMaterial(event.target.value)}
                    >
                      <option value="">请选择案卷表格材料…</option>
                      {tableMaterials.length > 0 ? (
                        <optgroup label="推荐表格材料（.xlsx / .csv）">
                          {tableMaterials.map((item) => (
                            <option key={item.material_id} value={item.material_id}>
                              [表格] {item.display_name}
                            </option>
                          ))}
                        </optgroup>
                      ) : null}
                      {!onlyTables && otherMaterials.length > 0 ? (
                        <optgroup label="其他案卷材料">
                          {otherMaterials.map((item) => (
                            <option key={item.material_id} value={item.material_id}>
                              {item.display_name}
                            </option>
                          ))}
                        </optgroup>
                      ) : null}
                    </select>
                  </label>
                  <label className="graph-checkbox" style={{ marginTop: 6 }}>
                    <input
                      type="checkbox"
                      style={{ width: 'auto' }}
                      checked={onlyTables}
                      onChange={(event) => setOnlyTables(event.target.checked)}
                    />
                    仅在下拉中列出 .xlsx / .csv 材料
                  </label>
                </div>

                <div>
                  <label>
                    或上传本地表格仅作预览（.xlsx / .csv）
                    <input
                      type="file"
                      accept=".xlsx,.csv"
                      disabled={busy}
                      onChange={(event) => handleUploadChange(event.target.files?.[0] ?? null)}
                    />
                  </label>
                  <p className="graph-hint">
                    临时上传仅检查工作表与列结构、不落库；正式写入关系图须选择左侧已导入本案材料，以保留稳定 material_id 溯源。
                  </p>
                </div>
              </div>

              {materialsLoading ? <p role="status" className="muted">正在加载本案材料目录…</p> : null}
              {materialsTotal !== null ? (
                <p role="status" className="graph-hint">
                  仅显示前 {materials.length} 份材料（全案共 {materialsTotal} 份）；未列出的表格可在工作区查找材料后导入。
                </p>
              ) : null}
              {materialsError ? <p role="alert" className="graph-error-inline">错误：{materialsError}</p> : null}

              {sourceMode === 'upload' && uploadFile ? (
                <div className="graph-freeze-warning">
                  <p>
                    当前正在预览本地文件「{uploadFile.name}」（仅预览模式）。
                    {materialId ? (
                      <>
                        {' '}
                        <button
                          type="button"
                          onClick={() => {
                            setSourceMode('material');
                            setRequestedSheet(null);
                            setPreview(null);
                            resetColumnMapping();
                          }}
                        >
                          切回已选案卷材料
                        </button>
                      </>
                    ) : ' 请先通过「导入材料」上传并在左侧下拉选定材料后再进入列映射。'}
                  </p>
                </div>
              ) : null}

              {preview || materialId || uploadFile ? (
                <div className="graph-form-row">
                  <label>
                    工作表（Sheet）
                    <select
                      disabled={busy || previewLoading || !preview || preview.sheets.length === 0}
                      value={requestedSheet ?? preview?.selected_sheet ?? ''}
                      onChange={(event) => {
                        setRequestedSheet(event.target.value || null);
                        resetColumnMapping();
                      }}
                    >
                      {(preview?.sheets ?? []).map((sheet) => (
                        <option key={sheet} value={sheet}>{sheet}</option>
                      ))}
                    </select>
                  </label>

                  <label>
                    表头所在行号（1–100）
                    <input
                      type="number"
                      min={1}
                      max={100}
                      step={1}
                      disabled={busy}
                      value={headerRowInput}
                      onChange={(event) => {
                        setHeaderRowInput(event.target.value);
                        resetColumnMapping();
                      }}
                    />
                  </label>

                  <div style={{ alignSelf: 'end' }}>
                    <button
                      type="button"
                      disabled={busy || previewLoading || (!materialId && !uploadFile)}
                      onClick={() => setPreviewRefreshNonce((n) => n + 1)}
                    >
                      {previewLoading ? '预览中…' : '刷新预览'}
                    </button>
                  </div>
                </div>
              ) : null}

              {previewLoading ? <p role="status" className="muted">正在解析表格工作表与前 20 行样例…</p> : null}
              {previewError ? <p role="alert" className="graph-error-inline">错误：{previewError}</p> : null}

              {preview ? (
                <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 8, minWidth: 0 }}>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
                    <span className="graph-badge import" style={{ marginLeft: 0 }}>
                      工作表：{preview.selected_sheet}
                    </span>
                    <span className="graph-badge ok" style={{ marginLeft: 0 }}>
                      共 {preview.total_rows} 行数据（预览前 {sampleRows.length} 行）
                    </span>
                  </div>

                  <div className="table-scroll" style={{ minWidth: 0 }} tabIndex={0} role="region" aria-labelledby={previewCaptionId}>
                    <table className="case-table" style={{ minWidth: 640 }}>
                      <caption id={previewCaptionId} style={{ textAlign: 'left', padding: '4px 0 8px', fontSize: 13, color: 'var(--muted)' }}>
                        第 {preview.header_row} 行为表头 · 列结构与样例数据预览
                      </caption>
                      <thead>
                        <tr>
                          <th scope="col">行号</th>
                          {headers.map((header, idx) => (
                            <th key={idx} scope="col">列 {idx + 1}：{header}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {sampleRows.length === 0 ? (
                          <tr>
                            <td colSpan={Math.max(1, headers.length + 1)} className="muted">
                              该表头行之下暂无数据行。
                            </td>
                          </tr>
                        ) : (
                          sampleRows.map((row, rowIdx) => (
                            <tr key={rowIdx}>
                              <th scope="row">{preview.header_row + 1 + rowIdx}</th>
                              {headers.map((_, colIdx) => (
                                <td key={colIdx}>{row[colIdx] ?? ''}</td>
                              ))}
                            </tr>
                          ))
                        )}
                      </tbody>
                    </table>
                  </div>
                </div>
              ) : null}

              <div className="work-actions" style={{ justifyContent: 'flex-end' }}>
                <button type="button" disabled={busy} onClick={onClose}>关闭</button>
                <button
                  type="submit"
                  disabled={busy || previewLoading || sourceMode !== 'material' || !materialId || !preview || parsedHeaderRow === null}
                  style={{
                    background: 'var(--accent)',
                    borderColor: 'var(--accent)',
                    color: '#fff',
                    fontWeight: 650,
                  }}
                >
                  下一步：列映射配置
                </button>
              </div>
            </form>
          </section>
        ) : null}

        {step === 2 ? (
          <section aria-label="步骤 2：列映射配置" style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 12, minWidth: 0 }}>
            <h3 ref={stepHeadingRef} tabIndex={-1} style={{ margin: 0, fontSize: 16 }}>
              步骤 2：列映射与确定性聚合配置
            </h3>

            <form
              onSubmit={(event) => void handleCompile(event)}
              {...imeGuard}
              style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 12, minWidth: 0 }}
            >
              <div className="graph-form-row">
                <fieldset className="graph-role-fieldset">
                  <legend>起点方式（付款方 / 股东等）</legend>
                  <div style={{ display: 'grid', gap: 8 }}>
                    <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
                      <label className="graph-checkbox">
                        <input
                          type="radio"
                          name="fromMode"
                          style={{ width: 'auto' }}
                          checked={fromMode === 'col'}
                          onChange={() => { setFromMode('col'); setActionError(''); }}
                        />
                        来自列
                      </label>
                      <label className="graph-checkbox">
                        <input
                          type="radio"
                          name="fromMode"
                          style={{ width: 'auto' }}
                          checked={fromMode === 'constant'}
                          onChange={() => { setFromMode('constant'); setActionError(''); }}
                        />
                        固定常量主体
                      </label>
                    </div>
                    {fromMode === 'col' ? (
                      <label>
                        起点列
                        <select
                          disabled={busy}
                          value={fromCol}
                          onChange={(event) => { setFromCol(event.target.value); setActionError(''); }}
                        >
                          <option value="">请选择起点列…</option>
                          {headerOptions.map((opt) => (
                            <option key={opt.value} value={opt.value}>{opt.label}</option>
                          ))}
                        </select>
                      </label>
                    ) : (
                      <label>
                        起点固定主体名称
                        <input
                          type="text"
                          maxLength={200}
                          disabled={busy}
                          placeholder="例如：甲有限公司"
                          value={fromConstant}
                          onChange={(event) => { setFromConstant(event.target.value); setActionError(''); }}
                        />
                      </label>
                    )}
                  </div>
                </fieldset>

                <fieldset className="graph-role-fieldset">
                  <legend>终点方式（收款方 / 被投企业等）</legend>
                  <div style={{ display: 'grid', gap: 8 }}>
                    <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
                      <label className="graph-checkbox">
                        <input
                          type="radio"
                          name="toMode"
                          style={{ width: 'auto' }}
                          checked={toMode === 'col'}
                          onChange={() => { setToMode('col'); setActionError(''); }}
                        />
                        来自列
                      </label>
                      <label className="graph-checkbox">
                        <input
                          type="radio"
                          name="toMode"
                          style={{ width: 'auto' }}
                          checked={toMode === 'constant'}
                          onChange={() => { setToMode('constant'); setActionError(''); }}
                        />
                        固定常量主体
                      </label>
                    </div>
                    {toMode === 'col' ? (
                      <label>
                        终点列
                        <select
                          disabled={busy}
                          value={toCol}
                          onChange={(event) => { setToCol(event.target.value); setActionError(''); }}
                        >
                          <option value="">请选择终点列…</option>
                          {headerOptions.map((opt) => (
                            <option key={opt.value} value={opt.value}>{opt.label}</option>
                          ))}
                        </select>
                      </label>
                    ) : (
                      <label>
                        终点固定主体名称
                        <input
                          type="text"
                          maxLength={200}
                          disabled={busy}
                          placeholder="例如：乙有限公司"
                          value={toConstant}
                          onChange={(event) => { setToConstant(event.target.value); setActionError(''); }}
                        />
                      </label>
                    )}
                  </div>
                </fieldset>
              </div>

              <div className="graph-form-row">
                <label>
                  收支方向列（可选）
                  <select
                    disabled={busy}
                    value={directionCol}
                    onChange={(event) => setDirectionCol(event.target.value)}
                  >
                    <option value="">不指定（保持起点 → 终点）</option>
                    {headerOptions.map((opt) => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                </label>

                <label>
                  关系类型
                  <select
                    disabled={busy}
                    value={relationKind}
                    onChange={(event) => setRelationKind(event.target.value)}
                  >
                    {rules.relation_kinds.map((kind) => (
                      <option key={kind.value} value={kind.value}>{kind.label}</option>
                    ))}
                  </select>
                </label>
              </div>

              <div className="graph-form-row">
                <label>
                  金额列（可选）
                  <select
                    disabled={busy}
                    value={amountCol}
                    onChange={(event) => { setAmountCol(event.target.value); setActionError(''); }}
                  >
                    <option value="">不映射金额列</option>
                    {headerOptions.map((opt) => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                </label>

                <label>
                  币种列（可选）
                  <select
                    disabled={busy}
                    value={currencyCol}
                    onChange={(event) => { setCurrencyCol(event.target.value); setActionError(''); }}
                  >
                    <option value="">不映射币种列（使用默认币种）</option>
                    {headerOptions.map((opt) => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                </label>

                <label>
                  默认币种
                  <input
                    type="text"
                    maxLength={8}
                    disabled={busy}
                    value={currency}
                    onChange={(event) => setCurrency(event.target.value)}
                  />
                </label>

                <label>
                  最小金额过滤（可选）
                  <input
                    type="text"
                    maxLength={40}
                    disabled={busy}
                    placeholder="例如：1000"
                    value={filterMinAmount}
                    onChange={(event) => { setFilterMinAmount(event.target.value); setActionError(''); }}
                  />
                </label>
              </div>

              <div className="graph-form-row">
                <label>
                  持股比例列（可选）
                  <select
                    disabled={busy}
                    value={sharePctCol}
                    onChange={(event) => setSharePctCol(event.target.value)}
                  >
                    <option value="">不映射持股比例</option>
                    {headerOptions.map((opt) => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                </label>

                <label>
                  日期列（可选）
                  <select
                    disabled={busy}
                    value={dateCol}
                    onChange={(event) => setDateCol(event.target.value)}
                  >
                    <option value="">不映射日期列</option>
                    {headerOptions.map((opt) => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                </label>

                <label>
                  标签列（可选）
                  <select
                    disabled={busy}
                    value={labelCol}
                    onChange={(event) => setLabelCol(event.target.value)}
                  >
                    <option value="">不映射标签列</option>
                    {headerOptions.map((opt) => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                </label>
              </div>

              <label className="graph-checkbox">
                <input
                  type="checkbox"
                  style={{ width: 'auto' }}
                  disabled={busy}
                  checked={aggregateByPair}
                  onChange={(event) => setAggregateByPair(event.target.checked)}
                />
                按起止主体对汇总（求总额、笔数与起止日期）
              </label>

              {actionError ? <p role="alert" className="graph-error-inline">错误：{actionError}</p> : null}

              <div className="work-actions" style={{ justifyContent: 'flex-end' }}>
                <button type="button" disabled={busy} onClick={() => goToStep(1)}>
                  上一步：选表与预览
                </button>
                <button
                  type="submit"
                  disabled={busy}
                  style={{
                    background: 'var(--accent)',
                    borderColor: 'var(--accent)',
                    color: '#fff',
                    fontWeight: 650,
                  }}
                >
                  {busy ? '正在计算汇总…' : '生成预览'}
                </button>
              </div>
            </form>
          </section>
        ) : null}

        {step === 3 && compileResult ? (
          <section aria-label="步骤 3：核对确定性汇总与异常行" style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 12, minWidth: 0 }}>
            <h3 ref={stepHeadingRef} tabIndex={-1} style={{ margin: 0, fontSize: 16 }}>
              步骤 3：核对确定性汇总与异常行
            </h3>

            <p role="status" className="graph-notice-inline" style={{ margin: 0 }}>
              处理数据行 {compileResult.processed_rows} · 将新建主体 {compileResult.new_entities.length} 个 · 生成/关联边 {compileResult.compiled_edges.length} 条 · 跳过异常行 {compileResult.skipped_rows.length} 条
            </p>

            {(() => {
              const pairCurrencies = new Map<string, Set<string>>();
              for (const edge of compileResult.compiled_edges) {
                if (!edge.currency) continue;
                const key = `${edge.from_name}\u0000${edge.to_name}`;
                const set = pairCurrencies.get(key) ?? new Set<string>();
                set.add(edge.currency);
                pairCurrencies.set(key, set);
              }
              const hasMulti = [...pairCurrencies.values()].some((set) => set.size > 1);
              return hasMulti ? (
                <p role="status" className="graph-freeze-warning" style={{ margin: 0 }}>
                  提示：同一主体对存在多种币种，预览已按币种拆分而不混算；受关系图同向同类唯一约束，直接整批提交时同向第二币种行将安全转入跳过清单而不合并金额。
                </p>
              ) : null;
            })()}

            {compileResult.new_entities.length > 0 ? (
              <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 6, minWidth: 0 }}>
                <h4 style={{ margin: 0, fontSize: 13 }}>将新建主体（{compileResult.new_entities.length}）</h4>
                <ul
                  style={{
                    listStyle: 'none',
                    margin: 0,
                    padding: 0,
                    display: 'flex',
                    flexWrap: 'wrap',
                    gap: 6,
                  }}
                >
                  {compileResult.new_entities.map((entity) => (
                    <li
                      key={`${entity.entity_kind}:${entity.name}`}
                      className="graph-role"
                      style={{ padding: '3px 8px', fontSize: 12 }}
                    >
                      {entity.name}（{entityKindLabel(rules, entity.entity_kind)}）
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            <div className="table-scroll" style={{ minWidth: 0 }} tabIndex={0} role="region" aria-labelledby={edgesCaptionId}>
              <table className="case-table" style={{ minWidth: 820 }}>
                <caption id={edgesCaptionId} style={{ textAlign: 'left', padding: '4px 0 8px', fontSize: 13, color: 'var(--muted)' }}>
                  拟写入关系汇总预览（{compileResult.selected_sheet}）
                </caption>
                <thead>
                  <tr>
                    <th scope="col">起点 → 终点</th>
                    <th scope="col">关系类型</th>
                    <th scope="col">金额</th>
                    <th scope="col">持股比例</th>
                    <th scope="col">笔数</th>
                    <th scope="col">起止日期</th>
                    <th scope="col">标签</th>
                    <th scope="col">出处定位</th>
                    <th scope="col">状态</th>
                  </tr>
                </thead>
                <tbody>
                  {compileResult.compiled_edges.length === 0 ? (
                    <tr>
                      <td colSpan={9} className="muted">没有可生成的有效关系边，请返回检查列映射或过滤条件。</td>
                    </tr>
                  ) : (
                    compileResult.compiled_edges.map((edge, idx) => (
                      <tr key={`${edge.from_name}-${edge.to_name}-${idx}`}>
                        <th scope="row">{edge.from_name} → {edge.to_name}</th>
                        <td>{relationKindLabel(rules, edge.relation_kind)}</td>
                        <td style={{ fontVariantNumeric: 'tabular-nums' }}>
                          {formatAmountCents(edge.amount_cents, edge.currency)}
                        </td>
                        <td>{edge.share_pct != null ? `${edge.share_pct}%` : '—'}</td>
                        <td>{edge.tx_count}</td>
                        <td style={{ fontVariantNumeric: 'tabular-nums' }}>
                          {formatDateRange(edge.date_start, edge.date_end)}
                        </td>
                        <td>{edge.label ?? '—'}</td>
                        <td>{edge.proves}</td>
                        <td>
                          <span className={`graph-badge ${edge.existing_relation_id ? 'import' : 'ok'}`} style={{ marginLeft: 0 }}>
                            {edge.existing_relation_id ? '已有关系补出处' : '新建关系'}
                          </span>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>

            {compileResult.skipped_rows.length > 0 ? (
              <section className="graph-freeze-warning" aria-label="跳过异常行警告">
                <p>
                  <strong>警告：以下 {compileResult.skipped_rows.length} 行数据因校验未通过被跳过（不会静默丢弃或折算为 0）：</strong>
                </p>
                <div className="table-scroll" style={{ minWidth: 0 }} tabIndex={0} role="region" aria-labelledby={skippedCaptionId}>
                  <table className="case-table" style={{ minWidth: 540 }}>
                    <caption id={skippedCaptionId} className="sr-only">已跳过的异常行明细</caption>
                    <thead>
                      <tr>
                        <th scope="col">行号</th>
                        <th scope="col">跳过原因</th>
                        <th scope="col">原始行内容</th>
                      </tr>
                    </thead>
                    <tbody>
                      {compileResult.skipped_rows.map((skipped) => (
                        <tr key={skipped.row_number}>
                          <th scope="row">第 {skipped.row_number} 行</th>
                          <td>{formatSkipReason(skipped.reason)}</td>
                          <td>{skipped.raw_row.length > 0 ? skipped.raw_row.join(' | ') : '（空行）'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            ) : null}

            {actionError ? <p role="alert" className="graph-error-inline">错误：{actionError}</p> : null}

            <div className="work-actions" style={{ justifyContent: 'flex-end' }}>
              <button type="button" disabled={busy} onClick={() => goToStep(2)}>
                上一步：修改映射
              </button>
              <button type="button" disabled={busy} onClick={onClose}>关闭</button>
              <button
                type="button"
                disabled={readOnly || busy || compileResult.compiled_edges.length === 0}
                onClick={() => void handleCommit()}
                style={{
                  background: 'var(--accent)',
                  borderColor: 'var(--accent)',
                  color: '#fff',
                  fontWeight: 650,
                }}
              >
                {busy ? '正在写入关系图…' : '确认写入当前关系图'}
              </button>
            </div>
          </section>
        ) : null}
      </div>
    </dialog>
  );
}
