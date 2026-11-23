/**
 * 中文输入法保护：组词期间按回车确认候选，不能提交表单。
 * Safari 在确认候选的回车里可能已把 isComposing 置为 false，因此同时看 keyCode 229
 * 与 compositionend 后的短保护窗口（由调用方的 composing ref 提供）。
 */
import type { KeyboardEvent as ReactKeyboardEvent } from 'react';

export function isImeEnter(event: KeyboardEvent | ReactKeyboardEvent): boolean {
  if (event.key !== 'Enter') return false;
  const native = 'nativeEvent' in event ? event.nativeEvent : event;
  return Boolean(native.isComposing) || native.keyCode === 229;
}

/** 表单级守卫：仅拦截输入框/下拉的回车；textarea 的回车保留换行。 */
export function shouldBlockFormEnter(
  event: ReactKeyboardEvent<HTMLFormElement>,
  composing: boolean,
  compositionEndedAt: number,
  now = Date.now(),
): boolean {
  if (event.key !== 'Enter') return false;
  const target = event.target as HTMLElement | null;
  if (target?.tagName === 'TEXTAREA') return false;
  return composing || now - compositionEndedAt < 60 || isImeEnter(event);
}
