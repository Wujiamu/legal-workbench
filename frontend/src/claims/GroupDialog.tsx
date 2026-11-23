/** 新建/编辑诉请分组（原生 dialog，中文输入法回车不误提交）。 */
import { useRef, useState } from 'react';
import { shouldBlockFormEnter } from './ime';
import { Modal } from './Modal';
import { CURRENCIES, CURRENCY_LABELS } from './types';
import type { ClaimCommand, ClaimCommandRunner, ClaimGroup, Currency } from './types';

export function GroupDialog({ group, onSubmit, onClose }: {
  group?: ClaimGroup;
  onSubmit: ClaimCommandRunner;
  onClose(): void;
}) {
  const [title, setTitle] = useState(group?.title ?? '');
  const [currency, setCurrency] = useState<Currency>(group?.currency ?? 'CNY');
  const [note, setNote] = useState(group?.note ?? '');
  const [include, setInclude] = useState(group?.include_in_total ?? true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const composing = useRef(false);
  const compositionEndedAt = useRef(0);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!title.trim()) { setError('分组名称不能为空'); return; }
    setBusy(true);
    try {
      const result = group
        ? await onSubmit({
            type: 'update_group', group_id: group.id, title, currency, note,
            include_in_total: include,
          })
        : await onSubmit({ type: 'add_group', title, currency, note });
      if (!result.ok) setError(result.message ?? '保存失败');
      else onClose();
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal labelledBy="claims-group-heading" onClose={onClose}>
      <form
        onSubmit={submit}
        onCompositionStart={() => { composing.current = true; }}
        onCompositionEnd={() => { composing.current = false; compositionEndedAt.current = Date.now(); }}
        onKeyDown={(event) => {
          if (shouldBlockFormEnter(event, composing.current, compositionEndedAt.current)) {
            event.preventDefault();
          }
        }}
      >
        <h2 id="claims-group-heading">{group ? '编辑诉请分组' : '新建诉请分组'}</h2>
        <p className="muted">一组对应一项独立的诉请口径，例如“对被告一的诉请”“二审上诉请求”。</p>
        <label>分组名称
          <input autoFocus required maxLength={200} value={title}
                 onChange={(event) => setTitle(event.target.value)} />
        </label>
        <label>币种
          <select value={currency} onChange={(event) => setCurrency(event.target.value as Currency)}>
            {CURRENCIES.map((code) => (
              <option key={code} value={code}>{CURRENCY_LABELS[code]}（{code}）</option>
            ))}
          </select>
        </label>
        <label>备注（可选）
          <textarea rows={3} maxLength={2000} value={note}
                    onChange={(event) => setNote(event.target.value)} />
        </label>
        {group ? (
          <label className="claims-check">
            <input type="checkbox" checked={include}
                   onChange={(event) => setInclude(event.target.checked)} />
            计入全案总计
          </label>
        ) : (
          <p className="field-hint">创建后默认计入全案总计，可在分组头部取消。</p>
        )}
        {error ? <p role="alert" className="claims-error">错误：{error}</p> : null}
        <div className="work-actions">
          <button type="button" onClick={onClose}>取消</button>
          <button type="submit" disabled={busy}>{busy ? '保存中…' : group ? '保存' : '创建分组'}</button>
        </div>
      </form>
    </Modal>
  );
}
