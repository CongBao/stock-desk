import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { TaskCenterPage } from './TaskCenterPage';
import { mergeTaskSnapshots, updateTaskSnapshot } from './taskState';
import {
  TaskApiError,
  type TaskApi,
  type TaskEventView,
  type TaskView,
} from './taskApi';

const TASK_ID = '11111111-1111-4111-8111-111111111111';
const SECOND_ID = '22222222-2222-4222-8222-222222222222';
const RUN_ID = '33333333-3333-4333-8333-333333333333';

function task(overrides: Partial<TaskView> = {}): TaskView {
  return {
    id: TASK_ID,
    kind: 'backtest.run',
    status: 'running',
    progress: 0.4,
    cancelRequested: false,
    createdAt: '2026-07-08T00:00:00Z',
    updatedAt: '2026-07-08T00:00:02Z',
    startedAt: '2026-07-08T00:00:01Z',
    finishedAt: null,
    durationMs: null,
    presentation: {
      label: '股票池回测',
      stage: 'executing',
      processed: 2,
      total: 5,
      failed: 1,
      target: { type: 'backtest_run', id: RUN_ID },
    },
    ...overrides,
  };
}

function api(overrides: Partial<TaskApi> = {}): TaskApi {
  return {
    listTasks: vi.fn(() => Promise.resolve([task()])),
    getMetrics: vi.fn(() =>
      Promise.resolve({
        total: 12,
        byStatus: {
          queued: 1,
          running: 2,
          succeeded: 6,
          failed: 2,
          cancelled: 1,
        },
        failureCount: 2,
        completedCount: 9,
        averageDurationMs: 500,
        minDurationMs: 100,
        maxDurationMs: 900,
      }),
    ),
    getTask: vi.fn(() => Promise.resolve(task())),
    listEvents: vi.fn((): Promise<readonly TaskEventView[]> =>
      Promise.resolve([
        {
          id: '44444444-4444-4444-8444-444444444444',
          taskId: TASK_ID,
          level: 'info',
          progress: 0.4,
          occurredAt: '2026-07-08T00:00:02Z',
          presentation: {
            label: '已处理回测标的',
            stage: 'executing',
            processed: 2,
            total: 5,
            failed: 1,
          },
        },
      ]),
    ),
    cancelTask: vi.fn(() =>
      Promise.resolve(task({ status: 'running', cancelRequested: true })),
    ),
    ...overrides,
  };
}

function renderPage(client = api()) {
  return {
    client,
    ...render(
      <MemoryRouter>
        <TaskCenterPage api={client} pollIntervalMs={1_000} />
      </MemoryRouter>,
    ),
  };
}

afterEach(() => {
  vi.useRealTimers();
});

it('renders all-time metrics and a safe selected pool-backtest view', async () => {
  renderPage();

  expect(
    await screen.findByRole('heading', { name: '任务中心' }),
  ).toBeVisible();
  expect(screen.getByText('全部任务')).toBeVisible();
  expect(screen.getByText('12')).toBeVisible();
  const metrics = screen.getByRole('region', { name: '全部任务汇总' });
  expect(within(metrics).getByText('成功')).toBeVisible();
  expect(within(metrics).getByText('6')).toBeVisible();
  expect(screen.getByText('执行中')).toBeVisible();
  expect(screen.getByText('2 / 5')).toBeVisible();
  expect(screen.getByText('失败 1')).toBeVisible();
  expect(screen.getByRole('progressbar')).toHaveAttribute(
    'aria-valuenow',
    '40',
  );
  expect(screen.getByRole('link', { name: '打开回测报告' })).toHaveAttribute(
    'href',
    `/backtests/${RUN_ID}`,
  );
  expect(await screen.findByText('已处理回测标的')).toBeVisible();
  expect(document.body.textContent).not.toMatch(
    /PAYLOAD|RESULT|ERROR|SENTINEL/u,
  );
});

it('does not count a queued cancellation as a successful task', async () => {
  const client = api({
    getMetrics: vi.fn(() =>
      Promise.resolve({
        total: 1,
        byStatus: {
          queued: 0,
          running: 0,
          succeeded: 0,
          failed: 0,
          cancelled: 1,
        },
        failureCount: 0,
        completedCount: 0,
        averageDurationMs: null,
        minDurationMs: null,
        maxDurationMs: null,
      }),
    ),
  });
  renderPage(client);

  const metrics = await screen.findByRole('region', { name: '全部任务汇总' });
  const successful = within(metrics).getByText('成功').parentElement;
  expect(successful).not.toBeNull();
  expect(within(successful!).getByText('0')).toBeVisible();
});

it('filters the latest 100 client-side and preserves a stable selection', async () => {
  const user = userEvent.setup();
  const first = task();
  const second = task({
    id: SECOND_ID,
    kind: 'analysis.run',
    status: 'succeeded',
    progress: 1,
    startedAt: '2026-07-08T00:00:01Z',
    finishedAt: '2026-07-08T00:00:04Z',
    durationMs: 3_000,
    presentation: {
      label: '智能分析',
      stage: null,
      processed: null,
      total: null,
      failed: null,
      target: null,
    },
  });
  const client = api({
    listTasks: vi.fn(() => Promise.resolve([first, second])),
    getTask: vi.fn((id) => Promise.resolve(id === SECOND_ID ? second : first)),
  });
  renderPage(client);

  await user.click(await screen.findByRole('button', { name: /智能分析/u }));
  expect(screen.getAllByText(SECOND_ID)).toHaveLength(2);
  await user.selectOptions(screen.getByLabelText('状态筛选'), 'running');
  expect(
    screen.queryByRole('button', { name: /智能分析/u }),
  ).not.toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText('状态筛选'), 'all');
  expect(screen.getByRole('button', { name: /智能分析/u })).toHaveAttribute(
    'aria-current',
    'true',
  );
  await user.selectOptions(screen.getByLabelText('类型筛选'), 'analysis.run');
  expect(screen.getByText('筛选范围：最近 100 项')).toBeVisible();
});

it('keeps stale tasks visible when a refresh is partially degraded', async () => {
  const client = api();
  vi.mocked(client.listTasks)
    .mockResolvedValueOnce([task()])
    .mockRejectedValueOnce(new TaskApiError('network'));
  renderPage(client);
  await screen.findByRole('button', { name: /股票池回测/u });

  await userEvent.click(screen.getByRole('button', { name: '刷新任务' }));

  expect(await screen.findByRole('alert')).toHaveTextContent(
    '任务列表刷新失败',
  );
  expect(screen.getByRole('button', { name: /股票池回测/u })).toBeVisible();
});

it('shows unavailable instead of empty when the first task load fails', async () => {
  const client = api({
    listTasks: vi.fn(() => Promise.reject(new TaskApiError('storage'))),
  });
  renderPage(client);

  expect(await screen.findByRole('alert')).toHaveTextContent(
    '任务列表刷新失败',
  );
  expect(screen.getByText('任务列表暂不可用')).toBeVisible();
  expect(screen.queryByText('暂无任务')).not.toBeInTheDocument();
});

it('never shows a previous task timeline under a newly selected task', async () => {
  const first = task();
  const second = task({
    id: SECOND_ID,
    kind: 'analysis.run',
    presentation: {
      label: '智能分析',
      stage: null,
      processed: null,
      total: null,
      failed: null,
      target: null,
    },
  });
  const client = api({
    listTasks: vi.fn(() => Promise.resolve([first, second])),
    getTask: vi.fn((id) => Promise.resolve(id === TASK_ID ? first : second)),
    listEvents: vi.fn((id): Promise<readonly TaskEventView[]> =>
      id === TASK_ID
        ? Promise.resolve<TaskEventView[]>([
            {
              id: '44444444-4444-4444-8444-444444444444',
              taskId: TASK_ID,
              level: 'info',
              progress: 0.4,
              occurredAt: '2026-07-08T00:00:02Z',
              presentation: {
                label: '已处理回测标的',
                stage: 'executing',
                processed: 2,
                total: 5,
                failed: 1,
              },
            },
          ])
        : new Promise<readonly TaskEventView[]>(() => undefined),
    ),
  });
  renderPage(client);
  expect(await screen.findByText('已处理回测标的')).toBeVisible();

  await userEvent.click(screen.getByRole('button', { name: /智能分析/u }));

  expect(screen.queryByText('已处理回测标的')).not.toBeInTheDocument();
  expect(screen.getByText('暂无可显示事件。')).toBeVisible();
});

it('merges task snapshots monotonically by updated time', () => {
  const terminal = task({
    status: 'cancelled',
    cancelRequested: true,
    updatedAt: '2026-07-08T00:00:05Z',
    finishedAt: '2026-07-08T00:00:05Z',
    durationMs: 4_000,
  });
  const stale = task({ updatedAt: '2026-07-08T00:00:02Z' });

  expect(mergeTaskSnapshots([terminal], [stale])).toEqual([terminal]);
});

it('keeps lifecycle, progress and cancellation monotonic at equal timestamps', () => {
  const running = task({ progress: 0.6, cancelRequested: true });
  const queued = task({
    status: 'queued',
    progress: 0.8,
    cancelRequested: true,
    startedAt: null,
    presentation: {
      ...task().presentation,
      stage: 'queued',
      processed: 4,
    },
  });
  const lowerProgress = task({ progress: 0.3, cancelRequested: true });
  const lostCancellation = task({ progress: 0.7, cancelRequested: false });
  const terminal = task({
    status: 'succeeded',
    progress: 1,
    finishedAt: '2026-07-08T00:00:02Z',
    durationMs: 1_000,
    presentation: { ...task().presentation, stage: 'completed', processed: 5 },
  });

  expect(updateTaskSnapshot([running], queued)).toEqual([running]);
  expect(updateTaskSnapshot([running], lowerProgress)).toEqual([running]);
  expect(updateTaskSnapshot([running], lostCancellation)).toEqual([running]);
  expect(updateTaskSnapshot([terminal], running)).toEqual([terminal]);
});

it('coalesces refreshes and never overlaps serialized polling', async () => {
  vi.useFakeTimers();
  let resolveRefresh: ((value: readonly TaskView[]) => void) | undefined;
  const pending = new Promise<readonly TaskView[]>((resolve) => {
    resolveRefresh = resolve;
  });
  const client = api();
  vi.mocked(client.listTasks)
    .mockResolvedValueOnce([task()])
    .mockReturnValue(pending);
  renderPage(client);
  await act(async () => Promise.resolve());
  await act(async () => vi.advanceTimersByTimeAsync(1_000));
  expect(client.listTasks).toHaveBeenCalledTimes(2);

  await act(async () => vi.advanceTimersByTimeAsync(5_000));
  expect(client.listTasks).toHaveBeenCalledTimes(2);
  resolveRefresh?.([task()]);
  await act(async () => pending);
  await act(async () => vi.advanceTimersByTimeAsync(999));
  expect(client.listTasks).toHaveBeenCalledTimes(2);
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(client.listTasks).toHaveBeenCalledTimes(3);
});

it('stops polling after terminal state', async () => {
  vi.useFakeTimers();
  const terminal = task({
    status: 'succeeded',
    progress: 1,
    finishedAt: '2026-07-08T00:00:05Z',
    durationMs: 4_000,
  });
  const client = api({
    listTasks: vi.fn(() => Promise.resolve([terminal])),
    getTask: vi.fn(() => Promise.resolve(terminal)),
  });
  const view = renderPage(client);
  await act(async () => Promise.resolve());
  const calls = vi.mocked(client.listTasks).mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  expect(client.listTasks).toHaveBeenCalledTimes(calls);
  view.unmount();
});

it('aborts in-flight requests on unmount', async () => {
  let observedSignal: AbortSignal | undefined;
  const client = api({
    listTasks: vi.fn((options?: { readonly signal?: AbortSignal }) => {
      observedSignal = options?.signal;
      return new Promise<readonly TaskView[]>(() => undefined);
    }),
  });
  const view = renderPage(client);
  await act(async () => Promise.resolve());

  view.unmount();

  expect(observedSignal?.aborted).toBe(true);
});

it('cancels queued or running work once and reflects idempotent request state', async () => {
  const user = userEvent.setup();
  const client = api();
  renderPage(client);
  const cancel = await screen.findByRole('button', { name: '取消任务' });

  await user.click(cancel);
  await user.click(cancel);

  expect(client.cancelTask).toHaveBeenCalledTimes(1);
  expect(
    await screen.findByRole('button', { name: '已请求取消' }),
  ).toBeVisible();
  expect(screen.getByTestId('task-live-status')).toHaveTextContent(
    '已请求取消',
  );
  expect(cancel).toBeDisabled();
});

it('handles cancel conflicts safely and does not repeat ambiguous network POSTs', async () => {
  const user = userEvent.setup();
  const conflictClient = api({
    cancelTask: vi.fn(() => Promise.reject(new TaskApiError('conflict'))),
  });
  const first = renderPage(conflictClient);
  await user.click(await screen.findByRole('button', { name: '取消任务' }));
  expect(
    await screen.findByText('任务状态已变化，正在同步最新状态'),
  ).toBeVisible();
  expect(conflictClient.getTask).toHaveBeenCalledTimes(2);
  first.unmount();

  const networkClient = api({
    cancelTask: vi.fn(() => Promise.reject(new TaskApiError('network'))),
    getTask: vi.fn(() => Promise.reject(new TaskApiError('network'))),
  });
  renderPage(networkClient);
  await user.click(await screen.findByRole('button', { name: '取消任务' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('取消结果未知');
  expect(screen.getByRole('button', { name: '取消任务' })).toBeDisabled();
  await act(
    async () => new Promise((resolve) => window.setTimeout(resolve, 10)),
  );
  expect(networkClient.cancelTask).toHaveBeenCalledTimes(1);
  await user.click(screen.getByRole('button', { name: '刷新任务' }));
  await waitFor(() =>
    expect(screen.getByRole('button', { name: '取消任务' })).toBeEnabled(),
  );
});

it('announces lifecycle changes in one dedicated live region', async () => {
  renderPage();
  await screen.findByRole('button', { name: /股票池回测/u });
  const live = screen.getByTestId('task-live-status');
  expect(live).toHaveAttribute('aria-live', 'polite');
  expect(
    document.querySelectorAll('[data-testid="task-live-status"]'),
  ).toHaveLength(1);
  expect(
    document.querySelectorAll('[aria-live="polite"], [role="status"]'),
  ).toHaveLength(1);
  expect(within(live).getByText(/正在运行/u)).toBeInTheDocument();
});
