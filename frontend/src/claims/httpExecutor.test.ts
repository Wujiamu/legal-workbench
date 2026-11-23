/**
 * 诉请金额 HTTP 数据源：候选金额 GET、整数分换算与同币种拒绝的回归。
 * mock 全局 fetch，真实走 client / endpoints，路径或载荷写错会失败。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { candidateSearch, createHttpClaimSource } from './httpExecutor';

const CASE = '22222222-2222-4222-8222-222222222222';
const GROUP = '33333333-3333-4333-8333-333333333333';

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300, status,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function snapshot(overrides: Record<string, unknown> = {}) {
  return {
    case_id: CASE, seq: 1,
    groups: [{
      id: GROUP, case_id: CASE, title: '对被告一主诉请', currency: 'CNY',
      include_in_total: true, position: 0, note: null, created_by: 'human',
      updated_by: 'human', request_key: null, removed_at: null, revision: 0,
      created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
    }],
    items: [{
      id: 'item-1', case_id: CASE, group_id: GROUP, title: '货款本金',
      category: 'principal', amount_cents: 128000000, calc_note: '合同价款',
      segment_id: null, material_id: null, annotation_id: null, position: 0,
      created_by: 'human', updated_by: 'human', request_key: null, removed_at: null,
      revision: 2, created_at: '2026-11-20T00:00:00Z', updated_at: '2026-11-20T00:00:00Z',
    }],
    group_subtotals: { [GROUP]: 128000000 },
    totals_by_currency: [{ currency: 'CNY', total_cents: 128000000, included_group_ids: [GROUP] }],
    pending_changes: [], changes: [], pending_count: 0,
    excluded_group_count: 0, included_group_count: 1, last_change_id: null,
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
    if (!handler && url.includes('/annotations')) {
      return jsonResponse({ items: [], offset: 0, limit: 200, total: 0 });
    }
    if (!handler) throw new Error(`unexpected request: ${call.method} ${url}`);
    return handler(call);
  }));
}

beforeEach(() => { calls = []; });
afterEach(() => { vi.unstubAllGlobals(); });

/** 让 load() 能读到材料/标注（真实入口会先取一次名称）。 */
function withLookups(handlers: Record<string, (call: Call) => Response>) {
  return {
    [`GET /api/v1/cases/${CASE}/evidence-catalog`]: () => jsonResponse({
      case_id: CASE, seq: 1, groups: [], items: [], bindings: [],
      materials: [{ id: 'mat-1', display_name: '购销合同.pdf' }],
      numbers: {}, group_numbers: [],
    }),
    ...handlers,
  };
}

describe('claims httpExecutor', () => {
  it('maps the snapshot and keeps integer cents', async () => {
    route(withLookups({
      [`GET /api/v1/cases/${CASE}/claims`]: () => jsonResponse(snapshot()),
    }));

    const source = createHttpClaimSource(CASE);
    const result = await source.load();
    expect(result.groups[0].currency).toBe('CNY');
    expect(result.items[0].amount_cents).toBe(128000000);
    expect(result.group_subtotals[GROUP]).toBe(128000000);
    expect(result.totals_by_currency[0].total_cents).toBe(128000000);
  });

  it('reads amount candidates from the real GET and keeps null for unrepresentable cents', async () => {
    route({
      [`GET /api/v1/cases/${CASE}/claims/amount-candidates?query=%E5%90%88%E5%90%8C`]: () => jsonResponse({
        case_id: CASE,
        candidates: [
          {
            exact_value_id: 'ev-1', segment_id: 'seg-1', material_id: 'mat-1',
            material_title: '购销合同.pdf', page: 3, locator: { unit_key: '第1条' },
            raw_text: '人民币1,280,000元', currency: 'CNY', amount_cents: 128000000,
            excerpt: '合同价款为人民币1,280,000元', span: { start: 4, end: 20 },
          },
          {
            exact_value_id: 'ev-2', segment_id: 'seg-2', material_id: 'mat-1',
            material_title: '结算单.pdf', page: 2, locator: {},
            raw_text: '1.005元', currency: 'CNY', amount_cents: null,
            excerpt: '结算 1.005元', span: { start: 0, end: 5 },
          },
        ],
      }),
    });
    const rows = await candidateSearch(CASE)('合同');
    expect(rows).toHaveLength(2);
    expect(rows[0].amount_cents).toBe(128000000);
    expect(rows[0].amount_text).toBe('1,280,000.00');
    expect(rows[0].source?.segment_id).toBe('seg-1');
    // 无法精确到分的候选仍返回，但标记 null，由界面排除选取。
    expect(rows[1].amount_cents).toBeNull();
  });

  it('posts add_item with amount_cents derived from the entered amount', async () => {
    route(withLookups({
      [`GET /api/v1/cases/${CASE}/claims`]: () => jsonResponse(snapshot()),
      [`POST /api/v1/cases/${CASE}/claims/items`]: () => jsonResponse(snapshot({ seq: 2 })),
    }));

    const source = createHttpClaimSource(CASE);
    await source.load();
    const result = await source.human({
      type: 'add_item', group_id: GROUP, title: '利息', category: 'interest',
      amount: '86,400.00', calc_note: '按日万分之一点七五',
      source: {
        segment_id: 'seg-1', material_id: 'mat-1', material_title: '购销合同.pdf',
        page: 4, locator: '第7.2条', excerpt: '', span: { start: 0, end: 0 }, raw_text: '',
      },
    });

    expect(result.ok).toBe(true);
    const post = calls.find((call) => call.method === 'POST')!;
    expect(post.url).toBe(`/api/v1/cases/${CASE}/claims/items`);
    expect(post.body).toMatchObject({
      case_id: CASE, group_id: GROUP, title: '利息', category: 'interest',
      amount_cents: 8640000, segment_id: 'seg-1', material_id: null, annotation_id: null,
    });
  });

  it('rejects a foreign-currency amount locally instead of posting it', async () => {
    route(withLookups({
      [`GET /api/v1/cases/${CASE}/claims`]: () => jsonResponse(snapshot()),
    }));
    const source = createHttpClaimSource(CASE);
    await source.load();
    const before = calls.length;
    const result = await source.human({
      type: 'add_item', group_id: GROUP, title: '美元项', category: 'other',
      amount: '美元12,345.67',
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.message).toContain('不一致');
    expect(calls).toHaveLength(before);
  });

  it('reads the claims sequence for polling', async () => {
    route({
      [`GET /api/v1/cases/${CASE}/changes/seq`]: () => jsonResponse({
        seqs: { progress: 0, graph: 0, evidence: 0, claims: 9 },
      }),
    });
    const source = createHttpClaimSource(CASE);
    expect(await source.seq!()).toBe(9);
  });
});
