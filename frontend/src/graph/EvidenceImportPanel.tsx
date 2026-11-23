/**
 * 证据目录外部导入（真实入口）：只用已上传到本案的材料作为来源，
 * 解析预览不写库；提交时每一行必须提供材料映射，且必须选定该案的
 * import_material_id，否则后端会拒绝，这里提前禁用提交并说明原因。
 */
import { useMemo, useState } from 'react';
import { describeError } from '../api/client';
import { evidenceApi, materialsApi } from '../api/endpoints';
import type { GraphSnapshot } from './types';

interface DraftRow {
  key: string;
  group_title: string;
  item_title: string;
  item_purpose: string | null;
  material_id: string;
  page_start: number | null;
  page_end: number | null;
  /** 解析出的材料名（用于默认映射与提示）。 */
  material_title: string | null;
}

function matchMaterial(rows: { id: string; title: string }[], title: string | null): string {
  if (!title) return '';
  const exact = rows.filter((row) => row.title === title);
  return exact.length === 1 ? exact[0].id : '';
}

export function EvidenceImportPanel({ caseId, ws, readOnly, onImported }: {
  caseId: string;
  ws: GraphSnapshot;
  readOnly: boolean;
  /** 导入成功后让目录重新读取（编号与待确认徽标随之刷新）。 */
  onImported: () => void;
}) {
  const [importMaterialId, setImportMaterialId] = useState('');
  const [rows, setRows] = useState<DraftRow[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const materials = useMemo(
    () => ws.materials.map((material) => ({ id: material.id, title: material.title })),
    [ws.materials]);

  async function parse() {
    if (!importMaterialId) { setError('请先选择已上传到本案的目录文件。'); return; }
    const material = ws.materials.find((entry) => entry.id === importMaterialId);
    const name = material?.title ?? '';
    // 解析只认 .docx / .xlsx / .csv；材料名就是原始文件名，后缀不符时先说清楚再传。
    if (!/\.(docx|xlsx|csv)$/i.test(name)) {
      setError('该材料的文件名不是 .docx / .xlsx / .csv，无法作为证据目录解析。');
      return;
    }
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const original = await materialsApi.original(caseId, importMaterialId);
      const blob = await materialsApi.downloadBlob(caseId, original.file_version_id);
      const form = new FormData();
      form.append('file', new File([blob], name));
      const preview = await evidenceApi.previewImport(caseId, form);
      setRows(preview.rows.map((row, index) => ({
        key: `${index}-${row.group_title}-${row.item_title}`,
        group_title: row.group_title,
        item_title: row.item_title,
        item_purpose: row.item_purpose ?? null,
        material_id: matchMaterial(materials, row.material_title ?? null),
        page_start: row.page_start ?? null,
        page_end: row.page_end ?? null,
        material_title: row.material_title ?? null,
      })));
      setWarnings(preview.warnings ?? []);
      if (preview.rows.length === 0) setError('该文件里没有解析到证据行；可换一份表格或 CSV。');
    } catch (cause) {
      setError(describeError(cause) || '目录解析失败');
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (!importMaterialId) { setError('提交前必须选定本案的导入材料（import_material_id）。'); return; }
    if (rows.length === 0) { setError('尚未解析出待导入的目录行。'); return; }
    const unmapped = rows.filter((row) => !row.material_id).length;
    setBusy(true);
    setError('');
    try {
      const out = await evidenceApi.importCatalog(caseId, {
        import_material_id: importMaterialId,
        request_key: `web-import-${importMaterialId}-${Date.now()}`,
        rows: rows.map((row) => ({
          group_title: row.group_title,
          group_purpose: null,
          item_title: row.item_title,
          item_purpose: row.item_purpose,
          bindings: row.material_id
            ? [{ material_id: row.material_id, page_start: row.page_start, page_end: row.page_end }]
            : [],
        })),
      });
      setNotice(out.notice
        ?? `已提交 ${rows.length} 条待确认草稿${unmapped > 0 ? `（其中 ${unmapped} 条未绑定材料）` : ''}；确认前不参与编号。`);
      setRows([]);
      setWarnings([]);
      onImported();
    } catch (cause) {
      setError(describeError(cause) || '导入失败');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="graph-ev-import form-block" aria-label="导入外部证据目录">
      <header className="graph-ev-import-head">
        <h3>导入外部目录（Word / Excel / CSV）</h3>
        <p className="muted graph-hint">
          目录文件须先通过「导入材料」上传到本案；解析只生成草稿，人工确认前不参与编号。
        </p>
      </header>
      {readOnly ? (
        <p role="status" className="graph-archived">案件已归档：目录导入只读。</p>
      ) : (
        <>
          <div className="graph-form-row">
            <label>导入材料（import_material_id）
              <select value={importMaterialId} disabled={busy}
                onChange={(event) => setImportMaterialId(event.target.value)}>
                <option value="">选择已上传的目录文件…</option>
                {materials.map((material) => (
                  <option key={material.id} value={material.id}>{material.title}</option>
                ))}
              </select>
            </label>
            <button type="button" disabled={busy || !importMaterialId} onClick={() => void parse()}>
              {busy ? '处理中…' : '解析预览'}
            </button>
          </div>
          {warnings.map((warning) => (
            <p key={warning} role="note" className="graph-hint warn">{warning}</p>
          ))}
          {rows.length > 0 ? (
            <div className="graph-ev-draft">
              <p>
                <strong>解析出 {rows.length} 行草稿</strong>：请为每行确认对应材料，
                未选择材料的行仍会导入为未绑定草稿。
              </p>
              <ul className="graph-ev-draft-list">
                {rows.map((row, index) => (
                  <li key={row.key}>
                    <span className="graph-ev-draft-no">{index + 1}</span>
                    <span className="graph-ev-draft-title">
                      <strong>{row.group_title}</strong> · {row.item_title}
                      {row.material_title ? <span className="muted">（原文材料：{row.material_title}）</span> : null}
                    </span>
                    <label>对应材料
                      <select value={row.material_id} disabled={busy}
                        onChange={(event) => {
                          const value = event.target.value;
                          setRows((current) => current.map((entry) => entry.key === row.key
                            ? { ...entry, material_id: value } : entry));
                        }}>
                        <option value="">不绑定</option>
                        {materials.map((material) => (
                          <option key={material.id} value={material.id}>{material.title}</option>
                        ))}
                      </select>
                    </label>
                    <span className="muted">
                      {row.page_start
                        ? (row.page_end && row.page_end !== row.page_start
                          ? `第${row.page_start}–${row.page_end}页` : `第${row.page_start}页`)
                        : '整份材料'}
                    </span>
                  </li>
                ))}
              </ul>
              <div className="graph-form-actions">
                <button type="button" disabled={busy} onClick={() => setRows([])}>放弃草稿</button>
                <button type="button" disabled={busy || !importMaterialId} onClick={() => void submit()}>
                  {busy ? '提交中…' : `提交导入（${rows.length} 条）`}
                </button>
              </div>
            </div>
          ) : null}
        </>
      )}
      {notice ? <p role="status" className="graph-notice-inline">{notice}</p> : null}
      {error ? <p role="alert" className="graph-error-inline">错误：{error}</p> : null}
    </section>
  );
}
