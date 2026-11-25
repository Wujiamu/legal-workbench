/**
 * 图谱防漏审计抽屉：围绕当前关系图从「覆盖缺口」「孤儿机构」「强指纹碰撞」三个维度检视漏项。
 *
 * 原生 <dialog>（showModal）+ 原生 <table> + 语义化 tablist；不依赖外部图表或抽屉库。
 */
import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { describeError } from '../api/client';
import { graphAssistApi } from '../api/endpoints';
import type { GraphAuditOut, Schemas } from '../api/endpoints';
import { COVERAGE_STATUSES } from '../components/CoverageMatrix';

type AuditTabKey = 'coverage' | 'orphans' | 'bridges';

const IDENTIFIER_KIND_LABEL: Record<Schemas['IdentifierBridge']['kind'], string> = {
  cn_mobile: '手机号',
  uscc: '统一社会信用代码',
  bank_account: '银行账号',
  email: '电子邮箱',
};

const GAP_STATUS_LABEL: Record<string, string> = {
  referenced: '已入图/已引用',
  parsed_unused: '已解析未引用',
  partial_or_failed: 'OCR告警/失败',
  pending_or_unsupported: '未解析/不支持',
};

export interface GraphAuditDrawerProps {
  open: boolean;
  onClose: () => void;
  caseId: string;
  graphId: string;
  readOnly: boolean;
  onOpenSourcePreview: (info: {
    fileVersionId: string;
    title: string;
    page?: number;
    segmentId?: string;
  }) => void;
  onAddOrphanEntity: (name: string, segmentId?: string) => Promise<void>;
  onCreateAffiliateRelation: (prefill: {
    fromEntityId?: string;
    toEntityId?: string;
    label: string;
    segmentIds: string[];
  }) => void;
  refreshToken?: number;
}

export function GraphAuditDrawer(props: GraphAuditDrawerProps) {
  if (!props.open) return null;
  return <GraphAuditDrawerInner key={`${props.caseId}:${props.graphId}`} {...props} />;
}

function GraphAuditDrawerInner({
  open,
  onClose,
  caseId,
  graphId,
  readOnly,
  onOpenSourcePreview,
  onAddOrphanEntity,
  onCreateAffiliateRelation,
  refreshToken = 0,
}: GraphAuditDrawerProps) {
  const baseId = useId();
  const headingId = `${baseId}-heading`;
  const dialogRef = useRef<HTMLDialogElement>(null);
  const mountedRef = useRef(true);
  const scopeKey = `${caseId}:${graphId}`;
  const activeScopeRef = useRef(scopeKey);
  activeScopeRef.current = scopeKey;
  const tabRefs = useRef<Record<AuditTabKey, HTMLButtonElement | null>>({
    coverage: null,
    orphans: null,
    bridges: null,
  });

  const [activeTab, setActiveTab] = useState<AuditTabKey>('coverage');
  const [localRefresh, setLocalRefresh] = useState(0);
  const [addingOrg, setAddingOrg] = useState<string | null>(null);
  const [actionError, setActionError] = useState('');
  const [actionNotice, setActionNotice] = useState('');

  const [auditState, setAuditState] = useState<{
    scopeKey: string;
    data: GraphAuditOut | null;
    loading: boolean;
    error: string;
  }>({ scopeKey, data: null, loading: false, error: '' });

  useEffect(() => {
    mountedRef.current = true;
    const dialog = dialogRef.current;
    if (open && dialog && !dialog.open) {
      dialog.showModal();
    }
    return () => {
      mountedRef.current = false;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    const { signal } = controller;
    setActionError('');
    setAuditState((prev) => (prev.scopeKey === scopeKey
      ? { ...prev, loading: true, error: '' }
      : { scopeKey, data: null, loading: true, error: '' }));

    graphAssistApi.audit(caseId, graphId ? { graph_id: graphId } : undefined, signal)
      .then((result) => {
        if (signal.aborted) return;
        setAuditState({ scopeKey, data: result, loading: false, error: '' });
      })
      .catch((cause: unknown) => {
        if (signal.aborted) return;
        setAuditState((prev) => ({
          scopeKey,
          data: prev.scopeKey === scopeKey ? prev.data : null,
          loading: false,
          error: describeError(cause),
        }));
      });

    return () => controller.abort();
  }, [open, caseId, graphId, scopeKey, refreshToken, localRefresh]);

  if (!open) return null;

  const audit = auditState.scopeKey === scopeKey ? auditState.data : null;
  const loading = auditState.scopeKey === scopeKey ? auditState.loading : true;
  const loadError = auditState.scopeKey === scopeKey ? auditState.error : '';

  const gapMaterials = audit
    ? audit.coverage.materials.filter(
      (item) => item.status === 'parsed_unused' || item.status === 'partial_or_failed',
    )
    : [];

  const totalCount = audit ? (audit.coverage.totals.total ?? audit.coverage.materials.length) : 0;
  const referencedCount = audit ? (audit.coverage.totals.referenced ?? 0) : 0;
  const unusedCount = audit ? (audit.coverage.totals.parsed_unused ?? 0) : 0;
  const warnCount = audit ? (audit.coverage.totals.partial_or_failed ?? 0) : 0;
  const pendingCount = audit ? (audit.coverage.totals.pending_or_unsupported ?? 0) : 0;

  const cellMap = new Map<string, number>();
  if (audit) {
    for (const cell of audit.coverage.cells) {
      cellMap.set(`${cell.category}\0${cell.status}`, cell.count);
    }
  }

  const tabs: { key: AuditTabKey; label: string }[] = [
    { key: 'coverage', label: `覆盖缺口（${gapMaterials.length}）` },
    { key: 'orphans', label: `孤儿机构（${audit?.orphan_orgs.length ?? 0}）` },
    { key: 'bridges', label: `强指纹碰撞（${audit?.identifier_bridges.length ?? 0}）` },
  ];

  function handleTabKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const order: AuditTabKey[] = ['coverage', 'orphans', 'bridges'];
    const currentIndex = order.indexOf(activeTab);
    let nextIndex = -1;
    if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % order.length;
    else if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + order.length) % order.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = order.length - 1;
    if (nextIndex >= 0) {
      event.preventDefault();
      const nextKey = order[nextIndex];
      setActiveTab(nextKey);
      tabRefs.current[nextKey]?.focus();
    }
  }

  async function handleAddOrphan(name: string, segmentId?: string) {
    if (readOnly || addingOrg !== null) return;
    const startScope = scopeKey;
    setAddingOrg(name);
    setActionError('');
    setActionNotice('');
    try {
      await onAddOrphanEntity(name, segmentId);
      if (!mountedRef.current || activeScopeRef.current !== startScope) return;
      setActionNotice(`已将「${name}」加入案件主体`);
      setLocalRefresh((value) => value + 1);
    } catch (cause) {
      if (!mountedRef.current || activeScopeRef.current !== startScope) return;
      setActionError(describeError(cause));
    } finally {
      if (mountedRef.current && activeScopeRef.current === startScope) {
        setAddingOrg(null);
      }
    }
  }

  return (
    <dialog
      ref={dialogRef}
      className="graph-snapshot-dialog graph-audit-dialog"
      aria-labelledby={headingId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <style>{`
        .graph-audit-body {
          padding: 14px 18px 20px;
          overflow: auto;
          max-height: calc(94vh - 78px);
          display: grid;
          grid-template-columns: minmax(0, 1fr);
          gap: 12px;
          align-content: start;
        }
        .graph-audit-tablist {
          display: flex;
          gap: 6px;
          overflow-x: auto;
          border-bottom: 1px solid var(--line);
          padding-bottom: 4px;
        }
        .graph-audit-tab {
          position: relative;
          flex: none;
          border: 1px solid var(--line);
          border-radius: 6px;
          background: var(--surface);
          color: var(--muted);
          padding: 7px 12px;
          font: inherit;
          font-size: 13px;
          cursor: pointer;
        }
        .graph-audit-tab[aria-selected="true"] {
          color: var(--accent);
          font-weight: 650;
          border-color: var(--accent);
        }
        .graph-audit-tab[aria-selected="true"]::after {
          content: "";
          position: absolute;
          inset: auto 4px 0;
          border-block-end: 3px solid var(--accent);
        }
        .graph-audit-summary {
          border: 1px solid var(--line);
          border-radius: 8px;
          background: var(--warm);
          padding: 10px 12px;
          font-size: 13px;
          margin: 0;
        }
        .graph-audit-table {
          width: 100%;
          border-collapse: collapse;
          font-size: 12.5px;
          text-align: left;
        }
        .graph-audit-table th,
        .graph-audit-table td {
          padding: 7px 10px;
          border-bottom: 1px solid var(--line);
        }
        .graph-audit-table thead th {
          background: var(--warm);
          color: var(--muted);
          font-weight: 650;
          white-space: nowrap;
        }
        .graph-audit-cards {
          list-style: none;
          margin: 0;
          padding: 0;
          display: grid;
          gap: 12px;
        }
        .graph-audit-card {
          border: 1px solid var(--line);
          border-radius: 10px;
          background: var(--surface);
          padding: 12px 14px;
          display: grid;
          grid-template-columns: minmax(0, 1fr);
          gap: 8px;
          min-width: 0;
        }
        .graph-audit-card-head {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          gap: 10px;
          flex-wrap: wrap;
        }
        .graph-audit-card-head > div {
          min-width: 0;
          flex: 1 1 220px;
        }
        .graph-audit-card-head h3 {
          margin: 0;
          font-size: 15px;
          color: var(--ink);
          overflow-wrap: anywhere;
        }
        .graph-audit-samples {
          list-style: none;
          margin: 0;
          padding: 0;
          display: grid;
          grid-template-columns: minmax(0, 1fr);
          gap: 6px;
        }
        .graph-audit-sample {
          border: 1px solid var(--line);
          border-radius: 8px;
          background: var(--surface-canvas);
          padding: 8px 10px;
          font-size: 12.5px;
          display: grid;
          grid-template-columns: minmax(0, 1fr);
          gap: 4px;
          min-width: 0;
        }
        .graph-audit-bridge-grid {
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 10px;
        }
        @media (max-width: 760px) {
          .graph-audit-bridge-grid {
            grid-template-columns: minmax(0, 1fr);
          }
        }
        .graph-audit-excerpt {
          margin: 0;
          line-height: 1.55;
          white-space: pre-wrap;
          overflow-wrap: anywhere;
        }
      `}</style>

      <header className="graph-snapshot-head">
        <div>
          <h2 id={headingId}>防漏审计</h2>
          <p className="muted">核对全案材料覆盖缺口、尚未入图的高频机构，以及跨材料重复出现的强指纹。</p>
        </div>
        <div className="graph-form-actions">
          <button type="button" disabled={loading} onClick={() => setLocalRefresh((value) => value + 1)}>
            {loading ? '扫描中…' : '重新扫描'}
          </button>
          <button type="button" onClick={onClose}>关闭</button>
        </div>
      </header>

      <div className="graph-audit-body">
        {actionNotice ? <p role="status" className="graph-notice-inline">{actionNotice}</p> : null}
        {actionError ? <p role="alert" className="graph-error-inline">错误：{actionError}</p> : null}
        {loadError ? (
          <p role="alert" className="graph-error-inline">
            错误：{loadError}{' '}
            <button type="button" onClick={() => setLocalRefresh((value) => value + 1)}>重试</button>
          </p>
        ) : null}

        <div
          className="graph-audit-tablist"
          role="tablist"
          aria-label="防漏审计维度"
          onKeyDown={handleTabKeyDown}
        >
          {tabs.map((tab) => (
            <button
              key={tab.key}
              ref={(element) => { tabRefs.current[tab.key] = element; }}
              type="button"
              role="tab"
              id={`${baseId}-tab-${tab.key}`}
              className="graph-audit-tab"
              aria-selected={activeTab === tab.key}
              tabIndex={activeTab === tab.key ? 0 : -1}
              onClick={() => setActiveTab(tab.key)}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {loading && !audit ? <p role="status" className="muted">正在加载防漏审计结果…</p> : null}

        {audit ? (
          <>
            <div
              role="tabpanel"
              id={`${baseId}-panel-coverage`}
              aria-labelledby={`${baseId}-tab-coverage`}
              hidden={activeTab !== 'coverage'}
            >
              <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 12, minWidth: 0 }}>
                <p className="graph-audit-summary">
                  共 {totalCount} 份 · 已引用 {referencedCount} · 未引用 {unusedCount} · 异常/告警 {warnCount} · 待解析 {pendingCount}
                </p>

                {audit.coverage.categories.length > 0 ? (
                  <div tabIndex={0} role="region" aria-label="按分类汇总覆盖情况" className="table-scroll">
                    <table className="graph-audit-table">
                      <caption className="sr-only">案卷分类与引用状态汇总表</caption>
                      <thead>
                        <tr>
                          <th scope="col">分类</th>
                          {COVERAGE_STATUSES.map((status) => (
                            <th key={status.key} scope="col">{status.label}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {audit.coverage.categories.map((category) => (
                          <tr key={category}>
                            <th scope="row">{category}</th>
                            {COVERAGE_STATUSES.map((status) => (
                              <td key={status.key}>{cellMap.get(`${category}\0${status.key}`) ?? 0}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : null}

                {gapMaterials.length === 0 ? (
                  <p className="muted">当前没有已解析未引用或 OCR 告警/失败的材料。</p>
                ) : (
                  <div tabIndex={0} role="region" aria-label="未引用及异常材料明细" className="table-scroll">
                    <table className="graph-audit-table">
                      <caption className="sr-only">需要关注的覆盖缺口材料</caption>
                      <thead>
                        <tr>
                          <th scope="col">材料名称</th>
                          <th scope="col">分类</th>
                          <th scope="col">状态</th>
                          <th scope="col">解析覆盖</th>
                          <th scope="col">操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {gapMaterials.map((item) => (
                          <tr key={item.material_id}>
                            <th scope="row">{item.display_name}</th>
                            <td>{item.category}</td>
                            <td>
                              <span className={`graph-badge ${item.status === 'partial_or_failed' ? 'pending' : 'muted'}`}>
                                {GAP_STATUS_LABEL[item.status] ?? item.status}
                              </span>
                            </td>
                            <td>
                              完整 {item.coverage.complete} · 部分 {item.coverage.partial} · 失败 {item.coverage.failed}
                            </td>
                            <td>
                              {item.file_version_id ? (
                                <button
                                  type="button"
                                  onClick={() => onOpenSourcePreview({
                                    fileVersionId: item.file_version_id!,
                                    title: item.display_name,
                                  })}
                                >
                                  查看原文
                                </button>
                              ) : (
                                <span className="muted">无文件版本</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>

            <div
              role="tabpanel"
              id={`${baseId}-panel-orphans`}
              aria-labelledby={`${baseId}-tab-orphans`}
              hidden={activeTab !== 'orphans'}
            >
              {audit.orphan_orgs.length === 0 ? (
                <p className="muted">未发现尚未入图的重复出现机构。</p>
              ) : (
                <ul className="graph-audit-cards">
                  {audit.orphan_orgs.map((org) => (
                    <li key={org.name} className="graph-audit-card">
                      <div className="graph-audit-card-head">
                        <div>
                          <h3>{org.name}</h3>
                          <p className="muted" style={{ margin: '3px 0 0', fontSize: 12 }}>
                            提及 {org.mention_count} 次 · 涉及 {org.material_count} 份材料
                          </p>
                        </div>
                        <button
                          type="button"
                          disabled={readOnly || addingOrg !== null}
                          onClick={() => void handleAddOrphan(org.name, org.sample_segments[0]?.segment_id)}
                        >
                          {addingOrg === org.name ? '添加中…' : '添加为案件主体'}
                        </button>
                      </div>
                      {org.sample_segments.length > 0 ? (
                        <ul className="graph-audit-samples">
                          {org.sample_segments.map((sample) => (
                            <li key={sample.segment_id} className="graph-audit-sample">
                              <div className="graph-audit-card-head">
                                <strong>
                                  {sample.material_title}
                                  {sample.page ? ` · 第 ${sample.page} 页` : ''}
                                </strong>
                                <button
                                  type="button"
                                  onClick={() => onOpenSourcePreview({
                                    fileVersionId: sample.file_version_id,
                                    title: sample.material_title,
                                    page: sample.page ?? undefined,
                                    segmentId: sample.segment_id,
                                  })}
                                >
                                  查看原文
                                </button>
                              </div>
                              <p className="graph-audit-excerpt">{sample.excerpt}</p>
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div
              role="tabpanel"
              id={`${baseId}-panel-bridges`}
              aria-labelledby={`${baseId}-tab-bridges`}
              hidden={activeTab !== 'bridges'}
            >
              {audit.identifier_bridges.length === 0 ? (
                <p className="muted">未发现跨材料或跨主体的强指纹碰撞。</p>
              ) : (
                <ul className="graph-audit-cards">
                  {audit.identifier_bridges.map((bridge) => {
                    const kindLabel = IDENTIFIER_KIND_LABEL[bridge.kind] ?? bridge.kind;
                    const relationLabel = `共用${kindLabel} ${bridge.value}`;
                    return (
                      <li key={`${bridge.kind}:${bridge.value}`} className="graph-audit-card">
                        <div className="graph-audit-card-head">
                          <div>
                            <h3>{kindLabel}：{bridge.value}</h3>
                            <p className="muted" style={{ margin: '3px 0 0', fontSize: 12 }}>
                              跨 {bridge.material_count} 份材料 · {bridge.occurrences.length} 处出现
                            </p>
                          </div>
                          <button
                            type="button"
                            disabled={readOnly}
                            onClick={() => onCreateAffiliateRelation({
                              fromEntityId: bridge.entity_ids.length >= 2 ? bridge.entity_ids[0] : undefined,
                              toEntityId: bridge.entity_ids.length >= 2 ? bridge.entity_ids[1] : undefined,
                              label: relationLabel,
                              segmentIds: Array.from(
                                new Set(bridge.occurrences.map((occ) => occ.segment_id)),
                              ),
                            })}
                          >
                            创建关联关系
                          </button>
                        </div>

                        <div className="graph-audit-bridge-grid">
                          {bridge.occurrences.map((occ) => (
                            <article key={occ.segment_id} className="graph-audit-sample">
                              <div className="graph-audit-card-head">
                                <strong>
                                  {occ.material_title}
                                  {occ.page ? ` · 第 ${occ.page} 页` : ''}
                                </strong>
                                <button
                                  type="button"
                                  onClick={() => onOpenSourcePreview({
                                    fileVersionId: occ.file_version_id,
                                    title: occ.material_title,
                                    page: occ.page ?? undefined,
                                    segmentId: occ.segment_id,
                                  })}
                                >
                                  查看原文
                                </button>
                              </div>
                              <p className="graph-audit-excerpt">{occ.excerpt}</p>
                              <p className="muted" style={{ margin: 0, fontSize: 11.5 }}>
                                {occ.matched_entity_names.length > 0
                                  ? `匹配主体：${occ.matched_entity_names.join('、')}`
                                  : '未匹配到已有主体'}
                              </p>
                            </article>
                          ))}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </>
        ) : null}
      </div>
    </dialog>
  );
}
