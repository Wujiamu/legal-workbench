/** Route table. */
import { lazy, Suspense } from 'react';
import { Navigate, Route, Routes } from 'react-router';
import { EnabledModulesProvider, ModuleGuard } from './modules';
import { CaseWorkspacePage } from './pages/CaseWorkspacePage';
import { CasesPage } from './pages/CasesPage';

// P1 原型：内存演示数据，不接后端；入口在案件工作表，明确标注演示。
const ProgressPrototypePage = lazy(() => import('./progress/ProgressPage'));
const GraphPrototypePage = lazy(() => import('./graph/GraphPrototypePage'));
const ClaimsPrototypePage = lazy(() => import('./claims/ClaimsPrototypePage'));
// P4 真实进度页：复用同一套视图组件，数据来自后端 progress 服务。
const CaseProgressPage = lazy(() => import('./progress/CaseProgressPage'));
// P4 真实关系图 / 证据目录 / 诉请金额：与原型共用视图，数据来自 graph / claims 契约。
const CaseGraphPage = lazy(() => import('./graph/CaseGraphPage'));
const CaseEvidencePage = lazy(() => import('./graph/CaseEvidencePage'));
const CaseClaimsPage = lazy(() => import('./claims/CaseClaimsPage'));

const LOADING = <p style={{ padding: 24 }}>加载中…</p>;

export function App() {
  return (
    <EnabledModulesProvider>
      <Routes>
        <Route path="/" element={<CasesPage />} />
        <Route path="/cases/:caseId" element={<CaseWorkspacePage />} />
        <Route path="/cases/:caseId/progress" element={
          <ModuleGuard module="progress">
            <Suspense fallback={LOADING}><CaseProgressPage /></Suspense>
          </ModuleGuard>
        } />
        <Route path="/cases/:caseId/graph" element={
          <ModuleGuard module="graph">
            <Suspense fallback={LOADING}><CaseGraphPage /></Suspense>
          </ModuleGuard>
        } />
        <Route path="/cases/:caseId/evidence" element={
          <ModuleGuard module="evidence">
            <Suspense fallback={LOADING}><CaseEvidencePage /></Suspense>
          </ModuleGuard>
        } />
        <Route path="/cases/:caseId/claims" element={
          <ModuleGuard module="claims">
            <Suspense fallback={LOADING}><CaseClaimsPage /></Suspense>
          </ModuleGuard>
        } />
        <Route path="/prototype/progress" element={
          <Suspense fallback={LOADING}><ProgressPrototypePage /></Suspense>
        } />
        <Route path="/prototype/graph" element={
          <Suspense fallback={LOADING}><GraphPrototypePage /></Suspense>
        } />
        <Route path="/prototype/claims" element={
          <Suspense fallback={LOADING}><ClaimsPrototypePage /></Suspense>
        } />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </EnabledModulesProvider>
  );
}
