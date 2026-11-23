/** Small presentational primitives shared by every page. */
import type { CSSProperties, ReactNode } from 'react';
import { palette, radius, spacing, fontStack } from './tokens';

export function Button({
  children,
  onClick,
  type = 'button',
  disabled = false,
  variant = 'primary',
}: {
  children: ReactNode;
  onClick?: () => void;
  type?: 'button' | 'submit';
  disabled?: boolean;
  variant?: 'primary' | 'quiet' | 'danger';
}) {
  const base: CSSProperties = {
    font: `inherit`,
    fontFamily: fontStack,
    fontSize: 14,
    padding: `${spacing.sm}px ${spacing.md}px`,
    borderRadius: radius.sm,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: palette.borderStrong,
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.55 : 1,
    background: palette.surface,
    color: palette.text,
  };
  const variants: Record<string, CSSProperties> = {
    primary: { background: palette.accent, borderColor: palette.accent, color: '#ffffff' },
    quiet: {},
    danger: { background: palette.dangerSoft, borderColor: palette.danger, color: palette.danger },
  };
  return (
    <button type={type} onClick={onClick} disabled={disabled} style={{ ...base, ...variants[variant] }}>
      {children}
    </button>
  );
}

export function Field({
  label,
  controlId,
  children,
  hint,
}: {
  label: string;
  controlId: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <div className="field">
      <label className="field-label" htmlFor={controlId}>{label}</label>
      {children}
      {hint ? <span className="field-hint" id={`${controlId}-hint`}>{hint}</span> : null}
    </div>
  );
}

export const inputStyle: CSSProperties = {
  width: '100%',
  boxSizing: 'border-box',
  fontFamily: fontStack,
  fontSize: 14,
  padding: `${spacing.sm}px ${spacing.md}px`,
  border: `1px solid ${palette.border}`,
  borderRadius: radius.sm,
  background: palette.surface,
  color: palette.text,
};

export function StatusText({ tone, children }: { tone: 'ok' | 'warn' | 'error' | 'muted'; children: ReactNode }) {
  const tones = {
    ok: palette.accent,
    warn: palette.warning,
    error: palette.danger,
    muted: palette.textMuted,
  } as const;
  return (
    <span style={{ color: tones[tone], fontSize: 13 }}>
      {/* Prefix is text, not colour: status must read without colour perception. */}
      {tone === 'error' ? '错误：' : tone === 'warn' ? '注意：' : ''}
      {children}
    </span>
  );
}

export function EmptyState({ title, detail }: { title: string; detail?: string }) {
  return (
    <div style={{ padding: spacing.xl, color: palette.textMuted }}>
      <p style={{ margin: 0, fontSize: 15, color: palette.text }}>{title}</p>
      {detail ? <p style={{ margin: `${spacing.sm}px 0 0`, fontSize: 13 }}>{detail}</p> : null}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div
      role="alert"
      style={{
        padding: spacing.md,
        border: `1px solid ${palette.danger}`,
        background: palette.dangerSoft,
        color: palette.danger,
        borderRadius: radius.sm,
        fontSize: 13,
      }}
    >
      <span>错误：{message}</span>
      {onRetry ? (
        <span style={{ marginLeft: spacing.md }}>
          <Button variant="quiet" onClick={onRetry}>
            重试
          </Button>
        </span>
      ) : null}
    </div>
  );
}

export function LoadingState({ label = '加载中…' }: { label?: string }) {
  return <p style={{ color: palette.textMuted, fontSize: 13, padding: spacing.md }}>{label}</p>;
}

export function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <h2 style={{ fontSize: 13, letterSpacing: '0.04em', textTransform: 'uppercase',
                 color: palette.textMuted, margin: `0 0 ${spacing.sm}px` }}>
      {children}
    </h2>
  );
}
