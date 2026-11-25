import type { Schemas } from '../api/endpoints';

export type CaseWorkStatus = Schemas['CaseSummary']['work_status'];

export const WORK_STATUS_OPTIONS: ReadonlyArray<{
  value: CaseWorkStatus;
  label: string;
}> = [
  { value: 'normal', label: '正常推进' },
  { value: 'need_materials', label: '待补材料' },
  { value: 'need_sync', label: '待沟通/开会' },
  { value: 'waiting_external', label: '待法院/对方' },
  { value: 'paused', label: '暂缓/中止' },
];

const LABELS: Record<CaseWorkStatus, string> = {
  normal: '正常推进',
  need_materials: '待补材料',
  need_sync: '待沟通/开会',
  waiting_external: '待法院/对方',
  paused: '暂缓/中止',
};

export function workStatusLabel(status: CaseWorkStatus | string | null | undefined): string {
  if (!status || !(status in LABELS)) return LABELS.normal;
  return LABELS[status as CaseWorkStatus];
}
