/**
 * 诉请金额面板：分组导航、全案总计、组内瀑布图与明细、Agent 待确认区。
 * 所有金额来自执行器的整数分快照；瀑布图末柱与明细小计使用同一个值。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { chartsApi } from '../api/endpoints';
import type { ChartPreflightOut } from '../api/endpoints';
import { ChartExportDialog } from '../components/ChartExportDialog';
import type {
  ApprovedIssue, ChartExportFormat, ChartExportSettings,
} from '../components/ChartExportDialog';
import {
  buildClaimsSvg, claimsExportFileName, downloadPngFile, downloadSvgFile, downloadTextFile,
} from './exportClaimsSvg';
import type { ClaimsExportableItem } from './exportClaimsSvg';
import { GroupDialog } from './GroupDialog';
import { ItemDialog } from './ItemDialog';
import { formatMoney, signedCents, sumCents } from './money';
import { SourceDialog } from './SourceDialog';
import { WaterfallChart } from './WaterfallChart';
import {
  CATEGORY_LABELS, CURRENCIES, CURRENCY_LABELS,
} from './types';
import type {
  AmountCandidateSearch, ClaimCategory, ClaimCommand, ClaimCommandRunner, ClaimGroup, ClaimItem,
  ClaimSnapshot, ClaimSource, CommandResult, Currency,
} from './types';

const CLAIM_CATEGORY_SET = new Set<string>([
  'principal', 'interest', 'liquidated_damages', 'expense', 'deduction', 'other',
]);

function toClaimCategory(kind: string): ClaimCategory {
  return CLAIM_CATEGORY_SET.has(kind) ? (kind as ClaimCategory) : 'other';
}

function toCurrency(code: string | null | undefined, fallback: Currency): Currency {
  return code && (CURRENCIES as readonly string[]).includes(code) ? (code as Currency) : fallback;
}

interface DeleteTarget {
  kind: 'group' | 'item';
  id: string;
  title: string;
  itemCount?: number;
}

function timeLabel(iso: string): string {
  return new Date(iso).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

export function ClaimsPanel({
  snapshot, onCommand, onOpenSource, searchCandidates, readOnly = false, caseId, caseTitle,
}: {
  snapshot: ClaimSnapshot;
  onCommand: ClaimCommandRunner;
  /** 归档案件：全部写入口禁用；查看与出处预览保持可用。 */
  readOnly?: boolean;
  /** 出处点击：默认打开模拟原文；真实案件页传入 SourcePreview 打开逻辑。 */
  onOpenSource?: (source: ClaimSource) => void;
  /** 真实案件页：GET 本案精确值；缺省用合成候选。 */
  searchCandidates?: AmountCandidateSearch;
  /** 真实案件页：开启一源三态图表与引用索引导出。 */
  caseId?: string;
  /** 真实案件名称（用于导出图表副标题）。 */
  caseTitle?: string;
}) {
  const [selectedGroupId, setSelectedGroupId] = useState(snapshot.groups[0]?.id ?? '');
  const [groupEditor, setGroupEditor] = useState<{ group?: ClaimGroup } | null>(null);
  const [itemEditor, setItemEditor] = useState<{ item?: ClaimItem } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);
  const [source, setSource] = useState<ClaimSource | null>(null);
  const [error, setError] = useState('');
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [locatedItemId, setLocatedItemId] = useState<string | null>(null);

  useEffect(() => {
    if (!snapshot.groups.some((group) => group.id === selectedGroupId)) {
      setSelectedGroupId(snapshot.groups[0]?.id ?? '');
    }
  }, [snapshot.groups, selectedGroupId]);

  const group = snapshot.groups.find((entry) => entry.id === selectedGroupId) ?? snapshot.groups[0] ?? null;
  const groupId = group?.id ?? '';
  const items = useMemo(
    () => (group ? snapshot.items.filter((item) => item.group_id === group.id) : []),
    [snapshot.items, group],
  );
  const pendingAdds = useMemo(
    () => snapshot.pending_changes.filter((change) => change.command === 'add_item' && change.group_id === group?.id),
    [snapshot.pending_changes, group],
  );
  const pendingUpdates = useMemo(
    () => new Map(snapshot.pending_changes
      .filter((change) => change.command === 'update_item')
      .map((change) => [change.after.id, change])),
    [snapshot.pending_changes],
  );
  const subtotal = group ? snapshot.group_subtotals[group.id] ?? 0 : 0;

  const handleExportPreflight = useCallback(
    (settings: ChartExportSettings, signal: AbortSignal): Promise<ChartPreflightOut> => {
      if (!caseId || !groupId) {
        return Promise.reject(new Error('缺少案件或分组信息'));
      }
      return chartsApi.preflight(
        caseId,
        {
          chart_type: 'claims',
          target_id: groupId,
          profile: settings.profile,
          snapshot_id: null,
        },
        signal,
      );
    },
    [caseId, groupId],
  );

  async function handleExportDownload(
    format: ChartExportFormat,
    settings: ChartExportSettings,
    _preview: ChartPreflightOut | null,
    approved: ApprovedIssue[],
    _showWorkingLabels: boolean,
    signal?: AbortSignal,
  ) {
    if (!caseId || !group) return;
    const resolvedCaseTitle = caseTitle || snapshot.case_title || undefined;

    if (settings.profile === 'working') {
      if (format === 'md' || format === 'csv') return;
      const { svg, width, height } = buildClaimsSvg({
        title: group.title,
        caseTitle: resolvedCaseTitle,
        currency: group.currency,
        items,
        subtotalCents: subtotal,
      });
      if (signal?.aborted) return;
      if (format === 'svg') {
        downloadSvgFile(claimsExportFileName(group.title, 'svg'), svg);
      } else {
        await downloadPngFile(claimsExportFileName(group.title, 'png'), svg, width, height);
      }
      return;
    }

    if (format === 'md' || format === 'csv') {
      const text = await chartsApi.citationTableText(caseId, {
        chart_type: 'claims',
        target_id: group.id,
        profile: settings.profile,
        snapshot_id: null,
        format,
        waivers: approved,
      }, signal);
      if (signal?.aborted) return;
      downloadTextFile(
        claimsExportFileName(group.title, format),
        text,
        format === 'md' ? 'text/markdown;charset=utf-8' : 'text/csv;charset=utf-8',
      );
      return;
    }

    const table = await chartsApi.citationTableJson(caseId, {
      chart_type: 'claims',
      target_id: group.id,
      profile: settings.profile,
      snapshot_id: null,
      waivers: approved,
    }, signal);
    if (signal?.aborted) return;
    const itemsById = new Map(items.map((item) => [item.id, item]));
    const verifiedItems: ClaimsExportableItem[] = table.assertions.map((assertion) => {
      const local = itemsById.get(assertion.target_id);
      const baseTitle = local?.title ?? (assertion.display.split(' · ')[0] || '诉请项');
      const titleWithHeld = assertion.held_for_proof && !baseTitle.includes('待举证主张')
        ? `${baseTitle}（待举证主张）`
        : baseTitle;
      return {
        id: assertion.target_id,
        title: titleWithHeld,
        category: toClaimCategory(assertion.kind),
        amount_cents: assertion.amount_cents ?? local?.amount_cents ?? 0,
      };
    });
    const verifiedSubtotal = sumCents(
      verifiedItems.map((item) => signedCents(item.category, item.amount_cents)),
    );
    const verifiedCurrency = toCurrency(table.chart_meta.currency, group.currency);
    const verifiedTitle = table.chart_meta.title || group.title;
    const references = table.rows.map((row) => ({
      targetId: row.target_id,
      index: row.index,
      label: [row.evidence_number, row.location, row.evidence_name].filter(Boolean).join(' '),
    }));
    const noticeParts: string[] = [];
    if (table.waivers.length > 0) {
      noticeParts.push(`含 ${table.waivers.length} 项人工确认例外`);
    }
    if (table.chart_meta.contains_suspected) {
      noticeParts.push('含待查项');
    }
    const { svg, width, height } = buildClaimsSvg({
      title: verifiedTitle,
      caseTitle: resolvedCaseTitle,
      currency: verifiedCurrency,
      items: verifiedItems,
      subtotalCents: verifiedSubtotal,
      projection: {
        profile: settings.profile,
        references,
        frozenAt: table.chart_meta.frozen_at ?? table.chart_meta.generated_at,
        digest: table.chart_meta.digest ?? undefined,
        notice: noticeParts.length > 0 ? noticeParts.join(' · ') : undefined,
      },
    });
    if (format === 'svg') {
      downloadSvgFile(claimsExportFileName(verifiedTitle, 'svg'), svg);
    } else {
      await downloadPngFile(claimsExportFileName(verifiedTitle, 'png'), svg, width, height);
    }
  }

  function handleLocateTarget(targetId: string) {
    setExportOpen(false);
    setLocatedItemId(targetId);
    const rows = document.querySelectorAll<HTMLElement>('[data-item-id]');
    const targetRow = Array.from(rows).find((row) => row.getAttribute('data-item-id') === targetId);
    targetRow?.scrollIntoView?.({ block: 'center' });
  }

  async function run(command: ClaimCommand): Promise<CommandResult> {
    const result = await onCommand(command);
    setError(result.ok ? '' : result.message ?? '操作失败');
    return result;
  }

  function openSource(next: ClaimSource) {
    if (onOpenSource) onOpenSource(next);
    else setSource(next);
  }

  async function confirmDelete() {
    if (!deleteTarget || deleteBusy) return;
    setDeleteBusy(true);
    try {
      const result = await run(deleteTarget.kind === 'group'
        ? { type: 'remove_group', group_id: deleteTarget.id }
        : { type: 'remove_item', item_id: deleteTarget.id });
      if (result.ok) setDeleteTarget(null);
    } finally {
      setDeleteBusy(false);
    }
  }

  return (
    <div className="claims-panel">
      <section className="claims-totals" aria-label="全案总计">
        <div className="claims-totals-head">
          <div>
            <h2>全案总计</h2>
            <p className="muted">按币种分别汇总；抵扣项按减项计入；整数分累加，不经过浮点。</p>
          </div>
          <span className="claims-seq">变更序号 seq={snapshot.seq}</span>
        </div>
        <div className="claims-total-figures">
          {snapshot.totals_by_currency.length === 0
            ? <p className="muted">暂无计入全案总计的分组。</p>
            : snapshot.totals_by_currency.map((total) => (
              <div key={total.currency} className="claims-total-figure">
                <span>{CURRENCY_LABELS[total.currency]} · {total.currency}</span>
                <strong data-testid={`total-${total.currency}`}>
                  {formatMoney(total.total_cents, total.currency)}
                </strong>
                <small>{total.included_group_ids.length} 组计入</small>
              </div>
            ))}
        </div>
        {snapshot.excluded_group_count > 0 ? (
          <p role="note" className="claims-excluded">
            另有 {snapshot.excluded_group_count} 组未计入全案总计（例如一审/二审请求重叠）。
            勾选分组头部的“计入全案总计”可对比重复加总。
          </p>
        ) : null}
        {snapshot.pending_count > 0 ? (
          <p role="note" className="claims-pending-note">
            含 {snapshot.pending_count} 条 Agent 待确认修改，确认前不计入小计与总计。
          </p>
        ) : null}
      </section>

      {error ? <p role="alert" className="claims-error">错误：{error}</p> : null}

      <div className="claims-layout">
        <aside className="claims-groups" aria-label="诉请分组">
          <div className="claims-groups-head">
            <h2>诉请分组</h2>
            <button type="button" disabled={readOnly} onClick={() => setGroupEditor({})}>＋ 新建分组</button>
          </div>
          {snapshot.groups.length === 0 ? (
            <p className="muted">还没有诉请分组。</p>
          ) : (
            <ul className="claims-group-list">
              {snapshot.groups.map((entry, index) => {
                const entrySubtotal = snapshot.group_subtotals[entry.id] ?? 0;
                const pendingCount = snapshot.pending_changes.filter((change) => change.group_id === entry.id).length;
                return (
                  <li key={entry.id} className={entry.id === group?.id ? 'is-selected' : undefined}>
                    <button type="button" className="claims-group-select"
                            aria-current={entry.id === group?.id ? 'true' : undefined}
                            onClick={() => setSelectedGroupId(entry.id)}>
                      <span className="claims-group-title">{entry.title}</span>
                      <span className="claims-group-sum">{formatMoney(entrySubtotal, entry.currency)}</span>
                      <span className="claims-group-meta">
                        {CURRENCY_LABELS[entry.currency]}
                        {entry.include_in_total ? '' : ' · 未计入总计'}
                      </span>
                      {pendingCount > 0
                        ? <span className="agent-badge agent-pending">待确认 {pendingCount}</span>
                        : null}
                    </button>
                    <div className="claims-group-move">
                      <button type="button" aria-label={`上移分组「${entry.title}」`} disabled={readOnly || index === 0}
                              onClick={() => { void run({ type: 'reorder_groups', group_id: entry.id, direction: 'up' }); }}>↑</button>
                      <button type="button" aria-label={`下移分组「${entry.title}」`} disabled={readOnly || index === snapshot.groups.length - 1}
                              onClick={() => { void run({ type: 'reorder_groups', group_id: entry.id, direction: 'down' }); }}>↓</button>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>

        <section className="claims-detail" aria-label="诉请明细">
          {!group ? (
            <p className="muted">新建分组后，可添加本金、利息、违约金、费用与抵扣项。</p>
          ) : (
            <>
              <header className="claims-group-head">
                <div className="claims-group-heading">
                  <h2>{group.title}</h2>
                  <p className="muted">
                    {CURRENCY_LABELS[group.currency]} · {group.note || '无备注'}
                  </p>
                </div>
                <div className="work-actions claims-group-actions">
                  <label className="claims-check">
                    <input type="checkbox" checked={group.include_in_total} disabled={readOnly}
                           onChange={(event) => { void run({
                             type: 'update_group', group_id: group.id,
                             include_in_total: event.target.checked,
                           }); }} />
                    计入全案总计
                  </label>
                  {caseId ? (
                    <button type="button" onClick={() => setExportOpen(true)}>导出图表…</button>
                  ) : null}
                  <button type="button" disabled={readOnly} onClick={() => setGroupEditor({ group })}>编辑分组</button>
                  <button type="button" className="claims-danger" disabled={readOnly}
                          onClick={() => setDeleteTarget({
                            kind: 'group', id: group.id, title: group.title, itemCount: items.length,
                          })}>删除分组</button>
                </div>
              </header>

              <WaterfallChart items={items} subtotalCents={subtotal}
                              currency={group.currency} groupTitle={group.title} />

              <div className="claims-table-head">
                <h3>金额明细</h3>
                <button type="button" disabled={readOnly} onClick={() => setItemEditor({})}>＋ 添加金额项</button>
              </div>
              {items.length === 0 && pendingAdds.length === 0 ? (
                <p className="muted claims-empty">
                  本组还没有金额项。组内小计 {formatMoney(subtotal, group.currency)}。
                </p>
              ) : (
                <div className="claims-table-scroll">
                  <table className="claims-table">
                    <caption className="sr-only">{group.title}的金额明细与组内小计</caption>
                    <thead>
                      <tr>
                        <th scope="col">金额项</th>
                        <th scope="col">类别</th>
                        <th scope="col">金额</th>
                        <th scope="col">计算说明</th>
                        <th scope="col">出处</th>
                        <th scope="col">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((item, index) => {
                        const pendingUpdate = pendingUpdates.get(item.id);
                        const deduction = item.category === 'deduction';
                        return (
                          <tr
                            key={item.id}
                            data-testid="claim-item-row"
                            data-item-id={item.id}
                            data-located={locatedItemId === item.id ? 'true' : undefined}
                          >
                            <th scope="row">
                              <span className="claims-item-title">{item.title}</span>
                              {item.review_state === 'accepted'
                                ? <span className="agent-badge agent-accepted">Agent 已确认</span>
                                : null}
                              {pendingUpdate
                                ? <span className="agent-badge agent-pending">待确认修改</span>
                                : null}
                            </th>
                            <td data-label="类别">
                              <span className={`claims-cat claims-cat-${item.category}`}>
                                {CATEGORY_LABELS[item.category]}
                              </span>
                            </td>
                            <td data-label="金额" className={deduction ? 'claims-amount is-deduction' : 'claims-amount'}>
                              <span>{deduction ? '−' : ''}{formatMoney(item.amount_cents, group.currency)}</span>
                              <small>{item.amount_cents.toLocaleString('en-US')} 分</small>
                            </td>
                            <td data-label="计算说明" className="claims-note">{item.calc_note || '—'}</td>
                            <td data-label="出处">
                              {item.source ? (
                                <button type="button" className="claims-source-link"
                                        onClick={() => openSource(item.source!)}>
                                  {item.source.material_title}
                                  {item.source.page ? ` · 第 ${item.source.page} 页` : ''}
                                </button>
                              ) : <span className="muted">未附出处</span>}
                            </td>
                            <td data-label="操作">
                              <div className="claims-row-actions">
                                <button type="button" aria-label={`上移「${item.title}」`}
                                        disabled={readOnly || index === 0}
                                        onClick={() => { void run({ type: 'reorder_items', item_id: item.id, direction: 'up' }); }}>↑</button>
                                <button type="button" aria-label={`下移「${item.title}」`}
                                        disabled={readOnly || index === items.length - 1}
                                        onClick={() => { void run({ type: 'reorder_items', item_id: item.id, direction: 'down' }); }}>↓</button>
                                <button type="button" aria-label={`编辑「${item.title}」`} disabled={readOnly}
                                        onClick={() => setItemEditor({ item })}>编辑</button>
                                <button type="button" className="claims-danger" aria-label={`删除「${item.title}」`}
                                        disabled={readOnly}
                                        onClick={() => setDeleteTarget({ kind: 'item', id: item.id, title: item.title })}>
                                  删除
                                </button>
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                      {pendingAdds.map((change) => (
                        <tr key={change.id} className="claims-row-pending" data-testid="claim-pending-row">
                          <th scope="row">
                            <span className="claims-item-title">{change.after.title}</span>
                            <span className="agent-badge agent-pending">Agent 待确认</span>
                          </th>
                          <td data-label="类别">
                            <span className={`claims-cat claims-cat-${change.after.category}`}>
                              {CATEGORY_LABELS[change.after.category]}
                            </span>
                          </td>
                          <td data-label="金额" className={change.after.category === 'deduction' ? 'claims-amount is-deduction' : 'claims-amount'}>
                            <span>{change.after.category === 'deduction' ? '−' : ''}{formatMoney(change.after.amount_cents, group.currency)}</span>
                            <small>待确认，不计入小计</small>
                          </td>
                          <td data-label="计算说明" className="claims-note">{change.after.calc_note || '—'}</td>
                          <td data-label="出处">
                            {change.after.source ? (
                              <button type="button" className="claims-source-link"
                                      onClick={() => openSource(change.after.source!)}>
                                {change.after.source.material_title}
                            {change.after.source.page ? ` · 第 ${change.after.source.page} 页` : ''}
                              </button>
                            ) : <span className="muted">未附出处</span>}
                          </td>
                          <td data-label="操作"><span className="muted">请在右侧确认或撤回</span></td>
                        </tr>
                      ))}
                    </tbody>
                    <tfoot>
                      <tr>
                        <th scope="row" colSpan={2}>组内小计（{items.length} 项，含抵扣减项）</th>
                        <td data-testid="group-subtotal" className={subtotal < 0 ? 'claims-amount is-deduction' : 'claims-amount'}>
                          {formatMoney(subtotal, group.currency)}
                        </td>
                        <td colSpan={3} className="muted">与瀑布图末柱使用同一整数分合计</td>
                      </tr>
                    </tfoot>
                  </table>
                </div>
              )}
            </>
          )}
        </section>

        <aside className="claims-review" aria-label="Agent 待确认修改">
          <h2>Agent 待确认修改</h2>
          {snapshot.pending_changes.length === 0 ? (
            <p className="muted">没有待确认的 Agent 修改。</p>
          ) : (
            <ul className="claims-review-list">
              {snapshot.pending_changes.map((change) => {
                const changeGroup = snapshot.groups.find((entry) => entry.id === change.group_id);
                const currency = changeGroup?.currency ?? 'CNY';
                return (
                  <li key={change.id} className="claims-review-item" data-testid="claim-review-item">
                    <header>
                      <strong>{change.command === 'add_item' ? '新增金额项' : '修改金额项'}</strong>
                      <span className="agent-badge agent-pending">待确认</span>
                      <span className="muted">{change.group_title} · {timeLabel(change.created_at)}</span>
                    </header>
                    <p className="claims-review-title">{change.after.title}</p>
                    <dl className="claims-diff">
                      {change.before ? (
                        <>
                          {change.before.title !== change.after.title ? (
                            <div><dt>名称</dt><dd>{change.before.title} → {change.after.title}</dd></div>
                          ) : null}
                          {change.before.category !== change.after.category ? (
                            <div><dt>类别</dt><dd>{CATEGORY_LABELS[change.before.category]} → {CATEGORY_LABELS[change.after.category]}</dd></div>
                          ) : null}
                          {change.before.amount_cents !== change.after.amount_cents ? (
                            <div><dt>金额</dt><dd>
                              {formatMoney(change.before.amount_cents, currency)} → {formatMoney(change.after.amount_cents, currency)}
                            </dd></div>
                          ) : null}
                          {change.before.calc_note !== change.after.calc_note ? (
                            <div><dt>计算说明</dt><dd>{change.before.calc_note || '—'} → {change.after.calc_note || '—'}</dd></div>
                          ) : null}
                        </>
                      ) : (
                        <>
                          <div><dt>类别</dt><dd>{CATEGORY_LABELS[change.after.category]}</dd></div>
                          <div><dt>金额</dt><dd>{formatMoney(change.after.amount_cents, currency)}</dd></div>
                          <div><dt>计算说明</dt><dd>{change.after.calc_note || '—'}</dd></div>
                        </>
                      )}
                      <div><dt>出处</dt><dd>
                        {change.after.source ? (
                          <button type="button" className="claims-source-link"
                                  onClick={() => openSource(change.after.source!)}>
                            {change.after.source.material_title}
                            {change.after.source.page ? ` · 第 ${change.after.source.page} 页` : ''}
                          </button>
                        ) : '未附出处'}
                      </dd></div>
                    </dl>
                    <div className="work-actions">
                      <button type="button" aria-label={`确认「${change.after.title}」`} disabled={readOnly}
                              onClick={() => { void run({ type: 'accept_change', change_id: change.id }); }}>确认</button>
                      <button type="button" aria-label={`撤回「${change.after.title}」`} disabled={readOnly}
                              onClick={() => { void run({ type: 'revert_change', change_id: change.id }); }}>撤回</button>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>
      </div>

      {groupEditor ? (
        <GroupDialog group={groupEditor.group} onSubmit={async (command) => {
          const result = await run(command);
          if (result.ok && result.snapshot && command.type === 'add_group') {
            const created = result.snapshot.groups.find((entry) => entry.title === command.title.trim());
            if (created) setSelectedGroupId(created.id);
          }
          return result;
        }} onClose={() => setGroupEditor(null)} />
      ) : null}
      {itemEditor && group ? (
        <ItemDialog item={itemEditor.item} groupId={group.id} currency={group.currency}
                    searchCandidates={searchCandidates}
                    onSubmit={run} onClose={() => setItemEditor(null)} />
      ) : null}
      {deleteTarget ? (
        <dialog className="work-dialog" aria-labelledby="claims-delete-heading"
                onCancel={(event) => { event.preventDefault(); setDeleteTarget(null); }}
                ref={(element) => { if (element && !element.open) element.showModal(); }}>
          <h2 id="claims-delete-heading">
            {deleteTarget.kind === 'group' ? '删除诉请分组？' : '删除金额项？'}
          </h2>
          <p>
            {deleteTarget.kind === 'group'
              ? `将删除「${deleteTarget.title}」及其 ${deleteTarget.itemCount ?? 0} 个金额项。`
              : `将删除「${deleteTarget.title}」。`}
            原型数据在内存中，刷新或重置演示即可恢复基线。
          </p>
          <div className="work-actions">
            <button type="button" onClick={() => setDeleteTarget(null)}>取消</button>
            <button type="button" className="claims-danger" disabled={deleteBusy}
                    onClick={() => void confirmDelete()}>
              {deleteBusy ? '删除中…' : '确认删除'}
            </button>
          </div>
        </dialog>
      ) : null}
      {source ? <SourceDialog source={source} onClose={() => setSource(null)} /> : null}
      {exportOpen && caseId && group ? (
        <ChartExportDialog
          key={group.id}
          title={group.title}
          readOnly={readOnly}
          allowWorkingLabels={false}
          onClose={() => setExportOpen(false)}
          onLocate={handleLocateTarget}
          preflight={handleExportPreflight}
          download={handleExportDownload}
        />
      ) : null}
    </div>
  );
}
