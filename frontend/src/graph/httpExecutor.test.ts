/**
 * P4 HTTP 执行器：请求路径、载荷与错误透传的回归。
 * 只 mock 全局 fetch，走真实 client / endpoints 代码，因此路径写错会直接失败。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createHttpGraphSource, createSourceSearch, mapRules } from './httpExecutor';

const CASE = '11111111-1111-4111-8111-111111111111';

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function workspace(overrides: Record<string, unknown> = {}) {
  return {
    case_id: CASE, case_title: '甲公司诉乙公司', archived: false, seq: 3,
    seqs: { graph: 3, evidence: 1 },
    entities: [{
      id: 'ent-1', case_id: CASE, kind: 'org', name: '甲投资', roles: ['plaintiff'],
      note: null, created_by: 'human', updated_by: 'human', request_key: null,
      removed_at: null, revision: 0, created_at: '2026-11-20T00:00:00Z',
      updated_at: '2026-11-20T00:00:00Z',
    }],
    aliases: [], relations: [], sources: [{
      id: 'src-1', case_id: CASE, entity_id: 'ent-1', relation_id: null,
      segment_id: 'seg-1', material_id: null, annotation_id: null, proves: '证明持股',
      created_by: 'human', removed_at: null, created_at: '2026-11-20T00:00:00Z',
    }],
    graphs: [], nodes: [], edges: [], groups: [], snapshots: [], consents: [],
    evidence: {
      case_id: CASE, seq: 1, groups: [], items: [], bindings: [],
      materials: [{ id: 'mat-1', display_name: '购销合同.pdf' }],
      numbers: {}, group_numbers: {},
    },
    labels: {
      'src-1': {
        labels: ['证据1-1'], text: '证据1-1 第2页', material_id: 'mat-1',
        material_title: '购销合同.pdf', page: 2, text_excerpt: '合同价款…',
        source_ref: 'seg-1', file_version_id: 'ver-9',
      },
    },
    pending_changes: [], changes: [], last_change_id: null, notice: null,
    ...overrides,
  };
}

interface Call { url: string; method: string; body: unknown }

let calls: Call[] = [];

function route(handlers: Record<string, (call: Call) => Response>) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const call: Call = {
      url, method: init?.method ?? 'GET',
      body: init?.body && typeof init.body === 'string' ? JSON.parse(init.body) : init?.body,
    };
    calls.push(call);
    const handler = handlers[`${call.method} ${url}`] ?? handlers[url];
    // 标注/材料等辅助清单按空页返回：本文件只关心被测请求本身。
    if (!handler && url.includes('/annotations')) {
      return jsonResponse({ items: [], offset: 0, limit: 200, total: 0 });
    }
    if (!handler) throw new Error(`unexpected request: ${call.method} ${url}`);
    return handler(call);
  }));
}

beforeEach(() => { calls = []; });
afterEach(() => { vi.unstubAllGlobals(); });

describe('graph httpExecutor', () => {
  it('loads the case workspace and maps labels/材料到可预览定位', async () => {
    route({
      [`GET /api/v1/cases/${CASE}/graphs`]: () => jsonResponse(workspace()),
      [`GET /api/v1/cases/${CASE}/progress`]: () => jsonResponse({
        case_id: CASE, seq: 1, nodes: [{ id: 'p-1', title: '立案', removed_at: null }],
        relations: [], links: [], pending_changes: [], changes: [],
      }),
    });
    const source = createHttpGraphSource(CASE);
    const snapshot = await source.load();

    expect(calls.some((call) => call.url === `/api/v1/cases/${CASE}/graphs`)).toBe(true);
    expect(snapshot.case_id).toBe(CASE);
    expect(snapshot.case_title).toBe('甲公司诉乙公司');
    expect(snapshot.seqs).toEqual({ graph: 3, evidence: 1 });
    expect(snapshot.materials[0].title).toBe('购销合同.pdf');
    expect(snapshot.labels['src-1'].file_version_id).toBe('ver-9');
    expect(snapshot.labels['src-1'].page).toBe(2);
    expect(snapshot.progress_nodes).toEqual([{ value: 'p-1', label: '立案' }]);
  });

  it('posts add_source with the immutable segment ref and returns the new workspace', async () => {
    route({
      [`POST /api/v1/cases/${CASE}/graph/sources`]: () => jsonResponse(workspace({
        seq: 4, seqs: { graph: 4, evidence: 1 }, notice: '已添加出处',
      })),
    });
    const source = createHttpGraphSource(CASE);
    const result = await source.human({
      type: 'add_source', entity_id: 'ent-1', ref: { segment_id: 'seg-1' }, proves: '证明持股',
    });

    expect(result.ok).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(`/api/v1/cases/${CASE}/graph/sources`);
    expect(calls[0].method).toBe('POST');
    expect(calls[0].body).toEqual({
      case_id: CASE, entity_id: 'ent-1', relation_id: null,
      ref: { segment_id: 'seg-1', material_id: null, annotation_id: null },
      proves: '证明持股', request_key: null,
    });
    if (result.ok) expect(result.snapshot.seq).toBe(4);
  });

  it('keeps the server message when an archived case rejects a write', async () => {
    route({
      [`POST /api/v1/cases/${CASE}/graph/entities`]: () => jsonResponse({
        code: 'invalid_input', message: '案件已归档，关系图只读；如需修改请先取消归档',
        details: {}, request_id: 'r-1',
      }, 422),
    });
    const source = createHttpGraphSource(CASE);
    const result = await source.human({ type: 'add_entity', kind: 'org', name: '丙公司' });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.code).toBe('validation');
      expect(result.message).toContain('已归档');
    }
  });

  it('undo reverts the last write change and refuses a second undo', async () => {
    route({
      [`POST /api/v1/cases/${CASE}/graph/sources`]: () => jsonResponse(workspace({
        seq: 4, seqs: { graph: 4, evidence: 1 }, last_change_id: 'chg-1',
      })),
      [`POST /api/v1/cases/${CASE}/graph/changes/chg-1/revert`]: () => jsonResponse(workspace({
        seq: 5, seqs: { graph: 5, evidence: 1 }, last_change_id: 'chg-2',
      })),
    });
    const source = createHttpGraphSource(CASE);
    await source.human({
      type: 'add_source', entity_id: 'ent-1', ref: { segment_id: 'seg-1' }, proves: '证明持股',
    });
    const undone = await source.undo!();
    expect(undone.ok).toBe(true);
    expect(calls.at(-1)!.url).toBe(`/api/v1/cases/${CASE}/graph/changes/chg-1/revert`);

    // 撤销自身不进入撤销栈：再按一次只得到明确提示，不发请求。
    const before = calls.length;
    const again = await source.undo!();
    expect(again.ok).toBe(false);
    if (!again.ok) expect(again.message).toContain('没有可撤销');
    expect(calls).toHaveLength(before);
  });

  it('posts add_to_graph with entity_ids/relation_ids and save_layout with node_id/x/y', async () => {
    route({
      [`POST /api/v1/cases/${CASE}/graph/graphs/g-1/members`]: () => jsonResponse(workspace({
        seq: 6, seqs: { graph: 6, evidence: 1 },
      })),
      [`POST /api/v1/cases/${CASE}/graph/graphs/g-1/layout`]: () => jsonResponse(workspace({
        seq: 7, seqs: { graph: 7, evidence: 1 },
      })),
    });
    const source = createHttpGraphSource(CASE);
    await source.human({
      type: 'add_to_graph', graph_id: 'g-1',
      entity_ids: ['ent-1', 'ent-2'], relation_ids: ['rel-1'],
    });
    // 契约要求数组字段；逐条 entity_id 会被服务端以 extra_forbidden 拒绝。
    expect(calls[0].body).toEqual({
      case_id: CASE, graph_id: 'g-1',
      entity_ids: ['ent-1', 'ent-2'], relation_ids: ['rel-1'],
    });

    await source.human({
      type: 'save_layout', graph_id: 'g-1',
      nodes: [{ node_id: 'n-1', x: 40, y: 60 }],
    });
    expect(calls[1].body).toEqual({
      case_id: CASE, graph_id: 'g-1',
      nodes: [{ node_id: 'n-1', x: 40, y: 60 }], groups: [],
    });
  });

  it('reads the four-domain change seqs from /changes/seq', async () => {
    route({
      [`GET /api/v1/cases/${CASE}/changes/seq`]: () => jsonResponse({
        seqs: { progress: 2, graph: 7, evidence: 5, claims: 1 },
      }),
    });
    const source = createHttpGraphSource(CASE);
    expect(await source.seqs!()).toEqual({ graph: 7, evidence: 5 });
  });

  it('refuses the prototype-only import command instead of calling a wrong endpoint', async () => {
    const source = createHttpGraphSource(CASE);
    const result = await source.human({ type: 'import_evidence_catalog' });
    expect(result.ok).toBe(false);
    expect(calls).toHaveLength(0);
  });

  it('searches parsed segments for the source picker', async () => {
    route({
      [`POST /api/v1/cases/${CASE}/search`]: () => jsonResponse({
        hits: [{
          source: {
            source_ref: 'seg-9', material_id: 'mat-1', file_version_id: 'ver-9',
            parse_run_id: 'run-1', locator: { page: 5 }, text: '股东会决议 内容', method: 'text',
          },
          analysis_id: null, analysis_version_id: null, source_type: 'originals',
          context: '股东会决议 内容', match_start: 0, match_end: 5, amount: null, currency: null,
        }],
        coverage: { complete: 1, partial: 0, failed: 0, pending: 0, unsupported: 0 },
        mode_used: 'literal', offset: 0, limit: 10, total: 1,
      }),
    });
    const options = await createSourceSearch(CASE)('股东会');
    expect(options).toEqual([{ value: 'segment:seg-9', label: '片段 · 第5页 · 股东会决议 内容' }]);
    expect(calls[0].url).toBe(`/api/v1/cases/${CASE}/search`);
    expect((calls[0].body as { mode: string }).mode).toBe('literal');
  });

  it('maps the rule dictionary and falls back to local defaults for missing keys', () => {
    const rules = mapRules({ snapshot_stages: ['庭前'] });
    expect(rules.snapshot_stages).toEqual(['庭前']);
    expect(rules.credibility.length).toBeGreaterThan(0);
    expect(rules.relation_kinds.length).toBeGreaterThan(0);
  });

  it('maps RelationGraphNode.id to entity_id in save_layout after load()', async () => {
    const loadedWs = workspace({
      nodes: [{
        id: 'n-1', case_id: CASE, graph_id: 'g-1', entity_id: 'ent-1', group_id: null,
        x: 0, y: 0, revision: 0, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
      }],
    });
    route({
      [`GET /api/v1/cases/${CASE}/graphs`]: () => jsonResponse(loadedWs),
      [`GET /api/v1/cases/${CASE}/annotations`]: () => jsonResponse({ items: [], offset: 0, limit: 200, total: 0 }),
      [`GET /api/v1/cases/${CASE}/progress`]: () => jsonResponse({
        case_id: CASE, seq: 1, nodes: [], relations: [], links: [],
        pending_changes: [], changes: [], last_change_id: null,
      }),
      [`POST /api/v1/cases/${CASE}/graph/graphs/g-1/layout`]: () => jsonResponse(loadedWs),
    });
    const source = createHttpGraphSource(CASE);
    await source.load();
    await source.human({
      type: 'save_layout', graph_id: 'g-1',
      nodes: [{ node_id: 'n-1', x: 180, y: 240 }],
    });
    const layoutCall = calls.find((c) => c.url.endsWith('/layout'));
    expect(layoutCall?.body).toEqual({
      case_id: CASE, graph_id: 'g-1',
      nodes: [{ node_id: 'ent-1', x: 180, y: 240 }], groups: [],
    });
  });
});
