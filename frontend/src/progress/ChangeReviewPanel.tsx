/**
 * Agent 待确认修改面板（总体计划 6.4）：修改前/后对照、确认、撤回。
 * 撤回冲突时保留条目并显示原因，由人工处理。
 */
import { useState } from 'react';
import { kindLabel, relationLabel, stateLabel } from './rules';
import type {
  ChangeDiffEntry, CommandResult, ProgressChange, ProgressCommand,
  ProgressNode, ProgressRules, ProgressSnapshot,
} from './types';

const COMMAND_LABELS: Record<string, string> = {
  add_node: '新建节点',
  update_node: '修改节点',
  move_node: '移动节点',
  set_active_stage: '设置当前阶段',
  insert_fragment: '插入片段',
  remove_node: '删除节点',
  restore_node: '恢复节点',
  add_relation: '建立关系',
  remove_relation: '移除关系',
  link: '建立关联',
  unlink: '移除关联',
};

const FIELD_LABELS: Record<string, string> = {
  title: '标题', state: '状态', note: '备注',
  date_start: '开始日期', date_end: '结束日期', removed_at: '删除标记',
};

function nodeOf(entry: ChangeDiffEntry): ProgressNode | null {
  return (entry.after ?? entry.before) as ProgressNode | null;
}

function describeValue(rules: ProgressRules, kind: string, field: string, value: unknown): string {
  if (value === null || value === undefined || value === '') return '（空）';
  if (field === 'state') return stateLabel(rules, kind, String(value));
  if (field === 'removed_at') return '已删除';
  return String(value);
}

function DiffLine({ entry, rules, snapshot }: {
  entry: ChangeDiffEntry; rules: ProgressRules; snapshot: ProgressSnapshot;
}) {
  if (entry.target === 'relation') {
    const relation = (entry.after ?? entry.before) as { type: string; from_node_id: string; to_node_id: string } | null;
    if (!relation) return null;
    const title = (id: string) => snapshot.nodes.find((node) => node.id === id)?.title ?? '未知节点';
    const action = entry.before === null ? '建立' : entry.after === null ? '移除' : '修改';
    return <li>{action}关系：{relationLabel(rules, relation.type)}（{title(relation.from_node_id)} → {title(relation.to_node_id)}）</li>;
  }
  if (entry.target === 'link') {
    const link = (entry.after ?? entry.before) as { display_name: string } | null;
    if (!link) return null;
    return <li>{entry.before === null ? '建立' : '移除'}关联：{link.display_name}</li>;
  }
  const node = nodeOf(entry);
  if (!node) return null;
  if (entry.before === null) {
    return <li>新建：{node.title}（{kindLabel(rules, node.kind)}）</li>;
  }
  if (entry.after === null) {
    return <li>删除：{node.title}</li>;
  }
  const before = entry.before as unknown as ProgressNode;
  const after = entry.after as unknown as ProgressNode;
  const lines: string[] = [];
  for (const field of Object.keys(FIELD_LABELS)) {
    const oldValue = (before as unknown as Record<string, unknown>)[field];
    const newValue = (after as unknown as Record<string, unknown>)[field];
    if (oldValue !== newValue) {
      lines.push(`${FIELD_LABELS[field]}：${describeValue(rules, after.kind, field, oldValue)} → ${describeValue(rules, after.kind, field, newValue)}`);
    }
  }
  if (before.parent_id !== after.parent_id || before.position !== after.position) {
    lines.push('位置变化');
  }
  return <li>{after.title}：{lines.join('；') || '无字段变化'}</li>;
}

export function ChangeReviewPanel({ snapshot, rules, readOnly = false, onCommand, focused, onFocusZone, undoDepth, onUndo }: {
  snapshot: ProgressSnapshot;
  rules: ProgressRules;
  readOnly?: boolean;
  onCommand(command: ProgressCommand): CommandResult | Promise<CommandResult>;
  /** 撤销焦点区域高亮（用户反馈第七轮）。 */
  focused?: boolean;
  onFocusZone?: () => void;
  undoDepth?: number;
  onUndo?: () => void;
}) {
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [confirmAllOpen, setConfirmAllOpen] = useState(false);
  // 审查 04：打开确认框时冻结已展示的变更 ID 与序号，确认时只提交这些条目，
  // 其间 Agent 新写入的待审修改不被一并确认。
  const [shownIds, setShownIds] = useState<string[]>([]);
  const [shownSeq, setShownSeq] = useState<number | null>(null);

  async function run(change: ProgressChange, command: ProgressCommand) {
    if (readOnly) return;
    const result = await onCommand(command);
    setErrors((current) => ({ ...current, [change.id]: result.ok ? '' : result.message }));
  }

  function openConfirmAll() {
    if (readOnly) return;
    setShownIds(snapshot.pending_changes.map((change) => change.id));
    setShownSeq(snapshot.seq);
    setConfirmAllOpen(true);
  }

  async function acceptAll() {
    const result = await onCommand({ type: 'accept_all', change_ids: shownIds, expected_seq: shownSeq });
    if (!result.ok) setErrors((current) => ({ ...current, __all__: result.message }));
    setConfirmAllOpen(false);
  }

  return (
    <section className={`progress-review${focused ? ' zone-focused' : ''}`} aria-label="Agent 待确认修改"
      onMouseDown={onFocusZone} onFocus={onFocusZone}>
      <header className="progress-review-head">
        <h2>Agent 待确认修改</h2>
        <div className="work-actions">
          {undoDepth && undoDepth > 0 && !readOnly ? (
            <button type="button" onClick={onUndo} title="撤销本区域最近一步（⌘Z）">撤销（{undoDepth}）</button>
          ) : null}
          {snapshot.pending_changes.length > 0 ? (
            <button type="button" disabled={readOnly} onClick={openConfirmAll}>一键全部确认</button>
          ) : null}
        </div>
      </header>
      {errors.__all__ ? <p role="alert" className="progress-error">错误：{errors.__all__}</p> : null}
      {confirmAllOpen ? (
        <dialog open className="progress-editor" aria-labelledby="accept-all-heading">
          <h2 id="accept-all-heading">确认全部 Agent 修改？</h2>
          <p>将一次性确认打开本框时展示的 {shownIds.length} 条 Agent 修改。
            <strong>请逐条人工审核后再确认</strong>；确认后仍可在本区域用「撤销」逐条撤回。
            若其间有新的 Agent 修改写入，本次不会一并确认。</p>
          <div className="work-actions">
            <button type="button" onClick={() => setConfirmAllOpen(false)}>返回逐条审核</button>
            <button type="button" onClick={acceptAll}>我已审核，全部确认</button>
          </div>
        </dialog>
      ) : null}
      {snapshot.pending_changes.length === 0 ? (
        <p className="muted">没有待确认的 Agent 修改。</p>
      ) : (
        <ul className="progress-review-list">
          {snapshot.pending_changes.map((change) => (
            <li key={change.id} className="progress-review-item">
              <header>
                <strong>{COMMAND_LABELS[change.command] ?? change.command}</strong>
                <span className="muted">
                  Agent · {change.entry} · {new Date(change.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}
                </span>
              </header>
              <ul className="progress-diff">
                {change.diff.map((entry) => (
                  <DiffLine key={`${entry.target}-${entry.id}`} entry={entry} rules={rules} snapshot={snapshot} />
                ))}
              </ul>
              {errors[change.id] ? <p role="alert" className="progress-error">错误：{errors[change.id]}</p> : null}
              <div className="work-actions">
                <button type="button" disabled={readOnly} onClick={() => run(change, { type: 'accept_change', change_id: change.id })}>确认</button>
                <button type="button" disabled={readOnly} onClick={() => run(change, { type: 'revert_change', change_id: change.id })}>撤回</button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}