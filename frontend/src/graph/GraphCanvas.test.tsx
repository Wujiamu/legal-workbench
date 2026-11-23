/**
 * 关系图画布回归测试：
 * 1. 主体卡片正确显示 `出处 N`（不再读取缺失字段渲染成 `出处 undefined`）。
 * 2. 同对主体双向/平行多条边分配对称法向偏移与二次贝塞尔标签中点，避免标签完全重合；重合端点不产生 NaN。
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { GraphCanvas, assignPairCurveOffsets, curvedPairPath } from './GraphCanvas';
import { defaultRules } from './rules';
import type { CanvasModel } from './types';

beforeAll(() => {
  class ResizeObserverMock {
    callback: ResizeObserverCallback;
    constructor(callback: ResizeObserverCallback) { this.callback = callback; }
    observe(target: Element) {
      this.callback([{ target, contentRect: { width: 1200, height: 800 } } as ResizeObserverEntry],
        this as unknown as ResizeObserver);
    }
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal('ResizeObserver', ResizeObserverMock);
  class DOMMatrixMock {
    a = 1; b = 0; c = 0; d = 1; e = 0; f = 0;
    m11 = 1; m12 = 0; m13 = 0; m14 = 0;
    m21 = 0; m22 = 1; m23 = 0; m24 = 0;
    m31 = 0; m32 = 0; m33 = 1; m34 = 0;
    m41 = 0; m42 = 0; m43 = 0; m44 = 1;
    is2D = true; isIdentity = true;
    multiply() { return this; }
    translate() { return this; }
    scale() { return this; }
    inverse() { return this; }
    transformPoint(point: { x: number; y: number }) { return point; }
  }
  vi.stubGlobal('DOMMatrix', DOMMatrixMock);
  vi.stubGlobal('DOMMatrixReadOnly', DOMMatrixMock);
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
    configurable: true, get() { return 1200; },
  });
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
    configurable: true, get() { return 800; },
  });
  Element.prototype.getBoundingClientRect = function getBoundingClientRect() {
    return {
      x: 0, y: 0, top: 0, left: 0, right: 1200, bottom: 800, width: 1200, height: 800,
      toJSON: () => ({}),
    } as DOMRect;
  };
  (SVGElement.prototype as unknown as { getBBox(): DOMRect }).getBBox = function getBBox() {
    return { x: 0, y: 0, width: 40, height: 16 } as DOMRect;
  };
});

afterEach(() => cleanup());

describe('GraphCanvas', () => {
  it('主体卡片正确渲染 source_count，不出现 undefined', async () => {
    const model: CanvasModel = {
      groups: [],
      nodes: [
        {
          id: 'entity-1',
          entity_id: 'entity-1',
          node_id: 'node-1',
          name: 'TEST-ONLY 华辰智造',
          kind: 'org',
          roles: ['plaintiff'],
          aliases: ['华辰智造'],
          group_id: null,
          x: 80,
          y: 120,
          source_count: 2,
          source_labels: ['证据1 第1页'],
          pending: false,
          accepted: false,
        },
        {
          id: 'entity-2',
          entity_id: 'entity-2',
          node_id: 'node-2',
          name: 'TEST-ONLY 启航建工',
          kind: 'org',
          roles: ['defendant'],
          aliases: [],
          group_id: null,
          x: 420,
          y: 120,
          source_count: 0,
          source_labels: [],
          pending: false,
          accepted: false,
        },
      ],
      edges: [],
    };

    const { container } = render(
      <GraphCanvas
        model={model}
        rules={defaultRules}
        graphId="graph-1"
        selected={null}
        onSelect={vi.fn()}
      />,
    );

    const card1 = await screen.findByRole('button', { name: /TEST-ONLY 华辰智造/ });
    expect(card1.textContent).toContain('出处 2 · 证据1 第1页');
    expect(container.textContent).not.toContain('undefined');
    const card2 = screen.getByRole('button', { name: /TEST-ONLY 启航建工/ });
    expect(card2.textContent).toContain('无出处');
  });

  it('同对主体双向边与平行边生成对称错开曲线和独立标签坐标，零长度端点安全回退', () => {
    const single = [{ source: 'a', target: 'b' }];
    expect(assignPairCurveOffsets(single).get(single[0])).toBe(0);

    const bidirectional = [
      { source: 'a', target: 'b' },
      { source: 'b', target: 'a' },
    ];
    const offsets = assignPairCurveOffsets(bidirectional);
    const offAB = offsets.get(bidirectional[0])!;
    const offBA = offsets.get(bidirectional[1])!;
    expect(offAB).toBe(-80);
    expect(offBA).toBe(-80); // B->A 法向相反，同号即代表落到空间异侧

    const [pathAB, xAB, yAB] = curvedPairPath(0, 0, 200, 0, offAB);
    const [pathBA, xBA, yBA] = curvedPairPath(200, 0, 0, 0, offBA);
    expect(pathAB).not.toBe(pathBA);
    expect(xAB).toBe(100);
    expect(xBA).toBe(100);
    expect(Math.abs(yAB - yBA)).toBe(80);

    // 同向平行两条边同样分居两侧
    const parallelSameDir = [
      { id: '1', source: 'a', target: 'b' },
      { id: '2', source: 'a', target: 'b' },
    ];
    const pOffsets = assignPairCurveOffsets(parallelSameDir);
    const [, px1, py1] = curvedPairPath(0, 0, 120, 80, pOffsets.get(parallelSameDir[0])!);
    const [, px2, py2] = curvedPairPath(0, 0, 120, 80, pOffsets.get(parallelSameDir[1])!);
    expect(Math.hypot(px1 - px2, py1 - py2)).toBeCloseTo(80, 4);

    // 重合端点不产生 NaN
    const [zeroPath, zx, zy] = curvedPairPath(100, 100, 100, 100, -80);
    expect(zeroPath).not.toContain('NaN');
    expect(Number.isFinite(zx)).toBe(true);
    expect(Number.isFinite(zy)).toBe(true);
  });
});
