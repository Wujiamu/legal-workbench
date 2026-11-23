/**
 * 图形视图渲染：React Flow 在 jsdom 需要 ResizeObserver / DOMMatrix /
 * offsetWidth / getBBox 补齐。布局正确性由 layout.test.ts 用真实 elkjs 断言；
 * 这里断言视图把同一快照画出来、关系边带标签、点击触发选中。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { buildLitigationFixture } from './fixtures';
import { GraphView } from './GraphView';
import { defaultRules } from './rules';

beforeAll(() => {
  class ResizeObserverMock {
    callback: ResizeObserverCallback;
    constructor(callback: ResizeObserverCallback) { this.callback = callback; }
    observe(target: Element) {
      // 延迟到挂载完成后回调，保证 handle 子元素已在 DOM 中。
      setTimeout(() => {
        this.callback([{ target, contentRect: { width: 1200, height: 800 } } as ResizeObserverEntry], this as unknown as ResizeObserver);
      }, 0);
    }
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal('ResizeObserver', ResizeObserverMock);
  // React Flow 用 DOMMatrixReadOnly 解析视口 transform；jsdom 未实现。
  class DOMMatrixMock {
    a = 1; b = 0; c = 0; d = 1; e = 0; f = 0;
    m11 = 1; m12 = 0; m13 = 0; m14 = 0;
    m21 = 0; m22 = 1; m23 = 0; m24 = 0;
    m31 = 0; m32 = 0; m33 = 1; m34 = 0;
    m41 = 0; m42 = 0; m43 = 0; m44 = 1;
    is2D = true; isIdentity = true;
    constructor(init?: string) {
      if (typeof init !== 'string') return;
      const translate = /translate\(([-\d.]+)px,\s*([-\d.]+)px\)/.exec(init);
      if (translate) {
        this.e = Number(translate[1]); this.f = Number(translate[2]);
        this.m41 = this.e; this.m42 = this.f; this.isIdentity = false;
      }
    }
    multiply() { return this; }
    translate() { return this; }
    scale() { return this; }
    inverse() { return this; }
    transformPoint(point: { x: number; y: number }) {
      return { x: point.x + this.e, y: point.y + this.f, z: 0, w: 1 };
    }
  }
  vi.stubGlobal('DOMMatrixReadOnly', DOMMatrixMock);
  vi.stubGlobal('DOMMatrix', DOMMatrixMock);
  // jsdom 布局尺寸全 0，React Flow 无法完成节点初始化；给非零尺寸。
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get() { return 132; } });
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get() { return 52; } });
  Element.prototype.getBoundingClientRect = function getBoundingClientRect() {
    return { x: 0, y: 0, top: 0, left: 0, right: 132, bottom: 52, width: 132, height: 52, toJSON: () => ({}) } as DOMRect;
  };
  // 边标签用 SVG getBBox 测量文字；jsdom 未实现。
  (SVGElement.prototype as unknown as { getBBox(): DOMRect }).getBBox = function getBBox() {
    return { x: 0, y: 0, width: 40, height: 16 } as DOMRect;
  };
});

describe('GraphView', () => {
  it('渲染快照中的节点与关系标签，点击节点触发选中', async () => {
    const executor = buildLitigationFixture();
    executor.human({ type: 'add_node', parent_id: null, kind: 'program', title: '二审' });
    const nodes = executor.getSnapshot().nodes;
    executor.human({
      type: 'add_relation',
      from_node_id: nodes.find((node) => node.title === '二审')!.id,
      to_node_id: nodes.find((node) => node.title === '一审')!.id,
      relation_type: 'causes',
    });
    const onSelect = vi.fn();
    render(<GraphView snapshot={executor.getSnapshot()} rules={defaultRules}
      selectedId={null} onSelect={onSelect} />);
    await waitFor(() => {
      expect(screen.getByText('庭前程序')).toBeTruthy();
    }, { timeout: 8000 });
    expect(screen.getByText('证据交换（第一次）')).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByText('引起')).toBeTruthy();
    }, { timeout: 8000 });
    fireEvent.click(screen.getByText('裁判'));
    expect(onSelect).toHaveBeenCalledWith(expect.any(String));
  }, 20000);
});
describe('GraphView · 收起子树', () => {
  it('点击分组收起按钮后子节点不再渲染', async () => {
    const executor = buildLitigationFixture();
    const snapshot = executor.getSnapshot();
    const program = snapshot.nodes.find((node) => node.title === '一审')!;
    const onToggleCollapse = vi.fn();
    const view = render(<GraphView snapshot={snapshot} rules={defaultRules}
      selectedId={null} onSelect={() => {}}
      collapsedIds={new Set()} onToggleCollapse={onToggleCollapse} />);
    await waitFor(() => { expect(screen.getByText('立案')).toBeTruthy(); }, { timeout: 8000 });
    fireEvent.click(screen.getByRole('button', { name: '收起 一审' }));
    expect(onToggleCollapse).toHaveBeenCalledWith(program.id);
    view.rerender(<GraphView snapshot={snapshot} rules={defaultRules}
      selectedId={null} onSelect={() => {}}
      collapsedIds={new Set([program.id])} onToggleCollapse={onToggleCollapse} />);
    await waitFor(() => { expect(screen.queryByText('立案')).toBeNull(); }, { timeout: 8000 });
    expect(screen.getByText('4 项')).toBeTruthy();
  }, 25000);
});

describe('GraphView · 节点交互', () => {
  it('右键菜单设为进行中；双击编辑', async () => {
    const executor = buildLitigationFixture();
    const onCommand = vi.fn((command: import('./types').ProgressCommand) => executor.human(command));
    const onEditRequest = vi.fn();
    render(<GraphView snapshot={executor.getSnapshot()} rules={defaultRules}
      selectedId={null} onSelect={() => {}}
      onCommand={onCommand} onEditRequest={onEditRequest}
      onMoveRequest={() => {}} onAddRequest={() => {}} />);
    await waitFor(() => { expect(screen.getByText('裁判')).toBeTruthy(); }, { timeout: 8000 });
    const nodeEl = [...document.querySelectorAll('.react-flow__node')].find((n) => n.textContent!.includes('裁判'))!;
    fireEvent.contextMenu(nodeEl, { clientX: 60, clientY: 60 });
    fireEvent.click(screen.getByRole('menuitem', { name: '设为进行中' }));
    expect(onCommand).toHaveBeenCalledWith(expect.objectContaining({ type: 'set_active_stage' }));
    fireEvent.doubleClick(nodeEl);
    expect(onEditRequest).toHaveBeenCalledWith(expect.objectContaining({ title: '裁判' }));
  }, 25000);
});
