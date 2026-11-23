/**
 * 真实案件「关系图」页签：归档只读、出处直达 SourcePreview、刷新失败保留提示。
 * 数据边界用 endpoints mock（组件测试不连数据库、不连 8765）。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    graphApi: {
      ...actual.graphApi, workspace: vi.fn(), rules: vi.fn(), removalImpact: vi.fn(),
    },
    progressApi: { ...actual.progressApi, snapshot: vi.fn(), seqs: vi.fn() },
    annotationsApi: { ...actual.annotationsApi, list: vi.fn() },
    casesApi: {
      ...actual.casesApi, overview: vi.fn(), archive: vi.fn(), unarchive: vi.fn(),
    },
  };
});

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useParams: () => ({ caseId: 'case-graph-1' }) };
});

import { annotationsApi, casesApi, graphApi, progressApi } from '../api/endpoints';
import CaseGraphPage from './CaseGraphPage';

const workspace = vi.mocked(graphApi.workspace);
const rules = vi.mocked(graphApi.rules);
const snapshotNodes = vi.mocked(progressApi.snapshot);
const seqs = vi.mocked(progressApi.seqs);
const annotations = vi.mocked(annotationsApi.list);
const overview = vi.mocked(casesApi.overview);

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
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.open = false;
  };
});

const CASE = 'case-graph-1';

function apiWorkspace(overrides: Record<string, unknown> = {}) {
  return {
    case_id: CASE, case_title: '甲公司诉乙公司合同纠纷', archived: false, seq: 5,
    seqs: { graph: 5, evidence: 2 },
    entities: [
      {
        id: 'ent-1', case_id: CASE, kind: 'person', name: '张三', roles: ['controller'],
        note: null, created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0, created_at: '2026-11-20T00:00:00Z',
        updated_at: '2026-11-20T00:00:00Z',
      },
      {
        id: 'ent-2', case_id: CASE, kind: 'org', name: '丙投资', roles: ['shareholder'],
        note: null, created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0, created_at: '2026-11-20T00:00:00Z',
        updated_at: '2026-11-20T00:00:00Z',
      },
    ],
    aliases: [],
    relations: [{
      id: 'rel-1', case_id: CASE, from_entity_id: 'ent-1', to_entity_id: 'ent-2',
      directed: true, kind: 'equity_control', label: '持股 45%', credibility: 'evidenced',
      attrs: { share_pct: 45 }, note: null, created_by: 'human', updated_by: 'human',
      request_key: null, removed_at: null, revision: 1,
      created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
    }],
    sources: [{
      id: 'src-1', case_id: CASE, entity_id: null, relation_id: 'rel-1',
      segment_id: 'seg-1', material_id: null, annotation_id: null, proves: '证明持股 45%',
      created_by: 'human', removed_at: null, created_at: '2026-11-20T00:00:00Z',
    }],
    graphs: [{
      id: 'graph-1', case_id: CASE, title: '股权结构', description: null, position: 0,
      created_by: 'human', updated_by: 'human', request_key: null, removed_at: null,
      revision: 0, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
    }],
    nodes: [
      { id: 'n-1', case_id: CASE, graph_id: 'graph-1', entity_id: 'ent-1', group_id: null,
        x: 40, y: 40, revision: 0, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z' },
      { id: 'n-2', case_id: CASE, graph_id: 'graph-1', entity_id: 'ent-2', group_id: null,
        x: 320, y: 40, revision: 0, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z' },
    ],
    edges: [{ id: 'e-1', case_id: CASE, graph_id: 'graph-1', relation_id: 'rel-1',
      created_at: '2026-11-20T00:00:00Z' }],
    groups: [], snapshots: [], consents: [],
    evidence: {
      case_id: CASE, seq: 2, groups: [], items: [], bindings: [],
      materials: [{ id: 'mat-1', display_name: '股权转让协议.pdf' }],
      numbers: {}, group_numbers: {},
    },
    labels: {
      'src-1': {
        labels: ['证据1-1'], text: '证据1-1 第2页', material_id: 'mat-1',
        material_title: '股权转让协议.pdf', page: 2, text_excerpt: '张三持股45%',
        source_ref: 'seg-1', file_version_id: 'ver-9',
      },
    },
    pending_changes: [], changes: [], last_change_id: null, notice: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  workspace.mockReset();
  rules.mockResolvedValue({ rules: {} });
  annotations.mockResolvedValue({ items: [], offset: 0, limit: 200, total: 0 });
  snapshotNodes.mockResolvedValue({
    case_id: CASE, seq: 1, nodes: [], relations: [], links: [],
    pending_changes: [], changes: [], last_change_id: null,
  } as never);
  seqs.mockResolvedValue({ seqs: { graph: 5, evidence: 2 } });
  overview.mockResolvedValue({
    case_id: CASE, title: '甲公司诉乙公司合同纠纷', revision: 1, archived_at: null,
  } as never);
});

function renderPage() {
  return render(<MemoryRouter><CaseGraphPage /></MemoryRouter>);
}

function nodeNames(): string[] {
  return [...document.querySelectorAll('.graph-node-name')].map((node) => node.textContent ?? '');
}

describe('真实案件关系图页', () => {
  it('归档案件只显示只读提示，写入口全部禁用', async () => {
    workspace.mockResolvedValue(apiWorkspace({ archived: true }) as never);
    renderPage();
    await waitFor(() => { expect(nodeNames()).toContain('张三'); }, { timeout: 12000 });
    expect(screen.getByText(/本案已归档：关系图、证据目录、快照与客户同意全部只读/)).toBeTruthy();
    expect((screen.getByRole('button', { name: '＋ 主体' }) as HTMLButtonElement).disabled)
      .toBe(true);
    expect((screen.getByRole('button', { name: '＋ 关系' }) as HTMLButtonElement).disabled)
      .toBe(true);
    // 归档仍可取消：只读不等于整页不可操作。
    expect((screen.getByRole('button', { name: '取消归档' }) as HTMLButtonElement).disabled)
      .toBe(false);
  }, 30000);

  it('出处行点击后用真实文件版本与页码打开 SourcePreview', async () => {
    workspace.mockResolvedValue(apiWorkspace() as never);
    renderPage();
    await waitFor(() => { expect(nodeNames()).toContain('张三'); }, { timeout: 12000 });
    const label = [...document.querySelectorAll<HTMLElement>('.graph-edge-label')]
      .find((node) => node.textContent?.includes('持股 45%'));
    expect(label).toBeTruthy();
    fireEvent.click(label!);
    const inspector = await screen.findByLabelText('关系详情');
    await userEvent.click(within(inspector).getAllByRole('button', { name: '查看原文' })[0]);
    const dialog = await screen.findByRole('dialog', { name: '原件预览' });
    expect(dialog.textContent).toContain('股权转让协议.pdf');
    expect(dialog.textContent).toContain('第 2 页');
    const image = within(dialog).getByRole('img') as HTMLImageElement;
    expect(image.getAttribute('src'))
      .toBe(`/api/v1/cases/${CASE}/file-versions/ver-9/page-preview?page=2`);
  }, 30000);

  it('刷新失败时保留明确提示并可重试，不清空已显示的案件', async () => {
    workspace.mockResolvedValueOnce(apiWorkspace() as never);
    workspace.mockRejectedValueOnce(new Error('network down'));
    workspace.mockResolvedValue(apiWorkspace() as never);
    // 序号先显示为 5，轮询看到 9 才触发刷新；刷新那次会失败。
    seqs.mockResolvedValue({ seqs: { graph: 9, evidence: 2 } });
    renderPage();
    await waitFor(() => { expect(nodeNames()).toContain('张三'); }, { timeout: 12000 });
    window.dispatchEvent(new Event('focus'));
    await waitFor(() => {
      expect(screen.getByText(/刷新失败/)).toBeTruthy();
    }, { timeout: 8000 });
    // 数据仍在，重试后提示消失。
    expect(nodeNames()).toContain('张三');
    await userEvent.click(screen.getByRole('button', { name: '重试刷新' }));
    await waitFor(() => {
      expect(screen.queryByText(/刷新失败/)).toBeNull();
    }, { timeout: 8000 });
  }, 40000);
});
