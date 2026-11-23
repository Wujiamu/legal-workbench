/** Save a search result as an analysis with its source reference attached. */
import { useId, useState } from 'react';
import { describeError } from '../api/client';
import { analysisApi } from '../api/endpoints';
import { Button, Field, StatusText, inputStyle } from '../ui/primitives';
import { palette, spacing } from '../ui/tokens';

export function SaveAnalysisForm({ caseId, sourceRef, archived = false }: {
  caseId: string;
  sourceRef: string;
  archived?: boolean;
}) {
  const fieldId = useId();
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function save() {
    if (archived || !title.trim() || !content.trim()) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const created = await analysisApi.save(caseId, {
        title: title.trim(), content, statement_kind: 'agent_analysis',
        source_refs: sourceRef ? [sourceRef] : [],
      });
      setNotice(`已保存（版本 ${created.version_id.slice(0, 8)}…）。`);
      setTitle('');
      setContent('');
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section data-source-ref={sourceRef} style={{ marginTop: spacing.lg }}>
      <h3 style={{ fontSize: 13, color: palette.textMuted, margin: `0 0 ${spacing.sm}px` }}>
        把当前片段存为分析
      </h3>
      <Field label="标题" controlId={`${fieldId}-title`}>
        <input id={`${fieldId}-title`} value={title} disabled={busy || archived}
               onChange={(event) => setTitle(event.target.value)}
               style={inputStyle} />
      </Field>
      <Field label="正文（Markdown，按纯文本保存与显示）" controlId={`${fieldId}-content`}>
        <textarea id={`${fieldId}-content`} value={content} disabled={busy || archived}
                  onChange={(event) => setContent(event.target.value)}
                  rows={5} style={{ ...inputStyle, resize: 'vertical' }} />
      </Field>
      <p style={{ fontSize: 12, color: palette.textMuted, margin: `0 0 ${spacing.sm}px` }}>保存时附带当前来源引用。</p>
      {error ? <p><StatusText tone="error">{error}</StatusText></p> : null}
      {notice ? <p><StatusText tone="ok">{notice}</StatusText></p> : null}
      <Button onClick={() => void save()} disabled={archived || busy || !title.trim() || !content.trim()}>
        保存分析
      </Button>
    </section>
  );
}
