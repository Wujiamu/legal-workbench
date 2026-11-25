/**
 * 真实案件「诉请金额」页签：案件 UUID 来自路由，数据走 /api/v1/cases/{id}/claims。
 *
 * - 金额候选走真实 GET（同币种、整数分由 ItemDialog 过滤）；
 * - 出处点击交给现有 SourcePreview：片段/材料/标注解析出文件版本与页码；
 * - 归档只读由案件概览判断，写命令在服务端同样被拒；
 * - 后台刷新失败保留明确提示，不把旧响应写进正在提交的界面。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router';
import { describeError } from '../api/client';
import { casesApi, searchApi } from '../api/endpoints';
import { AppBrand, ThemeSwitcher } from '../components/BrandMark';
import { CaseNavBar } from '../components/CaseNavBar';
import { SourcePreview } from '../components/SourcePreview';
import { ClaimsPanel } from './ClaimsPanel';
import { candidateSearch, createHttpClaimSource } from './httpExecutor';
import type { ClaimSource } from './types';
import { useClaimsWorkspace } from './useClaimsWorkspace';
import './claims.css';

interface PreviewTarget {
  versionId: string;
  name: string;
  page?: number;
  nonce: number;
}

export default function CaseClaimsPage() {
  const { caseId = '' } = useParams();
  return <CaseClaims key={caseId} caseId={caseId} />;
}

function CaseClaims({ caseId }: { caseId: string }) {
  const source = useMemo(() => createHttpClaimSource(caseId), [caseId]);
  const candidates = useMemo(() => candidateSearch(caseId), [caseId]);
  const workspace = useClaimsWorkspace({ caseId, source });
  const [title, setTitle] = useState('');
  const [archived, setArchived] = useState(false);
  const [overviewError, setOverviewError] = useState('');
  const [preview, setPreview] = useState<PreviewTarget | null>(null);
  const snapshot = workspace.snapshot;

  useEffect(() => {
    const controller = new AbortController();
    casesApi.overview(caseId, controller.signal)
      .then((item) => {
        if (controller.signal.aborted) return;
        setTitle(item.title);
        setArchived(Boolean(item.archived_at));
        setOverviewError('');
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setOverviewError(describeError(cause));
      });
    return () => controller.abort();
  }, [caseId]);

  const openSource = useCallback(async (item: ClaimSource) => {
    let versionId = item.file_version_id ?? null;
    let name = item.material_title || '出处材料';
    let page = item.page ?? undefined;
    // 候选金额来自 exact_values，只带 segment 引用；缺文件版本时按不可变 source_ref 现取一次。
    const ref = item.source_ref ?? item.segment_id;
    if (!versionId && ref) {
      try {
        const hit = await searchApi.readSource(caseId, ref, 120);
        versionId = hit.file_version_id;
        name = hit.display_name ?? name;
        page = page ?? (hit.locator as { page?: number }).page;
      } catch (cause) {
        setOverviewError(describeError(cause) || '出处定位失败，可稍后重试。');
        return;
      }
    }
    if (!versionId) {
      setOverviewError('该出处没有可定位的文件版本（可能属于未解析材料或已删除标注）。');
      return;
    }
    setPreview((current) => ({
      versionId: versionId!, name, page, nonce: (current?.nonce ?? 0) + 1,
    }));
  }, [caseId]);

  if (!snapshot) {
    if (workspace.loadError) {
      return (
        <p role="alert" className="claims-error" style={{ margin: 24 }}>
          错误：{workspace.loadError}
          <button type="button" onClick={() => void workspace.reload()}>重试</button>
        </p>
      );
    }
    return <p className="progress-loading" style={{ margin: 24 }}>加载中…</p>;
  }

  return (
    <div className="claims-prototype">
      <header className="app-bar"><div className="app-bar-inner">
        <AppBrand />
        <span className="app-section">诉请金额</span>
        <Link to="/">← 案件工作表</Link>
        <ThemeSwitcher />
      </div></header>
      <CaseNavBar caseId={caseId} active="claims" />
      <main className="claims-main">
        <div className="page-heading">
          <div>
            <h1>{title || '诉请金额'}</h1>
            <p className="muted claims-subtitle">
              按分组维护本金、利息、违约金、费用与抵扣；金额全程整数分，出处可回溯到原件页码。
            </p>
          </div>
        </div>
        {archived ? (
          <p role="status" className="claims-notice">本案已归档：诉请金额只读，查看与出处预览仍可用。</p>
        ) : null}
        {workspace.loadError ? (
          <p role="alert" className="claims-error">
            刷新失败：{workspace.loadError}
            <button type="button" onClick={() => void workspace.reload()}>重试刷新</button>
          </p>
        ) : null}
        {overviewError ? (
          <p role="alert" className="claims-error">
            {overviewError}
            <button type="button" onClick={() => setOverviewError('')}>关闭</button>
          </p>
        ) : null}
        <ClaimsPanel
          snapshot={snapshot}
          onCommand={workspace.run}
          onOpenSource={openSource}
          searchCandidates={candidates}
          readOnly={archived}
          caseId={caseId}
          caseTitle={title}
        />
      </main>
      {preview ? (
        <SourcePreview key={`${preview.versionId}-${preview.nonce}`} defaultOpen
          caseId={caseId} versionId={preview.versionId}
          name={preview.name} page={preview.page} />
      ) : null}
    </div>
  );
}
