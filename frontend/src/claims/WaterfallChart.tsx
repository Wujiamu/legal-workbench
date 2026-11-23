/**
 * 组内瀑布图：纯 SVG，无图表依赖。
 * 正项向上、抵扣项向下，末柱为组内净小计；柱高与明细表使用同一份整数分数据。
 */
import { useId } from 'react';
import { formatCents, formatCompactCents, formatMoney, signedCents } from './money';
import { CURRENCY_LABELS } from './types';
import type { ClaimCategory, ClaimItem, Currency } from './types';

export interface WaterfallColumn {
  key: string;
  label: string;
  kind: 'item' | 'total';
  category?: ClaimCategory;
  /** 柱条起点/终点的累计值（整数分，可为负）。 */
  from: number;
  to: number;
  /** 本项金额（抵扣项仍为正数，方向由 category 决定）。 */
  amount: number;
}

export function waterfallColumns(
  items: readonly Pick<ClaimItem, 'id' | 'title' | 'category' | 'amount_cents'>[],
  subtotalCents: number,
): WaterfallColumn[] {
  let running = 0;
  const columns: WaterfallColumn[] = items.map((item) => {
    const from = running;
    const to = running + signedCents(item.category, item.amount_cents);
    running = to;
    return {
      key: item.id, label: item.title, kind: 'item',
      category: item.category, from, to, amount: item.amount_cents,
    };
  });
  columns.push({
    key: 'total', label: '组内小计', kind: 'total', from: 0, to: subtotalCents, amount: subtotalCents,
  });
  return columns;
}

export interface WaterfallScale {
  max: number;
  min: number;
  y(value: number): number;
  top: number;
  height: number;
  bottom: number;
}

export function waterfallScale(columns: WaterfallColumn[], height = 268,
                               top = 30, bottom = 58): WaterfallScale {
  const values = [0];
  for (const column of columns) values.push(column.from, column.to);
  let max = Math.max(...values);
  let min = Math.min(...values);
  if (max === min) {
    max += 100;
    min -= 100;
  } else {
    const padding = (max - min) * 0.08;
    max = Math.min(Number.MAX_SAFE_INTEGER, Math.ceil(max + padding));
    min = Math.max(-Number.MAX_SAFE_INTEGER, Math.floor(min - padding));
  }
  const plotHeight = height - top - bottom;
  return {
    max, min, top, height, bottom,
    y: (value: number) => top + ((max - value) / (max - min)) * plotHeight,
  };
}

function truncate(text: string, length: number): string {
  const characters = Array.from(text);
  return characters.length > length ? `${characters.slice(0, length).join('')}…` : text;
}

export function columnAmountLabel(column: WaterfallColumn, currency: Currency): string {
  if (column.kind === 'total') return formatMoney(column.to, currency);
  if (column.category === 'deduction') return `−${formatMoney(column.amount, currency)}`;
  return formatMoney(column.amount, currency);
}

export function WaterfallChart({ items, subtotalCents, currency, groupTitle }: {
  items: ClaimItem[];
  subtotalCents: number;
  currency: Currency;
  groupTitle: string;
}) {
  const columns = waterfallColumns(items, subtotalCents);
  const captionId = useId();
  const width = Math.max(560, columns.length * 96);
  const height = 268;
  const top = 30;
  const bottom = 58;
  const scale = waterfallScale(columns, height, top, bottom);
  const plotWidth = width - 32;
  const columnWidth = plotWidth / columns.length;
  const barWidth = Math.min(54, columnWidth * 0.5);
  const baseline = scale.y(0);
  const label = columns.map((column) =>
    `${column.label} ${column.kind === 'total' ? '合计' : column.category === 'deduction' ? '减' : '加'} ${formatCents(column.kind === 'total' ? column.to : column.amount)}`).join('；');

  return (
    <figure className="claims-waterfall" aria-labelledby={captionId}>
      <figcaption id={captionId}>
        组内瀑布图 · {groupTitle}（{CURRENCY_LABELS[currency]}，精确到分）
      </figcaption>
      <div className="claims-waterfall-scroll">
        <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`组内瀑布图：${label}`}
             style={{ minWidth: width }}>
          <line x1={16} x2={width - 16} y1={baseline} y2={baseline} className="wf-baseline" />
          <text x={16} y={scale.y(scale.max) + 12} className="wf-axis">{formatCompactCents(scale.max)}</text>
          <text x={16} y={baseline - 5} className="wf-axis">0</text>
          {scale.min < 0
            ? <text x={16} y={scale.y(scale.min) - 3} className="wf-axis">{formatCompactCents(scale.min)}</text>
            : null}
          {columns.slice(0, -1).map((column, index) => {
            const startX = 16 + columnWidth * index + (columnWidth + barWidth) / 2;
            const endX = 16 + columnWidth * (index + 1) + (columnWidth - barWidth) / 2;
            const y = scale.y(column.to);
            return <line key={`link-${column.key}`} x1={startX} x2={endX} y1={y} y2={y}
                         className="wf-connector" />;
          })}
          {columns.map((column, index) => {
            const x = 16 + columnWidth * index + (columnWidth - barWidth) / 2;
            const yFrom = scale.y(column.from);
            const yTo = scale.y(column.to);
            const y = Math.min(yFrom, yTo);
            const barHeight = Math.max(2, Math.abs(yTo - yFrom));
            const negative = column.to < column.from;
            const className = column.kind === 'total'
              ? (column.to < 0 ? 'wf-total wf-negative' : 'wf-total')
              : negative ? 'wf-deduction' : 'wf-positive';
            const textY = height - 34;
            return (
              <g key={column.key}>
                <rect
                  x={x} y={y} width={barWidth} height={barHeight}
                  className={className}
                  data-role={column.kind === 'total' ? 'total' : 'bar'}
                  data-item-id={column.key}
                  data-kind={column.kind === 'total' ? 'total' : column.category}
                  data-from={column.from}
                  data-to={column.to}
                >
                  <title>{`${column.label}：${column.kind === 'total' ? '小计' : column.category === 'deduction' ? '抵扣' : '计入'} ${formatCents(column.amount)}`}</title>
                </rect>
                <text x={x + barWidth / 2} y={textY} className="wf-label">
                  {truncate(column.label, column.kind === 'total' ? 6 : 8)}
                </text>
                <text x={x + barWidth / 2} y={textY + 15}
                      className={negative ? 'wf-amount wf-amount-negative' : 'wf-amount'}>
                  {columnAmountLabel(column, currency)}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
    </figure>
  );
}
