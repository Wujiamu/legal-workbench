/**
 * 工时域（P1 原型，内存实现；用户反馈第七轮）。
 * 计时器 + 快捷时长 + 日视图 + 按标题 billable 汇总。
 * 独立于 progress 命令集：引用节点 ID 与标题快照，不改进度数据。
 * 时长粒度 0.25h（15 分钟），计时向上取整；归档只读由页面层守卫。
 */

export interface TimeEntry {
  id: string;
  case_id: string;
  node_id: string;
  node_title: string;
  /** 工作日期（本地日历日 YYYY-MM-DD）。 */
  date: string;
  hours: number;
  source: 'timer' | 'manual';
  started_at: string | null;
  stopped_at: string | null;
  note: string | null;
}

export type TimeResult =
  | { ok: true; entry: TimeEntry }
  | { ok: false; message: string };

export interface DayView {
  date: string;
  entries: TimeEntry[];
  totalHours: number;
}

export interface TitleSummaryRow {
  title: string;
  hours: number;
  entries: number;
}

export interface TimeLog {
  start(nodeId: string, nodeTitle: string, now?: () => number): TimeResult;
  stop(entryId: string, now?: () => number, override?: { hours?: number; note?: string }): TimeResult;
  add(nodeId: string, nodeTitle: string, hours: number, date: string, note?: string): TimeResult;
  remove(entryId: string): { ok: boolean; message?: string };
  running(): TimeEntry[];
  entries(): TimeEntry[];
  day(date: string): DayView;
  summaryByTitle(): TitleSummaryRow[];
}

const QUARTER_MS = 15 * 60 * 1000;
const MAX_HOURS = 24;

function localDate(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function validHours(hours: number): boolean {
  return Number.isFinite(hours) && hours > 0 && hours <= MAX_HOURS
    && Math.abs(hours * 4 - Math.round(hours * 4)) < 1e-9;
}

export function createTimeLog({ caseId }: { caseId: string }): TimeLog {
  const entries = new Map<string, TimeEntry>();

  function fail(message: string): TimeResult {
    return { ok: false, message };
  }

  return {
    start(nodeId, nodeTitle, now = Date.now) {
      const running = [...entries.values()].find((entry) => entry.node_id === nodeId && entry.stopped_at === null);
      if (running) return fail('该节点已有进行中的计时');
      const ms = now();
      const entry: TimeEntry = {
        id: crypto.randomUUID(), case_id: caseId, node_id: nodeId, node_title: nodeTitle,
        date: localDate(ms), hours: 0, source: 'timer',
        started_at: new Date(ms).toISOString(), stopped_at: null, note: null,
      };
      entries.set(entry.id, entry);
      return { ok: true, entry: structuredClone(entry) };
    },
    stop(entryId, now = Date.now, override) {
      const entry = entries.get(entryId);
      if (!entry || entry.stopped_at !== null) return fail('计时不存在或已停止');
      const ms = now();
      const elapsed = ms - Date.parse(entry.started_at!);
      const hours = override?.hours ?? Math.max(0.25, Math.ceil(elapsed / QUARTER_MS) * 0.25);
      if (!validHours(hours)) return fail('时长需为 0.25–24 小时且按 0.25 步进');
      entry.hours = hours;
      entry.stopped_at = new Date(ms).toISOString();
      if (override?.note !== undefined) entry.note = override.note;
      return { ok: true, entry: structuredClone(entry) };
    },
    add(nodeId, nodeTitle, hours, date, note) {
      if (!validHours(hours)) return fail('时长需为 0.25–24 小时且按 0.25 步进');
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return fail('日期格式需为 YYYY-MM-DD');
      const entry: TimeEntry = {
        id: crypto.randomUUID(), case_id: caseId, node_id: nodeId, node_title: nodeTitle,
        date, hours, source: 'manual', started_at: null, stopped_at: null, note: note ?? null,
      };
      entries.set(entry.id, entry);
      return { ok: true, entry: structuredClone(entry) };
    },
    remove(entryId) {
      if (!entries.has(entryId)) return { ok: false, message: '记录不存在' };
      entries.delete(entryId);
      return { ok: true };
    },
    running() {
      return [...entries.values()].filter((entry) => entry.stopped_at === null).map((entry) => structuredClone(entry));
    },
    entries() {
      return [...entries.values()].map((entry) => structuredClone(entry));
    },
    day(date) {
      const list = [...entries.values()]
        .filter((entry) => entry.date === date && (entry.source === 'manual' || entry.stopped_at !== null))
        .map((entry) => structuredClone(entry));
      return { date, entries: list, totalHours: list.reduce((sum, entry) => sum + entry.hours, 0) };
    },
    summaryByTitle() {
      const map = new Map<string, TitleSummaryRow>();
      for (const entry of entries.values()) {
        if (entry.source === 'timer' && entry.stopped_at === null) continue;
        const row = map.get(entry.node_title) ?? { title: entry.node_title, hours: 0, entries: 0 };
        row.hours += entry.hours;
        row.entries += 1;
        map.set(entry.node_title, row);
      }
      return [...map.values()].sort((a, b) => b.hours - a.hours);
    },
  };
}