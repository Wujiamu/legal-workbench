import { useEffect, useRef } from 'react';
import { THEME_OPTIONS, themeMetaOf, useTheme } from '../ui/theme';
import type { ThemeId } from '../ui/theme';

/**
 * 素章 · PlainFolio 品牌标识（图形 C：编简成章）。
 * 三列高矮错落的素白简册代表多源材料，一道低饱和绛印封签横贯编联成“章”；
 * 颜色绑定主题 CSS 变量，切换界面配色时实时响应。
 */
export function BrandMark({ size = 26 }: { size?: number }) {
  return (
    <svg
      className="app-brand-mark"
      width={size}
      height={size}
      viewBox="0 0 512 512"
      aria-hidden="true"
    >
      <rect
        x="20"
        y="20"
        width="472"
        height="472"
        rx="106"
        fill="var(--brand-bg, #1b1c1f)"
        stroke="var(--brand-border, #45474e)"
        strokeWidth="10"
      />
      <rect x="120" y="106" width="74" height="300" rx="15" fill="var(--brand-slip, #f6f3ec)" />
      <rect x="219" y="138" width="74" height="268" rx="15" fill="var(--brand-slip, #f6f3ec)" fillOpacity="0.91" />
      <rect x="318" y="118" width="74" height="288" rx="15" fill="var(--brand-slip, #f6f3ec)" fillOpacity="0.82" />
      <rect x="94" y="234" width="324" height="54" rx="13" fill="var(--brand-band, #a34b42)" />
      <circle cx="157" cy="261" r="9.5" fill="var(--brand-slip, #f6f3ec)" />
      <circle cx="256" cy="261" r="9.5" fill="var(--brand-slip, #f6f3ec)" />
      <circle cx="355" cy="261" r="9.5" fill="var(--brand-slip, #f6f3ec)" />
    </svg>
  );
}

export function AppBrand() {
  return (
    <span className="app-brand">
      <BrandMark size={26} />
      <span>素章</span>
    </span>
  );
}

export function ThemeSwitcher() {
  const [theme, setTheme] = useTheme();
  const detailsRef = useRef<HTMLDetailsElement>(null);
  const current = themeMetaOf(theme);

  useEffect(() => {
    function onPointerDown(event: PointerEvent) {
      const el = detailsRef.current;
      if (el?.open && event.target instanceof Node && !el.contains(event.target)) {
        el.open = false;
      }
    }
    function onKeyDown(event: KeyboardEvent) {
      const el = detailsRef.current;
      if (el?.open && event.key === 'Escape') {
        el.open = false;
        el.querySelector('summary')?.focus();
      }
    }
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, []);

  function choose(next: ThemeId) {
    setTheme(next);
    if (detailsRef.current) {
      detailsRef.current.open = false;
      detailsRef.current.querySelector('summary')?.focus();
    }
  }

  return (
    <details ref={detailsRef} className="theme-switcher">
      <summary className="theme-trigger" aria-label={`界面配色：${current.label}`}>
        <span
          className="theme-dot"
          aria-hidden="true"
          style={{ background: current.bandHex }}
        />
        <span>配色</span>
      </summary>
      <div className="theme-popover" role="group" aria-label="选择界面配色方案">
        {THEME_OPTIONS.map((item) => {
          const active = item.id === theme;
          return (
            <button
              key={item.id}
              type="button"
              className="theme-option"
              aria-pressed={active}
              onClick={() => choose(item.id)}
            >
              <span className="theme-swatches" aria-hidden="true">
                <span style={{ background: item.shellHex }} />
                <span style={{ background: item.accentHex }} />
                <span style={{ background: item.bandHex }} />
              </span>
              <span className="theme-option-text">
                <span className="theme-option-label">
                  {item.label}
                  {active ? <span className="theme-option-check">当前</span> : null}
                </span>
                <small>{item.subtitle}</small>
              </span>
            </button>
          );
        })}
      </div>
    </details>
  );
}
