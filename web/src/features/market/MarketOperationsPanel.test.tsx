import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { MarketOperationsPanel } from './MarketOperationsPanel';
import type { JsonValue } from '../../shared/api/client';
import type {
  MarketTask,
  MarketTaskEvent,
  MarketWorkflowApi,
} from './marketWorkflowApi';

const queued: MarketTask = {
  id: '11111111-1111-1111-1111-111111111111',
  kind: 'market.update',
  status: 'queued',
  progress: 0,
  payload: {
    symbols: ['600000.SH'],
    period: '1d',
    adjustment: 'qfq',
    start: '2026-01-01T00:00:00Z',
    end: '2026-07-01T00:00:00Z',
  },
  result: null,
  error: null,
  cancelRequested: false,
  createdAt: '2026-07-06T08:00:00Z',
  updatedAt: '2026-07-06T08:00:00Z',
  startedAt: null,
  finishedAt: null,
};

function renderPanel(api: MarketWorkflowApi) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <MarketOperationsPanel
          api={api}
          period="1d"
          adjustment="qfq"
          selectedInstrument={{ symbol: '600000.SH', name: '浦发银行' }}
          selectedPool={{
            id: 'custom-watch',
            name: '观察池',
            symbols: ['600000.SH'],
            kind: 'custom',
            revision: 1,
          }}
        />
      </QueryClientProvider>,
    ),
    queryClient,
  };
}

function terminalTask(
  value: MarketTask,
  result: Readonly<Record<string, JsonValue>>,
): MarketTask {
  return {
    ...value,
    status: 'succeeded',
    progress: 1,
    result,
    updatedAt: '2026-07-06T08:00:01Z',
    startedAt: '2026-07-06T08:00:00Z',
    finishedAt: '2026-07-06T08:00:01Z',
  };
}

it('creates a custom pool from searched selections and launches a cache update', async () => {
  const user = userEvent.setup();
  const createPool = vi.fn(() => Promise.resolve({ poolId: 'new-pool' }));
  const createUpdate = vi.fn(() => Promise.resolve(queued));
  const api = {
    createPool,
    createUpdate,
    getTask: vi.fn(() => Promise.resolve(queued)),
    getTaskEvents: vi.fn(() => Promise.resolve([])),
    getUpdateItems: vi.fn(() => Promise.resolve([])),
    createCatalogUpdate: vi.fn(() => Promise.resolve(queued)),
    cancelTask: vi.fn(() =>
      Promise.resolve({ ...queued, cancelRequested: true }),
    ),
    getDailySchedule: vi.fn(() => Promise.reject(new Error('missing'))),
    saveDailySchedule: vi.fn(),
  } as unknown as MarketWorkflowApi;
  renderPanel(api);

  await user.click(screen.getByRole('button', { name: '新建自定义池' }));
  await user.type(
    screen.getByRole('textbox', { name: '股票池名称' }),
    '核心观察',
  );
  await user.click(screen.getByRole('button', { name: /加入浦发银行/u }));
  await user.click(screen.getByRole('button', { name: '创建股票池' }));
  await waitFor(() =>
    expect(createPool).toHaveBeenCalledWith(
      { name: '核心观察', symbols: ['600000.SH'] },
      expect.objectContaining({ signal: expect.any(AbortSignal) as unknown }),
    ),
  );

  await user.click(screen.getByRole('button', { name: '启动更新' }));
  await waitFor(() => expect(createUpdate).toHaveBeenCalledTimes(1));
  expect(createUpdate).toHaveBeenCalledWith(
    expect.objectContaining({
      symbols: ['600000.SH'],
      period: '1d',
      adjustment: 'qfq',
    }),
    expect.objectContaining({ signal: expect.any(AbortSignal) as unknown }),
  );
  expect(screen.getByText('排队中')).toBeInTheDocument();
});

it('saves an Asia/Shanghai schedule with an explicit frozen pool snapshot', async () => {
  const user = userEvent.setup();
  const saveDailySchedule = vi.fn(() =>
    Promise.resolve({
      enabled: true,
      localTime: '18:30',
      symbolsFrozen: true,
      nextDueAt: '2026-07-07T10:30:00Z',
      lastEnqueuedLocalDate: null,
    }),
  );
  const api = {
    getDailySchedule: vi.fn(() => Promise.reject(new Error('missing'))),
    saveDailySchedule,
  } as unknown as MarketWorkflowApi;
  renderPanel(api);

  await user.click(screen.getByRole('radio', { name: '当前股票池' }));
  await user.click(screen.getByRole('checkbox', { name: '启用每日更新' }));
  await user.clear(screen.getByLabelText('每日更新时间'));
  await user.type(screen.getByLabelText('每日更新时间'), '18:30');
  await user.click(screen.getByRole('button', { name: '保存每日计划' }));

  await waitFor(() => expect(saveDailySchedule).toHaveBeenCalledTimes(1));
  expect(saveDailySchedule).toHaveBeenCalledWith(
    expect.objectContaining({
      enabled: true,
      localTime: '18:30',
      payload: expect.objectContaining({ symbols: ['600000.SH'] }) as unknown,
    }),
    expect.objectContaining({ signal: expect.any(AbortSignal) as unknown }),
  );
  expect(await screen.findByText(/范围快照已冻结/u)).toBeInTheDocument();
});

it('shows the newest progress event from the descending durable event feed', async () => {
  const user = userEvent.setup();
  const running: MarketTask = {
    ...queued,
    status: 'running',
    progress: 0.5,
    updatedAt: '2026-07-06T08:00:02Z',
    startedAt: '2026-07-06T08:00:00Z',
  };
  const event = (
    id: string,
    symbol: string,
    processed: number,
  ): MarketTaskEvent => ({
    id,
    taskId: queued.id,
    eventName: 'task.progressed',
    progress: processed / 4,
    detail: {
      stage: 'routing',
      current_symbol: symbol,
      processed,
      total: 4,
      succeeded: processed,
      failed: 0,
      cancelled: 0,
    },
    occurredAt: `2026-07-06T08:00:0${String(processed)}Z`,
  });
  const api = {
    createUpdate: vi.fn(() => Promise.resolve(running)),
    getTask: vi.fn(() => Promise.resolve(running)),
    getTaskEvents: vi.fn(() =>
      Promise.resolve([
        event('22222222-2222-2222-2222-222222222222', '600036.SH', 2),
        event('33333333-3333-3333-3333-333333333333', '600000.SH', 1),
      ]),
    ),
    getUpdateItems: vi.fn(() => Promise.resolve([])),
    getDailySchedule: vi.fn(() => Promise.reject(new Error('missing'))),
  } as unknown as MarketWorkflowApi;
  renderPanel(api);

  await user.click(screen.getByRole('button', { name: '启动更新' }));

  expect(await screen.findByText(/600036\.SH/u)).toBeInTheDocument();
  expect(screen.queryByText(/600000\.SH.*1\/4/u)).not.toBeInTheDocument();
});

it('refreshes catalog queries and cached bars after successful durable tasks', async () => {
  const user = userEvent.setup();
  const updateResult = terminalTask(queued, {
    total: 1,
    succeeded: 1,
    failed: 0,
    cancelled: 0,
    configuration_fingerprint: `sha256:${'a'.repeat(64)}`,
  });
  const distinctUpdateResult: MarketTask = {
    ...updateResult,
    id: '44444444-4444-4444-4444-444444444444',
  };
  const catalogResult: MarketTask = {
    ...terminalTask(queued, {
      source: 'akshare',
      row_count: 3,
      manifest_record_id: `sha256:${'b'.repeat(64)}`,
      full_a_pool_id: 'preset:all-a',
      preset_successes: [{ preset_key: 'all-a', category: 'all_a' }],
      preset_failures: [],
      configuration_fingerprint: `sha256:${'c'.repeat(64)}`,
    }),
    kind: 'market.catalog.update',
    payload: {},
  };
  const api = {
    createCatalogUpdate: vi.fn(() => Promise.resolve(catalogResult)),
    createUpdate: vi.fn(() => Promise.resolve(distinctUpdateResult)),
    getTaskEvents: vi.fn(() => Promise.resolve([])),
    getUpdateItems: vi.fn(() => Promise.resolve([])),
    getDailySchedule: vi.fn(() => Promise.reject(new Error('missing'))),
  } as unknown as MarketWorkflowApi;
  const { queryClient } = renderPanel(api);
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries');

  await user.click(screen.getByRole('button', { name: '更新证券目录' }));
  await waitFor(() =>
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ['market', 'instrument-search'],
    }),
  );
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['market', 'pools'] });
  expect(invalidate).toHaveBeenCalledWith({
    queryKey: ['market', 'pool-member-search'],
  });
  expect(await screen.findByText(/目录 3 只/u)).toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: '启动更新' }));
  await waitFor(() =>
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ['market', 'bars'],
    }),
  );
  expect(await screen.findByText(/成功 1/u)).toBeInTheDocument();
});

it('requires an explicit second confirmation before deleting a custom pool', async () => {
  const user = userEvent.setup();
  const deletePool = vi.fn(() => Promise.resolve());
  const api = {
    deletePool,
    getDailySchedule: vi.fn(() => Promise.reject(new Error('missing'))),
  } as unknown as MarketWorkflowApi;
  renderPanel(api);

  await user.click(screen.getByRole('button', { name: '编辑当前股票池' }));
  await user.click(screen.getByRole('button', { name: '删除股票池' }));
  expect(deletePool).not.toHaveBeenCalled();
  expect(screen.getByRole('alert')).toHaveTextContent('删除后无法撤销');

  await user.click(screen.getByRole('button', { name: '确认删除' }));
  await waitFor(() => expect(deletePool).toHaveBeenCalledTimes(1));
  expect(deletePool).toHaveBeenCalledWith(
    'custom-watch',
    1,
    expect.objectContaining({ signal: expect.any(AbortSignal) as unknown }),
  );
});
