/**
 * 节点交互共用件（用户反馈第三轮）：节点即按钮，
 * 右键菜单承载操作，悬浮加号附着在鼠标靠近的边上。
 */
import { useEffect, useRef } from 'react';
import { kindLabel } from './rules';
import type { ProgressRules } from './types';

export type Edge = 'top' | 'right' | 'bottom' | 'left';

/** 光标离哪条边最近，加号就附着在哪条边。 */
export function nearestEdge(clientX: number, clientY: number, rect: DOMRect): Edge {
  const distances: [Edge, number][] = [
    ['top', Math.abs(clientY - rect.top)],
    ['bottom', Math.abs(rect.bottom - clientY)],
    ['left', Math.abs(clientX - rect.left)],
    ['right', Math.abs(rect.right - clientX)],
  ];
  distances.sort((a, b) => a[1] - b[1]);
  return distances[0][0];
}

export interface MenuItem {
  label: string;
  onSelect(): void;
  danger?: boolean;
  disabled?: boolean;
  separator?: boolean;
}

export function ContextMenu({ x, y, items, onClose, label }: {
  x: number;
  y: number;
  items: MenuItem[];
  onClose(): void;
  label: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.querySelector<HTMLButtonElement>('[role=menuitem]:not(:disabled)')?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    const onDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onClose();
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onDown);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onDown);
    };
  }, [onClose]);
  const left = Math.max(8, Math.min(x, window.innerWidth - 200));
  const top = Math.max(8, Math.min(y, window.innerHeight - items.length * 34 - 16));
  return (
    <div role="menu" aria-label={label} ref={ref} className="ctx-menu"
      style={{ position: 'fixed', left, top, zIndex: 120 }}>
      {items.map((item, index) => item.separator ? (
        <hr key={`sep-${index}`} className="ctx-sep" />
      ) : (
        <button key={`${item.label}-${index}`} type="button" role="menuitem"
          disabled={item.disabled}
          className={item.danger ? 'ctx-item danger' : 'ctx-item'}
          onClick={() => { if (!item.disabled) { item.onSelect(); onClose(); } }}>
          {item.label}
        </button>
      ))}
    </div>
  );
}

/** 种类菜单项：完全由规则配置驱动。 */
export function kindMenuItems(rules: ProgressRules, parentKind: string,
                              onPick: (kind: string) => void): MenuItem[] {
  return (rules.containment[parentKind] ?? []).map((kind) => ({
    label: kindLabel(rules, kind),
    onSelect: () => onPick(kind),
  }));
}

/** 节点右键菜单：两种视图共用同一份操作口径。 */
export function nodeMenuItems(node: { id: string; kind: string; state: string; revision: number }, opts: {
  hasChildren: boolean;
  collapsed: boolean;
  onSetActive(): void;
  onEdit(): void;
  onMove(): void;
  onToggle(): void;
  onRemove(): void;
  /** 审查：网页关系/关联/恢复入口（可选，原型可不提供）。 */
  onAddRelation?(): void;
  onLinkMaterial?(): void;
  onRestore?(): void;
  removed?: boolean;
  readOnly?: boolean;
}): MenuItem[] {
  const items: MenuItem[] = [];
  if (opts.removed) {
    // 已删除节点：只提供恢复
    if (opts.onRestore) items.push({ label: '恢复节点', disabled: opts.readOnly, onSelect: opts.onRestore });
    return items;
  }
  if (node.kind === 'stage' && node.state !== 'in_progress') {
    items.push({ label: '设为进行中', onSelect: opts.onSetActive });
  }
  items.push({ label: '编辑', disabled: opts.readOnly, onSelect: opts.onEdit });
  items.push({ label: '移至…', disabled: opts.readOnly, onSelect: opts.onMove });
  if (opts.onAddRelation) items.push({ label: '建立关系…', disabled: opts.readOnly, onSelect: opts.onAddRelation });
  if (opts.onLinkMaterial) items.push({ label: '关联材料…', disabled: opts.readOnly, onSelect: opts.onLinkMaterial });
  if (opts.hasChildren) {
    items.push({ label: opts.collapsed ? '展开子节点' : '收起子节点', onSelect: opts.onToggle });
  }
  items.push({ label: '', onSelect: () => {}, separator: true });
  items.push({ label: '删除', danger: true, disabled: opts.readOnly, onSelect: opts.onRemove });
  return items;
}