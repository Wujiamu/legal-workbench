/**
 * 模块开关（前端侧）：后端 `/api/v1/modules` 决定本机启用了哪些功能面。
 *
 * 三条纪律：
 *  - 初始态是"全部启用"。开关是裁剪入口，不是权限；请求失败、404 或单测未
 *    mock 时保持全开，绝不让一次探测失败把用户锁在功能外。
 *  - 隐藏入口与守卫渲染都读同一个 context，避免"导航还在、页面已 404"。
 *  - 探测失败静默处理：不打印 console.error，控制台噪音只会掩盖真问题。
 */
import { createContext, useContext, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Link, useParams } from 'react-router';
import { modulesApi } from './api/endpoints';

export type ModuleName = 'core' | 'notes' | 'evidence' | 'graph' | 'claims' | 'progress' | 'time';

export const ALL_MODULES: readonly ModuleName[] = [
  'core', 'notes', 'evidence', 'graph', 'claims', 'progress', 'time',
];

const MODULE_LABELS: Record<ModuleName, string> = {
  core: '材料与检索',
  notes: '分析与标注',
  evidence: '证据目录',
  graph: '关系图',
  claims: '诉请金额',
  progress: '办案进度',
  time: '工时',
};

function isModuleName(value: string): value is ModuleName {
  return (ALL_MODULES as readonly string[]).includes(value);
}

export function isModuleEnabled(
  enabled: readonly ModuleName[] | null | undefined,
  name: ModuleName,
): boolean {
  return (enabled ?? ALL_MODULES).includes(name);
}

const EnabledModulesContext = createContext<readonly ModuleName[]>(ALL_MODULES);

export function useEnabledModules(): readonly ModuleName[] {
  return useContext(EnabledModulesContext);
}

export function EnabledModulesProvider({
  children,
  initial,
}: {
  children: ReactNode;
  initial?: readonly ModuleName[];
}) {
  const [enabled, setEnabled] = useState<readonly ModuleName[]>(initial ?? ALL_MODULES);

  useEffect(() => {
    const controller = new AbortController();
    modulesApi
      .list(controller.signal)
      .then((payload) => {
        if (controller.signal.aborted) return;
        const names = (payload.enabled_modules ?? []).filter(isModuleName);
        // 空列表只可能来自异常响应；保持全开比"整站消失"安全。
        if (names.length > 0) setEnabled(names);
      })
      .catch(() => {
        /* 旧后端没有该接口、请求被中止或单测未 mock：保持全部启用。 */
      });
    return () => controller.abort();
  }, []);

  return (
    <EnabledModulesContext.Provider value={enabled}>
      {children}
    </EnabledModulesContext.Provider>
  );
}

/** 页面对应模块被禁用时的占位：说明原因并给出回到案件工作区的路径。 */
export function ModuleDisabledNotice({
  module,
  caseId,
}: {
  module: ModuleName;
  caseId?: string;
}) {
  const params = useParams();
  const target = caseId ?? params.caseId ?? '';
  const label = MODULE_LABELS[module] ?? module;
  return (
    <div className="module-disabled" role="status" style={{ padding: 24 }}>
      <h1>当前未启用该模块</h1>
      <p>
        本机未启用「{label}」（{module}）功能面。如需使用，请在安装目录的
        <code> .env </code> 中将 <code>LWB_ENABLED_MODULES</code> 设为包含
        <code> {module} </code> 的列表（或 <code>all</code>），然后重启素章。
      </p>
      {target ? <Link to={`/cases/${target}`}>← 返回案件工作区</Link> : <Link to="/">← 返回案件工作表</Link>}
    </div>
  );
}

/** 路由守卫：模块关闭时不渲染页面，避免页面对禁用接口发一堆必然 404 的请求。 */
export function ModuleGuard({
  module,
  children,
}: {
  module: ModuleName;
  children: ReactNode;
}) {
  const enabled = useEnabledModules();
  if (!isModuleEnabled(enabled, module)) return <ModuleDisabledNotice module={module} />;
  return <>{children}</>;
}
