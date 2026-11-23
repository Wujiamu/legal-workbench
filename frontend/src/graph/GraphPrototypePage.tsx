/**
 * 路由 /prototype/graph：主体关系图 P1 原型（TEST-ONLY 内存数据，不接后端、不建表）。
 *
 * 页面只负责注入内存数据源与演示脚本；界面、命令等待与竞态守卫都在
 * GraphWorkspace / useGraphWorkspace 中，与 P4 真实案件页共用。
 * 行为与 P1 基线一致：一案多图、4 档线型、拖拽布局、证据编号联动、
 * 快照/客户同意/对比/恢复、Agent 待确认、归档只读、路径高亮与导出。
 */
import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import { GraphWorkspace } from './GraphWorkspace';
import { buildGraphFixture, FIXTURE_CASE_ID } from './fixtures';
import { createMemoryGraphSource } from './memorySource';
import { credibilityRule, defaultRules } from './rules';
import { useGraphWorkspace } from './useGraphWorkspace';
import './graph.css';

export default function GraphPrototypePage() {
  const [source, setSource] = useState(() => createMemoryGraphSource(buildGraphFixture()));
  // 重置演示时重建视图内部状态（筛选、路径、选中与弹窗），不动工作区数据。
  const [viewKey, setViewKey] = useState(0);
  const [resetPending, setResetPending] = useState(false);
  const workspace = useGraphWorkspace({ caseId: FIXTURE_CASE_ID, source });
  const { snapshot: ws, rules } = workspace;

  useEffect(() => {
    if (!resetPending || !ws) return;
    workspace.setNotice('演示已重置为初始合成案卷。');
    setResetPending(false);
  }, [resetPending, ws]);

  async function simulateAgentSource() {
    if (!ws) return;
    const candidates = ws.relations.filter((relation) => relation.removed_at === null
      && relation.credibility !== 'evidenced'
      && !ws.sources.some((source) => source.relation_id === relation.id && source.removed_at === null));
    const relation = candidates[0];
    if (!relation) {
      workspace.setError('没有可补充出处的待查关系；可先新建一条关系再试。');
      return;
    }
    const added = await workspace.runAgent({
      type: 'add_source', relation_id: relation.id, ref: { segment_id: 'seg-board-2' },
      proves: 'Agent 从股东会决议识别：同意提供保证',
      request_key: `agent-src-${Date.now()}`,
    });
    if (!added.ok) return;
    const fresh = added.snapshot.relations.find((item) => item.id === relation.id);
    if (!fresh) return;
    await workspace.runAgent({
      type: 'update_relation', relation_id: relation.id,
      expected_revision: fresh.revision, changes: { credibility: 'evidenced' },
    });
  }

  async function simulateAgentEntity() {
    if (!ws) return;
    const activeGraph = ws.graphs.find((graph) => graph.removed_at === null);
    if (!activeGraph) return;
    const name = `TEST-ONLY 临时主体 ${new Date().toLocaleTimeString('zh-CN', {
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    })}`;
    const created = await workspace.runAgent({
      type: 'add_entity', kind: 'org', name, roles: ['outsider'],
      note: 'Agent 从材料中识别的候选主体，待人工确认。',
      request_key: `agent-entity-${Date.now()}`,
    });
    if (!created.ok) return;
    const entity = created.snapshot.entities.find((item) => item.name === name);
    const anchor = created.snapshot.entities.find((item) => item.name.includes('甲公司')
      && item.removed_at === null);
    if (!entity || !anchor) return;
    await workspace.runAgent({
      type: 'add_relation', from_entity_id: entity.id, to_entity_id: anchor.id,
      kind: 'affiliate', label: '新发现关联', credibility: 'suspected', directed: true,
      graph_id: activeGraph.id, note: 'Agent 识别的关联，待人工确认。',
      request_key: `agent-relation-${Date.now()}`,
    });
  }

  function resetDemo() {
    setSource(createMemoryGraphSource(buildGraphFixture()));
    setViewKey((value) => value + 1);
    setResetPending(true);
  }

  if (workspace.loadError) {
    return <p role="alert" className="graph-error" style={{ margin: 24 }}>错误：{workspace.loadError}</p>;
  }
  if (!ws || !rules) return <p className="progress-loading" style={{ margin: 24 }}>加载中…</p>;

  return (
    <GraphWorkspace
      key={viewKey}
      snapshot={ws}
      rules={rules}
      run={workspace.run}
      runAgent={workspace.runAgent}
      undo={workspace.undo}
      notice={workspace.notice}
      error={workspace.error}
      onDismissNotice={workspace.dismissNotice}
      onDismissError={workspace.dismissError}
      onLocalError={workspace.setError}
      section="主体关系图原型 · TEST-ONLY 合成案卷"
      headerAction={<Link to="/">← 案件工作表</Link>}
      loadImpact={(target) => workspace.impact(target)}
      onArchive={async (next) => {
        await workspace.archive(next);
        workspace.setNotice(next ? '已归档：全部写命令被拒，查看与导出可用。' : '已取消归档，恢复可写。');
      }}
      demo={<>
        <button type="button" disabled={ws.archived} onClick={() => void simulateAgentSource()}>
          模拟 Agent 补出处并升级
        </button>
        <button type="button" disabled={ws.archived} onClick={() => void simulateAgentEntity()}>
          模拟 Agent 新增主体与关系
        </button>
        <button type="button" disabled={ws.archived}
          onClick={() => { void workspace.runAgent({
            type: 'import_evidence_catalog', request_key: `import-${Date.now()}`,
          }); }}>模拟外部目录导入</button>
        <button type="button" onClick={() => void workspace.undo()}>撤销（⌘Z）</button>
        <button type="button" onClick={resetDemo}>重置演示</button>
        <span className="muted">
          seq={ws.seq} · 待确认 {ws.pending_changes.length} · 证据 seq={ws.seqs.evidence}
        </span>
      </>}
    />
  );
}

/** 供测试与调试：当前规则的四档线型。 */
export function prototypeCredibilityRules() {
  return defaultRules.credibility.map((rule) => credibilityRule(defaultRules, rule.value));
}
