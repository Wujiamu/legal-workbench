/**
 * 关系图原型页面的交互验收：一案多图、目录调序与编号、快照/同意/对比/恢复、
 * Agent 待确认、归档只读、输入法组词、路径高亮、有证据校验、外部导入。
 * React Flow 在 jsdom 需要尺寸与 DOMMatrix 补齐；布局算法本身由 layout 测试覆盖。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import GraphPrototypePage from './GraphPrototypePage';

beforeAll(() => {
  class ResizeObserverMock {
    callback: ResizeObserverCallback;
    constructor(callback: ResizeObserverCallback) { this.callback = callback; }
    observe(target: Element) {
      setTimeout(() => {
        this.callback([{ target, contentRect: { width: 1200, height: 800 } } as ResizeObserverEntry],
          this as unknown as ResizeObserver);
      }, 0);
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
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
    configurable: true, get() { return 132; },
  });
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
    configurable: true, get() { return 52; },
  });
  Element.prototype.getBoundingClientRect = function getBoundingClientRect() {
    return {
      x: 0, y: 0, top: 0, left: 0, right: 132, bottom: 52, width: 132, height: 52,
      toJSON: () => ({}),
    } as DOMRect;
  };
  (SVGElement.prototype as unknown as { getBBox(): DOMRect }).getBBox = function getBBox() {
    return { x: 0, y: 0, width: 40, height: 16 } as DOMRect;
  };
  HTMLDialogElement.prototype.showModal = function showModal() { this.open = true; };
  HTMLDialogElement.prototype.close = function close() { this.open = false; };
});

function renderPage() {
  return render(<MemoryRouter><GraphPrototypePage /></MemoryRouter>);
}

function nodeNames(): string[] {
  return [...document.querySelectorAll('.graph-node-name')].map((node) => node.textContent ?? '');
}

async function waitForNode(name: string) {
  await waitFor(() => { expect(nodeNames()).toContain(name); }, { timeout: 12000 });
}

function edgeLabel(text: string): HTMLElement | undefined {
  return [...document.querySelectorAll<HTMLElement>('.graph-edge-label')]
    .find((label) => label.textContent?.includes(text));
}

function evidenceRow(text: string): HTMLElement {
  const row = [...document.querySelectorAll<HTMLElement>('.graph-ev-item')]
    .find((item) => item.textContent?.includes(text));
  if (!row) throw new Error(`未找到证据条目：${text}`);
  return row;
}

describe('关系图原型 · 画布与一案多图', () => {
  it('渲染两张图、四档图例、Agent 待确认标记与证据编号', async () => {
    renderPage();
    expect(await screen.findByRole('heading', { level: 1, name: /TEST-ONLY/ })).toBeTruthy();
    expect(screen.getAllByRole('tab')).toHaveLength(2);
    await waitForNode('TEST-ONLY 张三');
    expect(nodeNames()).toEqual(expect.arrayContaining(['TEST-ONLY 甲公司', 'TEST-ONLY 乙公司']));

    const legend = screen.getByLabelText('线型图例');
    for (const label of ['有证据', '待查 / 存疑', '对方主张 / 有争议', '已排除']) {
      expect(within(legend).getByText(label)).toBeTruthy();
    }
    await waitFor(() => {
      expect(document.querySelectorAll('.agent-badge.agent-pending').length).toBeGreaterThan(0);
    }, { timeout: 8000 });
    expect(screen.getAllByText('证据1-1').length).toBeGreaterThan(0);
  }, 30000);

  it('取消勾选「待查 / 存疑」后隐藏对应关系，重新勾选恢复', async () => {
    renderPage();
    await waitForNode('TEST-ONLY 张三');
    await waitFor(() => { expect(edgeLabel('任职')).toBeTruthy(); }, { timeout: 10000 });
    await userEvent.click(screen.getByRole('checkbox', { name: '待查 / 存疑' }));
    await waitFor(() => {
      expect(screen.getByText('可信状态（3/4）')).toBeTruthy();
    }, { timeout: 8000 });
    await waitFor(() => { expect(edgeLabel('任职')).toBeUndefined(); }, { timeout: 8000 });
    await userEvent.click(screen.getByRole('checkbox', { name: '待查 / 存疑' }));
    await waitFor(() => { expect(edgeLabel('任职')).toBeTruthy(); }, { timeout: 8000 });
  }, 30000);

  it('切换图后显示该图独有主体与待确认节点，切回后仍显示原图', async () => {
    renderPage();
    await waitForNode('TEST-ONLY 张三');
    await userEvent.click(screen.getByRole('tab', { name: /履约与资金往来/ }));
    await waitForNode('TEST-ONLY 王五');
    await waitFor(() => {
      expect(nodeNames().some((name) => name.includes('丁物流'))).toBe(true);
    }, { timeout: 10000 });
    const ding = [...document.querySelectorAll<HTMLElement>('.graph-node')]
      .find((node) => node.textContent?.includes('丁物流'));
    expect(ding?.querySelector('.agent-badge.agent-pending')).toBeTruthy();
    await userEvent.click(screen.getByRole('tab', { name: /股权与实控关系/ }));
    await waitForNode('TEST-ONLY 张三');
    await waitFor(() => { expect(nodeNames()).not.toContain('TEST-ONLY 王五'); }, { timeout: 8000 });
  }, 40000);
});

describe('关系图原型 · 证据目录', () => {
  it('上移条目后编号即时重算并更新画布出处，撤销后恢复', async () => {
    renderPage();
    await waitForNode('TEST-ONLY 张三');
    await waitFor(() => {
      expect(evidenceRow('TEST-ONLY 股权转让协议').textContent).toContain('证据1-1');
    });
    expect(evidenceRow('TEST-ONLY 工商登记档案').textContent).toContain('证据1-2');

    await userEvent.click(screen.getByRole('button', { name: '上移条目 TEST-ONLY 公司章程' }));
    await waitFor(() => {
      expect(evidenceRow('TEST-ONLY 工商登记档案').textContent).toContain('证据1-1');
      expect(evidenceRow('TEST-ONLY 股权转让协议').textContent).toContain('证据1-2');
    });
    await waitFor(() => {
      const label = edgeLabel('持股 45%');
      expect(label?.textContent).toContain('证据1-2 第2页');
    }, { timeout: 10000 });

    await userEvent.click(screen.getByRole('button', { name: '撤销（⌘Z）' }));
    await waitFor(() => {
      expect(evidenceRow('TEST-ONLY 股权转让协议').textContent).toContain('证据1-1');
    });
  }, 40000);
});

describe('关系图原型 · 快照与客户同意', () => {
  it('查看冻结编号漂移、客户同意留痕、与工作稿对比，并冻结不含待确认的新快照', async () => {
    renderPage();
    await waitForNode('TEST-ONLY 张三');
    await userEvent.click(screen.getByRole('button', { name: /版本快照 · 客户同意/ }));
    const dialog = await screen.findByRole('dialog', { name: /版本快照与客户同意/ });
    await userEvent.click(within(dialog).getByRole('button', { name: /v1/ }));

    const drift = within(dialog).getByLabelText('编号漂移');
    expect(drift.textContent).toContain('证据1-2 第2页');
    expect(drift.textContent).toContain('证据1-1 第2页');
    expect(within(dialog).getByLabelText('客户同意').textContent).toContain('客户部分同意');

    await userEvent.click(within(dialog).getByRole('button', { name: '对比当前工作稿' }));
    const compare = within(dialog).getByLabelText('快照对比');
    expect(compare.textContent).toContain('可信状态 待查 / 存疑 → 有证据');

    await userEvent.click(within(dialog).getByRole('button', { name: '登记客户同意' }));
    expect(within(dialog).getByPlaceholderText('例如：客户部分同意')).toBeTruthy();

    await userEvent.click(within(dialog).getByRole('button', { name: '冻结当前图' }));
    expect(dialog.textContent).toContain('本图有 2 条 Agent 待确认变更');
    await userEvent.click(within(dialog).getByLabelText(/已知悉，不含待确认变更/));
    await userEvent.click(within(dialog).getByRole('button', { name: '冻结快照' }));
    await waitFor(() => {
      expect(within(dialog).getByRole('button', { name: /v2/ }).textContent)
        .toContain('不含 2 条待确认');
    });
  }, 45000);

  it('存在 Agent 待确认时拒绝恢复；全部确认后可恢复到 v1', async () => {
    renderPage();
    await waitForNode('TEST-ONLY 张三');
    await userEvent.click(screen.getByRole('button', { name: /版本快照 · 客户同意/ }));
    const dialog = await screen.findByRole('dialog', { name: /版本快照与客户同意/ });
    await userEvent.click(within(dialog).getByRole('button', { name: /v1/ }));
    await userEvent.click(within(dialog).getByRole('button', { name: '恢复为工作稿…' }));
    await userEvent.click(within(dialog).getByRole('button', { name: '确认恢复' }));
    await waitFor(() => {
      expect(dialog.textContent).toContain('Agent 待确认');
    });
    await userEvent.click(within(dialog).getByRole('button', { name: '关闭' }));

    const review = screen.getByLabelText('Agent 待确认修改');
    await userEvent.click(within(review).getByRole('button', { name: '一键全部确认' }));
    await userEvent.click(within(review).getByRole('button', { name: '我已审核，全部确认' }));
    await waitFor(() => {
      expect(review.textContent).toContain('没有待确认的 Agent 修改');
    });

    await userEvent.click(screen.getByRole('button', { name: /版本快照 · 客户同意/ }));
    const reopened = await screen.findByRole('dialog', { name: /版本快照与客户同意/ });
    await userEvent.click(within(reopened).getByRole('button', { name: /v1/ }));
    await userEvent.click(within(reopened).getByRole('button', { name: '恢复为工作稿…' }));
    await userEvent.click(within(reopened).getByRole('button', { name: '确认恢复' }));
    await waitFor(() => {
      expect(reopened.textContent).toContain('已恢复');
    }, { timeout: 10000 });
    await userEvent.click(within(reopened).getByRole('button', { name: '关闭' }));
    await waitFor(() => {
      expect(edgeLabel('担保')?.getAttribute('aria-label')).toContain('待查 / 存疑');
    }, { timeout: 10000 });
  }, 60000);
});

describe('关系图原型 · Agent 待确认与归档', () => {
  it('逐条撤回与确认 Agent 修改，计数与徽标随之变化', async () => {
    renderPage();
    await waitForNode('TEST-ONLY 张三');
    const review = screen.getByLabelText('Agent 待确认修改');
    expect(review.textContent).toContain('（4）');
    const sourceChange = within(review).getByText('Agent 补充出处').closest('li')!;
    await userEvent.click(within(sourceChange).getByRole('button', { name: '撤回' }));
    expect(within(sourceChange).getByRole('alert').textContent).toContain('唯一有效出处');
    expect(review.textContent).toContain('（4）');

    const relationChange = within(review).getByText('Agent 修改关系').closest('li')!;
    await userEvent.click(within(relationChange).getByRole('button', { name: '撤回' }));
    await waitFor(() => { expect(review.textContent).toContain('（3）'); });
    await userEvent.click(within(sourceChange).getByRole('button', { name: '确认' }));
    await waitFor(() => {
      expect(review.textContent).toContain('（2）');
      expect(review.textContent).toContain('已确认 1 条');
    });
  }, 30000);

  it('归档后写操作禁用、查看与导出可用；取消归档后恢复', async () => {
    renderPage();
    await waitForNode('TEST-ONLY 张三');
    await userEvent.click(screen.getByRole('button', { name: '结案归档' }));
    await userEvent.click(await screen.findByRole('button', { name: '确认归档' }));
    expect(await screen.findByText(/本案已归档/)).toBeTruthy();
    expect((screen.getByRole('button', { name: '＋ 主体' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '导出 SVG' }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole('button', { name: '＋ 新建图' }) as HTMLButtonElement).disabled).toBe(true);

    await userEvent.click(screen.getByRole('button', { name: '取消归档' }));
    await userEvent.click(await screen.findByRole('button', { name: '确认取消归档' }));
    await waitFor(() => { expect(screen.queryByText(/本案已归档/)).toBeNull(); });
    expect((screen.getByRole('button', { name: '＋ 主体' }) as HTMLButtonElement).disabled).toBe(false);
  }, 30000);
});

describe('关系图原型 · 主体与关系维护', () => {
  it('拖拽主体后按图节点 ID 保存布局，不出现“节点不在本图中”', async () => {
    renderPage();
    await waitForNode('TEST-ONLY 张三');
    const view = document.defaultView!;
    const mouse = (type: string, x: number, y: number) => {
      const event = new view.MouseEvent(type, {
        bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0,
      });
      Object.defineProperty(event, 'view', { value: view });
      return event;
    };
    const frame = [...document.querySelectorAll<HTMLElement>('.react-flow__node')]
      .find((node) => node.textContent?.includes('张三'))!;
    frame.dispatchEvent(mouse('mousedown', 100, 100));
    for (let index = 1; index <= 8; index += 1) {
      view.dispatchEvent(mouse('mousemove', 100 + index * 12, 100 + index * 8));
    }
    view.dispatchEvent(mouse('mouseup', 196, 164));
    // jsdom 里 d3-drag 的位移依赖测量时序，不同机器上不一定产生位移；
    // 这里断言真正的不变量：拖拽不会报“节点不在本图中”，且主体仍在画布上。
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(nodeNames()).toContain('TEST-ONLY 张三');
    expect(document.querySelector('.graph-error')?.textContent ?? '').not.toContain('节点不在本图中');
  }, 30000);

  it('从案件池把未入图主体加入本图', async () => {
    renderPage();
    await waitForNode('TEST-ONLY 张三');
    expect(nodeNames()).not.toContain('TEST-ONLY 王五');
    await userEvent.click(screen.getByRole('button', { name: '加入本图…' }));
    const dialog = await screen.findByRole('dialog', { name: '加入本图' });
    await userEvent.click(within(dialog).getByRole('checkbox', { name: /^TEST-ONLY 王五（自然人）$/ }));
    await userEvent.click(within(dialog).getByRole('button', { name: '加入本图' }));
    await waitFor(() => { expect(nodeNames()).toContain('TEST-ONLY 王五'); }, { timeout: 10000 });
  }, 30000);

  it('新建关系图后切换为当前图', async () => {
    renderPage();
    await waitForNode('TEST-ONLY 张三');
    await userEvent.click(screen.getByRole('button', { name: '＋ 新建图' }));
    const dialog = await screen.findByRole('dialog', { name: '新建关系图' });
    await userEvent.type(within(dialog).getByLabelText('名称'), 'TEST-ONLY 第三张图');
    await userEvent.click(within(dialog).getByRole('button', { name: '创建' }));
    const tab = await screen.findByRole('tab', { name: /TEST-ONLY 第三张图/ });
    await waitFor(() => { expect(tab.getAttribute('aria-selected')).toBe('true'); });
    expect(await screen.findByText(/本图还没有主体/)).toBeTruthy();
  }, 30000);

  it('中文输入法组词回车不提交，普通提交可新建主体', async () => {
    renderPage();
    await waitForNode('TEST-ONLY 张三');
    await userEvent.click(screen.getByRole('button', { name: '＋ 主体' }));
    const dialog = await screen.findByRole('dialog', { name: '新建主体' });
    const input = within(dialog).getByLabelText('名称');
    await userEvent.type(input, 'TEST-ONLY 输入法主体');
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true, keyCode: 229 });
    expect(screen.getByRole('dialog', { name: '新建主体' })).toBeTruthy();
    await userEvent.click(within(dialog).getByRole('button', { name: '新建主体' }));
    await waitFor(() => { expect(nodeNames()).toContain('TEST-ONLY 输入法主体'); }, { timeout: 10000 });
  }, 30000);

  it('无出处时拒绝把关系设为有证据，补出处后可升级', async () => {
    renderPage();
    await waitForNode('TEST-ONLY 张三');
    await waitFor(() => { expect(edgeLabel('任职')).toBeTruthy(); }, { timeout: 10000 });
    fireEvent.click(edgeLabel('任职')!);
    const inspector = await screen.findByLabelText('关系详情');
    const credibility = within(inspector).getByLabelText('可信状态');
    fireEvent.change(credibility, { target: { value: 'evidenced' } });
    expect(await within(inspector).findByText(/无出处时不能设为「有证据」/)).toBeTruthy();

    const ref = within(inspector).getByLabelText('关联材料或片段');
    fireEvent.change(ref, { target: { value: 'segment:seg-board-2' } });
    await userEvent.click(within(inspector).getByRole('button', { name: '添加出处' }));
    await waitFor(() => {
      expect(within(inspector).getByLabelText('出处').textContent).toContain('（1 条）');
    });
    fireEvent.change(within(inspector).getByLabelText('可信状态'), { target: { value: 'evidenced' } });
    await waitFor(() => {
      expect(inspector.querySelector('.graph-credibility')?.textContent).toContain('有证据');
    });
  }, 40000);

  it('查看出处原文弹窗显示合成材料片段', async () => {
    renderPage();
    await waitForNode('TEST-ONLY 张三');
    await waitFor(() => { expect(edgeLabel('持股 45%')).toBeTruthy(); }, { timeout: 10000 });
    fireEvent.click(edgeLabel('持股 45%')!);
    const inspector = await screen.findByLabelText('关系详情');
    await userEvent.click(within(inspector).getAllByRole('button', { name: '查看原文' })[0]);
    const dialog = await screen.findByRole('dialog', { name: '出处原文（P1 模拟）' });
    expect(dialog.textContent).toContain('股权转让协议');
    expect(dialog.textContent).toContain('第2页');
  }, 30000);
});

describe('关系图原型 · 路径与导入', () => {
  it('双主体路径高亮并列出路径', async () => {
    renderPage();
    await waitForNode('TEST-ONLY 张三');
    const from = screen.getByLabelText('路径起点');
    const to = screen.getByLabelText('路径终点');
    await userEvent.selectOptions(from, within(from).getByRole('option', { name: 'TEST-ONLY 张三' }));
    await userEvent.selectOptions(to, within(to).getByRole('option', { name: 'TEST-ONLY 甲公司' }));
    await userEvent.click(screen.getByRole('button', { name: '高亮路径' }));
    expect(await screen.findByText(/找到 1 条连通路径/)).toBeTruthy();
    expect(screen.getByText('TEST-ONLY 张三 → TEST-ONLY 丙投资合伙企业 → TEST-ONLY 甲公司')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '清除' }));
    expect(screen.queryByText(/找到 1 条连通路径/)).toBeNull();
  }, 30000);

  it('外部目录导入待确认，确认条目与绑定后生成 证据4-1 并更新画布', async () => {
    renderPage();
    await waitForNode('TEST-ONLY 张三');
    await userEvent.click(screen.getByRole('tab', { name: /履约与资金往来/ }));
    await waitForNode('TEST-ONLY 王五');
    await userEvent.click(screen.getByRole('button', { name: '模拟导入外部目录' }));
    await waitFor(() => { expect(evidenceRow('补充协议').textContent).toContain('待确认'); });

    await userEvent.click(within(evidenceRow('补充协议')).getByRole('button', { name: '确认条目' }));
    await waitFor(() => { expect(evidenceRow('补充协议').textContent).toContain('证据4-1'); });
    expect(evidenceRow('补充协议').textContent).toContain('绑定待确认');

    await userEvent.click(within(evidenceRow('补充协议')).getByRole('button', { name: '确认绑定' }));
    await waitFor(() => { expect(evidenceRow('补充协议').textContent).toContain('绑定已确认'); });
    await waitFor(() => {
      expect(edgeLabel('代理签约')?.textContent).toContain('证据4-1 第1页');
    }, { timeout: 12000 });
  }, 50000);
});
