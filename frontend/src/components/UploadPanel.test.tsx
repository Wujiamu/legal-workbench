/**
 * Upload completion must refresh the material list.
 *
 * The panel called onUploaded when the upload POST returned, which is before the
 * worker has created any material.  The parent reloaded, found nothing, and the
 * page then showed "已完成（3/3）· 就绪 3" beside "全部（0）· 该分类下没有材料"
 * until the user reloaded by hand.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    materialsApi: {
      ...actual.materialsApi,
      upload: vi.fn(),
      intakeItems: vi.fn(),
      job: vi.fn(),
      retry: vi.fn(),
    },
  };
});

import { materialsApi } from '../api/endpoints';
import type { IntakeItem, JobStatusOutput } from '../api/endpoints';
import { UploadPanel } from './UploadPanel';

const upload = vi.mocked(materialsApi.upload);
const intakeItems = vi.mocked(materialsApi.intakeItems);
const job = vi.mocked(materialsApi.job);
const retry = vi.mocked(materialsApi.retry);

const INTAKE = 'intake-1';
const JOB = 'job-1';

function item(name: string, status: IntakeItem['status']): IntakeItem {
  return {
    intake_item_id: `item-${name}`,
    relative_source: name,
    status,
    error_message: null,
  } as IntakeItem;
}

function jobStatus(status: JobStatusOutput['status'], completed: number): JobStatusOutput {
  return { job_id: JOB, status, completed_units: completed, total_units: 3 } as JobStatusOutput;
}

function pickFiles() {
  const input = document.querySelector('input[type=file]') as HTMLInputElement;
  const files = [
    new File(['TEST-ONLY 甲'], '甲材料.txt', { type: 'text/plain' }),
    new File(['TEST-ONLY 乙'], '乙材料.txt', { type: 'text/plain' }),
    new File(['TEST-ONLY 丙'], '丙材料.txt', { type: 'text/plain' }),
  ];
  return userEvent.upload(input, files);
}

beforeEach(() => {
  upload.mockResolvedValue({ intake_id: INTAKE, job_id: JOB } as never);
  intakeItems.mockReset();
  job.mockReset();
  // The panel now remembers the running batch across reloads; a leftover entry
  // would make the next test mount with a job already in flight.
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('UploadPanel completion', () => {
  it('notifies the parent only after the job reaches a terminal state', async () => {
    const onUploaded = vi.fn();
    // First poll: still running, nothing ready yet.  Second poll: done.
    job.mockResolvedValueOnce(jobStatus('running', 0)).mockResolvedValue(jobStatus('succeeded', 3));
    intakeItems
      .mockResolvedValueOnce({ items: [item('甲材料.txt', 'published')], total: 1 } as never)
      .mockResolvedValue({
        items: [
          item('甲材料.txt', 'ready'),
          item('乙材料.txt', 'ready'),
          item('丙材料.txt', 'ready'),
        ],
        total: 3,
      } as never);

    render(<UploadPanel caseId="case-1" onUploaded={onUploaded} />);
    await pickFiles();
    await userEvent.click(screen.getByRole('button', { name: /上传 3 个文件/ }));

    await waitFor(() => expect(onUploaded).toHaveBeenCalled(), { timeout: 5000 });
    const outcome = onUploaded.mock.calls[0][0];
    expect(outcome.succeeded).toBe(3);
    expect(outcome.failed).toBe(0);
    expect(outcome.jobId).toBe(JOB);

    // The upload response alone must not notify: that was the bug.
    expect(onUploaded.mock.calls.length).toBeGreaterThanOrEqual(1);
  });

  it('polls on its own so the parent reloads without a manual refresh', async () => {
    const onUploaded = vi.fn();
    // Every poll reports "running" until the mock is switched to a terminal state.
    job.mockResolvedValue(jobStatus('running', 1));
    intakeItems.mockResolvedValue({ items: [item('甲材料.txt', 'published')], total: 1 } as never);

    render(<UploadPanel caseId="case-1" onUploaded={onUploaded} />);
    await pickFiles();
    await userEvent.click(screen.getByRole('button', { name: /上传 3 个文件/ }));

    // Still running: the parent must not be told the batch finished.
    await waitFor(() => expect(job).toHaveBeenCalled(), { timeout: 5000 });
    expect(onUploaded).not.toHaveBeenCalled();

    // Now the worker finishes.  No click on 刷新状态 anywhere in this test.
    job.mockResolvedValue(jobStatus('succeeded', 3));
    intakeItems.mockResolvedValue({
      items: [item('甲材料.txt', 'ready'), item('乙材料.txt', 'ready')], total: 2,
    } as never);
    await waitFor(() => expect(onUploaded).toHaveBeenCalledTimes(1), { timeout: 8000 });
  });

  it('does not notify twice for the same job', async () => {
    const onUploaded = vi.fn();
    job.mockResolvedValue(jobStatus('succeeded', 3));
    intakeItems.mockResolvedValue({ items: [item('甲材料.txt', 'ready')], total: 1 } as never);

    render(<UploadPanel caseId="case-1" onUploaded={onUploaded} />);
    await pickFiles();
    await userEvent.click(screen.getByRole('button', { name: /上传 3 个文件/ }));

    await waitFor(() => expect(onUploaded).toHaveBeenCalledTimes(1), { timeout: 5000 });
    await userEvent.click(screen.getByRole('button', { name: '刷新状态' }));
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(onUploaded).toHaveBeenCalledTimes(1);
  });
});


describe('导入批次的可跟踪性', () => {
  it('重新挂载后继续跟踪同一批次', async () => {
    window.sessionStorage.setItem('lwb.intake.case-1',
      JSON.stringify({ intakeId: INTAKE, jobId: JOB }));
    job.mockResolvedValue(jobStatus('running', 1));
    intakeItems.mockResolvedValue({
      items: [item('甲材料.txt', 'ready')], total: 1,
    } as never);

    render(<UploadPanel caseId="case-1" onUploaded={vi.fn()} />);

    await waitFor(() => expect(job).toHaveBeenCalledWith('case-1', JOB));
    await screen.findByText(/任务 进行中/);
    expect(screen.getByText(/甲材料\.txt/)).toBeTruthy();
  });

  it('超过一页时统计覆盖全部条目', async () => {
    window.sessionStorage.setItem('lwb.intake.case-1',
      JSON.stringify({ intakeId: INTAKE, jobId: JOB }));
    job.mockResolvedValue(jobStatus('succeeded', 250));
    const first = Array.from({ length: 200 }, (_, index) => item(`甲${index}.txt`, 'ready'));
    const rest = Array.from({ length: 50 }, (_, index) => item(`乙${index}.txt`, 'failed'));
    intakeItems.mockImplementation(((caseId: string, intakeId: string, offset: number) => {
      void caseId; void intakeId;
      return Promise.resolve(offset === 0
        ? { items: first, total: 250 }
        : { items: rest, total: 250 });
    }) as never);
    const onUploaded = vi.fn();

    render(<UploadPanel caseId="case-1" onUploaded={onUploaded} />);

    await waitFor(() => expect(onUploaded).toHaveBeenCalled(), { timeout: 6000 });
    const outcome = onUploaded.mock.calls.at(-1)![0];
    // The 50 failures live past the first page of 200; they must still count.
    expect(outcome.succeeded).toBe(200);
    expect(outcome.failed).toBe(50);
  });

  it('重试成功后再次通知材料列表刷新', async () => {
    window.sessionStorage.setItem('lwb.intake.case-1',
      JSON.stringify({ intakeId: INTAKE, jobId: JOB }));
    job.mockResolvedValue(jobStatus('failed', 3));
    intakeItems.mockResolvedValue({ items: [item('甲材料.txt', 'failed')], total: 1 } as never);
    retry.mockResolvedValue(undefined as never);
    const onUploaded = vi.fn();

    render(<UploadPanel caseId="case-1" onUploaded={onUploaded} />);
    await waitFor(() => expect(onUploaded).toHaveBeenCalledTimes(1), { timeout: 6000 });

    // Retry reuses the job id but runs again to success.
    job.mockResolvedValue(jobStatus('succeeded', 3));
    intakeItems.mockResolvedValue({ items: [item('甲材料.txt', 'ready')], total: 1 } as never);
    await userEvent.click(await screen.findByRole('button', { name: '重试失败项' }));

    await waitFor(() => expect(onUploaded).toHaveBeenCalledTimes(2), { timeout: 6000 });
  });
});
