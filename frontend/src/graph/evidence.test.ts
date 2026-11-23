import { describe, expect, it } from 'vitest';
import { computeEvidenceNumbers, resolveSource } from './evidence';
import type {
  Annotation, EvidenceBinding, EvidenceGroup, EvidenceItem, GraphSource, Material, Segment,
} from './types';

const caseId = 'case-1';

function material(id: string, title: string, pageCount = 10): Material {
  return { id, case_id: caseId, title, kind: 'pdf', page_count: pageCount };
}

function group(id: string, position: number, removed = false): EvidenceGroup {
  return {
    id, case_id: caseId, position, title: `组${id}`, purpose: null,
    created_by: 'human', request_key: null,
    removed_at: removed ? '2026-01-01T00:00:00Z' : null,
    revision: 0, created_at: '', updated_at: '',
  };
}

function item(id: string, groupId: string, position: number,
              review: EvidenceItem['review_state'] = 'confirmed', removed = false): EvidenceItem {
  return {
    id, case_id: caseId, group_id: groupId, position, title: `条目${id}`,
    purpose: null, source_kind: 'manual', import_material_id: null,
    review_state: review, created_by: 'human', request_key: null,
    removed_at: removed ? '2026-01-01T00:00:00Z' : null,
    revision: 0, created_at: '', updated_at: '',
  };
}

function binding(id: string, itemId: string, materialId: string,
                 start: number | null, end: number | null,
                 review: EvidenceBinding['review_state'] = 'confirmed'): EvidenceBinding {
  return {
    id, case_id: caseId, item_id: itemId, material_id: materialId,
    page_start: start, page_end: end, review_state: review,
    created_by: 'human', removed_at: null, created_at: '',
  };
}

function source(id: string, segmentId: string): GraphSource {
  return {
    id, case_id: caseId, entity_id: null, relation_id: 'rel-1',
    segment_id: segmentId, material_id: null, annotation_id: null,
    proves: null, created_by: 'human', removed_at: null, created_at: '',
  };
}

const materials = [material('m1', '合同.pdf', 12), material('m2', '流水.xlsx', 8)];
const segments: Segment[] = [
  { id: 's1', case_id: caseId, material_id: 'm1', page: 5, text: '合同第5页' },
  { id: 's2', case_id: caseId, material_id: 'm2', page: 4, text: '流水第4页' },
];
const annotations: Annotation[] = [];

describe('证据编号：组序 + 组内序', () => {
  it('按 (position, id) 生成 证据{组}-{序}，软删条目与分组不占号', () => {
    const groups = [group('g2', 1), group('g1', 0), group('g3', 2, true)];
    const items = [
      item('i2', 'g1', 1), item('i1', 'g1', 0), item('i3', 'g1', 2, 'confirmed', true),
      item('i4', 'g2', 0),
    ];
    const numbers = computeEvidenceNumbers({ groups, items, bindings: [], materials, segments, annotations });
    expect(numbers.numbers.i1).toBe('证据1-1');
    expect(numbers.numbers.i2).toBe('证据1-2');
    expect(numbers.numbers.i3).toBeUndefined();
    expect(numbers.numbers.i4).toBe('证据2-1');
    expect(numbers.groupNumbers.g3).toBeUndefined();
  });

  it('待确认条目与待确认绑定不参与编号', () => {
    const groups = [group('g1', 0)];
    const items = [item('i1', 'g1', 0), item('i2', 'g1', 1, 'pending'), item('i3', 'g1', 2)];
    const bindings = [binding('b1', 'i3', 'm1', 1, 12, 'pending')];
    const numbers = computeEvidenceNumbers({ groups, items, bindings, materials, segments, annotations });
    expect(numbers.numbers.i1).toBe('证据1-1');
    expect(numbers.numbers.i2).toBeUndefined();
    // i3 已确认但绑定待确认：仍占序号（编号存在），但出处解析不命中。
    expect(numbers.numbers.i3).toBe('证据1-2');
    const resolved = resolveSource(source('src1', 's1'),
      { groups, items, bindings, materials, segments, annotations }, numbers);
    expect(resolved.labels).toEqual([]);
    expect(resolved.text).toBe('合同.pdf（未编号）');
  });
});

describe('出处解析：页码与多命中', () => {
  it('命中页码范围时追加「第N页」；整份材料绑定不追加页码', () => {
    const groups = [group('g1', 0)];
    const items = [item('i1', 'g1', 0)];
    const bindings = [binding('b1', 'i1', 'm1', 1, 12)];
    const numbers = computeEvidenceNumbers({ groups, items, bindings, materials, segments, annotations });
    const withPage = resolveSource(source('src1', 's1'),
      { groups, items, bindings, materials, segments, annotations }, numbers);
    expect(withPage.text).toBe('证据1-1 第5页');
    const whole = resolveSource(
      { ...source('src2', 's1'), segment_id: null, material_id: 'm1' },
      { groups, items, bindings, materials, segments, annotations }, numbers,
    );
    expect(whole.text).toBe('证据1-1');
  });

  it('同一出处命中多个条目时按目录顺序全部列出', () => {
    const groups = [group('g2', 1), group('g1', 0)];
    const items = [item('i1', 'g1', 0), item('i2', 'g2', 0), item('i3', 'g2', 1)];
    const bindings = [
      binding('b1', 'i1', 'm1', 1, 12),
      binding('b2', 'i2', 'm1', 5, 5),
      binding('b3', 'i3', 'm1', 5, 8),
    ];
    const numbers = computeEvidenceNumbers({ groups, items, bindings, materials, segments, annotations });
    const resolved = resolveSource(source('src1', 's1'),
      { groups, items, bindings, materials, segments, annotations }, numbers);
    expect(resolved.labels).toEqual(['证据1-1 第5页', '证据2-1 第5页', '证据2-2 第5页']);
    expect(resolved.text).toBe('证据1-1 第5页、证据2-1 第5页、证据2-2 第5页');
  });

  it('页码不在绑定范围内时回退为「未编号」', () => {
    const groups = [group('g1', 0)];
    const items = [item('i1', 'g1', 0)];
    const bindings = [binding('b1', 'i1', 'm1', 1, 3)];
    const numbers = computeEvidenceNumbers({ groups, items, bindings, materials, segments, annotations });
    const resolved = resolveSource(source('src1', 's1'),
      { groups, items, bindings, materials, segments, annotations }, numbers);
    expect(resolved.labels).toEqual([]);
    expect(resolved.text).toContain('未编号');
  });
});
