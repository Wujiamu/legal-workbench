/**
 * 证据编号解析（D2）：编号唯一来源是已确认的证据目录。
 * 仅 removed_at IS NULL 且 review_state='confirmed' 的分组、条目与绑定参与计算；
 * 组号 = 有效分组按 (position, id) 的 1-based 序号；条号 = 组内有效条目按 (position, id) 的 1-based 序号；
 * 命中页码时追加「第N页」，命中多个条目按目录顺序全部列出（「、」连接）。
 * 待确认的外部导入条目/绑定绝不污染工作稿编号。
 */
import type {
  Annotation, EvidenceBinding, EvidenceGroup, EvidenceItem, GraphSource, Material,
  ResolvedLabel, Segment,
} from './types';

export interface EvidenceContext {
  groups: EvidenceGroup[];
  items: EvidenceItem[];
  bindings: EvidenceBinding[];
  materials: Material[];
  segments: Segment[];
  annotations: Annotation[];
}

export interface EvidenceNumbers {
  /** 证据条目 id → 「证据{组}-{序}」。 */
  numbers: Record<string, string>;
  /** 分组 id → 1-based 组号（仅有效分组）。 */
  groupNumbers: Record<string, number>;
  /** 分组 id → 组内有效条目（已排序）。 */
  itemsByGroup: Record<string, EvidenceItem[]>;
}

function byPosition<T extends { position: number; id: string }>(a: T, b: T): number {
  return a.position - b.position || a.id.localeCompare(b.id);
}

export function computeEvidenceNumbers(ctx: EvidenceContext): EvidenceNumbers {
  const groups = ctx.groups
    .filter((group) => group.removed_at === null)
    .sort(byPosition);
  const numbers: Record<string, string> = {};
  const groupNumbers: Record<string, number> = {};
  const itemsByGroup: Record<string, EvidenceItem[]> = {};
  groups.forEach((group, index) => {
    const groupNo = index + 1;
    groupNumbers[group.id] = groupNo;
    const items = ctx.items
      .filter((item) => item.group_id === group.id && item.removed_at === null
        && item.review_state === 'confirmed')
      .sort(byPosition);
    itemsByGroup[group.id] = items;
    items.forEach((item, itemIndex) => {
      numbers[item.id] = `证据${groupNo}-${itemIndex + 1}`;
    });
  });
  return { numbers, groupNumbers, itemsByGroup };
}

interface SourceLocation {
  material_id: string;
  page: number | null;
  excerpt: string | null;
}

/** 把出处引用解析到材料与页码；不存在的引用返回 null。 */
export function locateSource(source: Pick<GraphSource, 'segment_id' | 'material_id' | 'annotation_id'>,
                             ctx: EvidenceContext): SourceLocation | null {
  if (source.segment_id) {
    const segment = ctx.segments.find((entry) => entry.id === source.segment_id);
    if (!segment) return null;
    return { material_id: segment.material_id, page: segment.page, excerpt: segment.text };
  }
  if (source.annotation_id) {
    const annotation = ctx.annotations.find((entry) => entry.id === source.annotation_id);
    if (!annotation) return null;
    return { material_id: annotation.material_id, page: annotation.page, excerpt: annotation.text };
  }
  if (source.material_id) {
    return { material_id: source.material_id, page: null, excerpt: null };
  }
  return null;
}

function bindingCovers(binding: EvidenceBinding, page: number | null): boolean {
  if (page === null) return true;
  if (binding.page_start === null) return true;
  return page >= binding.page_start && page <= (binding.page_end ?? binding.page_start);
}

/**
 * 解析单条出处：返回命中的全部编号与展示文本。
 * 未命中已确认目录时显示材料名并标「未编号」。
 */
export function resolveSource(source: Pick<GraphSource, 'segment_id' | 'material_id' | 'annotation_id'>,
                              ctx: EvidenceContext,
                              numbers: EvidenceNumbers = computeEvidenceNumbers(ctx)): ResolvedLabel {
  const location = locateSource(source, ctx);
  if (!location) {
    return {
      labels: [], text: '（出处已失效）', material_id: '', material_title: '', page: null,
      text_excerpt: null,
    };
  }
  const material = ctx.materials.find((entry) => entry.id === location.material_id);
  if (!material) {
    return {
      labels: [], text: '（材料已不存在）', material_id: location.material_id, material_title: '',
      page: location.page, text_excerpt: location.excerpt,
    };
  }
  const confirmedBindings = ctx.bindings.filter((binding) => binding.removed_at === null
    && binding.review_state === 'confirmed' && binding.material_id === location.material_id
    && bindingCovers(binding, location.page));
  const itemIds = new Set(confirmedBindings.map((binding) => binding.item_id));
  const hits = ctx.items
    .filter((item) => itemIds.has(item.id) && item.removed_at === null
      && item.review_state === 'confirmed' && numbers.numbers[item.id])
    .sort((a, b) => {
      const groupA = numbers.groupNumbers[a.group_id] ?? Number.MAX_SAFE_INTEGER;
      const groupB = numbers.groupNumbers[b.group_id] ?? Number.MAX_SAFE_INTEGER;
      return groupA - groupB || a.position - b.position || a.id.localeCompare(b.id);
    });
  const suffix = location.page !== null ? ` 第${location.page}页` : '';
  const labels = hits.map((item) => `${numbers.numbers[item.id]}${suffix}`);
  return {
    labels,
    text: labels.length > 0 ? labels.join('、') : `${material.title}（未编号）`,
    material_id: material.id,
    material_title: material.title,
    page: location.page,
    text_excerpt: location.excerpt,
  };
}

/** 批量解析（关系图读快照时一次算完，避免每条边重复扫描目录）。 */
export function resolveAllSources(sources: GraphSource[], ctx: EvidenceContext):
    { labels: Record<string, ResolvedLabel>; numbers: EvidenceNumbers } {
  const numbers = computeEvidenceNumbers(ctx);
  const labels: Record<string, ResolvedLabel> = {};
  for (const source of sources) {
    labels[source.id] = resolveSource(source, ctx, numbers);
  }
  return { labels, numbers };
}
