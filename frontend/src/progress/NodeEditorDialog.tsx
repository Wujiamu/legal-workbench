/**
 * 节点编辑对话框（总体计划 6.4）：语义化 <dialog> + 原生表单。
 * 中文输入法组词中的回车不提交；冲突保留草稿，重新加载才覆盖。
 * 新建有预设的种类（工作/事件）：标题下拉＝预设＋其他，选其他才填自定义。
 */
import { useEffect, useId, useRef, useState } from 'react';
import { titlePresets } from './rules';
import type { CommandResult, NodeChanges, ProgressCommand, ProgressNode, ProgressRules } from './types';

const OTHER = '__other__';

export function NodeEditorDialog({ mode, node, kind, parentId, rules, onClose, onSubmit }: {
  mode: 'add' | 'edit';
  node?: ProgressNode;
  kind?: string;
  parentId?: string | null;
  rules: ProgressRules;
  onClose(): void;
  /** 审查 02：可返回 Promise；对话框等待本次命令完成，冲突时保留草稿不关闭。 */
  onSubmit(command: ProgressCommand): CommandResult | Promise<CommandResult>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const baseId = useId();
  const effectiveKind = mode === 'edit' ? node!.kind : kind!;
  const kindRule = rules.kinds[effectiveKind];
  const presets = mode === 'add' ? titlePresets(rules, effectiveKind) : [];
  const usePresetSelect = presets.length > 0;
  const [titleChoice, setTitleChoice] = useState(usePresetSelect ? presets[0] : '');
  const [title, setTitle] = useState(mode === 'edit' ? node!.title : '');
  const [state, setState] = useState(mode === 'edit' ? node!.state : kindRule.default_state);
  const [note, setNote] = useState(mode === 'edit' ? node!.note ?? '' : '');
  const [dateStart, setDateStart] = useState(mode === 'edit' ? node!.date_start ?? '' : '');
  const [dateEnd, setDateEnd] = useState(mode === 'edit' ? node!.date_end ?? '' : '');
  const [baseRevision, setBaseRevision] = useState(mode === 'edit' ? node!.revision : 0);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  function reload() {
    if (mode !== 'edit' || !node) return;
    setTitle(node.title);
    setState(node.state);
    setNote(node.note ?? '');
    setDateStart(node.date_start ?? '');
    setDateEnd(node.date_end ?? '');
    setBaseRevision(node.revision);
    setError('');
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (submitting) return;
    const effectiveTitle = (usePresetSelect
      ? (titleChoice === OTHER ? title : titleChoice)
      : title).trim();
    if (!effectiveTitle) return;
    const changes: NodeChanges = {
      title: effectiveTitle, state, note: note || null,
      date_start: dateStart || null, date_end: dateEnd || null,
    };
    const command: ProgressCommand = mode === 'edit'
      ? { type: 'update_node', node_id: node!.id, expected_revision: baseRevision, changes }
      : { type: 'add_node', parent_id: parentId ?? null, kind: effectiveKind,
          title: effectiveTitle, state, note: note || null,
          date_start: dateStart || null, date_end: dateEnd || null };
    // 审查 02：等待本次命令真正完成，再决定关闭或保留草稿。
    setSubmitting(true);
    setError('');
    try {
      const result = await onSubmit(command);
      if (result.ok) {
        onClose();
      } else {
        setError(result.message);
      }
    } finally {
      setSubmitting(false);
    }
  }

  const effectiveTitle = (usePresetSelect
    ? (titleChoice === OTHER ? title : titleChoice)
    : title).trim();
  const externalUpdate = mode === 'edit' && node && node.revision !== baseRevision;

  return (
    <dialog ref={dialog} className="work-dialog progress-editor" aria-labelledby={`${baseId}-heading`}
      onCancel={(event) => { event.preventDefault(); onClose(); }}>
      <form onSubmit={submit}
        onKeyDown={(event) => {
          if (event.key !== 'Enter') return;
          // 输入法候选确认（keyCode 229 / isComposing）不得触发提交。
          if (event.nativeEvent.isComposing || event.keyCode === 229) {
            event.preventDefault();
            return;
          }
          // 单行输入框回车显式提交：浏览器与 jsdom 行为一致，且不重复提交。
          const target = event.target as HTMLInputElement;
          if (target.tagName === 'INPUT' && target.type !== 'checkbox' && target.type !== 'date') {
            event.preventDefault();
            event.currentTarget.requestSubmit();
          }
        }}>
        <h2 id={`${baseId}-heading`}>{mode === 'edit' ? '编辑节点' : `新建${kindRule.label}`}</h2>
        {usePresetSelect ? (
          <>
            <label htmlFor={`${baseId}-title`}>标题
              <select id={`${baseId}-title`} value={titleChoice}
                onChange={(event) => setTitleChoice(event.target.value)}>
                {presets.map((preset) => <option key={preset} value={preset}>{preset}</option>)}
                <option value={OTHER}>其他…</option>
              </select>
            </label>
            {titleChoice === OTHER ? (
              <label htmlFor={`${baseId}-custom`}>自定义标题
                <input id={`${baseId}-custom`} required maxLength={200} value={title}
                  onChange={(event) => setTitle(event.target.value)} />
              </label>
            ) : null}
          </>
        ) : (
          <label htmlFor={`${baseId}-title`}>标题
            <input id={`${baseId}-title`} required maxLength={200} value={title}
              onChange={(event) => setTitle(event.target.value)} />
          </label>
        )}
        <label htmlFor={`${baseId}-state`}>状态
          <select id={`${baseId}-state`} value={state}
            onChange={(event) => setState(event.target.value)}>
            {kindRule.states.map((entry) =>
              <option key={entry.value} value={entry.value}>{entry.label}</option>)}
          </select>
        </label>
        <label htmlFor={`${baseId}-note`}>备注
          <textarea id={`${baseId}-note`} maxLength={4000} rows={3} value={note}
            onChange={(event) => setNote(event.target.value)} />
        </label>
        <div className="progress-editor-dates">
          <label htmlFor={`${baseId}-start`}>开始日期
            <input id={`${baseId}-start`} type="date" value={dateStart}
              onChange={(event) => setDateStart(event.target.value)} />
          </label>
          <label htmlFor={`${baseId}-end`}>结束日期
            <input id={`${baseId}-end`} type="date" value={dateEnd}
              onChange={(event) => setDateEnd(event.target.value)} />
          </label>
        </div>
        {error ? <p role="alert" className="progress-error">错误：{error}</p> : null}
        {externalUpdate && !error ? <p className="muted">注意：该节点已被外部更新，保存可能冲突。</p> : null}
        <div className="work-actions">
          {error && mode === 'edit' ? (
            <button type="button" onClick={reload}>重新加载节点</button>
          ) : null}
          <button type="button" onClick={onClose}>取消</button>
          <button type="submit" disabled={!effectiveTitle || submitting}>{submitting ? '保存中…' : mode === 'edit' ? '保存' : '创建'}</button>
        </div>
      </form>
    </dialog>
  );
}