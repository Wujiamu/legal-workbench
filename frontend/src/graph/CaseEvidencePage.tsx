/**
 * 真实案件「证据目录」页签：与关系图共用同一份工作区快照（编号唯一来源），
 * 单独列出分组、条目、绑定与待确认复核；导入与 CSV 导出都走真实接口。
 */
import { useMemo } from 'react';
import { Link, useParams } from 'react-router';
import { AppBrand, ThemeSwitcher } from '../components/BrandMark';
import { CaseNavBar } from '../components/CaseNavBar';
import { evidenceApi } from '../api/endpoints';
import { EvidenceCatalogPanel } from './EvidenceCatalogPanel';
import { EvidenceImportPanel } from './EvidenceImportPanel';
import { createHttpGraphSource } from './httpExecutor';
import { useGraphWorkspace } from './useGraphWorkspace';
import './graph.css';

export default function CaseEvidencePage() {
  const { caseId = '' } = useParams();
  return <CaseEvidence key={caseId} caseId={caseId} />;
}

function CaseEvidence({ caseId }: { caseId: string }) {
  const source = useMemo(() => createHttpGraphSource(caseId), [caseId]);
  const workspace = useGraphWorkspace({ caseId, source });
  const { snapshot: ws } = workspace;
  const runAgent = useMemo(() => async () => ({
    ok: false as const, code: 'read_only' as const,
    message: '网页入口不以 Agent 身份写入。',
  }), []);

  if (!ws) {
    if (workspace.loadError) {
      return (
        <p role="alert" className="graph-error" style={{ margin: 24 }}>
          错误：{workspace.loadError}
          <button type="button" onClick={() => void workspace.reload()}>重试</button>
        </p>
      );
    }
    return <p className="progress-loading" style={{ margin: 24 }}>加载中…</p>;
  }

  return (
    <div className="graph-prototype">
      <header className="app-bar"><div className="app-bar-inner">
        <AppBrand />
        <span className="app-section">证据目录</span>
        <Link to="/">← 案件工作表</Link>
        <ThemeSwitcher />
      </div></header>
      <CaseNavBar caseId={caseId} active="evidence" />
      <main className="graph-main">
        <div className="page-heading graph-heading">
          <div>
            <h1>{ws.case_title}</h1>
            <p className="muted graph-subtitle">
              证据编号唯一来源：分组与条目调序后，关系图出处立即按「证据{'{组}'}-{'{序}'}」更新。
            </p>
          </div>
        </div>
        {ws.archived ? (
          <p role="status" className="graph-archived">
            本案已归档：证据目录只读；查看、CSV 导出与一键装卷仍可用。
          </p>
        ) : null}
        {workspace.loadError ? (
          <p role="alert" className="graph-error">
            刷新失败：{workspace.loadError}
            <button type="button" onClick={() => void workspace.reload()}>重试刷新</button>
          </p>
        ) : null}
        {workspace.error ? (
          <p role="alert" className="graph-error">
            {workspace.error}
            <button type="button" onClick={workspace.dismissError}>关闭</button>
          </p>
        ) : null}
        <EvidenceCatalogPanel
          ws={ws}
          readOnly={ws.archived}
          run={workspace.run}
          runAgent={runAgent}
          exportHref={evidenceApi.exportUrl(caseId)}
          bundleHref={evidenceApi.bundleUrl(caseId)}
          importSlot={(
            <EvidenceImportPanel caseId={caseId} ws={ws} readOnly={ws.archived}
              onImported={() => { void workspace.reload(); }} />
          )}
        />
      </main>
    </div>
  );
}
