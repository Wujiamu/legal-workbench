/**
 * 证据目录面板（编号唯一来源）。
 * 分组/条目按真实 DOM 顺序渲染，调序用键盘可用的「上移/下移」按钮，不用拖拽；
 * 编号随调序即时重算并驱动画布出处标签；外部导入的条目与绑定在人工确认前不参与编号。
 */
import { useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import type {
  CommandResult, EvidenceGroup, EvidenceItem, GraphCommandRunner, GraphSnapshot,
} from './types';

function groupsOf(ws: GraphSnapshot): EvidenceGroup[] {
  return ws.evidence.groups.filter((group) => group.removed_at === null)
    .sort((a, b) => a.position - b.position || a.id.localeCompare(b.id));
}

function itemsOf(ws: GraphSnapshot, groupId: string): EvidenceItem[] {
  return ws.evidence.items.filter((item) => item.group_id === groupId && item.removed_at === null)
    .sort((a, b) => a.position - b.position || a.id.localeCompare(b.id));
}

function pagesText(start: number | null, end: number | null): string {
  if (start === null) return '整份材料';
  return end !== null && end !== start ? `第${start}–${end}页` : `第${start}页`;
}

function reviewBadge(item: EvidenceItem): { text: string; tone: string } {
  if (item.review_state === 'confirmed') return { text: '已确认', tone: 'ok' };
  if (item.review_state === 'rejected') return { text: '已驳回', tone: 'muted' };
  return { text: '待确认', tone: 'pending' };
}

export function EvidenceCatalogPanel({
  ws, readOnly, run, runAgent, importSlot, exportHref, bundleHref,
}: {
  ws: GraphSnapshot;
  readOnly: boolean;
  run: GraphCommandRunner;
  runAgent: GraphCommandRunner;
  /** 真实案件页：上传预览 + 材料映射 + 提交导入；原型不传，显示模拟导入按钮。 */
  importSlot?: ReactNode;
  /** 真实 CSV 导出地址；缺失时隐藏（原型不导出）。 */
  exportHref?: string;
  /** 真实案件自动装卷（ZIP）下载地址；缺失时隐藏。 */
  bundleHref?: string;
}) {
  const groups = groupsOf(ws);
  const [groupForm, setGroupForm] = useState<{ id?: string; title: string; purpose: string } | null>(null);
  const [itemForm, setItemForm] = useState<{ groupId: string; id?: string; title: string; purpose: string } | null>(null);
  const [bindingForm, setBindingForm] = useState<{
    itemId: string; materialId: string; start: string; end: string;
  } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<{
    kind: 'group' | 'item'; id: string; title: string;
  } | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [bundling, setBundling] = useState(false);
  const bundleAbortRef = useRef<AbortController | null>(null);

  useEffect(() => () => {
    bundleAbortRef.current?.abort();
  }, []);

  const pendingChanges = ws.evidence.pending_changes
    ?? ws.pending_changes.filter((change) => change.domain === 'evidence');
  const pendingCount = ws.evidence.items.filter((item) => item.review_state === 'pending'
    && item.removed_at === null).length
    + ws.evidence.bindings.filter((binding) => binding.review_state === 'pending'
      && binding.removed_at === null).length;

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

  /** 统一等待命令完成：提交期间禁用目录写操作，失败时把服务端原因留在面板内。 */
  async function submit(command: Parameters<GraphCommandRunner>[0], agent = false) {
    if (busy) return { ok: false as const, code: 'validation' as const, message: '正在提交，请稍候' };
    setBusy(true);
    try {
      return apply(await (agent ? runAgent(command) : run(command)));
    } finally {
      setBusy(false);
    }
  }

  async function moveGroup(index: number, delta: number) {
    const target = index + delta;
    if (target < 0 || target >= groups.length) return;
    const order = groups.map((group) => group.id);
    [order[index], order[target]] = [order[target], order[index]];
    await submit({ type: 'reorder_evidence_groups', group_ids: order });
  }

  async function moveItem(groupId: string, itemId: string, delta: number) {
    const siblings = itemsOf(ws, groupId);
    const index = siblings.findIndex((item) => item.id === itemId);
    const target = index + delta;
    if (index < 0 || target < 0 || target >= siblings.length) return;
    const before = delta < 0 ? siblings[target].id : (siblings[target + 1]?.id ?? null);
    await submit({ type: 'move_evidence_item', item_id: itemId, to_group_id: groupId, before_item_id: before });
  }

  async function moveItemToGroup(item: EvidenceItem, delta: number) {
    const index = groups.findIndex((group) => group.id === item.group_id);
    const target = groups[index + delta];
    if (!target) return;
    await submit({
      type: 'move_evidence_item', item_id: item.id, to_group_id: target.id, before_item_id: null,
    });
  }

  async function submitGroup(event: FormEvent) {
    event.preventDefault();
    if (!groupForm) return;
    const result = groupForm.id
      ? await submit({
        type: 'update_evidence_group', group_id: groupForm.id,
        expected_revision: ws.evidence.groups.find((group) => group.id === groupForm.id)?.revision ?? 0,
        changes: { title: groupForm.title, purpose: groupForm.purpose || null },
      })
      : await submit({
        type: 'add_evidence_group', title: groupForm.title,
        purpose: groupForm.purpose || null,
      });
    if (result.ok) setGroupForm(null);
  }

  async function submitItem(event: FormEvent) {
    event.preventDefault();
    if (!itemForm) return;
    const result = itemForm.id
      ? await submit({
        type: 'update_evidence_item', item_id: itemForm.id,
        expected_revision: ws.evidence.items.find((item) => item.id === itemForm.id)?.revision ?? 0,
        changes: { title: itemForm.title, purpose: itemForm.purpose || null },
      })
      : await submit({
        type: 'add_evidence_item', group_id: itemForm.groupId, title: itemForm.title,
        purpose: itemForm.purpose || null,
      });
    if (result.ok) setItemForm(null);
  }

  async function submitBinding(event: FormEvent) {
    event.preventDefault();
    if (!bindingForm) return;
    const start = bindingForm.start ? Number(bindingForm.start) : null;
    const end = bindingForm.end ? Number(bindingForm.end) : null;
    const result = await submit({
      type: 'add_evidence_binding', item_id: bindingForm.itemId,
      material_id: bindingForm.materialId, page_start: start, page_end: end,
    });
    if (result.ok) setBindingForm(null);
  }

  async function downloadBundle() {
    if (!bundleHref || bundling) return;
    bundleAbortRef.current?.abort();
    const controller = new AbortController();
    bundleAbortRef.current = controller;
    setBundling(true);
    setError('');
    setNotice('');
    try {
      const response = await fetch(bundleHref, {
        headers: { Accept: 'application/zip, application/json' },
        signal: controller.signal,
      });
      if (!response.ok) {
        let message = '装卷导出失败，请稍后重试';
        try {
          const body = (await response.json()) as { message?: string };
          if (body?.message) message = body.message;
        } catch {
          // ignore non-JSON error body
        }
        setError(message);
        return;
      }
      const disposition = response.headers.get('Content-Disposition') ?? '';
      const utf8Match = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
      const plainMatch = /filename="([^"]+)"/i.exec(disposition);
      let filename = '证据卷宗.zip';
      if (utf8Match?.[1]) {
        try {
          filename = decodeURIComponent(utf8Match[1]);
        } catch {
          filename = utf8Match[1];
        }
      } else if (plainMatch?.[1]) {
        filename = plainMatch[1];
      }
      const blob = await response.blob();
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
      setNotice('已生成并开始下载证据卷宗包（含合订 PDF、Word 目录及分项原件）');
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === 'AbortError') return;
      setError('无法连接本地服务，装卷导出失败');
    } finally {
      if (bundleAbortRef.current === controller) {
        bundleAbortRef.current = null;
        setBundling(false);
      }
    }
  }

  return (
    <section className="graph-evidence" aria-label="证据目录">
      <header className="graph-evidence-head">
        <div>
          <h2>证据目录 <span className="muted">（编号唯一来源）</span></h2>
          <p className="muted graph-hint">
            编号 = 证据{'{组}'}-{'{序}'}；调序后画布出处立即更新。待确认条目与绑定不参与编号。
          </p>
        </div>
        <div className="graph-evidence-actions">
          <button type="button" disabled={readOnly}
            onClick={() => setGroupForm({ title: '', purpose: '' })}>＋ 新建分组</button>
          {exportHref ? (
            <a className="graph-ev-export" href={exportHref} download>下载 CSV 目录</a>
          ) : null}
          {bundleHref ? (
            <button type="button" className="graph-ev-bundle" disabled={bundling}
              onClick={() => { void downloadBundle(); }}>
              {bundling ? '装卷中…' : '一键装卷（ZIP）'}
            </button>
          ) : null}
          {importSlot ? null : (
            <button type="button" disabled={readOnly || busy}
              onClick={() => { void submit({
                type: 'import_evidence_catalog', request_key: `import-${Date.now()}`,
              }, true); }}>模拟导入外部目录</button>
          )}
        </div>
      </header>
      {pendingCount > 0 ? (
        <p className="graph-hint warn">
          当前有 {pendingCount} 项外部导入草稿待人工确认；确认前不参与编号，也不进入快照。
        </p>
      ) : null}
      {importSlot}
      {notice ? <p role="status" className="graph-notice-inline">{notice}</p> : null}
      {error ? <p role="alert" className="graph-error-inline">错误：{error}</p> : null}

      {groupForm && !groupForm.id ? (
        <form className="graph-ev-form" onSubmit={submitGroup}>
          <h3>新建证据分组</h3>
          <label>分组名称
            <input required value={groupForm.title} maxLength={200}
              onChange={(event) => setGroupForm({ ...groupForm, title: event.target.value })} />
          </label>
          <label>分组说明（可选）
            <input value={groupForm.purpose} maxLength={1000}
              onChange={(event) => setGroupForm({ ...groupForm, purpose: event.target.value })} />
          </label>
          <div className="graph-form-actions">
            <button type="button" onClick={() => setGroupForm(null)}>取消</button>
            <button type="submit" disabled={!groupForm.title.trim()}>创建分组</button>
          </div>
        </form>
      ) : null}

      {groups.length === 0 ? <p className="muted">还没有证据分组。</p> : (
        <ol className="graph-ev-groups">
          {groups.map((group, groupIndex) => {
            const items = itemsOf(ws, group.id);
            return (
              <li key={group.id} className="graph-ev-group">
                <header className="graph-ev-group-head">
                  <span className="graph-ev-group-no">第 {groupIndex + 1} 组</span>
                  <div className="graph-ev-group-title">
                    <strong>{group.title}</strong>
                    {group.purpose ? <span className="muted"> · {group.purpose}</span> : null}
                  </div>
                  <div className="graph-ev-actions">
                    <button type="button" aria-label={`上移分组 ${group.title}`}
                      disabled={readOnly || busy || groupIndex === 0}
                      onClick={() => { void moveGroup(groupIndex, -1); }}>↑</button>
                    <button type="button" aria-label={`下移分组 ${group.title}`}
                      disabled={readOnly || busy || groupIndex === groups.length - 1}
                      onClick={() => { void moveGroup(groupIndex, 1); }}>↓</button>
                    <button type="button" disabled={readOnly}
                      onClick={() => setGroupForm({
                        id: group.id, title: group.title, purpose: group.purpose ?? '',
                      })}>编辑</button>
                    <button type="button" disabled={readOnly}
                      onClick={() => setConfirmDelete({ kind: 'group', id: group.id, title: group.title })}>
                      删除
                    </button>
                    <button type="button" disabled={readOnly}
                      onClick={() => setItemForm({ groupId: group.id, title: '', purpose: '' })}>
                      ＋ 条目
                    </button>
                  </div>
                </header>

                {groupForm?.id === group.id ? (
                  <form className="graph-ev-form" onSubmit={submitGroup}>
                    <label>分组名称
                      <input required value={groupForm.title} maxLength={200}
                        onChange={(event) => setGroupForm({ ...groupForm, title: event.target.value })} />
                    </label>
                    <label>分组说明
                      <input value={groupForm.purpose} maxLength={1000}
                        onChange={(event) => setGroupForm({ ...groupForm, purpose: event.target.value })} />
                    </label>
                    <div className="graph-form-actions">
                      <button type="button" onClick={() => setGroupForm(null)}>取消</button>
                      <button type="submit" disabled={!groupForm.title.trim()}>保存</button>
                    </div>
                  </form>
                ) : null}

                {itemForm?.groupId === group.id && !itemForm.id ? (
                  <form className="graph-ev-form" onSubmit={submitItem}>
                    <h3>新增证据条目</h3>
                    <label>证据名称
                      <input required value={itemForm.title} maxLength={300}
                        onChange={(event) => setItemForm({ ...itemForm, title: event.target.value })} />
                    </label>
                    <label>证明目的（可选）
                      <input value={itemForm.purpose} maxLength={2000}
                        onChange={(event) => setItemForm({ ...itemForm, purpose: event.target.value })} />
                    </label>
                    <div className="graph-form-actions">
                      <button type="button" onClick={() => setItemForm(null)}>取消</button>
                      <button type="submit" disabled={!itemForm.title.trim()}>创建条目</button>
                    </div>
                  </form>
                ) : null}

                {items.length === 0 ? <p className="muted graph-hint">本组暂无条目。</p> : (
                  <ol className="graph-ev-items">
                    {items.map((item, itemIndex) => {
                      const number = ws.evidence_numbers[item.id];
                      const badge = reviewBadge(item);
                      const bindings = ws.evidence.bindings.filter((binding) =>
                        binding.item_id === item.id && binding.removed_at === null);
                      return (
                        <li key={item.id} className="graph-ev-item">
                          <div className="graph-ev-item-head">
                            <span className={`graph-ev-no ${number ? '' : badge.tone}`}>
                              {number ?? (item.review_state === 'pending' ? '待确认'
                                : item.review_state === 'rejected' ? '已驳回' : '未编号')}
                            </span>
                            <div className="graph-ev-item-title">
                              <strong>{item.title}</strong>
                              {item.purpose ? <span className="muted"> · {item.purpose}</span> : null}
                              {item.source_kind === 'imported' ? (
                                <span className="graph-badge import">外部导入</span>
                              ) : null}
                              {item.review_state !== 'confirmed' ? (
                                <span className={`graph-badge ${badge.tone}`}>{badge.text}</span>
                              ) : null}
                            </div>
                            <div className="graph-ev-actions">
                              <button type="button" aria-label={`上移条目 ${item.title}`}
                                disabled={readOnly || busy || itemIndex === 0}
                                onClick={() => { void moveItem(group.id, item.id, -1); }}>↑</button>
                              <button type="button" aria-label={`下移条目 ${item.title}`}
                                disabled={readOnly || busy || itemIndex === items.length - 1}
                                onClick={() => { void moveItem(group.id, item.id, 1); }}>↓</button>
                              <button type="button" aria-label={`移到上一组 ${item.title}`}
                                disabled={readOnly || busy || groupIndex === 0}
                                onClick={() => { void moveItemToGroup(item, -1); }}>⇤</button>
                              <button type="button" aria-label={`移到下一组 ${item.title}`}
                                disabled={readOnly || busy || groupIndex === groups.length - 1}
                                onClick={() => { void moveItemToGroup(item, 1); }}>⇥</button>
                              <button type="button" disabled={readOnly}
                                onClick={() => setItemForm({
                                  groupId: group.id, id: item.id, title: item.title,
                                  purpose: item.purpose ?? '',
                                })}>编辑</button>
                              <button type="button" disabled={readOnly}
                                onClick={() => setConfirmDelete({
                                  kind: 'item', id: item.id, title: item.title,
                                })}>删除</button>
                            </div>
                          </div>

                          {item.review_state === 'pending' ? (
                            <div className="graph-ev-review">
                              <button type="button" disabled={readOnly || busy}
                                onClick={() => { void submit({
                                  type: 'confirm_evidence_item', item_id: item.id,
                                  expected_revision: item.revision,
                                }); }}>确认条目</button>
                              <button type="button" disabled={readOnly || busy}
                                onClick={() => { void submit({
                                  type: 'reject_evidence_item', item_id: item.id,
                                  expected_revision: item.revision,
                                }); }}>驳回</button>
                            </div>
                          ) : null}

                          {itemForm?.id === item.id ? (
                            <form className="graph-ev-form" onSubmit={submitItem}>
                              <label>证据名称
                                <input required value={itemForm.title} maxLength={300}
                                  onChange={(event) => setItemForm({ ...itemForm, title: event.target.value })} />
                              </label>
                              <label>证明目的
                                <input value={itemForm.purpose} maxLength={2000}
                                  onChange={(event) => setItemForm({ ...itemForm, purpose: event.target.value })} />
                              </label>
                              <div className="graph-form-actions">
                                <button type="button" onClick={() => setItemForm(null)}>取消</button>
                                <button type="submit" disabled={!itemForm.title.trim()}>保存</button>
                              </div>
                            </form>
                          ) : null}

                          <ul className="graph-ev-bindings">
                            {bindings.length === 0 ? (
                              <li className="muted">未绑定材料（不产生出处编号）</li>
                            ) : bindings.map((binding) => {
                              const material = ws.materials.find((entry) => entry.id === binding.material_id);
                              return (
                                <li key={binding.id}>
                                  <span className="graph-ev-binding-text">
                                    {material?.title ?? '材料'} · {pagesText(binding.page_start, binding.page_end)}
                                  </span>
                                  <span className={`graph-badge ${binding.review_state === 'confirmed'
                                    ? 'ok' : binding.review_state === 'pending' ? 'pending' : 'muted'}`}>
                                    {binding.review_state === 'confirmed' ? '绑定已确认'
                                      : binding.review_state === 'pending' ? '绑定待确认' : '绑定已驳回'}
                                  </span>
                                  {binding.review_state === 'pending' ? (
                                    <>
                                      <button type="button" disabled={readOnly || busy}
                                        onClick={() => { void submit({
                                          type: 'confirm_evidence_binding', binding_id: binding.id,
                                        }); }}>确认绑定</button>
                                      <button type="button" disabled={readOnly || busy}
                                        onClick={() => { void submit({
                                          type: 'reject_evidence_binding', binding_id: binding.id,
                                        }); }}>驳回</button>
                                    </>
                                  ) : (
                                    <button type="button" disabled={readOnly || busy}
                                      onClick={() => { void submit({
                                        type: 'remove_evidence_binding', binding_id: binding.id,
                                      }); }}>解除绑定</button>
                                  )}
                                </li>
                              );
                            })}
                          </ul>
                          <button type="button" className="graph-ev-add-binding" disabled={readOnly}
                            onClick={() => setBindingForm({
                              itemId: item.id,
                              materialId: ws.materials[0]?.id ?? '',
                              start: '', end: '',
                            })}>＋ 绑定材料</button>

                          {bindingForm?.itemId === item.id ? (
                            <form className="graph-ev-form" onSubmit={submitBinding}>
                              <label>材料
                                <select value={bindingForm.materialId}
                                  onChange={(event) => setBindingForm({
                                    ...bindingForm, materialId: event.target.value,
                                  })}>
                                  {ws.materials.map((material) => (
                                    <option key={material.id} value={material.id}>
                                      {material.title}{material.page_count > 0
                                      ? `（共 ${material.page_count} 页）` : ''}
                                    </option>
                                  ))}
                                </select>
                              </label>
                              <div className="graph-form-row">
                                <label>起始页（空 = 整份）
                                  <input type="number" min={1} value={bindingForm.start}
                                    onChange={(event) => setBindingForm({
                                      ...bindingForm, start: event.target.value,
                                    })} />
                                </label>
                                <label>结束页
                                  <input type="number" min={1} value={bindingForm.end}
                                    onChange={(event) => setBindingForm({
                                      ...bindingForm, end: event.target.value,
                                    })} />
                                </label>
                              </div>
                              <div className="graph-form-actions">
                                <button type="button" onClick={() => setBindingForm(null)}>取消</button>
                                <button type="submit" disabled={!bindingForm.materialId}>绑定</button>
                              </div>
                            </form>
                          ) : null}
                        </li>
                      );
                    })}
                  </ol>
                )}
              </li>
            );
          })}
        </ol>
      )}

      {pendingChanges.map((change) => {
        const imported = change.command === 'import_catalog' || change.command === 'import_evidence_catalog';
        const targets = new Set(change.diff.filter((entry) => entry.before === null).map((entry) => entry.id));
        const items = ws.evidence.items.filter((item) => targets.has(item.id)
          && item.review_state === 'pending' && item.removed_at === null);
        const bindings = ws.evidence.bindings.filter((binding) => targets.has(binding.id)
          && binding.review_state === 'pending' && binding.removed_at === null);
        const title = change.diff.find((entry) => typeof entry.after?.title === 'string')?.after?.title;
        return (
          <div className="graph-ev-import" key={change.id}>
            <p>
              <strong>{imported ? '导入草稿' : '证据目录待确认提案'}</strong>
              {imported ? ` · ${items.length} 条证据、${bindings.length} 条绑定待确认`
                : typeof title === 'string' ? ` · ${title}` : ''}
            </p>
            <div className="graph-form-actions">
              <button type="button" disabled={readOnly || busy}
                onClick={() => { void submit({
                  type: 'accept_evidence_change', change_id: change.id,
                }); }}>{imported ? '全部确认导入' : '确认提案'}</button>
              <button type="button" disabled={readOnly || busy}
                onClick={() => { void submit({
                  type: 'revert_evidence_change', change_id: change.id,
                }); }}>{imported ? '撤回导入' : '撤回提案'}</button>
            </div>
          </div>
        );
      })}

      {confirmDelete ? (
        <div className="graph-ev-confirm" role="alertdialog" aria-label="确认删除">
          <p>确认删除{confirmDelete.kind === 'group' ? '分组' : '证据条目'}「{confirmDelete.title}」？
            {confirmDelete.kind === 'group' ? '分组内必须没有有效条目。' : '编号会立即重算。'}</p>
          <div className="graph-form-actions">
            <button type="button" onClick={() => setConfirmDelete(null)}>取消</button>
            <button type="button" disabled={busy} onClick={async () => {
              const result = confirmDelete.kind === 'group'
                ? await submit({
                  type: 'remove_evidence_group', group_id: confirmDelete.id,
                  expected_revision: ws.evidence.groups.find((group) => group.id === confirmDelete.id)?.revision ?? 0,
                })
                : await submit({
                  type: 'remove_evidence_item', item_id: confirmDelete.id,
                  expected_revision: ws.evidence.items.find((item) => item.id === confirmDelete.id)?.revision ?? 0,
                });
              if (result.ok) setConfirmDelete(null);
            }}>确认删除</button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
