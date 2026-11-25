import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AppBrand, ThemeSwitcher } from '../components/BrandMark';
import { DEFAULT_THEME, STORAGE_KEY, THEME_OPTIONS } from './theme';

describe('素章品牌与五套实时主题切换', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.dataset.theme = DEFAULT_THEME;
    let meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) {
      meta = document.createElement('meta');
      meta.setAttribute('name', 'theme-color');
      document.head.appendChild(meta);
    }
    meta.setAttribute('content', '#1b1c1f');
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('AppBrand 渲染新品牌名「素章」', () => {
    render(<AppBrand />);
    expect(screen.getByText('素章')).toBeTruthy();
  });

  it('ThemeSwitcher 列出 5 套主题，切换同步 DOM、meta 与 localStorage', () => {
    render(<ThemeSwitcher />);
    const summary = screen.getByLabelText(/界面配色：宣素/);
    fireEvent.click(summary);

    for (const opt of THEME_OPTIONS) {
      expect(screen.getByRole('button', { name: new RegExp(opt.label) })).toBeTruthy();
    }

    // 切到纯灰度主题 mono
    fireEvent.click(screen.getByRole('button', { name: /墨素 · 纯粹灰度/ }));
    expect(document.documentElement.dataset.theme).toBe('mono');
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content')).toBe('#1f1f1f');
    expect(localStorage.getItem(STORAGE_KEY)).toBe('mono');

    // 切到浅色全素主题 pure
    fireEvent.click(screen.getByRole('button', { name: /全素 · 白纸浓墨/ }));
    expect(document.documentElement.dataset.theme).toBe('pure');
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content')).toBe('#faf9f6');
    expect(localStorage.getItem(STORAGE_KEY)).toBe('pure');

    // 切回默认主题 xuan 时移除 localStorage 键
    fireEvent.click(screen.getByRole('button', { name: /宣素 · 青黛绛印/ }));
    expect(document.documentElement.dataset.theme).toBe('xuan');
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it('支持跨标签页 storage 事件同步与 Escape 关闭菜单', () => {
    const { container } = render(<ThemeSwitcher />);
    const details = container.querySelector('details')!;
    details.open = true;

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(details.open).toBe(false);

    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: STORAGE_KEY, newValue: 'zitan' }));
    });
    expect(document.documentElement.dataset.theme).toBe('zitan');
    expect(screen.getByLabelText(/界面配色：麻素/)).toBeTruthy();
  });

  it('localStorage 读写异常时仍安全应用到 DOM', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Blocked', 'SecurityError');
    });
    render(<ThemeSwitcher />);
    fireEvent.click(screen.getByRole('button', { name: /汝素 · 天青砚灰/ }));
    expect(document.documentElement.dataset.theme).toBe('ruyao');
  });
});
