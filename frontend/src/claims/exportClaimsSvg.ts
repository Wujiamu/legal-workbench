/**
 * 诉请金额瀑布图三态导出（SVG / PNG 与文本引用索引下载）。
 * 纯函数生成独立 SVG（内联颜色与字体，不依赖外部 CSS），
 * PNG 在浏览器端通过 ../graph/exportSvg 的 downloadPngFile 渲染同一份 SVG。
 * 绝不读取或输出内部 calc_note 与 relative_source。
 */
import { downloadPngFile, downloadSvgFile, exportFileName } from '../graph/exportSvg';
import { formatCompactCents } from './money';
import { CURRENCY_LABELS } from './types';
import type { ClaimItem, Currency } from './types';
import { columnAmountLabel, waterfallColumns, waterfallScale } from './WaterfallChart';

const PAD = 36;
const HEADER = 84;
const LEFT_AXIS = 68;
const PLOT_TOP = HEADER + 30;
const PLOT_HEIGHT = 204;
const LABEL_BLOCK = 56;
const COL_STEP = 112;
const FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", '
  + '"Microsoft YaHei", "Source Han Sans SC", "Noto Sans CJK SC", sans-serif';

const COLOR_POSITIVE = '#2f5d50';
const COLOR_DEDUCTION = '#b3261e';
const COLOR_TOTAL = '#173e35';
const COLOR_TOTAL_NEGATIVE = '#8c2f2f';

export interface ClaimsExportReference {
  targetId: string;
  index: number;
  label: string;
}

export interface ClaimsSvgProjection {
  profile: 'client' | 'court';
  references: readonly ClaimsExportReference[];
  frozenAt?: string;
  digest?: string;
  notice?: string;
}

export type ClaimsExportableItem = Pick<ClaimItem, 'id' | 'title' | 'category' | 'amount_cents'>;

export interface ClaimsSvgExportInput {
  title: string;
  caseTitle?: string;
  currency: Currency;
  items: readonly ClaimsExportableItem[];
  subtotalCents: number;
  generatedAt?: string;
  projection?: ClaimsSvgProjection;
}

function escapeXml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;',
  })[char] ?? char);
}

function textWidth(text: string, size: number): number {
  let units = 0;
  for (const char of text) {
    units += /[\u2e80-\u9fff\uff00-\uffef]/.test(char) ? 1 : 0.56;
  }
  return units * size;
}

function truncateToWidth(text: string, maxWidth: number, size: number): string {
  if (textWidth(text, size) <= maxWidth) return text;
  let result = '';
  for (const char of text) {
    if (textWidth(`${result}${char}…`, size) > maxWidth) break;
    result += char;
  }
  return `${result}…`;
}

export function buildClaimsSvg(input: ClaimsSvgExportInput): {
  svg: string;
  width: number;
  height: number;
} {
  const { currency, items, subtotalCents, projection } = input;
  const columns = waterfallColumns(items, subtotalCents);
  const scale = waterfallScale(columns, PLOT_TOP + PLOT_HEIGHT + 62, PLOT_TOP, 62);

  const marks = new Map<string, number[]>();
  for (const reference of projection?.references ?? []) {
    const list = marks.get(reference.targetId) ?? [];
    list.push(reference.index);
    marks.set(reference.targetId, list);
  }

  const itemIds = new Set(items.map((item) => item.id));
  const visibleReferences = projection?.references.filter((ref) => itemIds.has(ref.targetId)) ?? [];
  const footnotes = visibleReferences.slice(0, 12);
  const hasOverflowReferences = visibleReferences.length > footnotes.length;

  const plotWidth = Math.max(540, columns.length * COL_STEP);
  const plotLeft = PAD + LEFT_AXIS;
  const width = Math.round(plotLeft + plotWidth + PAD);
  const chartBottom = PLOT_TOP + PLOT_HEIGHT + LABEL_BLOCK;
  const footnotesHeight = projection
    ? (footnotes.length > 0 ? 12 + footnotes.length * 20 : 0) + 38
    : 38;
  const height = Math.round(chartBottom + footnotesHeight);

  const columnWidth = plotWidth / columns.length;
  const barWidth = Math.min(54, columnWidth * 0.52);
  const baseline = scale.y(0);

  const modeLabel = projection
    ? (projection.profile === 'client' ? '客户沟通态' : '当庭呈递态')
    : '内部工作态 · 精确到分';
  const digestShort = projection?.digest
    ? (projection.digest.length > 12 ? projection.digest.slice(0, 12) : projection.digest)
    : null;
  const subtitle = [
    input.caseTitle,
    `${CURRENCY_LABELS[currency]}（${currency}）`,
    modeLabel,
    projection?.frozenAt ? `冻结 ${projection.frozenAt}` : null,
    digestShort ? `摘要 ${digestShort}` : null,
    !projection && input.generatedAt ? `导出 ${input.generatedAt}` : null,
  ].filter(Boolean).join(' · ');

  const parts: string[] = [];
  parts.push(
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" `
    + `viewBox="0 0 ${width} ${height}" font-family='${FONT}'>`,
  );
  parts.push(`<rect width="${width}" height="${height}" fill="#ffffff"/>`);

  // 页眉
  parts.push(
    `<text x="${PAD}" y="38" font-size="20" font-weight="700" fill="#202c29">`
    + `${escapeXml(input.title)}</text>`,
  );
  parts.push(
    `<text x="${PAD}" y="60" font-size="12" fill="#53635c">${escapeXml(subtitle)}</text>`,
  );
  parts.push(
    `<line x1="${PAD}" y1="${HEADER - 8}" x2="${width - PAD}" y2="${HEADER - 8}" `
    + 'stroke="#d7ded7" stroke-width="1"/>',
  );

  // 坐标轴与基线
  parts.push(
    `<line x1="${plotLeft - 8}" y1="${baseline.toFixed(1)}" x2="${width - PAD}" `
    + `y2="${baseline.toFixed(1)}" stroke="#a7b5aa" stroke-width="1"/>`,
  );
  parts.push(
    `<text x="${plotLeft - 12}" y="${(scale.y(scale.max) + 10).toFixed(1)}" `
    + 'text-anchor="end" font-size="10" fill="#6b6862">'
    + `${escapeXml(formatCompactCents(scale.max))}</text>`,
  );
  parts.push(
    `<text x="${plotLeft - 12}" y="${(baseline - 4).toFixed(1)}" `
    + 'text-anchor="end" font-size="10" fill="#6b6862">0</text>',
  );
  if (scale.min < 0) {
    parts.push(
      `<text x="${plotLeft - 12}" y="${(scale.y(scale.min) - 2).toFixed(1)}" `
      + 'text-anchor="end" font-size="10" fill="#6b6862">'
      + `${escapeXml(formatCompactCents(scale.min))}</text>`,
    );
  }

  // 柱间连接虚线
  for (let index = 0; index < columns.length - 1; index += 1) {
    const column = columns[index];
    const startX = plotLeft + columnWidth * index + (columnWidth + barWidth) / 2;
    const endX = plotLeft + columnWidth * (index + 1) + (columnWidth - barWidth) / 2;
    const y = scale.y(column.to);
    parts.push(
      `<line x1="${startX.toFixed(1)}" y1="${y.toFixed(1)}" x2="${endX.toFixed(1)}" `
      + `y2="${y.toFixed(1)}" stroke="#b9c6bd" stroke-width="1" stroke-dasharray="3 3"/>`,
    );
  }

  // 柱条、标签、金额与引用角标
  const labelY = PLOT_TOP + PLOT_HEIGHT + 22;
  const amountY = labelY + 16;
  columns.forEach((column, index) => {
    const x = plotLeft + columnWidth * index + (columnWidth - barWidth) / 2;
    const yFrom = scale.y(column.from);
    const yTo = scale.y(column.to);
    const y = Math.min(yFrom, yTo);
    const barHeight = Math.max(2, Math.abs(yTo - yFrom));
    const negative = column.to < column.from;
    const fill = column.kind === 'total'
      ? (column.to < 0 ? COLOR_TOTAL_NEGATIVE : COLOR_TOTAL)
      : negative ? COLOR_DEDUCTION : COLOR_POSITIVE;

    parts.push(
      `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" `
      + `height="${barHeight.toFixed(1)}" rx="2" fill="${fill}"/>`,
    );

    const cx = x + barWidth / 2;
    const barMarks = projection && column.kind === 'item' ? (marks.get(column.key) ?? []) : [];
    if (barMarks.length > 0) {
      const badge = barMarks.map((n) => `[${n}]`).join(' ');
      const badgeWidth = Math.max(24, textWidth(badge, 9.5) + 10);
      const badgeY = y - 22;
      parts.push(
        `<rect x="${(cx - badgeWidth / 2).toFixed(1)}" y="${badgeY.toFixed(1)}" `
        + `width="${badgeWidth.toFixed(1)}" height="16" rx="4" fill="#eef3ef" `
        + 'stroke="#a7b5aa" stroke-width="0.8"/>'
        + `<text x="${cx.toFixed(1)}" y="${(badgeY + 11.5).toFixed(1)}" text-anchor="middle" `
        + `font-size="9.5" font-weight="650" fill="#173e35">${escapeXml(badge)}</text>`,
      );
    }

    const label = truncateToWidth(column.label, columnWidth - 8, 11);
    parts.push(
      `<text x="${cx.toFixed(1)}" y="${labelY}" text-anchor="middle" font-size="11" fill="#44574d">`
      + `${escapeXml(label)}</text>`,
    );
    const amountText = columnAmountLabel(column, currency);
    parts.push(
      `<text x="${cx.toFixed(1)}" y="${amountY}" text-anchor="middle" font-size="11" `
      + `font-weight="650" fill="${negative ? '#8c2f2f' : '#202c29'}">${escapeXml(amountText)}</text>`,
    );
  });

  // 页脚分隔线与脚注
  parts.push(
    `<line x1="${PAD}" y1="${chartBottom}" x2="${width - PAD}" y2="${chartBottom}" `
    + 'stroke="#d7ded7" stroke-width="1"/>',
  );
  if (projection) {
    footnotes.forEach((reference, line) => {
      const lineText = truncateToWidth(`[${reference.index}] ${reference.label}`, width - PAD * 2, 11);
      parts.push(
        `<text x="${PAD}" y="${chartBottom + 20 + line * 20}" font-size="11" fill="#44574d">`
        + `${escapeXml(lineText)}</text>`,
      );
    });
  }
  const footerNote = projection
    ? [modeLabel, projection.notice, hasOverflowReferences ? '完整编号见引用索引附表' : null]
        .filter(Boolean).join(' · ')
    : '内部工作态 · 精确到分';
  parts.push(
    `<text x="${PAD}" y="${height - 16}" font-size="10.5" fill="#6b6862">`
    + `${escapeXml(footerNote)}</text>`,
  );
  parts.push('</svg>');
  return { svg: parts.join(''), width, height };
}

export function claimsExportFileName(
  groupTitle: string,
  extension: 'svg' | 'png' | 'md' | 'csv',
  date = new Date(),
): string {
  const safeTitle = groupTitle.trim() || '诉请金额';
  if (extension === 'svg' || extension === 'png') {
    return exportFileName(safeTitle, extension, date);
  }
  const stamp = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(date);
  const safe = safeTitle.replace(/[\\/:*?"<>|\s]+/g, '-').replace(/^-+|-+$/g, '') || '诉请金额';
  return `${safe}-${stamp}-引用索引.${extension}`;
}

export function downloadTextFile(filename: string, content: string, mimeType: string): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export { downloadPngFile, downloadSvgFile };
