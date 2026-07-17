import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { ApiError } from '../../shared/api/client';
import { BacktestRunPage } from './BacktestRunPage';
import { backtestPollDelay } from './backtestPolling';
import type { BacktestApi, BacktestOverview } from './backtestApi';
import { coalesceBacktestOverview } from './backtestOverviewState';
import { RunProgress } from './RunProgress';

const running: BacktestOverview = {
  createdAt: '2026-07-07T00:00:00Z',
  failed: 0,
  finishedAt: null,
  processed: 1,
  progress: 0.1,
  resultHash: null,
  runId: '11111111-1111-1111-1111-111111111111',
  snapshotId: `sha256:${'a'.repeat(64)}`,
  stage: 'executing',
  startedAt: '2026-07-07T00:00:01Z',
  status: 'running',
  taskId: '22222222-2222-2222-2222-222222222222',
  total: 10,
  updatedAt: '2026-07-07T00:00:02Z',
};

const testPollDelays = [1, 2, 4, 5] as const;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function renderRun(api: BacktestApi) {
  return render(
    <MemoryRouter initialEntries={[`/backtests/${running.runId}`]}>
      <Routes>
        <Route
          path="/backtests/:runId"
          element={<BacktestRunPage api={api} pollDelays={testPollDelays} />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

it('polls overview and append-only logs with bounded backoff, then stops terminally', async () => {
  const getRun = vi
    .fn()
    .mockResolvedValueOnce(running)
    .mockResolvedValueOnce({
      ...running,
      finishedAt: '2026-07-07T00:00:03Z',
      processed: 10,
      progress: 1,
      stage: 'completed',
      status: 'succeeded',
    });
  const getLogs = vi
    .fn()
    .mockResolvedValueOnce({
      afterCursor: 'tail-1',
      items: [{ detail: {}, level: 'info', message: '启动', ordinal: 1 }],
      nextCursor: null,
    })
    .mockResolvedValueOnce({
      afterCursor: 'tail-2',
      items: [
        { detail: {}, level: 'info', message: '启动', ordinal: 1 },
        { detail: {}, level: 'info', message: '完成', ordinal: 2 },
      ],
      nextCursor: null,
    });
  const api = {
    cancel: vi.fn(),
    create: vi.fn(),
    getLogs,
    getRun,
    listRuns: vi.fn(),
    preflight: vi.fn(),
  } satisfies BacktestApi;

  renderRun(api);
  expect(await screen.findByText('1 / 10')).toBeVisible();
  expect(await screen.findByText('启动')).toBeVisible();

  expect(await screen.findByText('回测结果')).toBeVisible();
  expect(screen.getAllByText('启动')).toHaveLength(1);
  expect(await screen.findByText('完成')).toBeVisible();
  expect(getLogs).toHaveBeenLastCalledWith(
    running.runId,
    expect.objectContaining({ afterCursor: 'tail-1' }),
  );

  const runCalls = getRun.mock.calls.length;
  await act(async () => {
    await new Promise((resolve) => window.setTimeout(resolve, 20));
  });
  expect(getRun).toHaveBeenCalledTimes(runCalls);
});

it('reuses an overview only when every response field is unchanged', () => {
  expect(coalesceBacktestOverview(null, running)).toBe(running);
  expect(coalesceBacktestOverview(running, { ...running })).toBe(running);

  for (const [key, value] of Object.entries(running)) {
    const replacement = {
      ...running,
      [key]:
        typeof value === 'number'
          ? value + 1
          : value === null
            ? 'changed'
            : `${value}-changed`,
    };
    expect(coalesceBacktestOverview(running, replacement), key).toBe(
      replacement,
    );
  }
});

it('exposes the exact rendered progress tuple for browser evidence', () => {
  render(<RunProgress run={running} />);

  expect(screen.getByRole('region', { name: '运行进度' })).toHaveAttribute(
    'data-rendered-progress',
    'running|executing|1|10|0',
  );
});

it('backs off at 500ms, 1s, 2s, then caps at 5s', () => {
  expect([0, 1, 2, 3, 8].map((attempt) => backtestPollDelay(attempt))).toEqual([
    500, 1000, 2000, 5000, 5000,
  ]);
});

it('offers cancellation once and preserves the partial-result shell', async () => {
  const user = userEvent.setup();
  const pending = deferred<Awaited<ReturnType<BacktestApi['cancel']>>>();
  const cancel = vi.fn().mockReturnValue(pending.promise);
  const cancelled = {
    runId: running.runId,
    snapshotId: running.snapshotId,
    taskId: running.taskId,
    warnings: [],
  };
  const api = {
    cancel,
    create: vi.fn(),
    getLogs: vi
      .fn()
      .mockResolvedValue({ afterCursor: null, items: [], nextCursor: null }),
    getRun: vi.fn().mockResolvedValue(running),
    listRuns: vi.fn(),
    preflight: vi.fn(),
  } satisfies BacktestApi;
  renderRun(api);

  const cancelButton = await screen.findByRole('button', { name: '取消回测' });
  await user.click(cancelButton);
  expect(cancelButton).toHaveAttribute('aria-busy', 'true');
  expect(cancelButton).toHaveTextContent('取消回测');
  expect(cancelButton).toBeDisabled();
  expect(screen.getAllByTestId('async-action-spinner')).toHaveLength(1);
  await user.dblClick(cancelButton);
  expect(cancel).toHaveBeenCalledTimes(1);
  pending.resolve(cancelled);
  await waitFor(() => expect(cancelButton).not.toHaveAttribute('aria-busy'));
  expect(screen.getByText(/已保留的部分结果/u)).toBeVisible();
});

it.each(['succeeded', 'partial_failed', 'failed', 'cancelled'])(
  'stops overview polling for terminal status %s',
  async (status) => {
    const getRun = vi.fn().mockResolvedValue({
      ...running,
      status,
      stage:
        status === 'cancelled'
          ? 'cancelled'
          : status === 'failed'
            ? 'failed'
            : 'completed',
      finishedAt: '2026-07-07T00:00:03Z',
    });
    const api = {
      cancel: vi.fn(),
      create: vi.fn(),
      getLogs: vi
        .fn()
        .mockResolvedValue({ afterCursor: null, items: [], nextCursor: null }),
      getRun,
      listRuns: vi.fn(),
      preflight: vi.fn(),
    } satisfies BacktestApi;
    renderRun(api);
    expect(await screen.findByText('回测结果')).toBeVisible();
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 15));
    });
    expect(getRun).toHaveBeenCalledTimes(1);
  },
);

it('does not retry a permanent missing-run response', async () => {
  const getRun = vi
    .fn()
    .mockRejectedValue(
      new ApiError('private details', { kind: 'http', status: 404 }),
    );
  const api = {
    cancel: vi.fn(),
    create: vi.fn(),
    getLogs: vi
      .fn()
      .mockResolvedValue({ afterCursor: null, items: [], nextCursor: null }),
    getRun,
    listRuns: vi.fn(),
    preflight: vi.fn(),
  } satisfies BacktestApi;
  renderRun(api);
  expect(await screen.findByRole('alert')).toHaveTextContent('该回测不存在');
  expect(screen.queryByText('正在读取运行状态…')).not.toBeInTheDocument();
  expect(screen.queryByText(/private details/u)).not.toBeInTheDocument();
  await act(async () => {
    await new Promise((resolve) => window.setTimeout(resolve, 15));
  });
  expect(getRun).toHaveBeenCalledTimes(1);
});

it('retries a transient terminal-log failure before declaring the tail complete', async () => {
  const getLogs = vi
    .fn()
    .mockRejectedValueOnce(new ApiError('offline', { kind: 'network' }))
    .mockResolvedValue({
      afterCursor: 'tail-1',
      items: [
        { detail: {}, level: 'info', message: '最终日志已补齐', ordinal: 1 },
      ],
      nextCursor: null,
    });
  const api = {
    cancel: vi.fn(),
    create: vi.fn(),
    getLogs,
    getRun: vi.fn().mockResolvedValue({
      ...running,
      status: 'succeeded',
      stage: 'completed',
      processed: 10,
      progress: 1,
      finishedAt: '2026-07-07T00:00:03Z',
    }),
    listRuns: vi.fn(),
    preflight: vi.fn(),
  } satisfies BacktestApi;
  renderRun(api);
  expect(await screen.findByText('最终日志已补齐')).toBeVisible();
  expect(getLogs.mock.calls.length).toBeGreaterThanOrEqual(2);
  expect(
    screen.queryByRole('button', { name: '重试读取日志' }),
  ).not.toBeInTheDocument();
});

it('drains terminal log pages with after_cursor and retains a bounded recent window', async () => {
  let page = 0;
  const getLogs = vi.fn().mockImplementation(() => {
    page += 1;
    const start = (page - 1) * 100 + 1;
    const end = page < 4 ? page * 100 : 350;
    return Promise.resolve({
      afterCursor: `tail-${String(end)}`,
      items: Array.from({ length: end - start + 1 }, (_, index) => ({
        detail: {},
        level: 'info',
        message: `日志 ${String(start + index)}`,
        ordinal: start + index,
      })),
      nextCursor: page < 4 ? `more-${String(page)}` : null,
    });
  });
  const api = {
    cancel: vi.fn(),
    create: vi.fn(),
    getLogs,
    getRun: vi.fn().mockResolvedValue({
      ...running,
      status: 'succeeded',
      stage: 'completed',
      processed: 10,
      progress: 1,
      finishedAt: '2026-07-07T00:00:03Z',
    }),
    listRuns: vi.fn(),
    preflight: vi.fn(),
  } satisfies BacktestApi;
  renderRun(api);
  expect(await screen.findByText('日志 350')).toBeVisible();
  expect(screen.queryByText('日志 1')).not.toBeInTheDocument();
  expect(
    screen.getByRole('list', { name: '' }).children.length,
  ).toBeLessThanOrEqual(300);
  expect(getLogs).toHaveBeenCalledTimes(4);
  expect(getLogs).toHaveBeenLastCalledWith(
    running.runId,
    expect.objectContaining({ afterCursor: 'tail-300' }),
  );
});

it('aborts outstanding overview and log requests on unmount', () => {
  const signals: AbortSignal[] = [];
  const pending = (
    _runId: string,
    options?: { readonly signal?: AbortSignal },
  ) => {
    if (options?.signal !== undefined) signals.push(options.signal);
    return new Promise<never>(() => undefined);
  };
  const api = {
    cancel: vi.fn(),
    create: vi.fn(),
    getLogs: vi.fn(pending),
    getRun: vi.fn(pending),
    listRuns: vi.fn(),
    preflight: vi.fn(),
  } as unknown as BacktestApi;
  const mounted = renderRun(api);
  expect(signals).toHaveLength(2);
  mounted.unmount();
  expect(signals.every((signal) => signal.aborted)).toBe(true);
});
