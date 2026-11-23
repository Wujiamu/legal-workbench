/**
 * Agent 待确认修改（关系图域）：逐条确认 / 撤回，一键全部确认。
 * 变更在写入时已作用于工作稿并显示黄标；确认后黄标变绿，撤回后恢复原状。
 */
import { useState } from 'react';
import { describeDiffEntry } from './model';
import type {
  GraphChange, GraphCommandRunner, GraphRules, GraphSnapshot,
} from './types';

const COMMAND_LABELS: Record<string, string> = {
  add_entity: 'Agent 新建主体',
  update_entity: 'Agent 修改主体',
  add_alias: 'Agent 新增别名',
  add_relation: 'Agent 新建关系',
  update_relation: 'Agent 修改关系',
  add_source: 'Agent 补充出处',
  create_graph: 'Agent 新建关系图',
  update_graph: 'Agent 修改关系图',
  add_to_graph: 'Agent 加入本图',
  add_evidence_group: 'Agent 新建证据分组',
  add_evidence_item: 'Agent 新建证据条目',
  update_evidence_item: 'Agent 修改证据条目',
  add_evidence_binding: 'Agent 提交材料绑定',
  import_evidence_catalog: 'Agent 导入外部证据目录',
};

export function GraphChangeReview({ ws, rules, readOnly, onCommand }: {
  ws: GraphSnapshot;
  rules: GraphRules;
  readOnly: boolean;
  onCommand: GraphCommandRunner;
}) {
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [confirmAllOpen, setConfirmAllOpen] = useState(false);
  const [shownIds, setShownIds] = useState<string[]>([]);
  const [shownSeq, setShownSeq] = useState<number | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const accepted = ws.changes.filter((change) => change.actor === 'agent'
    && change.review_state === 'accepted').length;

  async function run(change: GraphChange, command: Parameters<GraphCommandRunner>[0]) {
    if (busyId) return;
    setBusyId(change.id);
    try {
      const result = await onCommand(command);
      setErrors((current) => ({ ...current, [change.id]: result.ok ? '' : result.message }));
    } finally {
      setBusyId(null);
    }
  }

  function openConfirmAll() {
    setShownIds(ws.pending_changes.map((change) => change.id));
    setShownSeq(ws.seq);
    setConfirmAllOpen(true);
  }

  return (
    <section className="graph-review" aria-label="Agent 待确认修改">
      <header className="graph-review-head">
        <h2>Agent 待确认 <span className="muted">（{ws.pending_changes.length}）</span></h2>
        <div className="graph-form-actions">
          {ws.pending_changes.length > 0 && !readOnly ? (
            <button type="button" onClick={openConfirmAll}>一键全部确认</button>
          ) : null}
          {accepted > 0 ? <span className="muted">已确认 {accepted} 条</span> : null}
        </div>
      </header>
      {confirmAllOpen ? (
        <div className="graph-review-confirm" role="alertdialog" aria-label="确认全部 Agent 修改">
          <p>将确认打开本框时展示的 {shownIds.length} 条 Agent 修改。请先逐条审核；
            确认后仍可逐条撤回。其间新写入的 Agent 修改不会一并确认。</p>
          <div className="graph-form-actions">
            <button type="button" onClick={() => setConfirmAllOpen(false)}>返回逐条审核</button>
            <button type="button" onClick={async () => {
              if (busyId) return;
              setBusyId('__all__');
              try {
                const result = await onCommand({
                  type: 'accept_all', change_ids: shownIds, expected_seq: shownSeq,
                });
                if (!result.ok) setErrors({ __all__: result.message });
                setConfirmAllOpen(false);
              } finally {
                setBusyId(null);
              }
            }}>我已审核，全部确认</button>
          </div>
        </div>
      ) : null}
      {errors.__all__ ? <p role="alert" className="graph-error-inline">错误：{errors.__all__}</p> : null}
      {ws.pending_changes.length === 0 ? (
        <p className="muted">没有待确认的 Agent 修改。</p>
      ) : (
        <ul className="graph-review-list">
          {ws.pending_changes.map((change) => (
            <li key={change.id} className="graph-review-item">
              <header>
                <strong>{COMMAND_LABELS[change.command] ?? change.command}</strong>
                <span className="muted">
                  {new Date(change.created_at).toLocaleTimeString('zh-CN', {
                    hour: '2-digit', minute: '2-digit',
                  })}
                </span>
              </header>
              <ul className="graph-review-diff">
                {change.diff.map((entry) => (
                  <li key={`${entry.target}-${entry.id}`}>{describeDiffEntry(entry, ws, rules)}</li>
                ))}
              </ul>
              {errors[change.id] ? (
                <p role="alert" className="graph-error-inline">错误：{errors[change.id]}</p>
              ) : null}
              <div className="graph-form-actions">
                <button type="button" disabled={readOnly || busyId !== null}
                  onClick={() => void run(change, { type: 'accept_change', change_id: change.id })}>确认</button>
                <button type="button" disabled={readOnly || busyId !== null}
                  onClick={() => void run(change, { type: 'revert_change', change_id: change.id })}>撤回</button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
