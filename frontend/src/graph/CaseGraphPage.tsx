/**
 * 真实案件「关系图」页签：案件 UUID 来自路由，数据走 /api/v1/cases/{id}/graphs。
 *
 * - 出处点击走现有 SourcePreview：后端 ResolvedLabelOut 给出 file_version_id + page，
 *   不再使用原型的合成原文弹窗；
 * - 删除前的影响面、归档开关都走真实案件接口；
 * - 证据目录导入嵌入同一页面的目录面板（也单独出现在「证据目录」页签）。
 */
import { useCallback, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router';
import { describeError } from '../api/client';
import { evidenceApi } from '../api/endpoints';
import { CaseNavBar } from '../components/CaseNavBar';
import { SourcePreview } from '../components/SourcePreview';
import { EvidenceImportPanel } from './EvidenceImportPanel';
import { GraphWorkspace } from './GraphWorkspace';
import { createHttpGraphSource, createSourceSearch } from './httpExecutor';
import type { GraphSource } from './types';
import { useGraphWorkspace } from './useGraphWorkspace';
import './graph.css';

interface PreviewTarget {
  versionId: string;
  name: string;
  page?: number;
  segmentId?: string;
  /** 同一出处再次点击时用 nonce 重新挂载预览。 */
  nonce: number;
}

export default function CaseGraphPage() {
  const { caseId = '' } = useParams();
  return <CaseGraph key={caseId} caseId={caseId} />;
}

function CaseGraph({ caseId }: { caseId: string }) {
  const source = useMemo(() => createHttpGraphSource(caseId), [caseId]);
  const workspace = useGraphWorkspace({ caseId, source });
  const searchSources = useMemo(() => createSourceSearch(caseId), [caseId]);
  const [preview, setPreview] = useState<PreviewTarget | null>(null);
  const { snapshot: ws, rules } = workspace;

  const openSource = useCallback((item: GraphSource) => {
    if (!ws) return;
    const label = ws.labels[item.id];
    if (!label?.file_version_id) {
      workspace.setError('该出处没有可定位的文件版本（可能指向已删除的材料或标注）。');
      return;
    }
    setPreview((current) => ({
      versionId: label.file_version_id!,
      name: label.material_title || '出处材料',
      page: label.page ?? undefined,
      segmentId: item.segment_id ?? undefined,
      nonce: (current?.nonce ?? 0) + 1,
    }));
  }, [ws, workspace]);

  if (!ws || !rules) {
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
    <>
      <GraphWorkspace
        caseId={caseId}
        snapshot={ws}
        rules={rules}
        run={workspace.run}
        undo={workspace.undo}
        notice={workspace.notice}
        error={workspace.error}
        onDismissNotice={workspace.dismissNotice}
        onDismissError={workspace.dismissError}
        onLocalError={workspace.setError}
        onLocalNotice={workspace.setNotice}
        refreshError={workspace.loadError}
        onRetryRefresh={() => void workspace.reload()}
        onReload={workspace.reload}
        section="关系图"
        headerAction={<Link to="/">← 案件工作表</Link>}
        caseNav={<CaseNavBar caseId={caseId} active="graph" />}
        sourceOptions={searchSources}
        onOpenSource={openSource}
        onOpenSourcePreview={(info) => {
          setPreview((current) => ({
            versionId: info.fileVersionId,
            name: info.title,
            page: info.page,
            segmentId: info.segmentId,
            nonce: (current?.nonce ?? 0) + 1,
          }));
        }}
        loadImpact={(target) => workspace.impact(target)}
        exportHref={evidenceApi.exportUrl(caseId)}
        bundleHref={evidenceApi.bundleUrl(caseId)}
        evidenceImport={(
          <EvidenceImportPanel caseId={caseId} ws={ws} readOnly={ws.archived}
            onImported={() => { void workspace.reload(); }} />
        )}
        onArchive={async (next) => {
          try {
            await workspace.archive(next);
            workspace.setNotice(next ? '已归档：全部写命令被拒，查看与导出可用。' : '已取消归档，恢复可写。');
          } catch (cause) {
            // 归档是案件级 CAS：冲突时保留服务端说明并刷新一次工作区。
            workspace.setError(describeError(cause) || '归档操作失败');
            await workspace.reload();
          }
        }}
      />
      {preview ? (
        <SourcePreview key={`${preview.versionId}-${preview.nonce}`} defaultOpen
          caseId={caseId} versionId={preview.versionId}
          name={preview.name} page={preview.page}
          segmentId={preview.segmentId} archived={ws.archived} />
      ) : null}
    </>
  );
}
