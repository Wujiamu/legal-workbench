import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ChangeReviewPanel } from '../progress/ChangeReviewPanel';
import { buildLitigationFixture } from '../progress/fixtures';
import { defaultRules } from '../progress/rules';
import { isApplePlatform, undoShortcutLabel } from './shortcuts';

describe('shortcuts', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('returns Apple shortcut on macOS and Ctrl+Z on Windows/Linux', () => {
    expect(isApplePlatform({ platform: 'MacIntel', userAgent: '' })).toBe(true);
    expect(undoShortcutLabel({ platform: 'MacIntel', userAgent: '' })).toBe('⌘Z');

    expect(isApplePlatform({ platform: 'Win32', userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' })).toBe(false);
    expect(undoShortcutLabel({ platform: 'Win32', userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' })).toBe('Ctrl+Z');

    expect(undoShortcutLabel({ platform: 'Linux x86_64', userAgent: 'Mozilla/5.0 (X11; Linux x86_64)' })).toBe('Ctrl+Z');
  });

  it('renders Ctrl+Z tooltip on Windows and ⌘Z tooltip on macOS in ChangeReviewPanel', () => {
    const executor = buildLitigationFixture();
    const platformSpy = vi.spyOn(navigator, 'platform', 'get');
    const uaSpy = vi.spyOn(navigator, 'userAgent', 'get');

    platformSpy.mockReturnValue('Win32');
    uaSpy.mockReturnValue('Mozilla/5.0 (Windows NT 10.0; Win64; x64)');
    render(
      <ChangeReviewPanel
        snapshot={executor.getSnapshot()}
        rules={defaultRules}
        undoDepth={1}
        onUndo={() => {}}
        onCommand={async (cmd) => executor.human(cmd)}
      />,
    );
    expect(screen.getByRole('button', { name: '撤销（1）' }).getAttribute('title')).toBe('撤销本区域最近一步（Ctrl+Z）');

    cleanup();
    platformSpy.mockReturnValue('MacIntel');
    uaSpy.mockReturnValue('Mozilla/5.0 (Macintosh; Intel Mac OS X)');
    render(
      <ChangeReviewPanel
        snapshot={executor.getSnapshot()}
        rules={defaultRules}
        undoDepth={1}
        onUndo={() => {}}
        onCommand={async (cmd) => executor.human(cmd)}
      />,
    );
    expect(screen.getByRole('button', { name: '撤销（1）' }).getAttribute('title')).toBe('撤销本区域最近一步（⌘Z）');
  });
});
