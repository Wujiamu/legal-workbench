import { describe, expect, it } from 'vitest';
import { buildGraphFixture } from './fixtures';
import { buildCanvasModel } from './model';
import { buildGraphSvg, exportFileName } from './exportSvg';
import { defaultRules } from './rules';

function modelOfFixture() {
  const exec = buildGraphFixture();
  const ws = exec.getSnapshot();
  const graph = ws.graphs.find((item) => item.title === '股权与实控关系')!;
  return { model: buildCanvasModel(ws, graph.id), graph };
}

describe('带图例的 SVG 导出', () => {
  it('包含标题、主体、四档图例与线型；证据编号进入边标签', () => {
    const { model, graph } = modelOfFixture();
    const { svg, width, height } = buildGraphSvg({
      title: graph.title,
      caseTitle: 'TEST-ONLY 甲公司诉乙公司股权与担保纠纷案',
      model,
      rules: defaultRules,
      generatedAt: '2026-11-21 10:00',
      showWorkingLabels: true,
    });
    expect(width).toBeGreaterThan(700);
    expect(height).toBeGreaterThan(400);
    expect(svg).toContain('股权与实控关系');
    expect(svg).toContain('TEST-ONLY 甲公司');
    expect(svg).toContain('TEST-ONLY 张三');
    expect(svg).toContain('证据1-1 第2页');
    // 四档图例齐全，线型与规则一致。
    for (const rule of defaultRules.credibility) {
      expect(svg).toContain(rule.label);
      expect(svg).toContain(`stroke="${rule.stroke}"`);
      if (rule.dash) expect(svg).toContain(`stroke-dasharray="${rule.dash}"`);
    }
    // 有向边带箭头 marker。
    expect(svg).toContain('marker-end="url(#arrow-evidenced)"');
    expect(svg).toContain('已排除');
    expect(svg).toContain('内部工作图');
    expect(svg.startsWith('<svg')).toBe(true);
    expect(svg.endsWith('</svg>')).toBe(true);
  });

  it.each(['client', 'court'] as const)('对外态 %s 仅使用投影中的编号和对象', (profile) => {
    const { model } = modelOfFixture();
    const edge = model.edges.find((item) => item.credibility === 'evidenced' && item.source_labels.length)!;
    const keptIds = new Set([edge.id, edge.source, edge.target]);
    const { svg } = buildGraphSvg({
      title: 'TEST-ONLY 可导出图',
      caseTitle: 'TEST-ONLY',
      model,
      rules: defaultRules,
      projection: {
        profile,
        includedIds: keptIds,
        references: [
          { targetId: edge.id, index: 1, label: '证据4-2 第2页 <摘录>' },
          { targetId: 'excluded-id', index: 2, label: 'TEST-ONLY 排除项' },
        ],
        version: '快照 v2',
        notice: '含待查项',
      },
    });
    expect(svg).toContain('[1]');
    expect(svg).toContain('[1] 证据4-2 第2页 &lt;摘录&gt;');
    expect(svg).toContain('快照 v2');
    expect(svg).toContain('含待查项');
    expect(svg).not.toContain('证据1-1 第2页');
    expect(svg).not.toContain('TEST-ONLY 排除项');
    expect(svg).not.toContain('待确认');
    expect(svg).not.toContain('arrow-ruled_out');
  });

  it('特殊字符被转义，文件名可读且去掉非法字符', () => {
    const { model } = modelOfFixture();
    const { svg } = buildGraphSvg({
      title: 'A&B <关系> "图"',
      caseTitle: "O'Neil 案",
      model,
      rules: defaultRules,
    });
    expect(svg).toContain('A&amp;B &lt;关系&gt; &quot;图&quot;');
    expect(svg).toContain('O&apos;Neil 案');
    expect(exportFileName('股权与实控关系', 'svg', new Date('2026-11-21T02:00:00Z')))
      .toBe('股权与实控关系-2026-11-21.svg');
    expect(exportFileName('a/b:c d', 'png', new Date('2026-11-21T02:00:00Z')))
      .toBe('a-b-c-d-2026-11-21.png');
  });

  it('过滤已排除关系/待确认徽标/工作别名并截断超 12 条脚注', () => {
    const { model } = modelOfFixture();
    const firstEvidenced = model.edges.find((e) => e.credibility === 'evidenced')!;
    const mutatedModel = {
      ...model,
      nodes: model.nodes.map((n, idx) => (idx === 0
        ? { ...n, pending: true, aliases: ['内部密称不应外泄'] }
        : n)),
      edges: [
        firstEvidenced,
        {
          ...firstEvidenced,
          id: 'edge-ruled-out',
          credibility: 'ruled_out' as const,
          label: '内部已排除边不应导出',
          source_labels: [],
        },
        {
          ...firstEvidenced,
          id: 'edge-suspected',
          credibility: 'suspected' as const,
          label: '待证关联（待举证主张）',
          source_labels: [],
        },
      ],
    };
    const refs = Array.from({ length: 14 }, (_, idx) => ({
      targetId: idx % 2 === 0 ? firstEvidenced.id : 'edge-suspected',
      index: idx + 1,
      label: `证据1-${idx + 1}`,
    }));
    const { svg } = buildGraphSvg({
      title: '外发合规图',
      caseTitle: 'TEST-ONLY 案',
      model: mutatedModel,
      rules: defaultRules,
      projection: {
        profile: 'court',
        includedIds: new Set([
          firstEvidenced.source,
          firstEvidenced.target,
          firstEvidenced.id,
          'edge-suspected',
        ]),
        references: refs,
      },
    });
    expect(svg).not.toContain('内部已排除边不应导出');
    expect(svg).not.toContain('内部密称不应外泄');
    expect(svg).not.toContain('>待确认<');
    expect(svg).toContain('待举证主张');
    expect(svg).toContain('证据1-12');
    expect(svg).not.toContain('证据1-13');
    expect(svg).toContain('完整编号见引用索引附表');
  });
});
