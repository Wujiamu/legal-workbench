export interface PlatformNavigatorLike {
  platform?: string;
  userAgent?: string;
}

/** 按当前浏览器平台判断是否使用 Apple 修饰键（⌘），其余平台统一展示 Ctrl。 */
export function isApplePlatform(
  nav: PlatformNavigatorLike | undefined = typeof navigator !== 'undefined' ? navigator : undefined,
): boolean {
  if (!nav) return false;
  const platform = nav.platform ?? '';
  const userAgent = nav.userAgent ?? '';
  return /Mac|iPhone|iPad|iPod/i.test(platform) || /Mac OS|Macintosh|iPhone|iPad|iPod/i.test(userAgent);
}

export function undoShortcutLabel(nav?: PlatformNavigatorLike): string {
  return isApplePlatform(nav) ? '⌘Z' : 'Ctrl+Z';
}
