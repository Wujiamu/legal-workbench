/**
 * Phase 3 总装集成测试：CaseGraphPage 接入三态导出、防漏审计、初筛消歧、表格映射与预填关系。
 */
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    graphApi: {
      ...actual.graphApi,
      workspace: vi.fn(),
      rules: vi.fn(),
      addRelation: vi.fn(),
      addSource: vi.fn(),
      updateRelation: vi.fn(),
      createSnapshot: vi.fn(),
    },
    chartsApi: {
      ...actual.chartsApi,
      preflight: vi.fn(),
      citationTableText: vi.fn(),
      citationTableJson: vi.fn(),
    },
    graphAssistApi: {
      ...actual.graphAssistApi,
      audit: vi.fn(),
      entityCandidates: vi.fn(),
    },
    progressApi: { ...actual.progressApi, snapshot: vi.fn(), seqs: vi.fn() },
    annotationsApi: { ...actual.annotationsApi, list: vi.fn() },
    casesApi: { ...actual.casesApi, overview: vi.fn() },
  };
});

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useParams: () => ({ caseId: 'case-assist-1' }) };
});

import {
  annotationsApi, casesApi, chartsApi, graphApi, graphAssistApi, progressApi,
} from '../api/endpoints';
import CaseGraphPage from './CaseGraphPage';
import { RelationDialog } from './GraphDialogs';
import { defaultRules } from './rules';
import type { GraphSnapshot } from './types';

const CASE = 'case-assist-1';

function buildWorkspace(archived = false): GraphSnapshot {
  return {
    case_id: CASE,
    case_title: 'TEST-ONLY 总装测试案',
    archived,
    seq: 2,
    seqs: { graph: 2, evidence: 1 },
    entities: [
      {
        id: 'ent-1', case_id: CASE, kind: 'org', name: '甲公司', roles: ['plaintiff'],
        note: null, created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0, created_at: '2026-11-22T00:00:00Z',
        updated_at: '2026-11-22T00:00:00Z',
      },
      {
        id: 'ent-2', case_id: CASE, kind: 'org', name: '乙公司', roles: ['defendant'],
        note: null, created_by: 'human', updated_by: 'human', request_key: null,
        removed_at: null, revision: 0, created_at: '2026-11-22T00:00:00Z',
        updated_at: '2026-11-22T00:00:00Z',
      },
    ],
    aliases: [],
    relations: [],
    sources: [],
    graphs: [{
      id: 'graph-1', case_id: CASE, title: '主图', description: null, position: 0,
      created_by: 'human', updated_by: 'human', request_key: null, removed_at: null,
      revision: 0, created_at: '2026-11-22T00:00:00Z', updated_at: '2026-11-22T00:00:00Z',
    }],
    nodes: [
      {
        id: 'n-1', case_id: CASE, graph_id: 'graph-1', entity_id: 'ent-1', group_id: null,
        x: 20, y: 20, revision: 0, created_at: '2026-11-22T00:00:00Z',
        updated_at: '2026-11-22T00:00:00Z',
      },
      {
        id: 'n-2', case_id: CASE, graph_id: 'graph-1', entity_id: 'ent-2', group_id: null,
        x: 280, y: 20, revision: 0, created_at: '2026-11-22T00:00:00Z',
        updated_at: '2026-11-22T00:00:00Z',
      },
    ],
    edges: [],
    groups: [],
    snapshots: [{
      id: 'snap-1', case_id: CASE, graph_id: 'graph-1', version_no: 1, stage_label: '起诉前',
      note: null, progress_node_id: null, excluded_pending_count: 0,
      created_by: 'human', created_at: '2026-11-22T00:00:00Z',
      payload: {
        graph: { id: 'graph-1', title: '主图', description: null },
        nodes: [
          {
            entity_id: 'ent-1', name: '甲公司', kind: 'org', roles: ['plaintiff'],
            aliases: [], x: 20, y: 20, group_id: null, sources: [],
          },
          {
            entity_id: 'ent-2', name: '乙公司', kind: 'org', roles: ['defendant'],
            aliases: [], x: 280, y: 20, group_id: null, sources: [],
          },
        ],
        edges: [],
        groups: [],
      },
    }],
    consents: [],
    evidence: {
      groups: [], items: [], bindings: [],
      pending_changes: [],
      materials: [],
      numbers: {},
      group_numbers: {},
    } as GraphSnapshot['evidence'] & {
      pending_changes: [];
      materials: [];
      numbers: Record<string, string>;
      group_numbers: Record<string, string>;
    },
    labels: {},
    evidence_numbers: {},
    progress_nodes: [],
    segments: [],
    materials: [],
    annotations: [],
    pending_changes: [],
    changes: [],
    last_change_id: null,
  };
}

beforeAll(() => {
  class ResizeObserverMock {
    observe() {}
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
    transformPoint(p: { x: number; y: number }) { return { x: p.x, y: p.y, z: 0, w: 1 }; }
  }
  vi.stubGlobal('DOMMatrixReadOnly', DOMMatrixMock);
  vi.stubGlobal('DOMMatrix', DOMMatrixMock);
  (SVGElement.prototype as unknown as { getBBox(): DOMRect }).getBBox = () => ({
    x: 0, y: 0, width: 40, height: 16,
  } as DOMRect);
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.open = false;
  };
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: vi.fn(() => 'blob:test'),
    revokeObjectURL: vi.fn(),
  });
});

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(graphApi.rules).mockResolvedValue({ rules: {} } as never);
  vi.mocked(annotationsApi.list).mockResolvedValue({ items: [], offset: 0, limit: 200, total: 0 });
  vi.mocked(progressApi.snapshot).mockResolvedValue({
    case_id: CASE, seq: 1, nodes: [], relations: [], links: [],
    pending_changes: [], changes: [], last_change_id: null,
  } as never);
  vi.mocked(progressApi.seqs).mockResolvedValue({ seqs: { graph: 2, evidence: 1 } });
  vi.mocked(casesApi.overview).mockResolvedValue({
    case_id: CASE, title: 'TEST-ONLY 总装测试案', revision: 0, archived_at: null,
  } as never);
});

describe('CaseGraphPage 总装集成', () => {
  it('展示三态导出与辅助工具；归档时写工具禁用但三态导出与只读审计可用', async () => {
    vi.mocked(graphApi.workspace).mockResolvedValue(buildWorkspace(true) as never);
    render(<MemoryRouter><CaseGraphPage /></MemoryRouter>);

    const exportBtn = await screen.findByRole('button', { name: '三态导出…' });
    const auditBtn = screen.getByRole('button', { name: '防漏与暗线审计' });
    const discoveryBtn = screen.getByRole('button', { name: '初筛主体与别名' }) as HTMLButtonElement;
    const tableBtn = screen.getByRole('button', { name: '表格映射出边' }) as HTMLButtonElement;

    expect((exportBtn as HTMLButtonElement).disabled).toBe(false);
    expect((auditBtn as HTMLButtonElement).disabled).toBe(false);
    expect(discoveryBtn.disabled).toBe(true);
    expect(tableBtn.disabled).toBe(true);
  });

  it('从快照抽屉跳转三态导出并调用服务端 preflight 与 citationTableText', async () => {
    vi.mocked(graphApi.workspace).mockResolvedValue(buildWorkspace(false) as never);
    vi.mocked(chartsApi.preflight).mockResolvedValue({
      chart_meta: {
        case_id: CASE, chart_type: 'graph', target_id: 'graph-1', profile: 'court',
        title: '主图', generated_at: '2026-11-22T07:00:00Z',
        snapshot_id: 'snap-1', snapshot_version: 1, frozen_at: '2026-11-22T00:00:00Z',
        digest: null, currency: null, contains_suspected: false,
      },
      issues: [],
    });
    vi.mocked(chartsApi.citationTableText).mockResolvedValue('# 图示引用索引附表\n');

    render(<MemoryRouter><CaseGraphPage /></MemoryRouter>);
    await screen.findByRole('button', { name: '三态导出…' });

    await userEvent.click(screen.getByRole('button', { name: /版本快照/ }));
    const exportFromSnap = await screen.findByRole('button', { name: '三态导出此快照…' });
    await userEvent.click(exportFromSnap);

    const dialog = await screen.findByRole('dialog', { name: '导出「主图」' });
    await userEvent.selectOptions(within(dialog).getByLabelText('导出模式'), 'court');
    await waitFor(() => expect(chartsApi.preflight).toHaveBeenCalledWith(
      CASE,
      expect.objectContaining({ profile: 'court', snapshot_id: 'snap-1' }),
      expect.any(AbortSignal),
    ));
    await userEvent.click(within(dialog).getByRole('button', { name: '引用索引 Markdown' }));
    await waitFor(() => expect(chartsApi.citationTableText).toHaveBeenCalledWith(
      CASE,
      expect.objectContaining({ profile: 'court', snapshot_id: 'snap-1', format: 'md' }),
      expect.any(AbortSignal),
    ));

    // Verify SVG export path uses citationTableJson and triggers download cleanly
    vi.mocked(chartsApi.citationTableJson).mockResolvedValue({
      chart_meta: {
        case_id: CASE, chart_type: 'graph', target_id: 'graph-1', profile: 'court',
        title: '主图', generated_at: '2026-11-22T07:00:00Z',
        snapshot_id: 'snap-1', snapshot_version: 1, frozen_at: '2026-11-22T00:00:00Z',
        digest: null, currency: null, contains_suspected: false,
      },
      assertions: [],
      rows: [],
      waivers: [],
    });
    await userEvent.click(within(dialog).getByRole('button', { name: '下载 SVG' }));
    await waitFor(() => expect(chartsApi.citationTableJson).toHaveBeenCalledWith(
      CASE,
      expect.objectContaining({ profile: 'court', snapshot_id: 'snap-1' }),
      expect.any(AbortSignal),
    ));
    expect(URL.createObjectURL).toHaveBeenCalled();
  });

  it('强指纹审计建关系经预填 RelationDialog 按 suspected -> add_source -> evidenced 顺序写入且失败续接不重发 add_relation', async () => {
    const ws = buildWorkspace(false);
    const createdRel = {
      id: 'rel-new', case_id: CASE, from_entity_id: 'ent-1', to_entity_id: 'ent-2',
      directed: true, kind: 'affiliate', label: '同一手机号 13800138000',
      credibility: 'suspected' as const, attrs: {}, note: null,
      created_by: 'human' as const, updated_by: 'human' as const, request_key: null,
      removed_at: null, revision: 0,
      created_at: '2026-11-22T00:00:00Z', updated_at: '2026-11-22T00:00:00Z',
    };
    const commands: string[] = [];
    let failSecondSourceOnce = true;
    const onSubmit = vi.fn(async (cmd) => {
      commands.push(cmd.type);
      if (cmd.type === 'add_source' && commands.filter((c) => c === 'add_source').length === 2 && failSecondSourceOnce) {
        failSecondSourceOnce = false;
        return { ok: false as const, code: 'validation' as const, message: '模拟出处失败' };
      }
      const next = buildWorkspace(false);
      next.relations = [createdRel];
      next.last_change_id = 'chg-1';
      next.changes = [{
        id: 'chg-1', case_id: CASE, domain: 'graph', seq: 3, command: cmd.type,
        actor: 'human', entry: 'local-web', review_state: 'none', reviewed_at: null, reverted_by: null,
        created_at: '2026-11-22T00:00:00Z',
        diff: cmd.type === 'add_relation'
          ? [{ target: 'relation', id: 'rel-new', before: null, after: {}, before_rev: null, after_rev: 0 }]
          : [],
      }];
      return { ok: true as const, snapshot: next };
    });

    render(
      <RelationDialog
        mode="add"
        ws={ws}
        rules={defaultRules}
        graphId="graph-1"
        prefill={{
          fromEntityId: 'ent-1',
          toEntityId: 'ent-2',
          kind: 'affiliate',
          credibility: 'evidenced',
          label: '同一手机号 13800138000',
          initialSources: [
            { ref: { segment_id: 'seg-a' }, proves: '碰撞出处1' },
            { ref: { segment_id: 'seg-b' }, proves: '碰撞出处2' },
          ],
        }}
        onClose={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: '新建关系' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('第 2/2 条'));
    expect(commands).toEqual(['add_relation', 'add_source', 'add_source']);

    // Retry skips add_relation & 1st add_source, finishes 2nd add_source + update_relation
    await userEvent.click(screen.getByRole('button', { name: '新建关系' }));
    await waitFor(() => expect(commands).toEqual([
      'add_relation', 'add_source', 'add_source', 'add_source', 'update_relation',
    ]));
  });
});
