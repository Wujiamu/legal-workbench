/**
 * R06: switching hits must never show the previous body next to the new ref.
 *
 * The panel used to keep `payload` while the header, `source_ref` and save form
 * had already moved to the new hit, so a slow read could be read and copied as
 * the wrong source.
 */
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', async () => {
  const actual = await vi.importActual<typeof import('../api/endpoints')>('../api/endpoints');
  return {
    ...actual,
    searchApi: { ...actual.searchApi, readSource: vi.fn() },
  };
});

import { searchApi } from '../api/endpoints';
import type { ReadSourceOutput, SearchHit } from '../api/endpoints';
import { SourcePanelHarness } from './__fixtures__/SourcePanelHarness';

const readSource = vi.mocked(searchApi.readSource);

function payloadFor(ref: string, text: string, page = 1): ReadSourceOutput {
  return {
    source_ref: ref,
    material_id: 'm-1',
    file_version_id: 'v-1',
    parse_run_id: 'r-1',
    locator: { page },
    text,
    method: 'native', acquisition: null, display_name: null, source_label: null, relative_source: null, is_original: true,
    context_before: '',
    context_after: '',
  };
}

function hitFor(ref: string): SearchHit {
  return {
    source: {
      source_ref: ref,
      material_id: 'm-1',
      file_version_id: 'v-1',
      parse_run_id: 'r-1',
      locator: { page: 1 },
      text: '',
      method: 'native',
    },
    analysis_id: null,
    analysis_version_id: null,
    source_type: 'originals',
    context: '',
    match_start: 0,
    match_end: 1,
    amount: null,
    currency: null,
  };
}

/** A promise whose resolution the test controls. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

afterEach(cleanup);

describe('SourcePanel source switching', () => {
  beforeEach(() => {
    readSource.mockReset();
  });

  it('does not show the old body while the new ref is loading', async () => {
    const pendingB = deferred<ReadSourceOutput>();
    readSource.mockImplementation((_case, ref) => (
      ref === 'ref-a' ? Promise.resolve(payloadFor('ref-a', 'A 的正文')) : pendingB.promise
    ));

    const { rerender } = render(<SourcePanelHarness hit={hitFor('ref-a')} />);
    await waitFor(() => expect(screen.getByText('A 的正文')).toBeTruthy());

    rerender(<SourcePanelHarness hit={hitFor('ref-b')} />);

    // The moment B is selected, A's text must be gone — not merely hidden.
    expect(screen.queryByText('A 的正文')).toBeNull();
    // And no copy/save control may be offered for a body that is not on screen.
    expect(screen.queryByText('复制 source_ref')).toBeNull();

    pendingB.resolve(payloadFor('ref-b', 'B 的正文'));
    await waitFor(() => expect(screen.getByText('B 的正文')).toBeTruthy());
    expect(screen.getByText('复制来源链接')).toBeTruthy();
  });

  it('keeps the newest selection when responses arrive out of order', async () => {
    const pending = new Map<string, ReturnType<typeof deferred<ReadSourceOutput>>>();
    readSource.mockImplementation((_case, ref) => {
      const d = deferred<ReadSourceOutput>();
      pending.set(ref, d);
      return d.promise;
    });

    const { rerender } = render(<SourcePanelHarness hit={hitFor('ref-a')} />);
    rerender(<SourcePanelHarness hit={hitFor('ref-b')} />);
    rerender(<SourcePanelHarness hit={hitFor('ref-c')} />);

    // Resolve in the worst order: C first, then B, then A.
    pending.get('ref-c')!.resolve(payloadFor('ref-c', 'C 的正文', 3));
    await waitFor(() => expect(screen.getByText('C 的正文')).toBeTruthy());

    pending.get('ref-b')!.resolve(payloadFor('ref-b', 'B 的正文', 2));
    pending.get('ref-a')!.resolve(payloadFor('ref-a', 'A 的正文'));
    await Promise.resolve();

    expect(screen.getByText('C 的正文')).toBeTruthy();
    expect(screen.queryByText('B 的正文')).toBeNull();
    expect(screen.queryByText('A 的正文')).toBeNull();
    // R06 验收第 2 条：正文、标题、locator、复制引用、保存引用必须全部属于 C。
    expect(screen.getByTestId('current-ref').textContent).toBe('ref-c');
    expect(screen.getByTestId('current-locator').textContent).toBe('{"page":3}');
    const form = document.querySelector('[data-source-ref]');
    expect(form?.getAttribute('data-source-ref')).toBe('ref-c');
  });

  it('shows no old body and no save control when the new read fails', async () => {
    readSource.mockImplementation((_case, ref) => (
      ref === 'ref-a' ? Promise.resolve(payloadFor('ref-a', 'A 的正文'))
        : Promise.reject(new Error('读取失败'))
    ));

    const { rerender } = render(<SourcePanelHarness hit={hitFor('ref-a')} />);
    await waitFor(() => expect(screen.getByText('A 的正文')).toBeTruthy());

    rerender(<SourcePanelHarness hit={hitFor('ref-b')} />);

    // describeError normalises the message; any error state will do, the point
    // is that no stale body survives it.
    await waitFor(() => expect(screen.getByText(/错误|失败|无法/)).toBeTruthy());
    expect(screen.queryByText('A 的正文')).toBeNull();
    // No save form, so a B reference cannot be saved against A's text.
    expect(screen.queryByText('复制 source_ref')).toBeNull();
  });
});
