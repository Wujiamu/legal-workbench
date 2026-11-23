/**
 * Design tokens and layout primitives.
 *
 * Neutral light palette, system Chinese font stack, and status that is always
 * expressed with text as well as colour so it survives colour-blind viewing and
 * greyscale printing.
 */
export const palette = {
  background: 'var(--page-background)',
  surface: '#ffffff',
  border: '#d9d7d2',
  borderStrong: '#b9b6ae',
  text: 'var(--page-text)',
  textMuted: 'var(--field-muted)',
  accent: '#2f5d50',
  accentSoft: '#e8efec',
  warning: '#8a5a00',
  warningSoft: '#fdf3df',
  danger: '#8c2f2f',
  dangerSoft: '#fbeaea',
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
