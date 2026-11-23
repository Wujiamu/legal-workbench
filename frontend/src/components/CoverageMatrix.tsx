/**
 * 案卷材料覆盖矩阵（只读投影）：按「材料分类 × 解析与引用健康度」汇总计数。
 *
 * 纯语义化 <details> + <table>，不引入图表库；点击任意单元格可按分类/状态筛选材料列表。
 * 接口失败时静默降级为不渲染，避免阻断既有材料列表测试或工作区浏览。
 */
import { useEffect, useId, useRef, useState } from 'react';
import { materialsApi } from '../api/endpoints';
import type { CorpusCoverageMatrixOut, Schemas } from '../api/endpoints';

export type CorpusStatus = Schemas['CoverageCell']['status'];

export interface CoverageStatusMeta {
  key: CorpusStatus;
  label: string;
  shortLabel: string;
  toneClass: 'is-referenced' | 'is-unused' | 'is-warn' | 'is-pending';
}

export const COVERAGE_STATUSES: readonly CoverageStatusMeta[] = [
  { key: 'referenced', label: '已入图/已引用', shortLabel: '已引用', toneClass: 'is-referenced' },
  { key: 'parsed_unused', label: '已解析未引用', shortLabel: '未引用', toneClass: 'is-unused' },
  { key: 'partial_or_failed', label: 'OCR告警/失败', shortLabel: '异常/告警', toneClass: 'is-warn' },
  { key: 'pending_or_unsupported', label: '未解析/不支持', shortLabel: '待解析', toneClass: 'is-pending' },
];

export interface CoverageMatrixFilter {
  category: string | null;
  status: string | null;
}

export function CoverageMatrix({
  caseId,
  refreshKey = 0,
  activeFilter = null,
  onSelectCell,
  onMatrixLoaded,
}: {
  caseId: string;
  refreshKey?: number;
  activeFilter?: CoverageMatrixFilter | null;
  onSelectCell: (filter: CoverageMatrixFilter) => void;
  onMatrixLoaded?: (matrix: CorpusCoverageMatrixOut | null) => void;
}) {
  const captionId = useId();
  const onMatrixLoadedRef = useRef(onMatrixLoaded);
  onMatrixLoadedRef.current = onMatrixLoaded;
  const [state, setState] = useState<{
    caseId: string;
    matrix: CorpusCoverageMatrixOut | null;
    failed: boolean;
  }>({ caseId, matrix: null, failed: false });

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    setState((prev) => (prev.caseId === caseId ? { ...prev, failed: false } : { caseId, matrix: null, failed: false }));

    Promise.resolve(materialsApi.coverageMatrix(caseId, undefined, signal))
      .then((result) => {
        if (signal.aborted) return;
        setState({ caseId, matrix: result, failed: false });
        onMatrixLoadedRef.current?.(result);
      })
      .catch(() => {
        if (signal.aborted) return;
        setState({ caseId, matrix: null, failed: true });
        onMatrixLoadedRef.current?.(null);
      });

    return () => controller.abort();
  }, [caseId, refreshKey]);

  if (state.caseId !== caseId || state.failed || !state.matrix) {
    return null;
  }

  const matrix = state.matrix;
  const totalCount = matrix.totals.total ?? matrix.materials.length;
  const referencedCount = matrix.totals.referenced ?? 0;
  const unusedCount = matrix.totals.parsed_unused ?? 0;
  const warnCount = matrix.totals.partial_or_failed ?? 0;
  const pendingCount = matrix.totals.pending_or_unsupported ?? 0;

  const cellMap = new Map<string, number>();
  for (const cell of matrix.cells) {
    cellMap.set(`${cell.category}\0${cell.status}`, cell.count);
  }

  function toggleFilter(nextCategory: string | null, nextStatus: string | null) {
    const sameCategory = (activeFilter?.category ?? null) === nextCategory;
    const sameStatus = (activeFilter?.status ?? null) === nextStatus;
    if (sameCategory && sameStatus) {
      onSelectCell({ category: null, status: null });
      return;
    }
    onSelectCell({ category: nextCategory, status: nextStatus });
  }

  return (
    <details className="coverage-matrix-details">
      <style>{`
        .coverage-matrix-details {
          border: 1px solid var(--line);
          border-radius: 10px;
          background: var(--surface);
          margin: 0 0 12px;
        }
        .coverage-matrix-summary {
          cursor: pointer;
          padding: 10px 14px;
          font-size: 13px;
          color: var(--ink);
          display: flex;
          flex-wrap: wrap;
          gap: 6px 12px;
          align-items: baseline;
        }
        .coverage-matrix-summary-title { font-weight: 650; }
        .coverage-matrix-summary-meta { color: var(--muted); }
        .coverage-matrix-scroll {
          padding: 0 14px 12px;
          border-top: 1px solid var(--line);
          container-type: inline-size;
        }
        .coverage-matrix-table {
          width: 100%;
          border-collapse: separate;
          border-spacing: 0;
          font-size: 12.5px;
          margin-top: 10px;
          text-align: center;
        }
        .coverage-matrix-table th,
        .coverage-matrix-table td {
          padding: 6px 8px;
          border-bottom: 1px solid var(--line);
        }
        .coverage-matrix-table thead th {
          position: sticky;
          inset-block-start: 0;
          z-index: 1;
          background: var(--warm);
          color: var(--muted);
          font-weight: 650;
          white-space: nowrap;
        }
        .coverage-matrix-table tbody th,
        .coverage-matrix-table tfoot th {
          position: sticky;
          inset-inline-start: 0;
          background: var(--surface);
          text-align: left;
          font-weight: 600;
          color: var(--ink);
        }
        .coverage-matrix-cat-btn {
          border: 0;
          background: transparent;
          font: inherit;
          color: inherit;
          cursor: pointer;
          padding: 2px 4px;
          border-radius: 4px;
          text-align: left;
        }
        .coverage-matrix-cat-btn[aria-pressed="true"] {
          background: #e8efec;
          color: var(--accent);
          font-weight: 700;
        }
        .coverage-cell-btn {
          min-width: 44px;
          padding: 4px 10px;
          border-radius: 6px;
          border: 1px solid var(--line);
          font: inherit;
          font-variant-numeric: tabular-nums;
          font-weight: 650;
          cursor: pointer;
          background: var(--warm);
          color: var(--ink);
        }
        .coverage-cell-btn.is-zero {
          opacity: 0.55;
          font-weight: 450;
        }
        .coverage-cell-btn.is-referenced:not(.is-zero) {
          background: #e5f3ea;
          border-color: #7cc79a;
          color: #1c6b3c;
        }
        .coverage-cell-btn.is-unused:not(.is-zero) {
          background: #eef2f8;
          border-color: #9cb0d4;
          color: #2f4b7c;
        }
        .coverage-cell-btn.is-warn:not(.is-zero) {
          background: #fbeaea;
          border-color: #d8b4b4;
          color: #8c2f2f;
        }
        .coverage-cell-btn.is-pending:not(.is-zero) {
          background: #fdf3df;
          border-color: #e0c060;
          color: #8a5a00;
        }
        .coverage-cell-btn.is-active,
        .coverage-cell-btn[aria-pressed="true"] {
          outline: 2px solid var(--accent);
          outline-offset: 1px;
        }
      `}</style>
      <summary className="coverage-matrix-summary">
        <span className="coverage-matrix-summary-title">案卷覆盖矩阵</span>
        <span className="coverage-matrix-summary-meta">
          共 {totalCount} 份 · 已引用 {referencedCount} · 未引用 {unusedCount} · 异常/告警 {warnCount} · 待解析 {pendingCount}
        </span>
      </summary>
      {matrix.categories.length === 0 ? (
        <p className="muted" style={{ padding: '0 14px 12px', margin: 0, fontSize: 13 }}>
          暂无材料数据。
        </p>
      ) : (
        <div tabIndex={0} role="region" aria-labelledby={captionId} className="table-scroll coverage-matrix-scroll">
          <table className="coverage-matrix-table">
            <caption id={captionId} className="sr-only">
              按分类与引用状态统计的案卷覆盖矩阵（共 {totalCount} 份）
            </caption>
            <thead>
              <tr>
                <th scope="col">分类</th>
                {COVERAGE_STATUSES.map((status) => (
                  <th key={status.key} scope="col">{status.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matrix.categories.map((category) => {
                const rowActive = activeFilter?.category === category && activeFilter?.status === null;
                return (
                  <tr key={category}>
                    <th scope="row">
                      <button
                        type="button"
                        className="coverage-matrix-cat-btn"
                        aria-pressed={rowActive}
                        onClick={() => toggleFilter(category, null)}
                      >
                        {category}
                      </button>
                    </th>
                    {COVERAGE_STATUSES.map((status) => {
                      const count = cellMap.get(`${category}\0${status.key}`) ?? 0;
                      const isActive = activeFilter?.category === category
                        && activeFilter?.status === status.key;
                      return (
                        <td key={status.key}>
                          <button
                            type="button"
                            className={`coverage-cell-btn ${status.toneClass}${count === 0 ? ' is-zero' : ''}${isActive ? ' is-active' : ''}`}
                            aria-pressed={isActive}
                            aria-label={`${category} ${status.label} ${count} 份`}
                            onClick={() => toggleFilter(category, status.key)}
                          >
                            {count}
                          </button>
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
            <tfoot>
              <tr>
                <th scope="row">合计</th>
                {COVERAGE_STATUSES.map((status) => {
                  const count = matrix.totals[status.key] ?? 0;
                  const isActive = activeFilter?.category === null && activeFilter?.status === status.key;
                  return (
                    <td key={status.key}>
                      <button
                        type="button"
                        className={`coverage-cell-btn ${status.toneClass}${count === 0 ? ' is-zero' : ''}${isActive ? ' is-active' : ''}`}
                        aria-pressed={isActive}
                        aria-label={`全部 ${status.label} ${count} 份`}
                        onClick={() => toggleFilter(null, status.key)}
                      >
                        {count}
                      </button>
                    </td>
                  );
                })}
              </tr>
            </tfoot>
          </table>
        </div>
      )}
    </details>
  );
}
