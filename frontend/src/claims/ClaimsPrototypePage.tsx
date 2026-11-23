/**
 * P1 诉请金额原型页（/prototype/claims）：TEST-ONLY 内存数据源 + 合成案卷。
 * 不建表、不接业务 API；用于确认 Q3（Agent 待确认）与分组总计交互。
 *
 * 页面只注入数据源与演示脚本；ClaimsPanel / ItemDialog 的命令入口与竞态守卫
 * 由 useClaimsWorkspace 提供，P4 真实案件页换成 HTTP 数据源即可复用。
 */
import { useState } from 'react';
import { Link } from 'react-router';
import { ClaimsPanel } from './ClaimsPanel';
import { fixtureCandidates, FIXTURE_CASE_ID, FIXTURE_CASE_TITLE, buildClaimsFixture } from './fixtures';
import { createMemoryClaimSource } from './memorySource';
import { useClaimsWorkspace } from './useClaimsWorkspace';
import './claims.css';

export default function ClaimsPrototypePage() {
  const [source, setSource] = useState(() => createMemoryClaimSource(buildClaimsFixture()));
  const workspace = useClaimsWorkspace({ caseId: FIXTURE_CASE_ID, source });
  const [notice, setNotice] = useState('');
  const [noticeTone, setNoticeTone] = useState<'ok' | 'error'>('ok');
  const snapshot = workspace.snapshot;

  function show(message: string, tone: 'ok' | 'error') {
    setNotice(message);
    setNoticeTone(tone);
  }

  async function simulateAgentAdd() {
    const group = snapshot?.groups[0];
    if (!group) { show('没有可提交的分组', 'error'); return; }
    const time = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const result = await workspace.runAgent({
      type: 'add_item', group_id: group.id,
      title: `逾期利息（Agent 补充 ${time}）`,
      category: 'interest', amount: '3287.67',
      calc_note: 'Agent 依据补充协议第 2 条提取；提交后待承办人确认。',
      source: fixtureCandidates.find((candidate) => candidate.id === 'cand-interest-supplement')?.source ?? null,
    });
    show(result.ok
      ? 'Agent 已提交 1 条候选金额项，进入待确认区（未计入小计）。'
      : result.message ?? '操作失败', result.ok ? 'ok' : 'error');
  }

  async function simulateAgentUpdate() {
    const target = snapshot?.items.find((item) => item.title === '律师费（一审）');
    if (!target) { show('找不到「律师费（一审）」，可能已删除', 'error'); return; }
    const result = await workspace.runAgent({
      type: 'update_item', item_id: target.id, amount: '88000.00',
      calc_note: 'Agent 依据新发票拟调整为 88,000.00 元；原值待确认前不变。',
    });
    show(result.ok
      ? 'Agent 已提交金额修改，待确认区显示原值与拟改值。'
      : result.message ?? '操作失败', result.ok ? 'ok' : 'error');
  }

  function reset() {
    setSource(createMemoryClaimSource(buildClaimsFixture()));
    show('已恢复 TEST-ONLY 基线：两组诉请、1 条 Agent 待确认。', 'ok');
  }

  if (workspace.loadError) {
    return <p role="alert" className="claims-error" style={{ margin: 24 }}>错误：{workspace.loadError}</p>;
  }
  if (!snapshot) return <p style={{ margin: 24 }}>加载中…</p>;

  return (
    <div className="claims-prototype">
      <header className="app-bar"><div className="app-bar-inner">
        <span className="app-brand">法律工作台</span>
        <span className="app-section">诉请金额原型 · TEST-ONLY 合成案卷</span>
        <Link to="/">← 案件工作表</Link>
      </div></header>
      <main className="claims-main">
        <div className="page-heading">
          <div>
            <h1>{FIXTURE_CASE_TITLE}</h1>
            <p className="muted claims-subtitle">
              诉请金额构成 · P1 内存原型。不建表、不接业务 API；刷新页面即恢复基线。
            </p>
          </div>
        </div>

        <section className="claims-demo" aria-label="原型演示控制">
          <strong>演示控制</strong>
          <button type="button" onClick={() => void simulateAgentAdd()}>模拟 Agent 追加利息项</button>
          <button type="button" onClick={() => void simulateAgentUpdate()}>模拟 Agent 修改律师费</button>
          <button type="button" onClick={reset}>重置演示</button>
          <span className="muted">
            Agent 入口模拟 MCP 提交；确认/撤回仅人工。seq={snapshot.seq} · 待确认 {snapshot.pending_count}
          </span>
        </section>
        {notice ? (
          <p role={noticeTone === 'error' ? 'alert' : 'status'}
             className={noticeTone === 'error' ? 'claims-error' : 'claims-notice'}>
            {notice}
          </p>
        ) : null}

        <ClaimsPanel snapshot={snapshot} onCommand={workspace.run} />

        <p className="claims-scope-note">
          本原型仅验证交互与金额口径：分组与金额项增改删排、整数分与抵扣、按币种总计与计入开关、
          精确值出处与模拟原文、Agent 待确认/撤回。数据库、服务层、HTTP/MCP/CLI 入口与案件页签在 P2/P3
          实施；P2 待确认 Q3 与分组总计交互后再启动。
        </p>
      </main>
    </div>
  );
}
