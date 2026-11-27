/** Shared case-level navigation bar used across workspace tabs and dedicated views. */
import type { ReactNode } from 'react';
import { Link } from 'react-router';
import { isModuleEnabled, useEnabledModules } from '../modules';
import type { ModuleName } from '../modules';

export type WorkspaceTab = 'overview' | 'material' | 'search' | 'analysis' | 'annotations';
export type CaseNavKey = WorkspaceTab | 'progress' | 'graph' | 'evidence' | 'claims';

const WORKSPACE_TABS: readonly { key: WorkspaceTab; label: string; module?: ModuleName }[] = [
  { key: 'overview', label: '案件概览' },
  { key: 'material', label: '材料' },
  { key: 'search', label: '检索' },
  { key: 'analysis', label: '分析', module: 'notes' },
  { key: 'annotations', label: '标注复核', module: 'notes' },
];

const ROUTE_TABS: readonly {
  key: Exclude<CaseNavKey, WorkspaceTab>;
  label: string;
  suffix: string;
  module: ModuleName;
}[] = [
  { key: 'progress', label: '办案进度', suffix: 'progress', module: 'progress' },
  { key: 'graph', label: '关系图', suffix: 'graph', module: 'graph' },
  { key: 'evidence', label: '证据目录', suffix: 'evidence', module: 'evidence' },
  { key: 'claims', label: '诉请金额', suffix: 'claims', module: 'claims' },
];

export function isWorkspaceTab(value: string | null): value is WorkspaceTab {
  return value === 'overview' || value === 'material' || value === 'search'
    || value === 'analysis' || value === 'annotations';
}

export function CaseNavBar({
  caseId,
  active,
  onSwitchWorkspaceTab,
  rightSlot,
}: {
  caseId: string;
  active: CaseNavKey;
  onSwitchWorkspaceTab?: (tab: WorkspaceTab) => void;
  rightSlot?: ReactNode;
}) {
  // 默认全开（context 缺省值），因此没有 Provider 的组件单测仍看到全部页签；
  // 只有后端明确报告某模块未启用时才隐藏对应入口。
  const enabled = useEnabledModules();

  return (
    <div className="workspace-toolbar">
      <nav aria-label="案件工作区" className="workspace-tabs">
        {WORKSPACE_TABS.filter(({ module: name }) => !name || isModuleEnabled(enabled, name))
          .map(({ key, label }) =>
            onSwitchWorkspaceTab ? (
              <button
                key={key}
                type="button"
                aria-current={active === key ? 'page' : undefined}
                onClick={() => onSwitchWorkspaceTab(key)}
              >
                {label}
              </button>
            ) : (
              <Link
                key={key}
                to={`/cases/${caseId}?view=${key}`}
                aria-current={active === key ? 'page' : undefined}
              >
                {label}
              </Link>
            ),
          )}
        {ROUTE_TABS.filter(({ module: name }) => isModuleEnabled(enabled, name)).map(
          ({ key, label, suffix }) => (
            <Link
              key={key}
              to={`/cases/${caseId}/${suffix}`}
              aria-current={active === key ? 'page' : undefined}
            >
              {label}
            </Link>
          ),
        )}
      </nav>
      {rightSlot ?? null}
    </div>
  );
}
