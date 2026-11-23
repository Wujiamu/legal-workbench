/**
 * SourcePreview 定向回归（TEST-ONLY 合成数据，无后端、无真实端口）。
 *
 * 覆盖：正常页图仍显示；页图 onError 后自动切换到完整文件 iframe 并说明原因；
 * 原件链接与下载保留；非图片（如 docx）仍直接用 iframe 显示完整文件。
 */
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { SourcePreview } from './SourcePreview';

const CASE = 'case-test';
const VERSION = 'ver-test';
const BASE = `/api/v1/cases/${CASE}/file-versions/${VERSION}`;

afterEach(cleanup);

describe('SourcePreview', () => {
  it('页图正常加载时显示该页图片，不出现完整文件 iframe', () => {
    render(<SourcePreview caseId={CASE} versionId={VERSION} name="TEST-ONLY 判决书.pdf" page={3} defaultOpen />);
    const dialog = screen.getByRole('dialog', { name: '原件预览' });
    const image = within(dialog).getByRole('img') as HTMLImageElement;
    expect(image.getAttribute('src')).toBe(`${BASE}/page-preview?page=3`);
    expect(image.hidden).toBe(false);
    expect(dialog.textContent).toContain('第 3 页');
    expect(within(dialog).queryByTitle(/预览$/)).toBeNull();
  });

  it('页图加载失败后自动改为显示完整文件，并说明已显示完整文件', () => {
    render(<SourcePreview caseId={CASE} versionId={VERSION} name="TEST-ONLY 判决书.pdf" page={3} defaultOpen />);
    const dialog = screen.getByRole('dialog', { name: '原件预览' });
    fireEvent.error(within(dialog).getByRole('img'));

    const frame = within(dialog).getByTitle('TEST-ONLY 判决书.pdf完整文件预览') as HTMLIFrameElement;
    expect(frame.getAttribute('src')).toBe(`${BASE}/preview`);
    expect(within(dialog).queryByRole('img')).toBeNull();
    expect(within(dialog).getByRole('status').textContent).toContain('已改为显示完整文件');
    expect(dialog.textContent).toContain('完整文件');
  });

  it('切换后仍保留打开完整文件与下载入口', () => {
    render(<SourcePreview caseId={CASE} versionId={VERSION} name="TEST-ONLY 判决书.pdf" page={3} defaultOpen />);
    const dialog = screen.getByRole('dialog', { name: '原件预览' });
    fireEvent.error(within(dialog).getByRole('img'));

    const full = within(dialog).getByRole('link', { name: '打开完整文件' }) as HTMLAnchorElement;
    const download = within(dialog).getByRole('link', { name: '下载文件' }) as HTMLAnchorElement;
    expect(full.getAttribute('href')).toBe(`${BASE}/preview`);
    expect(full.getAttribute('target')).toBe('_blank');
    expect(download.getAttribute('href')).toBe(`${BASE}/download`);
  });

  it('非图片材料（docx）直接用 iframe 显示完整文件，无失败提示', () => {
    render(<SourcePreview caseId={CASE} versionId={VERSION} name="TEST-ONLY 代理意见.docx" defaultOpen />);
    const dialog = screen.getByRole('dialog', { name: '原件预览' });
    const frame = within(dialog).getByTitle('TEST-ONLY 代理意见.docx预览') as HTMLIFrameElement;
    expect(frame.getAttribute('src')).toBe(`${BASE}/preview`);
    expect(within(dialog).queryByRole('status')).toBeNull();
  });
});
