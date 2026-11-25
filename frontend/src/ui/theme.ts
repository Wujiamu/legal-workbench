/**
 * Multi-theme state & persistence for 素章 · PlainFolio.
 *
 * Pure client-side preference persisted in localStorage ('suzhang-theme') and
 * reflected on <html data-theme="..."> + <meta name="theme-color">. Exported
 * SVGs/PDFs deliberately stay on fixed print-safe colours and do not read this.
 */
import { useEffect, useState } from 'react';

export type ThemeId = 'xuan' | 'pure' | 'ruyao' | 'zitan' | 'mono';

export interface ThemeOption {
  id: ThemeId;
  label: string;
  subtitle: string;
  shellHex: string;
  paperHex: string;
  accentHex: string;
  bandHex: string;
}

export const STORAGE_KEY = 'suzhang-theme';
export const DEFAULT_THEME: ThemeId = 'xuan';
const THEME_EVENT = 'suzhang-theme-change';

export const THEME_OPTIONS: readonly ThemeOption[] = [
  {
    id: 'xuan',
    label: '宣素 · 青黛绛印',
    subtitle: '生宣暖白配松烟墨，低饱和古印泥点睛（默认）',
    shellHex: '#1b1c1f',
    paperHex: '#f6f4ef',
    accentHex: '#2a4f63',
    bandHex: '#a34b42',
  },
  {
    id: 'pure',
    label: '全素 · 白纸浓墨',
    subtitle: '浅色通透顶栏与浓墨黑交互，极简纸本感',
    shellHex: '#faf9f6',
    paperHex: '#f3f2ee',
    accentHex: '#1f2024',
    bandHex: '#9b483f',
  },
  {
    id: 'ruyao',
    label: '汝素 · 天青砚灰',
    subtitle: '宋汝窑天青配歙砚青灰，清冷护眼',
    shellHex: '#1e272e',
    paperHex: '#f2f5f6',
    accentHex: '#2f5d6e',
    bandHex: '#9e5a42',
  },
  {
    id: 'zitan',
    label: '麻素 · 紫檀镇纸',
    subtitle: '低饱和乌木老紫檀配原麻暖纸，沉静温润',
    shellHex: '#2a2123',
    paperHex: '#f5f2ed',
    accentHex: '#633a42',
    bandHex: '#824b53',
  },
  {
    id: 'mono',
    label: '墨素 · 纯粹灰度',
    subtitle: '全站零彩色纯粹灰度，专注沉浸阅卷',
    shellHex: '#1f1f1f',
    paperHex: '#f2f2f2',
    accentHex: '#262626',
    bandHex: '#6e6e6e',
  },
];

export function isThemeId(value: string | null | undefined): value is ThemeId {
  return value === 'xuan' || value === 'pure' || value === 'ruyao' || value === 'zitan' || value === 'mono';
}

export function themeMetaOf(id: ThemeId): ThemeOption {
  return THEME_OPTIONS.find((item) => item.id === id) ?? THEME_OPTIONS[0];
}

export function readCurrentTheme(): ThemeId {
  if (typeof document !== 'undefined') {
    const attr = document.documentElement.dataset.theme;
    if (isThemeId(attr)) return attr;
  }
  try {
    const stored = typeof localStorage !== 'undefined' ? localStorage.getItem(STORAGE_KEY) : null;
    if (isThemeId(stored)) return stored;
  } catch {
    // Ignore storage access errors in restricted webviews.
  }
  return DEFAULT_THEME;
}

export function applyTheme(next: ThemeId, options: { persist?: boolean } = {}): void {
  const { persist = true } = options;
  const meta = themeMetaOf(next);
  if (typeof document !== 'undefined') {
    document.documentElement.dataset.theme = next;
    const themeColorMeta = document.querySelector('meta[name="theme-color"]');
    if (themeColorMeta) {
      themeColorMeta.setAttribute('content', meta.shellHex);
    }
  }
  if (persist && typeof localStorage !== 'undefined') {
    try {
      if (next === DEFAULT_THEME) {
        localStorage.removeItem(STORAGE_KEY);
      } else {
        localStorage.setItem(STORAGE_KEY, next);
      }
    } catch {
      // Theme still applies to DOM even when storage is unavailable.
    }
  }
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent<ThemeId>(THEME_EVENT, { detail: next }));
  }
}

export function useTheme(): [ThemeId, (next: ThemeId) => void] {
  const [theme, setThemeState] = useState<ThemeId>(() => readCurrentTheme());

  useEffect(() => {
    // Sync DOM attribute on initial mount in case tests render without index.html bootstrap.
    const current = readCurrentTheme();
    applyTheme(current, { persist: false });
    setThemeState(current);

    function handleCustom(event: Event) {
      const detail = (event as CustomEvent<ThemeId>).detail;
      if (isThemeId(detail)) setThemeState(detail);
    }

    function handleStorage(event: StorageEvent) {
      if (event.key !== null && event.key !== STORAGE_KEY) return;
      const resolved = isThemeId(event.newValue) ? event.newValue : DEFAULT_THEME;
      applyTheme(resolved, { persist: false });
      setThemeState(resolved);
    }

    window.addEventListener(THEME_EVENT, handleCustom);
    window.addEventListener('storage', handleStorage);
    return () => {
      window.removeEventListener(THEME_EVENT, handleCustom);
      window.removeEventListener('storage', handleStorage);
    };
  }, []);

  return [theme, (next: ThemeId) => applyTheme(next, { persist: true })];
}
