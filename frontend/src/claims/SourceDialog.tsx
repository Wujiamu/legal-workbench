/**
 * 模拟原文预览（P1）：以 TEST-ONLY 合成片段代替真实 SourcePreview。
 * 不请求 /api 文件接口；P2 接入后改为复用 SourcePreview 打开原件/原页。
 */
import { Modal } from './Modal';
import type { ClaimSource } from './types';

export function SourceExcerpt({ source }: { source: ClaimSource }) {
  const { excerpt, span, raw_text: rawText } = source;
  const valid = span.start >= 0 && span.end <= excerpt.length && span.end > span.start;
  if (!valid) return <p className="claims-source-text">{excerpt}</p>;
  return (
    <p className="claims-source-text">
      {excerpt.slice(0, span.start)}
      <mark>{excerpt.slice(span.start, span.end)}</mark>
      {excerpt.slice(span.end)}
    </p>
  );
}

export function SourceDialog({ source, onClose }: { source: ClaimSource; onClose: () => void }) {
  return (
    <Modal labelledBy="claims-source-heading" className="work-dialog claims-source-dialog" onClose={onClose}>
      <div className="claims-dialog-head">
        <h2 id="claims-source-heading">模拟原文</h2>
        <span className="agent-badge agent-pending">TEST-ONLY</span>
      </div>
      <p className="muted">
        {source.material_title} · 第 {source.page} 页 · {source.locator}
      </p>
      <SourceExcerpt source={source} />
      <dl className="claims-source-meta">
        <div><dt>抽取原文</dt><dd>{source.raw_text}</dd></div>
        <div><dt>片段引用</dt><dd><code>{source.segment_id}</code></dd></div>
      </dl>
      <p className="muted claims-source-note">
        P1 原型用合成原文演示“从金额回溯到材料位置”；不读取真实文件。P2 接入后改为通过
        SourcePreview 打开原件或原页。
      </p>
      <div className="work-actions">
        <button type="button" onClick={onClose}>关闭</button>
      </div>
    </Modal>
  );
}
