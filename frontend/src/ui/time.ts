/**
 * Time display.
 *
 * The API speaks UTC (`timestamptz`), the user is in China: the plan requires
 * the page to show Asia/Shanghai.  Formatting is pinned to that zone explicitly
 * rather than relying on the browser locale, so a machine set to another
 * timezone still shows the same wall clock.
 */
export const DISPLAY_TIME_ZONE = 'Asia/Shanghai';

const formatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: DISPLAY_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

/** `2026-11-10T04:05:06Z` -> `2026/11/10 12:05`. Returns '' for unusable input. */
export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  // zh-CN renders "2026/11/10 12:05"; keep it stable across ICU versions.
  return formatter.format(date).replace(/\u200e|\u200f/g, '');
}

const dayFormatter = new Intl.DateTimeFormat('sv-SE', {
  timeZone: DISPLAY_TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit',
});

/**
 * “今天”的唯一日期口径：`YYYY-MM-DD`，按 Asia/Shanghai 计算。
 *
 * 首页期限时间轴与案件表的逾期判断共用它，避免浏览器系统时区把午夜前后的
 * 日期算偏；`now` 只在测试里注入。
 */
export function shanghaiToday(now: Date = new Date()): string {
  return dayFormatter.format(now);
}

/** 在本地日历日期上加减天数，按 UTC 中午运算以避开夏令时与闰秒。 */
export function addDays(iso: string, days: number): string {
  const [year, month, day] = iso.split('-').map(Number);
  const shifted = new Date(Date.UTC(year, month - 1, day) + days * 86_400_000);
  return shifted.toISOString().slice(0, 10);
}

const weekdayFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'UTC', weekday: 'short',
});

/** `2026-11-21` -> `11月21日 周六`（不依赖系统时区）。 */
export function formatDayLabel(iso: string): string {
  const [year, month, day] = iso.split('-').map(Number);
  if (!year || !month || !day) return iso;
  const weekday = weekdayFormatter.format(new Date(Date.UTC(year, month - 1, day)));
  return `${month}月${day}日 ${weekday}`;
}
