/**
 * Design tokens and layout primitives.
 *
 * Theme-reactive archival light palettes (driven by CSS custom properties on
 * :root[data-theme]), system Chinese font stack, and status that is always
 * expressed with text as well as colour so it survives colour-blind viewing and
 * greyscale printing.
 */
export const palette = {
  background: 'var(--page-background)',
  surface: 'var(--surface)',
  border: 'var(--line)',
  borderStrong: 'var(--border-strong)',
  text: 'var(--page-text)',
  textMuted: 'var(--field-muted)',
  accent: 'var(--accent)',
  accentSoft: 'var(--accent-soft)',
  warning: 'var(--warning)',
  warningSoft: 'var(--warning-soft)',
  danger: 'var(--danger)',
  dangerSoft: 'var(--danger-soft)',
} as const;

export const fontStack = 'var(--font-body)';

export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  xxl: 32,
} as const;

export const radius = { sm: 4, md: 6, lg: 10 } as const;
