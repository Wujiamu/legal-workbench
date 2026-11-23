import { useEffect, useRef, useState } from 'react';
import { Button } from '../ui/primitives';
import { SendToGraphDialog } from './SendToGraphDialog';

export function SourcePreview({ caseId, versionId, name, page, frame, raster, original = true,
  defaultOpen = false, segmentId, annotationId, archived = false, excerpt }: {
  caseId: string;
  versionId: string;
  name: string;
  page?: number;
  frame?: number;
  raster?: boolean;
  original?: boolean;
  /** 由调用方控制打开（例如出处行点击后直接显示预览，不再多一次按钮点击）。 */
  defaultOpen?: boolean;
  segmentId?: string;
  annotationId?: string;
  archived?: boolean;
  excerpt?: string;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [open, setOpen] = useState(defaultOpen);
  const [imageState, setImageState] = useState<'loading' | 'loaded' | 'failed'>('loading');
  const [sendOpen, setSendOpen] = useState(false);
  const [sentNotice, setSentNotice] = useState(false);
  const canSendToGraph = Boolean(segmentId || annotationId);
  const base = `/api/v1/cases/${caseId}/file-versions/${versionId}`;
  const fullFile = `${base}/preview`;
  const preview = page ? `${base}/page-preview?page=${page}` : `${fullFile}${frame === undefined ? '' : `?frame=${frame}`}`;
  const image = !!page || raster || /\.(png|jpe?g|gif|webp|tiff?|bmp)$/i.test(name);
  const showFullFile = !!page && imageState === 'failed';
  const label = original ? '原件' : '对应版本';
  const title = `${name}${page && !showFullFile ? ` · 第 ${page} 页` : showFullFile && page ? ' · 完整文件' : ''}`;
  useEffect(() => { if (open) dialog.current?.showModal(); }, [open]);
  return <>
    {defaultOpen ? null : (
      <Button variant="quiet" onClick={() => { setImageState('loading'); setSentNotice(false); setOpen(true); }}>
        {page ? `打开${label}第 ${page} 页` : `查看${label}`}
      </Button>
    )}
    {open ? <dialog ref={dialog} className="work-dialog preview-dialog" aria-label={`${label}预览`}
                    onClose={() => { setSendOpen(false); setOpen(false); }}>
      <div className="page-heading"><h2>{title}</h2>
        <Button variant="quiet" onClick={() => dialog.current?.close()}>关闭预览</Button>
      </div>
      {image && !showFullFile ? <>
        {imageState === 'loading' ? <p role="status">正在加载文件…</p> : null}
        {imageState === 'failed' ? <p role="alert">无法读取此位置。可尝试打开完整文件或下载核对。</p> : null}
        <img hidden={imageState === 'failed'} src={preview} alt={page ? `原文件第 ${page} 页` : name}
             onLoad={() => setImageState('loaded')}
             onError={() => setImageState('failed')} />
      </> : <>
        {page ? <p role="status">无法显示第 {page} 页的图片，已改为显示完整文件。</p> : null}
        <iframe title={`${name}${page ? '完整文件' : ''}预览`} src={showFullFile ? fullFile : preview} />
      </>}
      <div className="work-actions"><a href={fullFile} target="_blank" rel="noreferrer">打开完整文件</a>
        <a href={`${base}/download`}>下载文件</a>
        {canSendToGraph ? (
          <Button variant="quiet" disabled={archived} onClick={() => setSendOpen(true)}>
            送入关系图
          </Button>
        ) : null}
      </div>
      {sentNotice ? <p role="status">已送入关系图</p> : null}
    </dialog> : null}
    {sendOpen && canSendToGraph ? (
      <SendToGraphDialog
        caseId={caseId}
        segmentId={segmentId}
        annotationId={annotationId}
        excerpt={excerpt}
        onClose={() => setSendOpen(false)}
        onCreated={() => setSentNotice(true)}
      />
    ) : null}
  </>;
}
