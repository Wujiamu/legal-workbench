/**
 * Wave 2A：程序化「候选主体与别名」初筛与消歧工作台。
 *
 * 只读加载 /graphs/assist/entity-candidates 候选清单；批量写入逐条走工作区 run() 命令
 * （add_entity / add_alias / add_source），保持 CAS、操作审计与撤销一致性。
 */
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { describeError } from '../api/client';
import { graphAssistApi } from '../api/endpoints';
import type { Schemas } from '../api/endpoints';
import { useCompositionGuard } from './GraphDialogs';
import { entityKindLabel, roleLabel } from './rules';
import type { CaseEntity, CommandResult, GraphCommandRunner, GraphRules, GraphSnapshot } from './types';

type EntityCandidate = Schemas['EntityCandidate'];
type RowAction = 'create' | 'merge' | 'merge_existing' | 'ignore';

interface CandidateRowState {
  key: string;
  originalName: string;
  entityKind: EntityCandidate['entity_kind'];
  suggestedRoles: string[];
  candidateAliases: string[];
  mentionCount: number;
  existingEntityId: string | null;
  existingEntityName: string | null;
  sources: EntityCandidate['sources'];
  action: RowAction;
  editedName: string;
  editedKind: string;
  selectedRoles: string[];
  selectedAliases: string[];
  targetEntityId: string;
  addPrimaryAsAlias: boolean;
  createdEntityId: string | null;
  done: boolean;
}

export interface EntityDiscoveryDialogProps {
  open: boolean;
  onClose: () => void;
  caseId: string;
  graphId: string;
  ws: GraphSnapshot;
  rules: GraphRules;
  readOnly: boolean;
  run: GraphCommandRunner;
  onOpenSourcePreview?: (info: { fileVersionId: string; title: string; page?: number }) => void;
}

function normalizeName(value: string): string {
  return value.normalize('NFKC').replace(/\s+/g, ' ').trim().toLowerCase();
}

function isDuplicateAliasError(result: Extract<CommandResult, { ok: false }>): boolean {
  return /已存在|重复|already exists|duplicate/i.test(result.message);
}

function findCreatedEntityId(
  beforeIds: Set<string>,
  result: Extract<CommandResult, { ok: true }>,
  name: string,
): string | null {
  const changeId = result.last_change_id ?? result.snapshot.last_change_id;
  if (changeId) {
    const change = result.snapshot.changes.find((item) => item.id === changeId);
    const createdDiff = change?.diff.find((entry) => entry.target === 'entity' && entry.before === null);
    if (createdDiff) return createdDiff.id;
  }

  const active = result.snapshot.entities.filter((entity) => entity.removed_at === null);
  const trimmed = name.trim();
  const normalized = normalizeName(name);
  const added = active.filter((entity) => !beforeIds.has(entity.id));

  const addedExact = added.find((entity) => entity.name.trim() === trimmed);
  if (addedExact) return addedExact.id;

  const addedNorm = added.find((entity) => normalizeName(entity.name) === normalized);
  if (addedNorm) return addedNorm.id;

  if (added.length === 1) return added[0].id;

  const exact = active.find((entity) => entity.name.trim() === trimmed);
  if (exact) return exact.id;

  const norm = active.find((entity) => normalizeName(entity.name) === normalized);
  return norm ? norm.id : null;
}

function buildRowState(candidate: EntityCandidate, index: number, rules: GraphRules): CandidateRowState {
  const validRoleSet = new Set(rules.roles.map((role) => role.value));
  const validKindSet = new Set(rules.entity_kinds.map((kind) => kind.value));
  const initialKind = validKindSet.has(candidate.entity_kind)
    ? candidate.entity_kind
    : (rules.entity_kinds[0]?.value ?? 'other');
  const initialRoles = candidate.suggested_roles.filter((role) => validRoleSet.has(role));

  return {
    key: `${index}:${candidate.name}`,
    originalName: candidate.name,
    entityKind: candidate.entity_kind,
    suggestedRoles: initialRoles,
    candidateAliases: candidate.candidate_aliases,
    mentionCount: candidate.mention_count,
    existingEntityId: candidate.existing_entity_id,
    existingEntityName: candidate.existing_entity_name,
    sources: candidate.sources,
    action: candidate.existing_entity_id ? 'merge_existing' : 'create',
    editedName: candidate.name,
    editedKind: initialKind,
    selectedRoles: initialRoles,
    selectedAliases: [...candidate.candidate_aliases],
    targetEntityId: '',
    addPrimaryAsAlias: true,
    createdEntityId: null,
    done: false,
  };
}

export function EntityDiscoveryDialog(props: EntityDiscoveryDialogProps) {
  if (!props.open) return null;
  return <EntityDiscoveryDialogInner key={props.caseId} {...props} />;
}

function EntityDiscoveryDialogInner({
  onClose, caseId, graphId, ws, rules, readOnly, run, onOpenSourcePreview,
}: EntityDiscoveryDialogProps) {
  const titleId = useId();
  const tableCaptionId = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const mountedRef = useRef(true);
  const activeCaseRef = useRef(caseId);
  activeCaseRef.current = caseId;
  const rulesRef = useRef(rules);
  rulesRef.current = rules;
  const imeGuard = useCompositionGuard();

  const [selectedMaterialIds, setSelectedMaterialIds] = useState<string[]>([]);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [rows, setRows] = useState<CandidateRowState[]>([]);
  const [busy, setBusy] = useState(false);
  const [progressText, setProgressText] = useState('');
  const [submitError, setSubmitError] = useState('');
  const [summary, setSummary] = useState('');

  useEffect(() => {
    mountedRef.current = true;
    const element = dialogRef.current;
    if (element && !element.open) element.showModal();
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const materialIdsKey = selectedMaterialIds.join('\0');

  useEffect(() => {
    const controller = new AbortController();
    const filterIds = materialIdsKey ? materialIdsKey.split('\0') : [];
    setLoading(true);
    setLoadError('');
    setSubmitError('');
    setSummary('');

    void graphAssistApi.entityCandidates(caseId, filterIds, controller.signal).then(
      (response) => {
        if (controller.signal.aborted || !mountedRef.current || activeCaseRef.current !== caseId) return;
        setRows(response.candidates.map((candidate, index) => buildRowState(candidate, index, rulesRef.current)));
      },
      (cause: unknown) => {
        if (controller.signal.aborted || !mountedRef.current || activeCaseRef.current !== caseId) return;
        setLoadError(describeError(cause) || '加载候选主体失败。');
      },
    ).finally(() => {
      if (!controller.signal.aborted && mountedRef.current && activeCaseRef.current === caseId) {
        setLoading(false);
      }
    });

    return () => controller.abort();
  }, [caseId, materialIdsKey, refreshNonce]);

  const activeEntities: CaseEntity[] = useMemo(
    () => ws.entities.filter((entity) => entity.removed_at === null),
    [ws.entities],
  );

  const actionableRows = useMemo(
    () => rows.filter((row) => {
      if (row.done || row.action === 'ignore') return false;
      if (row.action === 'merge_existing') return row.selectedAliases.length > 0;
      if (row.action === 'merge') return row.addPrimaryAsAlias || row.selectedAliases.length > 0;
      return true;
    }),
    [rows],
  );

  function updateRow(key: string, updater: (current: CandidateRowState) => CandidateRowState) {
    setSubmitError('');
    setRows((current) => current.map((row) => (row.key === key ? updater(row) : row)));
  }

  function toggleMaterialFilter(materialId: string) {
    setSelectedMaterialIds((current) => (
      current.includes(materialId)
        ? current.filter((id) => id !== materialId)
        : [...current, materialId]
    ));
  }

  async function handleBatchSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (readOnly || busy || actionableRows.length === 0) return;

    for (const row of actionableRows) {
      if (row.action === 'create' && !row.editedName.trim()) {
        setSubmitError(`候选主体「${row.originalName}」的新建名称不能为空。`);
        return;
      }
      if (row.action === 'merge' && !row.targetEntityId) {
        setSubmitError(`请为「${row.originalName}」选择要合并到的已有主体。`);
        return;
      }
    }

    setBusy(true);
    setSubmitError('');
    setSummary('');

    let createdEntities = 0;
    let addedAliases = 0;
    let addedSources = 0;
    let skippedDuplicateAliases = 0;
    let latestEntities = ws.entities;
    let knownEntityIds = new Set(ws.entities.map((entity) => entity.id));

    try {
      for (let idx = 0; idx < actionableRows.length; idx += 1) {
        const row = actionableRows[idx];
        const label = row.action === 'create' ? row.editedName.trim() : row.originalName;
        setProgressText(`正在写入（${idx + 1}/${actionableRows.length}）：${label}…`);

        if (row.action === 'create') {
          const trimmedName = row.editedName.trim();
          let entityId = row.createdEntityId;
          if (!entityId) {
            const createRes = await run({
              type: 'add_entity',
              kind: row.editedKind,
              name: trimmedName,
              roles: row.selectedRoles,
              graph_id: graphId,
            });
            if (!mountedRef.current || activeCaseRef.current !== caseId) return;
            if (!createRes.ok) {
              setSubmitError(`新建主体「${trimmedName}」失败：${createRes.message}`);
              return;
            }
            entityId = findCreatedEntityId(knownEntityIds, createRes, trimmedName);
            latestEntities = createRes.snapshot.entities;
            knownEntityIds = new Set(latestEntities.map((entity) => entity.id));
            if (!entityId) {
              setSubmitError(`已新建主体「${trimmedName}」，但在返回快照中未找到主体 ID。`);
              return;
            }
            createdEntities += 1;
            updateRow(row.key, (prev) => ({ ...prev, createdEntityId: entityId }));
          }

          const targetNorm = normalizeName(trimmedName);
          const seenAliases = new Set<string>([targetNorm]);
          for (const alias of row.selectedAliases) {
            const cleaned = alias.trim();
            const aliasNorm = normalizeName(cleaned);
            if (!cleaned || seenAliases.has(aliasNorm)) continue;
            seenAliases.add(aliasNorm);
            const aliasRes = await run({ type: 'add_alias', entity_id: entityId, alias: cleaned });
            if (!mountedRef.current || activeCaseRef.current !== caseId) return;
            if (!aliasRes.ok) {
              if (isDuplicateAliasError(aliasRes)) {
                skippedDuplicateAliases += 1;
                continue;
              }
              setSubmitError(`为主体「${trimmedName}」添加别名「${cleaned}」失败：${aliasRes.message}`);
              return;
            }
            latestEntities = aliasRes.snapshot.entities;
            addedAliases += 1;
          }

          const firstSegmentId = row.sources[0]?.segment_id;
          if (firstSegmentId) {
            const sourceRes = await run({
              type: 'add_source',
              entity_id: entityId,
              ref: { segment_id: firstSegmentId },
            });
            if (!mountedRef.current || activeCaseRef.current !== caseId) return;
            if (!sourceRes.ok) {
              setSubmitError(`为主体「${trimmedName}」挂接出处失败：${sourceRes.message}`);
              return;
            }
            latestEntities = sourceRes.snapshot.entities;
            addedSources += 1;
          }

          updateRow(row.key, (prev) => ({ ...prev, done: true }));
          continue;
        }

        const targetId = row.action === 'merge_existing'
          ? row.existingEntityId
          : row.targetEntityId;
        if (!targetId) {
          setSubmitError(`未找到「${row.originalName}」的目标主体。`);
          return;
        }

        const targetEntity = latestEntities.find((entity) => entity.id === targetId)
          ?? activeEntities.find((entity) => entity.id === targetId);
        const targetName = targetEntity?.name ?? row.existingEntityName ?? '';
        const seenAliases = new Set<string>(targetName ? [normalizeName(targetName)] : []);

        const aliasesToAdd: string[] = [];
        if (row.action === 'merge' && row.addPrimaryAsAlias) {
          aliasesToAdd.push(row.originalName);
        }
        aliasesToAdd.push(...row.selectedAliases);

        for (const alias of aliasesToAdd) {
          const cleaned = alias.trim();
          const aliasNorm = normalizeName(cleaned);
          if (!cleaned || seenAliases.has(aliasNorm)) continue;
          seenAliases.add(aliasNorm);
          const aliasRes = await run({ type: 'add_alias', entity_id: targetId, alias: cleaned });
          if (!mountedRef.current || activeCaseRef.current !== caseId) return;
          if (!aliasRes.ok) {
            if (isDuplicateAliasError(aliasRes)) {
              skippedDuplicateAliases += 1;
              continue;
            }
            setSubmitError(`为已有主体添加别名「${cleaned}」失败：${aliasRes.message}`);
            return;
          }
          latestEntities = aliasRes.snapshot.entities;
          addedAliases += 1;
        }

        updateRow(row.key, (prev) => ({ ...prev, done: true }));
      }

      const skippedNote = skippedDuplicateAliases > 0
        ? `（已跳过 ${skippedDuplicateAliases} 条重复别名）`
        : '';
      setSummary(
        `已完成批量写入：新建主体 ${createdEntities} 个、新增别名 ${addedAliases} 条、挂接出处 ${addedSources} 条${skippedNote}。`,
      );
    } finally {
      if (mountedRef.current) {
        setBusy(false);
        setProgressText('');
      }
    }
  }

  return (
    <dialog
      ref={dialogRef}
      className="work-dialog graph-snapshot-dialog graph-discovery-dialog"
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <header className="graph-snapshot-head">
        <div>
          <h2 id={titleId}>程序化初筛主体与别名消歧台</h2>
          <p className="muted">
            从案卷文书首部与合同简称定义自动提取候选主体，核对后可批量新建主体、合并为已有主体别名或忽略。
          </p>
        </div>
        <button type="button" autoFocus disabled={busy} onClick={onClose}>关闭</button>
      </header>

      <div style={{ padding: '14px 18px', overflow: 'auto', maxHeight: 'calc(94vh - 84px)', display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 12 }}>
        {readOnly ? (
          <p role="status" className="graph-archived">
            本案已归档：仅可查看候选主体与原文出处，不能批量写入关系图。
          </p>
        ) : null}

        {ws.materials.length > 0 ? (
          <section aria-label="材料范围筛选">
            <fieldset className="graph-pick-fieldset">
              <legend>筛选材料范围（不选默认扫描全案已解析原文）</legend>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 14px', alignItems: 'center' }}>
                {ws.materials.map((material) => (
                  <label key={material.id} className="graph-checkbox">
                    <input
                      type="checkbox"
                      style={{ width: 'auto' }}
                      disabled={busy}
                      checked={selectedMaterialIds.includes(material.id)}
                      onChange={() => toggleMaterialFilter(material.id)}
                    />
                    {material.title}
                  </label>
                ))}
                {selectedMaterialIds.length > 0 ? (
                  <button type="button" disabled={busy} onClick={() => setSelectedMaterialIds([])}>
                    清除筛选
                  </button>
                ) : null}
                <button type="button" disabled={busy || loading} onClick={() => setRefreshNonce((n) => n + 1)}>
                  重新扫描
                </button>
              </div>
            </fieldset>
          </section>
        ) : null}

        {loading ? <p role="status" className="muted">正在从案卷原文提取候选主体与简称…</p> : null}
        {loadError ? (
          <p role="alert" className="graph-error-inline">
            错误：{loadError}{' '}
            <button type="button" onClick={() => setRefreshNonce((n) => n + 1)}>重试</button>
          </p>
        ) : null}

        {!loading && !loadError && rows.length === 0 ? (
          <p className="muted">当前范围未发现新的候选主体或简称定义；可调整材料筛选或直接手动新建主体。</p>
        ) : null}

        {rows.length > 0 ? (
          <form onSubmit={(event) => void handleBatchSubmit(event)} {...imeGuard} style={{ minWidth: 0 }}>
            <div className="table-scroll" style={{ minWidth: 0 }} tabIndex={0} role="region" aria-labelledby={tableCaptionId}>
              <table className="case-table" style={{ minWidth: 880 }}>
                <caption id={tableCaptionId} style={{ textAlign: 'left', padding: '4px 0 8px', fontSize: 13, color: 'var(--muted)' }}>
                  候选主体与别名清单（共 {rows.length} 条，待执行 {actionableRows.length} 条）
                </caption>
                <thead>
                  <tr>
                    <th scope="col">候选主体</th>
                    <th scope="col">推定类型与建议角色</th>
                    <th scope="col">候选别名（勾选写入）</th>
                    <th scope="col">提及与样例出处</th>
                    <th scope="col">处理方式</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => {
                    const rowDisabled = readOnly || busy || row.done;
                    return (
                      <tr key={row.key}>
                        <th scope="row" style={{ verticalAlign: 'top' }}>
                          <div>{row.originalName}</div>
                          {row.existingEntityId ? (
                            <span className="graph-badge import" style={{ marginLeft: 0, marginTop: 4 }}>
                              已匹配：{row.existingEntityName ?? row.existingEntityId}
                            </span>
                          ) : null}
                          {row.done ? (
                            <span className="graph-badge ok" style={{ marginLeft: 4, marginTop: 4 }}>
                              已写入
                            </span>
                          ) : null}
                          {row.action === 'create' && !row.done ? (
                            <div style={{ marginTop: 8, display: 'grid', gap: 6 }}>
                              <label>
                                写入名称
                                <input
                                  type="text"
                                  maxLength={200}
                                  disabled={rowDisabled}
                                  value={row.editedName}
                                  onChange={(event) => {
                                    const value = event.target.value;
                                    updateRow(row.key, (prev) => ({ ...prev, editedName: value }));
                                  }}
                                />
                              </label>
                            </div>
                          ) : null}
                        </th>

                        <td style={{ verticalAlign: 'top' }}>
                          {row.action === 'create' && !row.done ? (
                            <div style={{ display: 'grid', gap: 6 }}>
                              <label>
                                类型
                                <select
                                  disabled={rowDisabled}
                                  value={row.editedKind}
                                  onChange={(event) => {
                                    const value = event.target.value;
                                    updateRow(row.key, (prev) => ({ ...prev, editedKind: value }));
                                  }}
                                >
                                  {rules.entity_kinds.map((kind) => (
                                    <option key={kind.value} value={kind.value}>{kind.label}</option>
                                  ))}
                                </select>
                              </label>
                              {row.suggestedRoles.length > 0 ? (
                                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 8px' }}>
                                  {row.suggestedRoles.map((role) => (
                                    <label key={role} className="graph-checkbox">
                                      <input
                                        type="checkbox"
                                        style={{ width: 'auto' }}
                                        disabled={rowDisabled}
                                        checked={row.selectedRoles.includes(role)}
                                        onChange={(event) => {
                                          const checked = event.target.checked;
                                          updateRow(row.key, (prev) => ({
                                            ...prev,
                                            selectedRoles: checked
                                              ? [...prev.selectedRoles, role]
                                              : prev.selectedRoles.filter((item) => item !== role),
                                          }));
                                        }}
                                      />
                                      {roleLabel(rules, role)}
                                    </label>
                                  ))}
                                </div>
                              ) : (
                                <span className="muted">无建议角色</span>
                              )}
                            </div>
                          ) : (
                            <div style={{ display: 'grid', gap: 4 }}>
                              <span>{entityKindLabel(rules, row.entityKind)}</span>
                              {row.suggestedRoles.length > 0 ? (
                                <div className="graph-inspector-roles">
                                  {row.suggestedRoles.map((role) => (
                                    <span key={role} className="graph-role">{roleLabel(rules, role)}</span>
                                  ))}
                                </div>
                              ) : null}
                            </div>
                          )}
                        </td>

                        <td style={{ verticalAlign: 'top' }}>
                          <div style={{ display: 'grid', gap: 4 }}>
                            {row.action === 'merge' && !row.done ? (
                              <label className="graph-checkbox">
                                <input
                                  type="checkbox"
                                  style={{ width: 'auto' }}
                                  disabled={rowDisabled}
                                  checked={row.addPrimaryAsAlias}
                                  onChange={(event) => {
                                    const checked = event.target.checked;
                                    updateRow(row.key, (prev) => ({ ...prev, addPrimaryAsAlias: checked }));
                                  }}
                                />
                                主名也记为别名
                              </label>
                            ) : null}
                            {row.candidateAliases.length === 0 ? (
                              <span className="muted">无新简称</span>
                            ) : (
                              row.candidateAliases.map((alias) => (
                                <label key={alias} className="graph-checkbox">
                                  <input
                                    type="checkbox"
                                    style={{ width: 'auto' }}
                                    disabled={rowDisabled || row.action === 'ignore'}
                                    checked={row.selectedAliases.includes(alias)}
                                    onChange={(event) => {
                                      const checked = event.target.checked;
                                      updateRow(row.key, (prev) => ({
                                        ...prev,
                                        selectedAliases: checked
                                          ? [...prev.selectedAliases, alias]
                                          : prev.selectedAliases.filter((item) => item !== alias),
                                      }));
                                    }}
                                  />
                                  {alias}
                                </label>
                              ))
                            )}
                          </div>
                        </td>

                        <td style={{ verticalAlign: 'top' }}>
                          <div style={{ display: 'grid', gap: 6 }}>
                            <span><strong>{row.mentionCount}</strong> 次提及</span>
                            {row.sources.map((src) => (
                              <div
                                key={src.segment_id}
                                style={{
                                  border: '1px solid var(--line)',
                                  borderRadius: 6,
                                  padding: '5px 8px',
                                  background: 'var(--warm)',
                                  fontSize: 12,
                                }}
                              >
                                <div className="muted">
                                  {src.material_title} · {src.page != null ? `第 ${src.page} 页` : '整份材料'}
                                </div>
                                <div style={{ marginTop: 2 }}>{src.excerpt}</div>
                                {onOpenSourcePreview ? (
                                  <button
                                    type="button"
                                    style={{ marginTop: 4 }}
                                    onClick={() => onOpenSourcePreview({
                                      fileVersionId: src.file_version_id,
                                      title: src.material_title,
                                      page: src.page ?? undefined,
                                    })}
                                  >
                                    查看原文
                                  </button>
                                ) : null}
                              </div>
                            ))}
                          </div>
                        </td>

                        <td style={{ verticalAlign: 'top' }}>
                          <div style={{ display: 'grid', gap: 6 }}>
                            <label>
                              操作
                              <select
                                disabled={rowDisabled}
                                value={row.action}
                                onChange={(event) => {
                                  const nextAction = event.target.value as RowAction;
                                  updateRow(row.key, (prev) => ({ ...prev, action: nextAction }));
                                }}
                              >
                                {row.existingEntityId ? (
                                  <>
                                    <option value="merge_existing">
                                      追加别名到「{row.existingEntityName ?? '已有主体'}」
                                    </option>
                                    <option value="ignore">忽略</option>
                                  </>
                                ) : (
                                  <>
                                    <option value="create">新建主体</option>
                                    <option value="merge">合并为已有主体别名</option>
                                    <option value="ignore">忽略</option>
                                  </>
                                )}
                              </select>
                            </label>

                            {row.action === 'merge' && !row.done ? (
                              <label>
                                目标已有主体
                                <select
                                  disabled={rowDisabled}
                                  value={row.targetEntityId}
                                  onChange={(event) => {
                                    const targetId = event.target.value;
                                    updateRow(row.key, (prev) => ({ ...prev, targetEntityId: targetId }));
                                  }}
                                >
                                  <option value="">请选择已有主体…</option>
                                  {activeEntities.map((entity) => (
                                    <option key={entity.id} value={entity.id}>{entity.name}</option>
                                  ))}
                                </select>
                              </label>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {progressText ? <p role="status" className="graph-notice-inline">{progressText}</p> : null}
            {summary ? <p role="status" className="graph-notice-inline">{summary}</p> : null}
            {submitError ? <p role="alert" className="graph-error-inline">错误：{submitError}</p> : null}

            <div className="work-actions" style={{ justifyContent: 'flex-end' }}>
              <button type="button" disabled={busy} onClick={onClose}>关闭</button>
              <button
                type="submit"
                disabled={readOnly || busy || actionableRows.length === 0}
                style={{
                  background: 'var(--accent)',
                  borderColor: 'var(--accent)',
                  color: '#fff',
                  fontWeight: 650,
                }}
              >
                {busy ? '写入中…' : '批量写入关系图'}
              </button>
            </div>
          </form>
        ) : (
          <div className="work-actions" style={{ justifyContent: 'flex-end' }}>
            <button type="button" onClick={onClose}>关闭</button>
          </div>
        )}
      </div>
    </dialog>
  );
}
