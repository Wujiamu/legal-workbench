/**
 * 路由样例：真实案件的三个可视化页签都挂在 /cases/:caseId/... 下，
 * 保留同一案件 UUID；P1 原型路径仍然可用（不被真实页替换）。
 */
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it, vi } from 'vitest';

vi.mock('./pages/CasesPage', () => ({
  CasesPage: () => <p>案件工作表</p>,
}));
vi.mock('./pages/CaseWorkspacePage', () => ({
  CaseWorkspacePage: () => <p>案件工作区</p>,
}));
vi.mock('./graph/CaseGraphPage', () => ({
  default: () => <p>真实关系图页</p>,
}));
vi.mock('./graph/CaseEvidencePage', () => ({
  default: () => <p>真实证据目录页</p>,
}));
vi.mock('./claims/CaseClaimsPage', () => ({
  default: () => <p>真实诉请金额页</p>,
}));
vi.mock('./graph/GraphPrototypePage', () => ({
  default: () => <p>关系图原型</p>,
}));

import { App } from './App';

function renderAt(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
}

describe('路由样例', () => {
  it.each([
    ['/cases/11111111-1111-4111-8111-111111111111', '案件工作区'],
    ['/cases/11111111-1111-4111-8111-111111111111/graph', '真实关系图页'],
    ['/cases/11111111-1111-4111-8111-111111111111/evidence', '真实证据目录页'],
    ['/cases/11111111-1111-4111-8111-111111111111/claims', '真实诉请金额页'],
    // P1 原型入口保持独立，不与真实案件页混用。
    ['/prototype/graph', '关系图原型'],
  ])('%s → %s', async (path, text) => {
    renderAt(path);
    expect(await screen.findByText(text)).toBeTruthy();
  });
});
