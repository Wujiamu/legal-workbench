/**
 * 首页跨案期限时间轴（只读投影）。
 *
 * 数据来自 `GET /api/v1/dashboard/deadlines`：全部未归档案件的期限，服务端不分页。
 * 组件只做窗口裁剪、完成状态显隐与分组：
 *  - 默认窗口“前 7 天 ~ 后 60 天”，可切换 30/90 天或“全部未完成”；
 *  - 早于窗口左边界的未完成期限固定汇总到最左侧“更早逾期”，不静默隐藏；
 *  - 同一天多条纵向堆叠；宽屏是横向日期轨道（含“今天”竖线锚点），
 *    “更早逾期”是横轨左端的红色列，日期列在其右侧独立横向滚动；
 *    760px 及以下由 CSS 改为红区置顶 + 按日期分组的紧凑纵向列表（同一份 DOM，无横向溢出）；
 *  - 纯语义化 HTML + CSS，不引入图表库；每张卡片是指向案件概览的原生链接。
 */
import { useMemo, useState } from 'react';
import { Link } from 'react-router';
import type { DashboardDeadlineItem } from '../api/endpoints';
import { addDays, formatDayLabel, shanghaiToday } from '../ui/time';

export type DeadlineWindowKey = '30' | '60' | '90' | 'all';

/** 默认窗口：今天前 7 天至今天后 60 天。 */
export const DEFAULT_WINDOW: DeadlineWindowKey = '60';

const WINDOW_OPTIONS: { key: DeadlineWindowKey; label: string }[] = [
  { key: '30', label: '前7天~后30天' },
  { key: '60', label: '前7天~后60天' },
  { key: '90', label: '前7天~后90天' },
  { key: 'all', label: '全部未完成' },
];

const WINDOW_DAYS: Record<Exclude<DeadlineWindowKey, 'all'>, number> = {
  '30': 30, '60': 60, '90': 90,
};

export function deadlineKind(kind: DashboardDeadlineItem['kind']): string {
  return kind === 'external' ? '外部期限' : '内部计划';
}

interface DayGroup {
  date: string;
  items: DashboardDeadlineItem[];
}

export function groupByDue(items: DashboardDeadlineItem[]): DayGroup[] {
  const groups = new Map<string, DashboardDeadlineItem[]>();
  for (const item of items) {
    const bucket = groups.get(item.due);
    if (bucket) bucket.push(item);
    else groups.set(item.due, [item]);
  }
  return [...groups.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([date, groupItems]) => ({ date, items: groupItems }));
}

function DeadlineCard({ item, today, showDate }: {
  item: DashboardDeadlineItem;
  today: string;
  showDate: boolean;
}) {
  const overdue = !item.completed && item.due < today;
  const kind = deadlineKind(item.kind);
  const status = item.completed ? '已完成' : overdue ? '已逾期' : '';
  const classes = [
    'timeline-card',
    item.kind === 'external' ? 'is-external' : 'is-internal',
    overdue ? 'is-overdue' : '',
    item.completed ? 'is-completed' : '',
  ].filter(Boolean).join(' ');
  const label = [item.case_title, item.title, item.due, kind, status]
    .filter(Boolean).join('，');
  return <Link className={classes} to={`/cases/${item.case_id}?view=overview`}
               aria-label={label}>
    <span className="timeline-card-title">{item.title}</span>
    <span className="timeline-card-case">{item.case_title}</span>
    <span className="timeline-card-meta">
      {showDate ? <time dateTime={item.due}>{formatDayLabel(item.due)}</time> : null}
      <span className="timeline-card-kind">{kind}</span>
      {status ? <span className="timeline-card-status">{status}</span> : null}
    </span>
  </Link>;
}

export function DeadlineTimeline({ items, today = shanghaiToday(), caseTotal }: {
  items: DashboardDeadlineItem[];
  /** 统一“今天”锚点（Asia/Shanghai）；首页传服务端 as_of，避免客户端时区偏差。 */
  today?: string;
  /** 参与统计的未归档案件数，用于说明覆盖全部在办案件而非某一页。 */
  caseTotal?: number;
}) {
  const [windowKey, setWindowKey] = useState<DeadlineWindowKey>(DEFAULT_WINDOW);
  const [showCompleted, setShowCompleted] = useState(false);

  const { early, groups, shownCount, hiddenCompleted } = useMemo(() => {
    const left = addDays(today, -7);
    const right = windowKey === 'all' ? null : addDays(today, WINDOW_DAYS[windowKey]);
    const earlyOverdue: DashboardDeadlineItem[] = [];
    const visible: DashboardDeadlineItem[] = [];
    let hidden = 0;
    for (const item of items) {
      if (right !== null && item.due > right) continue;
      const beforeWindow = windowKey !== 'all' && item.due < left;
      if (item.completed) {
        if (!showCompleted) {
          if (!beforeWindow) hidden += 1;
          continue;
        }
        // 已完成期限同样受窗口约束；早于左边界的不计入“更早逾期”。
        if (beforeWindow) continue;
        visible.push(item);
      } else if (beforeWindow) {
        earlyOverdue.push(item);
      } else {
        visible.push(item);
      }
    }
    // 宽屏轨道要保留“今天”竖线：即使今天没有期限，也占一列作为锚点。
    const grouped = groupByDue(visible);
    if (!grouped.some((group) => group.date === today)) {
      grouped.push({ date: today, items: [] });
      grouped.sort((a, b) => a.date.localeCompare(b.date));
    }
    return { early: earlyOverdue, groups: grouped, shownCount: visible.length, hiddenCompleted: hidden };
  }, [items, today, windowKey, showCompleted]);

  const earlyCount = early.length;
  return <section className="deadline-timeline" aria-label="跨案期限时间轴">
    <details className="timeline-details" open>
      <summary className="timeline-summary">
        <span className="timeline-title">跨案期限时间轴</span>
        <span className="timeline-summary-meta">
          今天 {formatDayLabel(today)} · 窗口内 {shownCount} 项
          {typeof caseTotal === 'number' ? ` · 覆盖 ${caseTotal} 个在办案件` : ''}
        </span>
      </summary>
      <div className="timeline-controls">
        <div className="timeline-windows" role="group" aria-label="时间窗口">
          {WINDOW_OPTIONS.map((option) => (
            <button key={option.key} type="button" className="timeline-window"
                    aria-pressed={windowKey === option.key}
                    onClick={() => setWindowKey(option.key)}>
              {option.label}
            </button>
          ))}
        </div>
        <label className="timeline-toggle">
          <input type="checkbox" checked={showCompleted}
                 onChange={(event) => setShowCompleted(event.target.checked)} />
          显示已完成
        </label>
      </div>
      <div className="timeline-board">
        {earlyCount > 0 ? <div className="timeline-early" role="group"
                             aria-label={`更早逾期 ${earlyCount} 项`}>
          <h3 className="timeline-early-title">更早逾期（{earlyCount}）</h3>
          <ul className="timeline-early-list">
            {early.map((item, index) => (
              <li key={`${item.case_id}-${item.due}-${item.title}-${index}`}>
                <DeadlineCard item={item} today={today} showDate />
              </li>
            ))}
          </ul>
        </div> : null}
        {shownCount === 0 && earlyCount === 0
          ? <p className="timeline-empty">该窗口内没有未完成期限。</p>
          : <ol className="timeline-track" aria-label="按日期排列的期限">
            {groups.map((group) => {
              const isToday = group.date === today;
              return <li key={group.date}
                         className={`timeline-day${isToday ? ' is-today' : ''}`}>
                <div className="timeline-day-head">
                  <time dateTime={group.date} title={group.date}>{formatDayLabel(group.date)}</time>
                  {isToday ? <span className="timeline-today-flag">今天</span> : null}
                </div>
                {group.items.length > 0 ? <ul className="timeline-stack">
                  {group.items.map((item, index) => (
                    <li key={`${item.case_id}-${item.title}-${index}`}>
                      <DeadlineCard item={item} today={today} showDate={false} />
                    </li>
                  ))}
                </ul> : <span className="timeline-day-empty" aria-hidden="true">·</span>}
              </li>;
            })}
          </ol>}
      </div>
      {hiddenCompleted > 0
        ? <p className="timeline-note">另有 {hiddenCompleted} 项已完成期限未显示，勾选“显示已完成”查看。</p>
        : null}
    </details>
  </section>;
}
