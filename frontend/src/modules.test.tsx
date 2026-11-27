/**
 * 模块开关（Phase 1）前端行为：
 *  - 模块关闭时导航项消失，页面由守卫换成说明；
 *  - 默认（探测失败／未 mock／旧后端 404）保持全部启用，不能把用户锁在功能外。
 */
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./api/endpoints', () => ({
  modulesApi: { list: vi.fn() },
}));

import { modulesApi } from './api/endpoints';
import { CaseNavBar } from './components/CaseNavBar';
import {
  ALL_MODULES,
  EnabledModulesProvider,
  ModuleDisabledNotice,
  ModuleGuard,
  isModuleEnabled,
} from './modules';

const CASE_ID = '11111111-1111-4111-8111-111111111111';

function renderNav() {
  return render(
    <MemoryRouter initialEntries={[`/cases/${CASE_ID}`]}>
      <CaseNavBar caseId={CASE_ID} active="material" />
    </MemoryRouter>,
  );
}

const listMock = vi.mocked(modulesApi.list);

beforeEach(() => {
  // 默认返回"全部启用"；个别用例用 ...Once 覆盖成裁剪结果或失败。
  listMock.mockReset();
  listMock.mockResolvedValue({ enabled_modules: [...ALL_MODULES], all_modules: [...ALL_MODULES] });
});

describe('isModuleEnabled', () => {
  it('只在模块名出现在启用列表时返回 true', () => {
    expect(isModuleEnabled(['core', 'notes'], 'notes')).toBe(true);
    expect(isModuleEnabled(['core'], 'notes')).toBe(false);
    // 未提供列表（旧调用点／测试）按全部启用处理。
    expect(isModuleEnabled(null, 'claims')).toBe(true);
  });
});

describe('CaseNavBar 与模块开关', () => {
  it('默认全开：五个工作区页签与四个功能页签都在', () => {
    renderNav();
    for (const label of ['案件概览', '材料', '检索', '分析', '标注复核',
      '办案进度', '关系图', '证据目录', '诉请金额']) {
      expect(screen.queryByText(label)).toBeTruthy();
    }
  });

  it('关闭 notes 时隐藏分析、标注与其余未启用页签', async () => {
    listMock.mockResolvedValueOnce({ enabled_modules: ['core', 'progress'], all_modules: [...ALL_MODULES] });
    render(
      <EnabledModulesProvider>
        <MemoryRouter initialEntries={[`/cases/${CASE_ID}`]}>
          <CaseNavBar caseId={CASE_ID} active="material" />
        </MemoryRouter>
      </EnabledModulesProvider>,
    );
    await waitFor(() => expect(screen.queryByText('分析')).toBeNull());
    expect(screen.queryByText('标注复核')).toBeNull();
    expect(screen.queryByText('办案进度')).toBeTruthy();
    expect(screen.queryByText('关系图')).toBeNull();
    expect(screen.queryByText('证据目录')).toBeNull();
    expect(screen.queryByText('诉请金额')).toBeNull();
    // 核心入口不受开关影响。
    expect(screen.queryByText('材料')).toBeTruthy();
    expect(screen.queryByText('检索')).toBeTruthy();
  });

  it('探测失败时保持全部启用', async () => {
    listMock.mockRejectedValueOnce(new Error('offline'));
    render(
      <EnabledModulesProvider>
        <MemoryRouter initialEntries={[`/cases/${CASE_ID}`]}>
          <CaseNavBar caseId={CASE_ID} active="material" />
        </MemoryRouter>
      </EnabledModulesProvider>,
    );
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    expect(screen.queryByText('分析')).toBeTruthy();
    expect(screen.queryByText('关系图')).toBeTruthy();
  });
});

describe('ModuleGuard', () => {
  it('模块启用时渲染页面本身', () => {
    render(
      <EnabledModulesProvider initial={['core', 'graph']}>
        <MemoryRouter initialEntries={[`/cases/${CASE_ID}/graph`]}>
          <Routes>
            <Route path="/cases/:caseId/graph" element={
              <ModuleGuard module="graph"><p>关系图页面</p></ModuleGuard>
            } />
          </Routes>
        </MemoryRouter>
      </EnabledModulesProvider>,
    );
    expect(screen.queryByText('关系图页面')).toBeTruthy();
  });

  it('模块关闭时显示说明与回到案件工作区的链接，不渲染页面', () => {
    render(
      <EnabledModulesProvider initial={['core']}>
        <MemoryRouter initialEntries={[`/cases/${CASE_ID}/claims`]}>
          <Routes>
            <Route path="/cases/:caseId/claims" element={
              <ModuleGuard module="claims"><p>诉请金额页面</p></ModuleGuard>
            } />
          </Routes>
        </MemoryRouter>
      </EnabledModulesProvider>,
    );
    expect(screen.queryByText('诉请金额页面')).toBeNull();
    expect(screen.queryByText('当前未启用该模块')).toBeTruthy();
    const link = screen.queryByText('← 返回案件工作区') as HTMLAnchorElement | null;
    expect(link?.getAttribute('href')).toBe(`/cases/${CASE_ID}`);
  });

  it('说明组件在无 caseId 时回退到案件工作表', () => {
    render(
      <MemoryRouter initialEntries={['/somewhere']}>
        <ModuleDisabledNotice module="evidence" />
      </MemoryRouter>,
    );
    const link = screen.queryByText('← 返回案件工作表') as HTMLAnchorElement | null;
    expect(link?.getAttribute('href')).toBe('/');
  });
});
