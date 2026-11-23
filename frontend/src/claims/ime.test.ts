import { describe, expect, it } from 'vitest';
import type { KeyboardEvent as ReactKeyboardEvent } from 'react';
import { isImeEnter, shouldBlockFormEnter } from './ime';

function reactEnter(overrides: { isComposing?: boolean; keyCode?: number; tagName?: string } = {}) {
  return {
    key: 'Enter',
    target: { tagName: overrides.tagName ?? 'INPUT' },
    nativeEvent: { isComposing: overrides.isComposing ?? false, keyCode: overrides.keyCode ?? 13 },
  } as unknown as ReactKeyboardEvent<HTMLFormElement>;
}

describe('中文输入法回车保护', () => {
  it('组词中的回车与 keyCode 229 被识别', () => {
    expect(isImeEnter({ key: 'Enter', isComposing: true, keyCode: 13 } as unknown as KeyboardEvent)).toBe(true);
    expect(isImeEnter({ key: 'Enter', isComposing: false, keyCode: 229 } as unknown as KeyboardEvent)).toBe(true);
    expect(isImeEnter({ key: 'Enter', isComposing: false, keyCode: 13 } as unknown as KeyboardEvent)).toBe(false);
    expect(isImeEnter({ key: 'a', isComposing: true, keyCode: 65 } as unknown as KeyboardEvent)).toBe(false);
  });

  it('组词中或 compositionend 后的短窗口内阻止表单回车', () => {
    const now = 1_000_000;
    expect(shouldBlockFormEnter(reactEnter(), true, 0, now)).toBe(true);
    expect(shouldBlockFormEnter(reactEnter(), false, now - 10, now)).toBe(true);
    expect(shouldBlockFormEnter(reactEnter(), false, now - 100, now)).toBe(false);
    expect(shouldBlockFormEnter(reactEnter({ isComposing: true }), false, 0, now)).toBe(true);
  });

  it('textarea 的回车保留换行，不拦截', () => {
    expect(shouldBlockFormEnter(reactEnter({ tagName: 'TEXTAREA' }), true, 1_000_000, 1_000_000)).toBe(false);
  });

  it('非回车不拦截', () => {
    const event = { ...reactEnter(), key: 'a' } as ReactKeyboardEvent<HTMLFormElement>;
    expect(shouldBlockFormEnter(event, true, 0, 1_000_000)).toBe(false);
  });
});
