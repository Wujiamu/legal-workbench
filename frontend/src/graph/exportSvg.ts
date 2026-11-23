/**
 * 关系图 SVG 和 PNG 使用同一份导出投影。
 * 纯函数生成 SVG（可单测），PNG 在浏览器里把同一份 SVG 画到 canvas。
 * 图例的四档线型来自规则配置，导出与屏幕一致。
 */
import { credibilityRule, entityKindLabel, relationKindLabel, roleLabel } from './rules';
import type { CanvasEdge, CanvasGroup, CanvasModel, CanvasNode, GraphRules } from './types';
import { NODE_HEIGHT, NODE_WIDTH } from './layout';

const PAD = 36;
const HEADER = 78;
const LEGEND = 118;
const FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", '
  + '"Microsoft YaHei", "Source Han Sans SC", "Noto Sans CJK SC", sans-serif';

export interface GraphExportReference {
  targetId: string;
  index: number;
  label: string;
}

export interface GraphExportProjection {
  profile: 'client' | 'court';
  /** 由服务端过滤后的图对象，不从现行画布推断。 */
  includedIds: ReadonlySet<string>;
  references: readonly GraphExportReference[];
  version?: string;
  notice?: string;
}

export interface ExportInput {
  title: string;
  caseTitle: string;
  model: CanvasModel;
  rules: GraphRules;
  note?: string;
  generatedAt?: string;
  projection?: GraphExportProjection;
  showWorkingLabels?: boolean;
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

function truncate(text: string, maxWidth: number, size: number): string {
  if (textWidth(text, size) <= maxWidth) return text;
  let result = '';
  for (const char of text) {
    if (textWidth(`${result}${char}…`, size) > maxWidth) break;
    result += char;
  }
  return `${result}…`;
}

function nodeRect(node: CanvasNode): { x: number; y: number; w: number; h: number } {
  return { x: node.x, y: node.y, w: NODE_WIDTH, h: NODE_HEIGHT };
}

function groupRect(group: CanvasGroup): { x: number; y: number; w: number; h: number } {
  return { x: group.x, y: group.y, w: group.width, h: group.height };
}

function borderPoint(node: CanvasNode, towardX: number, towardY: number): { x: number; y: number } {
  const cx = node.x + NODE_WIDTH / 2;
  const cy = node.y + NODE_HEIGHT / 2;
  const dx = towardX - cx;
  const dy = towardY - cy;
  if (dx === 0 && dy === 0) return { x: cx, y: cy };
  const scale = Math.min((NODE_WIDTH / 2) / Math.abs(dx || 1e-6),
    (NODE_HEIGHT / 2) / Math.abs(dy || 1e-6));
  return { x: cx + dx * scale, y: cy + dy * scale };
}

export function buildGraphSvg(input: ExportInput): { svg: string; width: number; height: number } {
  const { model, rules, projection } = input;
  const marks = new Map<string, number[]>();
  for (const reference of projection?.references ?? []) {
    const values = marks.get(reference.targetId) ?? [];
    values.push(reference.index);
    marks.set(reference.targetId, values);
  }
  const referenceMark = (id: string) => (marks.get(id) ?? []).map((n) => `[${n}]`).join(' ');
  const groups = projection ? [] : model.groups;
  const legendRules = projection
    ? rules.credibility.filter((rule) => rule.value !== 'ruled_out')
    : rules.credibility;
  const collapsed = new Set(groups.filter((group) => group.collapsed).map((group) => group.id));
  const nodes = model.nodes.filter((node) => projection
    ? projection.includedIds.has(node.id)
    : !node.group_id || !collapsed.has(node.group_id));
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const edges = model.edges.filter((edge) => nodeById.has(edge.source) && nodeById.has(edge.target)
    && (!projection || (projection.includedIds.has(edge.id) && edge.credibility !== 'ruled_out')));
  const renderedIds = new Set([...nodes.map(({ id }) => id), ...edges.map(({ id }) => id)]);
  const visibleReferences = projection?.references.filter((item) => renderedIds.has(item.targetId)) ?? [];
  const footnotes = visibleReferences.slice(0, 12);
  const footerHeight = projection ? 24 + 20 * footnotes.length : 0;
  const rects = [...nodes.map(nodeRect), ...groups.map(groupRect)];
  const minX = rects.length > 0 ? Math.min(...rects.map((rect) => rect.x)) : 0;
  const minY = rects.length > 0 ? Math.min(...rects.map((rect) => rect.y)) : 0;
  const maxX = rects.length > 0 ? Math.max(...rects.map((rect) => rect.x + rect.w)) : 760;
  const maxY = rects.length > 0 ? Math.max(...rects.map((rect) => rect.y + rect.h)) : 320;
  const contentWidth = Math.max(700, maxX - minX);
  const contentHeight = Math.max(300, maxY - minY);
  const width = Math.round(contentWidth + PAD * 2);
  const height = Math.round(HEADER + contentHeight + PAD * 2 + LEGEND + footerHeight);
  const offsetX = PAD - minX;
  const offsetY = HEADER + PAD - minY;
  const parts: string[] = [];

  parts.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" `
    + `viewBox="0 0 ${width} ${height}" font-family='${FONT}'>`);
  parts.push('<defs>');
  for (const rule of legendRules) {
    parts.push(`<marker id="arrow-${rule.value}" viewBox="0 0 10 10" refX="9" refY="5" `
      + `markerWidth="7" markerHeight="7" orient="auto-start-reverse">`
      + `<path d="M 0 0 L 10 5 L 0 10 z" fill="${rule.stroke}"/></marker>`);
  }
  parts.push('</defs>');
  parts.push(`<rect width="${width}" height="${height}" fill="#ffffff"/>`);
  parts.push(`<text x="${PAD}" y="38" font-size="20" font-weight="700" fill="#202c29">`
    + `${escapeXml(input.title)}</text>`);
  const subtitle = [
    input.caseTitle,
    projection?.profile === 'client' ? '客户沟通' : projection ? '当庭呈递' : null,
    projection?.version,
    input.generatedAt ? `导出 ${input.generatedAt}` : null,
  ].filter(Boolean).join(' · ');
  parts.push(`<text x="${PAD}" y="60" font-size="12" fill="#53635c">${escapeXml(subtitle)}</text>`);
  parts.push(`<line x1="${PAD}" y1="${HEADER - 8}" x2="${width - PAD}" y2="${HEADER - 8}" `
    + 'stroke="#d7ded7" stroke-width="1"/>');

  const g = (x: number, y: number, w: number, h: number) =>
    `x="${(x + offsetX).toFixed(1)}" y="${(y + offsetY).toFixed(1)}" `
    + `width="${w.toFixed(1)}" height="${h.toFixed(1)}"`;

  // 分组框（底层）
  for (const group of groups) {
    const rect = groupRect(group);
    const fill = group.color ?? '#eef3ef';
    parts.push(`<rect ${g(rect.x, rect.y, rect.w, rect.h)} rx="12" fill="${fill}" fill-opacity="0.55" `
      + 'stroke="#c6d0c8" stroke-dasharray="6 4"/>');
    const memberCount = model.nodes.filter((node) => node.group_id === group.id).length;
    parts.push(`<text x="${(rect.x + offsetX + 14).toFixed(1)}" y="${(rect.y + offsetY + 24).toFixed(1)}" `
      + `font-size="12" font-weight="700" fill="#44574d">${escapeXml(group.title)}`
      + `${group.collapsed ? `（已收起，${memberCount} 项）` : ''}</text>`);
  }

  // 关系边（按同端点错开，避免完全重叠）
  const pairCount = new Map<string, number>();
  for (const edge of edges) {
    const key = [edge.source, edge.target].sort().join('|');
    const index = pairCount.get(key) ?? 0;
    pairCount.set(key, index + 1);
    const rule = credibilityRule(rules, edge.credibility);
    const source = nodeById.get(edge.source)!;
    const target = nodeById.get(edge.target)!;
    const sourceCenter = { x: source.x + NODE_WIDTH / 2, y: source.y + NODE_HEIGHT / 2 };
    const targetCenter = { x: target.x + NODE_WIDTH / 2, y: target.y + NODE_HEIGHT / 2 };
    const start = borderPoint(source, targetCenter.x, targetCenter.y);
    const end = borderPoint(target, sourceCenter.x, sourceCenter.y);
    const sx = start.x + offsetX;
    const sy = start.y + offsetY;
    const tx = end.x + offsetX;
    const ty = end.y + offsetY;
    const dx = tx - sx;
    const dy = ty - sy;
    const length = Math.hypot(dx, dy) || 1;
    const nx = -dy / length;
    const ny = dx / length;
    const bend = (index % 3 - 1) * 18;
    const cx = (sx + tx) / 2 + nx * bend;
    const cy = (sy + ty) / 2 + ny * bend;
    const dash = rule.dash ? ` stroke-dasharray="${rule.dash}"` : '';
    const marker = edge.directed ? ` marker-end="url(#arrow-${edge.credibility})"` : '';
    parts.push(`<path d="M ${sx.toFixed(1)} ${sy.toFixed(1)} Q ${cx.toFixed(1)} ${cy.toFixed(1)} `
      + `${tx.toFixed(1)} ${ty.toFixed(1)}" fill="none" stroke="${rule.stroke}" `
      + `stroke-width="${rule.width}"${dash}${marker} stroke-linecap="round"/>`);
    const labelX = 0.25 * sx + 0.5 * cx + 0.25 * tx;
    const labelY = 0.25 * sy + 0.5 * cy + 0.25 * ty;
    const main = `${edge.credibility === 'ruled_out' ? '✕ ' : ''}`
      + `${relationKindLabel(rules, edge.kind)}${edge.label ? ` · ${edge.label}` : ''}`;
    const sources = projection
      ? referenceMark(edge.id)
      : (input.showWorkingLabels && edge.source_labels.join('、'))
        || (edge.source_labels.length ? '有出处' : '无出处');
    const mainText = truncate(main, 260, 11);
    const sourceText = truncate(sources, 300, 10);
    const boxWidth = Math.max(textWidth(mainText, 11), textWidth(sourceText, 10)) + 14;
    parts.push(`<g><rect x="${(labelX - boxWidth / 2).toFixed(1)}" y="${(labelY - 20).toFixed(1)}" `
      + `width="${boxWidth.toFixed(1)}" height="30" rx="5" fill="#ffffff" fill-opacity="0.94" `
      + 'stroke="#d7ded7" stroke-width="0.8"/>'
      + `<text x="${labelX.toFixed(1)}" y="${(labelY - 7).toFixed(1)}" text-anchor="middle" `
      + `font-size="11" fill="#202c29">${escapeXml(mainText)}</text>`
      + `<text x="${labelX.toFixed(1)}" y="${(labelY + 5).toFixed(1)}" text-anchor="middle" `
      + `font-size="10" fill="#53635c">${escapeXml(sourceText)}</text></g>`);
  }

  // 主体节点
  for (const node of nodes) {
    const rect = nodeRect(node);
    parts.push(`<rect ${g(rect.x, rect.y, rect.w, rect.h)} rx="10" fill="#ffffff" `
      + 'stroke="#a7b5aa" stroke-width="1"/>');
    const x = rect.x + offsetX;
    const y = rect.y + offsetY;
    const name = truncate(node.name, NODE_WIDTH - 24, 13);
    parts.push(`<text x="${(x + 12).toFixed(1)}" y="${(y + 22).toFixed(1)}" font-size="13" `
      + `font-weight="650" fill="#202c29">${escapeXml(name)}</text>`);
    const roles = node.roles.map((role) => roleLabel(rules, role)).join('、');
    const meta = truncate(`${entityKindLabel(rules, node.kind)}${roles ? ` · ${roles}` : ''}`,
      NODE_WIDTH - 24, 10.5);
    parts.push(`<text x="${(x + 12).toFixed(1)}" y="${(y + 39).toFixed(1)}" font-size="10.5" `
      + `fill="#53635c">${escapeXml(meta)}</text>`);
    if (!projection && node.aliases.length > 0) {
      const aliases = truncate(`别名：${node.aliases.join('、')}`, NODE_WIDTH - 24, 10);
      parts.push(`<text x="${(x + 12).toFixed(1)}" y="${(y + 54).toFixed(1)}" font-size="10" `
        + `fill="#6b6862">${escapeXml(aliases)}</text>`);
    }
    const sourceText = projection ? referenceMark(node.id)
      : node.source_count > 0 ? `出处 ${node.source_count}` : '无出处';
    parts.push(`<text x="${(x + 12).toFixed(1)}" y="${(y + 68).toFixed(1)}" font-size="10" `
      + `fill="${node.source_count > 0 ? '#2f5d50' : '#8b8f8b'}">${escapeXml(sourceText)}</text>`);
    if (!projection && node.pending) {
      parts.push(`<rect x="${(x + rect.w - 54).toFixed(1)}" y="${(y + 8).toFixed(1)}" width="44" `
        + 'height="16" rx="4" fill="#fff3cd" stroke="#e0c060"/>'
        + `<text x="${(x + rect.w - 32).toFixed(1)}" y="${(y + 19.5).toFixed(1)}" `
        + 'text-anchor="middle" font-size="9.5" fill="#7a5b00">待确认</text>');
    }
  }

  // 图例
  const legendTop = height - LEGEND - footerHeight + 6;
  parts.push(`<line x1="${PAD}" y1="${legendTop - 10}" x2="${width - PAD}" y2="${legendTop - 10}" `
    + 'stroke="#d7ded7" stroke-width="1"/>');
  parts.push(`<text x="${PAD}" y="${legendTop + 8}" font-size="12" font-weight="700" `
    + 'fill="#44574d">图例</text>');
  const columnWidth = (width - PAD * 2) / legendRules.length;
  legendRules.forEach((rule, index) => {
    const x = PAD + index * columnWidth;
    const y = legendTop + 32;
    const dash = rule.dash ? ` stroke-dasharray="${rule.dash}"` : '';
    parts.push(`<line x1="${x}" y1="${y}" x2="${x + 44}" y2="${y}" stroke="${rule.stroke}" `
      + `stroke-width="${rule.width}"${dash} stroke-linecap="round"/>`);
    parts.push(`<text x="${x + 52}" y="${y + 4}" font-size="11.5" fill="#202c29">`
      + `${escapeXml(rule.label)}</text>`);
    parts.push(`<text x="${x + 52}" y="${y + 20}" font-size="9.5" fill="#6b6862">`
      + `${escapeXml(rule.hint)}</text>`);
  });
  if (projection) {
    for (const [line, reference] of footnotes.entries()) {
      const text = truncate(`[${reference.index}] ${reference.label}`, width - 2 * PAD, 11);
      parts.push(`<text x="${PAD}" y="${legendTop + 71 + line * 20}" font-size="11" fill="#44574d">`
        + `${escapeXml(text)}</text>`);
    }
  }
  const note = projection
    ? [projection.notice, visibleReferences.length > footnotes.length ? '完整编号见引用索引附表' : null]
        .filter(Boolean).join(' · ')
    : input.note ?? '内部工作图';
  parts.push(`<text x="${PAD}" y="${height - 18}" font-size="10" fill="#6b6862">`
    + `${escapeXml(note)}</text>`);
  parts.push('</svg>');
  return { svg: parts.join(''), width, height };
}

export function exportFileName(graphTitle: string, extension: 'svg' | 'png',
                               date = new Date()): string {
  const stamp = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(date);
  const safe = graphTitle.replace(/[\\/:*?"<>|\s]+/g, '-').replace(/^-+|-+$/g, '') || '关系图';
  return `${safe}-${stamp}.${extension}`;
}

function triggerDownload(url: string, filename: string): void {
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

export function downloadSvgFile(filename: string, svg: string): void {
  const blob = new Blob([svg], { type: 'image/svg+xml;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  triggerDownload(url, filename);
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function downloadPngFile(filename: string, svg: string,
                                      width: number, height: number, scale = 2): Promise<void> {
  const image = new Image();
  const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  await new Promise<void>((resolve, reject) => {
    image.onload = () => resolve();
    image.onerror = () => reject(new Error('PNG 导出失败：SVG 无法渲染'));
    image.src = url;
  });
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  const context = canvas.getContext('2d');
  if (!context) throw new Error('PNG 导出失败：浏览器不支持 canvas');
  context.scale(scale, scale);
  context.fillStyle = '#ffffff';
  context.fillRect(0, 0, width, height);
  context.drawImage(image, 0, 0, width, height);
  const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/png'));
  if (!blob) throw new Error('PNG 导出失败：无法生成图片');
  const objectUrl = URL.createObjectURL(blob);
  triggerDownload(objectUrl, filename);
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}

/** 供测试断言：判断某条边在导出 SVG 中使用了哪种线型。 */
export function edgeStyleOf(rules: GraphRules, edge: CanvasEdge): { stroke: string; dash: string } {
  const rule = credibilityRule(rules, edge.credibility);
  return { stroke: rule.stroke, dash: rule.dash };
}
