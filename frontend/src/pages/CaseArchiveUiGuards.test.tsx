/**
 * 归档只读界面入口回归：已归档案件的「导入材料」按钮禁用且不展开上传面板。
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    casesApi: { ...actual.casesApi, overview: vi.fn() },
    materialsApi: { ...actual.materialsApi, list: vi.fn(), original: vi.fn() },
    searchApi: { ...actual.searchApi, indexStatus: vi.fn(), extractedText: vi.fn() },
    analysisApi: { ...actual.analysisApi, list: vi.fn().mockResolvedValue({ items: [], total: 0 }) },
  };
});

vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useParams: () => ({ caseId: 'case-arch-1' }) };
});

import { casesApi, materialsApi, searchApi } from '../api/endpoints';
import { CaseOverviewPanel } from '../components/CaseOverview';
import { MaterialDetail } from '../components/MaterialDetail';
import { SaveAnalysisForm } from '../components/SaveAnalysisForm';
import { AnalysisPanel } from '../components/SearchPanel';
import { AnnotationDetail } from '../components/Annotations';
import { CaseWorkspacePage } from './CaseWorkspacePage';

afterEach(() => cleanup());

describe('CaseWorkspacePage 归档只读 UI 守卫', () => {
  it('已归档案件禁用「导入材料」按钮，即使 URL/状态切到归档也不展开上传面板', async () => {
    vi.mocked(casesApi.overview).mockResolvedValue({
      case_id: 'case-arch-1',
      title: 'TEST-ONLY 已归档工作区测试',
      description: null,
      case_number: null,
      plaintiff: null,
      defendant: null,
      work_status: 'normal',
      stage: null,
      active_stage: null,
      next_action: null,
      deadlines: [],
      revision: 3,
      archived_at: '2026-11-23T01:00:00Z',
      created_at: '2026-11-20T00:00:00Z',
    });
    vi.mocked(materialsApi.list).mockResolvedValue({
      items: [],
      offset: 0,
      limit: 50,
      total: 0,
      case_total: 0,
      categories: [],
    });
    vi.mocked(searchApi.indexStatus).mockResolvedValue({
      complete: 0, partial: 0, failed: 0, pending: 0, unsupported: 0,
    });

    render(<MemoryRouter><CaseWorkspacePage /></MemoryRouter>);

    const importBtn = await screen.findByRole('button', { name: '导入材料' }) as HTMLButtonElement;
    await waitFor(() => expect(importBtn.disabled).toBe(true));
    expect(screen.getByRole('status').textContent).toContain('本案已归档');
    expect(screen.getByRole('navigation', { name: '案件工作区' }).textContent).toContain('办案进度');
    expect(screen.queryByRole('region', { name: /上传/ })).toBeNull();
    expect(screen.queryByText(/选择文件/)).toBeNull();
  });

  it('工作区页签切换同步写入 URL ?view=，刷新/返回不丢失所在页签', async () => {
    vi.mocked(casesApi.overview).mockResolvedValue({
      case_id: 'case-arch-1',
      title: 'TEST-ONLY 在办工作区测试',
      description: null,
      case_number: null,
      plaintiff: null,
      defendant: null,
      work_status: 'normal',
      stage: null,
      active_stage: null,
      next_action: null,
      deadlines: [],
      revision: 0,
      archived_at: null,
      created_at: '2026-11-20T00:00:00Z',
    });
    vi.mocked(materialsApi.list).mockResolvedValue({
      items: [], offset: 0, limit: 50, total: 0, case_total: 0, categories: [],
    });
    vi.mocked(searchApi.indexStatus).mockResolvedValue({
      complete: 0, partial: 0, failed: 0, pending: 0, unsupported: 0,
    });

    function LocationProbe() {
      const location = useLocation();
      return <output data-testid="loc-search">{location.search}</output>;
    }

    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/cases/case-arch-1']}>
        <CaseWorkspacePage />
        <LocationProbe />
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole('button', { name: '案件概览' }));
    expect(screen.getByTestId('loc-search').textContent).toBe('?view=overview');
    await user.click(screen.getByRole('button', { name: '分析' }));
    expect(screen.getByTestId('loc-search').textContent).toBe('?view=analysis');
  });

  it('M1：已归档时同步禁用材料详情、标注复核、分析、保存分析与案件概览写入入口', async () => {
    vi.mocked(materialsApi.original).mockResolvedValue({
      file_version_id: 'ver-1',
      sha256: 'abc',
      size: 10,
      integrity_status: 'ok',
    } as never);
    vi.mocked(searchApi.extractedText).mockResolvedValue({ segments: [] } as never);

    render(
      <MemoryRouter>
        <div>
          <MaterialDetail
            caseId="case-arch-1"
            archived
            material={{
              material_id: 'm-1',
              display_name: '合同.pdf',
              category: '合同',
              revision: 1,
              intake_id: 'i-1',
              acquisition: '原件',
              source_label: '上传',
              relative_source: '合同.pdf',
              status: 'ready',
              coverage: { complete: 1, partial: 0, failed: 0, pending: 0, unsupported: 0 },
              created_at: '2026-11-20T00:00:00Z',
            }}
            onChanged={vi.fn()}
          />
          <AnnotationDetail
            archived
            onReviewed={vi.fn()}
            item={{
              annotation_id: 'a-1',
              case_id: 'case-arch-1',
              file_version_id: 'ver-1',
              page: 1,
              author: 'agent',
              kind: 'interpretation',
              body: '待复核',
              reviewed_text: null,
              revision: 0,
              created_at: '2026-11-20T00:00:00Z',
              display_name: '合同.pdf',
            }}
          />
          <SaveAnalysisForm caseId="case-arch-1" sourceRef="seg-1" archived />
          <CaseOverviewPanel
            archived
            item={{
              case_id: 'case-arch-1',
              title: 'TEST-ONLY 归档',
              description: null,
              case_number: null,
              plaintiff: null,
              defendant: null,
              work_status: 'normal',
              stage: null,
              active_stage: null,
              next_action: null,
              deadlines: [],
              revision: 1,
              archived_at: '2026-11-23T01:00:00Z',
              created_at: '2026-11-20T00:00:00Z',
            }}
            onSaved={vi.fn()}
            onReload={vi.fn()}
          />
          <AnalysisPanel caseId="case-arch-1" focus={null} archived onFocusConsumed={vi.fn()} />
        </div>
      </MemoryRouter>,
    );

    expect((await screen.findByRole('button', { name: '保存取得方式' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '创建工作副本' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '重新解析' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '保存人工标注' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '确认标注' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '暂存为工作笔记' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '保存分析' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '＋ 登记期限' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '保存办理信息' }) as HTMLButtonElement).disabled).toBe(true);
  });
});
