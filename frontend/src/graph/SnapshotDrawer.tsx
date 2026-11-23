/**
 * 版本快照抽屉：按单图冻结、只读查看（冻结时编号 + 现行编号旁注）、
 * 两版对比（含「快照 vs 当前工作稿」）、恢复为工作稿，以及只挂在快照上的客户同意追加留痕。
 * 冻结时若存在 Agent 待确认变更，先提示数量；继续冻结只包含已确认状态（D3）。
 */
import { useState } from 'react';
import type { FormEvent } from 'react';
import { GraphCanvas } from './GraphCanvas';
import {
  buildCanvasModel, buildSnapshotCanvasModel, buildSnapshotPayload, describeDiffEntry,
  diffHighlight, diffPayloads, pendingChangesInGraph,
} from './model';
import { channelLabel, credibilityRule } from './rules';
import type {
  CommandResult, ConsentAction, GraphCommandRunner, GraphRules, GraphSnapshot,
  RelationGraphSnapshot,
} from './types';

const ACTION_LABELS: Record<ConsentAction, string> = {
  confirm: '确认', amend: '更正', revoke: '撤回',
};

function localDateTime(): string {
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60_000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 16);
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  }).format(date);
}

/** 对比清单里的可信状态用中文标签，避免直接暴露内部值。 */
function fieldValue(rules: GraphRules, field: string, value: string): string {
  if (field === '可信状态') {
    return rules.credibility.find((rule) => rule.value === value)?.label ?? value;
  }
  return value;
}

function driftRows(ws: GraphSnapshot, snapshot: RelationGraphSnapshot) {
  const rows: { edge: string; frozen: string; current: string }[] = [];
  const seen = new Set<string>();
  const collect = (sources: { source_id: string; label: string }[], edgeLabel: string) => {
    for (const source of sources) {
      if (seen.has(source.source_id)) continue;
      const current = ws.labels[source.source_id]?.text;
      if (current && current !== source.label) {
        seen.add(source.source_id);
        rows.push({ edge: edgeLabel, frozen: source.label, current });
      }
    }
  };
  for (const edge of snapshot.payload.edges) {
    collect(edge.sources, edge.label ?? edge.kind);
  }
  for (const node of snapshot.payload.nodes) {
    collect(node.sources, node.name);
  }
  return rows;
}

export function SnapshotDrawer({
  open, onClose, ws, rules, graphId, readOnly, run, onExportSnapshot,
}: {
  open: boolean;
  onClose: () => void;
  ws: GraphSnapshot;
  rules: GraphRules;
  graphId: string;
  readOnly: boolean;
  run: GraphCommandRunner;
  onExportSnapshot?: (snapshotId: string) => void;
}) {
  const snapshots = ws.snapshots.filter((snapshot) => snapshot.graph_id === graphId)
    .sort((a, b) => b.version_no - a.version_no);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [freeze, setFreeze] = useState<{
    stage: string; custom: string; note: string; progress: string; ack: boolean;
  } | null>(null);
  const [consent, setConsent] = useState<{
    action: ConsentAction; parentId: string | null; consenter: string; consentedAt: string;
    channel: string; sourceDetail: string; materialId: string; note: string;
  } | null>(null);
  const [compare, setCompare] = useState<{ baseId: string; targetId: string } | null>(null);
  const [restoreConfirm, setRestoreConfirm] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);

  if (!open) return null;
  const pending = pendingChangesInGraph(ws, graphId);
  const current = snapshots.find((snapshot) => snapshot.id === selectedId) ?? snapshots[0] ?? null;
  const consents = current
    ? ws.consents.filter((record) => record.snapshot_id === current.id)
      .sort((a, b) => a.created_at.localeCompare(b.created_at))
    : [];
  const lastConsent = consents[consents.length - 1] ?? null;
  const drift = current ? driftRows(ws, current) : [];

  function apply(result: CommandResult) {
    if (result.ok) {
      setError('');
      setNotice(result.notice ?? '');
    } else {
      setError(result.message);
      setNotice('');
    }
    return result;
  }

  async function submitFreeze(event: FormEvent) {
    event.preventDefault();
    if (!freeze || busy) return;
    const stage = freeze.stage === '__custom' ? freeze.custom.trim() : freeze.stage;
    setBusy(true);
    try {
      const result = await run({
        type: 'create_snapshot', graph_id: graphId, stage_label: stage,
        note: freeze.note.trim() || null,
        progress_node_id: freeze.progress || null,
        acknowledge_pending: pending.length > 0 ? freeze.ack : undefined,
      });
      if (!result.ok) { setError(result.message); return; }
      setError('');
      setNotice(result.notice ?? '');
      const created = result.snapshot.snapshots.filter((snapshot) => snapshot.graph_id === graphId)
        .sort((a, b) => b.version_no - a.version_no)[0];
      if (created) setSelectedId(created.id);
      setFreeze(null);
    } finally {
      setBusy(false);
    }
  }

  function openConsent(action: ConsentAction, parentId: string | null) {
    const parent = consents.find((record) => record.id === parentId);
    setConsent({
      action, parentId,
      consenter: parent?.consenter ?? '',
      consentedAt: parent?.consented_at ?? localDateTime(),
      channel: parent?.channel ?? rules.consent_channels[0]?.value ?? 'meeting',
      sourceDetail: parent?.source_detail ?? '',
      materialId: parent?.evidence_material_id ?? '',
      note: parent?.note ?? '',
    });
  }

  async function submitConsent(event: FormEvent) {
    event.preventDefault();
    if (!consent || !current || busy) return;
    setBusy(true);
    try {
      const result = await run({
        type: 'add_snapshot_consent', snapshot_id: current.id, action: consent.action,
        parent_consent_id: consent.parentId,
        consenter: consent.consenter, consented_at: consent.consentedAt,
        channel: consent.channel, source_detail: consent.sourceDetail,
        evidence_material_id: consent.materialId || null,
        note: consent.note.trim() || null,
      });
      if (apply(result).ok) setConsent(null);
    } finally {
      setBusy(false);
    }
  }

  const base = compare ? snapshots.find((snapshot) => snapshot.id === compare.baseId) ?? snapshots[0] : null;
  const targetSnapshot = compare && compare.targetId !== 'current'
    ? snapshots.find((snapshot) => snapshot.id === compare.targetId) ?? null
    : null;
  const targetPayload = compare
    ? (targetSnapshot ? targetSnapshot.payload
      : buildSnapshotPayload(ws, graphId, { excludePending: false }))
    : null;
  const diff = base && targetPayload ? diffPayloads(base.payload, targetPayload) : null;
  const targetModel = compare
    ? (targetSnapshot ? buildSnapshotCanvasModel(targetSnapshot) : buildCanvasModel(ws, graphId))
    : null;

  return (
    <dialog className="graph-snapshot-dialog" aria-labelledby="snapshot-heading"
      ref={(element) => { if (element && !element.open) element.showModal(); }}
      onCancel={(event) => { event.preventDefault(); onClose(); }}>
      <header className="graph-snapshot-head">
        <div>
          <h2 id="snapshot-heading">版本快照与客户同意</h2>
          <p className="muted">快照按单图冻结、不可修改；客户同意只挂在快照上，更正与撤回以追加记录保留。</p>
        </div>
        <button type="button" onClick={onClose}>关闭</button>
      </header>

      {notice ? <p role="status" className="graph-notice-inline">{notice}</p> : null}
      {error ? <p role="alert" className="graph-error-inline">错误：{error}</p> : null}

      <div className="graph-snapshot-layout">
        <aside className="graph-snapshot-list" aria-label="快照列表">
          <div className="graph-snapshot-list-head">
            <h3>本图快照（{snapshots.length}）</h3>
            <button type="button" disabled={readOnly}
              onClick={() => {
                setFreeze({
                  stage: rules.snapshot_stages[1] ?? rules.snapshot_stages[0] ?? '',
                  custom: '', note: '', progress: '', ack: false,
                });
                setError('');
              }}>冻结当前图</button>
          </div>
          {snapshots.length === 0 ? <p className="muted">还没有快照。</p> : (
            <ul>
              {snapshots.map((snapshot) => {
                const snapshotConsents = ws.consents.filter((record) => record.snapshot_id === snapshot.id);
                return (
                  <li key={snapshot.id}>
                    <button type="button" aria-current={current?.id === snapshot.id}
                      className="graph-snapshot-item"
                      onClick={() => { setSelectedId(snapshot.id); setCompare(null); setRestoreConfirm(false); }}>
                      <span className="graph-snapshot-version">v{snapshot.version_no}</span>
                      <span>{snapshot.stage_label}</span>
                      <span className="muted">{formatTime(snapshot.created_at)}</span>
                      <span className={`graph-badge ${snapshotConsents.length > 0 ? 'ok' : 'muted'}`}>
                        {snapshotConsents.length > 0 ? `客户同意 ${snapshotConsents.length} 条` : '未登记同意'}
                      </span>
                      {snapshot.excluded_pending_count > 0 ? (
                        <span className="graph-badge pending">不含 {snapshot.excluded_pending_count} 条待确认</span>
                      ) : null}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}

          {freeze ? (
            <form className="graph-snapshot-form" onSubmit={submitFreeze}>
              <h3>冻结当前图</h3>
              <label>阶段
                <select value={freeze.stage}
                  onChange={(event) => setFreeze({ ...freeze, stage: event.target.value })}>
                  {rules.snapshot_stages.map((stage) => (
                    <option key={stage} value={stage}>{stage}</option>
                  ))}
                  <option value="__custom">自定义…</option>
                </select>
              </label>
              {freeze.stage === '__custom' ? (
                <label>自定义阶段
                  <input value={freeze.custom} maxLength={100}
                    onChange={(event) => setFreeze({ ...freeze, custom: event.target.value })} />
                </label>
              ) : null}
              <label>说明（可选）
                <textarea value={freeze.note} maxLength={2000} rows={2}
                  onChange={(event) => setFreeze({ ...freeze, note: event.target.value })} />
              </label>
              <label>关联进度节点（可选）
                <select value={freeze.progress}
                  onChange={(event) => setFreeze({ ...freeze, progress: event.target.value })}>
                  <option value="">不关联</option>
                  {ws.progress_nodes.map((node) => (
                    <option key={node.value} value={node.value}>{node.label}</option>
                  ))}
                </select>
              </label>
              {pending.length > 0 ? (
                <div className="graph-freeze-warning">
                  <p><strong>本图有 {pending.length} 条 Agent 待确认变更。</strong>
                    继续冻结只包含已确认状态，下列变更不会进入快照：</p>
                  <ul>
                    {pending.map((change) => (
                      <li key={change.id}>{describeDiffEntry(change.diff[0], ws, rules)}</li>
                    ))}
                  </ul>
                  <label className="graph-checkbox">
                    <input type="checkbox" checked={freeze.ack}
                      onChange={(event) => setFreeze({ ...freeze, ack: event.target.checked })} />
                    已知悉，不含待确认变更，继续冻结
                  </label>
                </div>
              ) : null}
              <div className="graph-form-actions">
                <button type="button" onClick={() => setFreeze(null)}>取消</button>
                <button type="submit" disabled={busy || (freeze.stage === '__custom' && !freeze.custom.trim())}>
                  {busy ? '冻结中…' : '冻结快照'}
                </button>
              </div>
            </form>
          ) : null}
        </aside>

        <section className="graph-snapshot-detail" aria-label="快照详情">
          {!current ? (
            <p className="muted">选择左侧快照查看只读正文，或先冻结当前图。</p>
          ) : (
            <>
              <header className="graph-snapshot-detail-head">
                <div>
                  <h3>v{current.version_no} · {current.stage_label}</h3>
                  <p className="muted">
                    冻结于 {formatTime(current.created_at)}
                    {current.progress_node_id
                      ? ` · 进度节点：${ws.progress_nodes.find((node) => node.value === current.progress_node_id)?.label ?? current.progress_node_id}`
                      : ''}
                    {current.excluded_pending_count > 0
                      ? ` · 已排除 ${current.excluded_pending_count} 条待确认变更` : ''}
                  </p>
                  {current.note ? <p>{current.note}</p> : null}
                </div>
                <div className="graph-form-actions">
                  {onExportSnapshot ? (
                    <button type="button" onClick={() => onExportSnapshot(current.id)}>
                      三态导出此快照…
                    </button>
                  ) : null}
                  <button type="button" disabled={readOnly}
                    onClick={() => {
                      setCompare({ baseId: current.id, targetId: 'current' });
                      setError('');
                    }}>对比当前工作稿</button>
                  <button type="button" disabled={readOnly} onClick={() => setRestoreConfirm(true)}>
                    恢复为工作稿…
                  </button>
                </div>
              </header>

              {restoreConfirm ? (
                <div className="graph-restore-confirm" role="alertdialog" aria-label="确认恢复快照">
                  <p>将把 v{current.version_no}「{current.stage_label}」的主体、关系、出处、分组与布局覆盖到当前工作稿，
                    并写入变更记录；可用撤销（⌘Z）回到恢复前。原快照不变。</p>
                  <div className="graph-form-actions">
                    <button type="button" onClick={() => setRestoreConfirm(false)}>取消</button>
                    <button type="button" disabled={busy} onClick={async () => {
                      setBusy(true);
                      try {
                        const result = await run({ type: 'restore_snapshot', snapshot_id: current.id });
                        if (apply(result).ok) setRestoreConfirm(false);
                      } finally {
                        setBusy(false);
                      }
                    }}>{busy ? '恢复中…' : '确认恢复'}</button>
                  </div>
                </div>
              ) : null}

              <div className="graph-snapshot-canvas">
                <GraphCanvas model={buildSnapshotCanvasModel(current)} rules={rules}
                  graphId={`snapshot-${current.id}`} selected={null} onSelect={() => {}} readOnly />
              </div>

              {drift.length > 0 ? (
                <section className="graph-drift" aria-label="编号漂移">
                  <h4>编号漂移（快照保留冻结时编号，旁注现行编号）</h4>
                  <ul>
                    {drift.map((row) => (
                      <li key={`${row.edge}-${row.frozen}`}>
                        <span>{row.edge}</span>
                        <span className="graph-drift-frozen">冻结：{row.frozen}</span>
                        <span className="graph-drift-current">现为：{row.current}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              ) : (
                <p className="muted graph-hint">现行目录编号与冻结时一致。</p>
              )}

              <section className="graph-consents" aria-label="客户同意">
                <header className="graph-consents-head">
                  <h4>客户同意（{consents.length} 条，仅追加）</h4>
                  {!readOnly ? (
                    <button type="button" onClick={() => openConsent('confirm', null)}>登记客户同意</button>
                  ) : null}
                </header>
                {consents.length === 0 ? (
                  <p className="muted">尚未登记客户同意。</p>
                ) : (
                  <ul className="graph-consent-list">
                    {consents.map((record) => (
                      <li key={record.id}>
                        <div className="graph-consent-head">
                          <span className={`graph-badge ${record.action === 'confirm' ? 'ok'
                            : record.action === 'amend' ? 'pending' : 'muted'}`}>
                            {ACTION_LABELS[record.action]}
                          </span>
                          <strong>{record.consenter}</strong>
                          <span className="muted">客户同意时间：{formatTime(record.consented_at)}</span>
                          <span className="muted">方式：{channelLabel(rules, record.channel)}</span>
                          {record.parent_consent_id ? <span className="muted">指向原记录</span> : null}
                        </div>
                        <p>{record.source_detail}</p>
                        {record.note ? <p className="graph-consent-note">备注：{record.note}</p> : null}
                        {record.evidence_material_id ? (
                          <p className="muted">凭证材料：{ws.materials.find((material) =>
                            material.id === record.evidence_material_id)?.title ?? '（已不存在）'}</p>
                        ) : null}
                        <p className="muted">系统登记：{formatTime(record.created_at)}</p>
                      </li>
                    ))}
                  </ul>
                )}
                {lastConsent && lastConsent.action !== 'revoke' && !readOnly ? (
                  <div className="graph-form-actions">
                    <button type="button" onClick={() => openConsent('amend', lastConsent.id)}>更正（追加）</button>
                    <button type="button" onClick={() => openConsent('revoke', lastConsent.id)}>撤回（追加）</button>
                  </div>
                ) : null}

                {consent ? (
                  <form className="graph-consent-form" onSubmit={submitConsent}>
                    <h4>{ACTION_LABELS[consent.action]}客户同意{consent.parentId ? '（追加记录）' : ''}</h4>
                    <label>同意人
                      <input required value={consent.consenter} maxLength={200}
                        onChange={(event) => setConsent({ ...consent, consenter: event.target.value })} />
                    </label>
                    <label>客户实际同意时间（可补录）
                      <input type="datetime-local" required value={consent.consentedAt}
                        onChange={(event) => setConsent({ ...consent, consentedAt: event.target.value })} />
                    </label>
                    <label>同意方式
                      <select value={consent.channel}
                        onChange={(event) => setConsent({ ...consent, channel: event.target.value })}>
                        {rules.consent_channels.map((channel) => (
                          <option key={channel.value} value={channel.value}>{channel.label}</option>
                        ))}
                      </select>
                    </label>
                    <label>来源说明
                      <textarea required value={consent.sourceDetail} maxLength={1000} rows={2}
                        placeholder="例如：微信语音确认，并发送截图"
                        onChange={(event) => setConsent({ ...consent, sourceDetail: event.target.value })} />
                    </label>
                    <label>凭证材料（可选）
                      <select value={consent.materialId}
                        onChange={(event) => setConsent({ ...consent, materialId: event.target.value })}>
                        <option value="">不关联</option>
                        {ws.materials.map((material) => (
                          <option key={material.id} value={material.id}>{material.title}</option>
                        ))}
                      </select>
                    </label>
                    <label>备注（可选）
                      <textarea value={consent.note} maxLength={2000} rows={2}
                        placeholder="例如：客户部分同意"
                        onChange={(event) => setConsent({ ...consent, note: event.target.value })} />
                    </label>
                    <div className="graph-form-actions">
                      <button type="button" onClick={() => setConsent(null)}>取消</button>
                      <button type="submit" disabled={busy}>{busy ? '提交中…' : '追加记录'}</button>
                    </div>
                  </form>
                ) : null}
              </section>

              <section className="graph-compare" aria-label="快照对比">
                <header className="graph-consents-head">
                  <h4>快照对比</h4>
                  {!compare ? (
                    <button type="button" onClick={() => setCompare({
                      baseId: current.id, targetId: 'current',
                    })}>选择对比版本</button>
                  ) : null}
                </header>
                {compare ? (
                  <>
                    <div className="graph-compare-controls">
                      <label>基准
                        <select value={compare.baseId}
                          onChange={(event) => setCompare({ ...compare, baseId: event.target.value })}>
                          {snapshots.map((snapshot) => (
                            <option key={snapshot.id} value={snapshot.id}>
                              v{snapshot.version_no} · {snapshot.stage_label}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label>对比对象
                        <select value={compare.targetId}
                          onChange={(event) => setCompare({ ...compare, targetId: event.target.value })}>
                          <option value="current">当前工作稿</option>
                          {snapshots.map((snapshot) => (
                            <option key={snapshot.id} value={snapshot.id}>
                              v{snapshot.version_no} · {snapshot.stage_label}
                            </option>
                          ))}
                        </select>
                      </label>
                      <button type="button" onClick={() => setCompare(null)}>关闭对比</button>
                    </div>
                    {diff ? (
                      <>
                        <p className="muted">
                          共 {diff.totalChanges} 处变化：新增主体 {diff.addedNodes.length}、删除主体 {diff.removedNodes.length}、
                          新增关系 {diff.addedEdges.length}、删除关系 {diff.removedEdges.length}、
                          字段变化 {diff.changedNodes.length + diff.changedEdges.length}、
                          布局移动 {diff.movedNodes.length}。
                        </p>
                        <ul className="graph-diff-list">
                          {diff.addedNodes.map((node) => (
                            <li key={`an-${node.entity_id}`} className="diff-added">新增主体：{node.name}</li>
                          ))}
                          {diff.removedNodes.map((node) => (
                            <li key={`rn-${node.entity_id}`} className="diff-removed">删除主体：{node.name}</li>
                          ))}
                          {diff.addedEdges.map((edge) => (
                            <li key={`ae-${edge.relation_id}`} className="diff-added">
                              新增关系：{edge.label ?? edge.kind}（{edge.credibility}）
                            </li>
                          ))}
                          {diff.removedEdges.map((edge) => (
                            <li key={`re-${edge.relation_id}`} className="diff-removed">
                              删除关系：{edge.label ?? edge.kind}
                            </li>
                          ))}
                          {diff.changedEdges.map((change) => (
                            <li key={`ce-${change.relation_id}`} className="diff-changed">
                              关系变化：{change.label} —— {change.fields.map((field) =>
                                `${field.field} ${fieldValue(rules, field.field, field.from)} → ${fieldValue(rules, field.field, field.to)}`).join('；')}
                            </li>
                          ))}
                          {diff.changedNodes.map((change) => (
                            <li key={`cn-${change.entity_id}`} className="diff-changed">
                              主体变化：{change.name} —— {change.fields.map((field) =>
                                `${field.field} ${fieldValue(rules, field.field, field.from)} → ${fieldValue(rules, field.field, field.to)}`).join('；')}
                            </li>
                          ))}
                          {diff.movedNodes.map((node) => (
                            <li key={`mn-${node.entity_id}`} className="diff-moved">
                              布局移动：{node.name}（{Math.round(node.from.x)},{Math.round(node.from.y)}）
                              →（{Math.round(node.to.x)},{Math.round(node.to.y)}）
                            </li>
                          ))}
                          {diff.addedGroups.map((group) => (
                            <li key={`ag-${group.group_id}`} className="diff-added">新增分组：{group.title}</li>
                          ))}
                          {diff.removedGroups.map((group) => (
                            <li key={`rg-${group.group_id}`} className="diff-removed">删除分组：{group.title}</li>
                          ))}
                          {diff.changedGroups.map((group) => (
                            <li key={`cg-${group.group_id}`} className="diff-changed">
                              分组变化：{group.title} —— {group.fields.map((field) =>
                                `${field.field} ${field.from} → ${field.to}`).join('；')}
                            </li>
                          ))}
                          {diff.totalChanges === 0 ? <li className="muted">两版内容一致。</li> : null}
                        </ul>
                        <div className="graph-snapshot-canvas">
                          <GraphCanvas model={targetModel!} rules={rules}
                            graphId={`compare-${compare.baseId}-${compare.targetId}`}
                            selected={null} onSelect={() => {}} readOnly
                            diff={diffHighlight(diff)} />
                        </div>
                        <p className="muted graph-hint">
                          高亮：绿色 = 新增，橙色 = 字段变化，蓝色 = 布局移动；删除项只在清单中列出。
                        </p>
                      </>
                    ) : null}
                  </>
                ) : null}
              </section>
            </>
          )}
        </section>
      </div>
    </dialog>
  );
}

/** 导出给测试：确认四档图例在快照视图中也使用同一套规则。 */
export function snapshotCredibilityRule(rules: GraphRules, value: string) {
  return credibilityRule(rules, value);
}
