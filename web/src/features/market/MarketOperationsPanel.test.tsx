import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { MarketOperationsPanel } from './MarketOperationsPanel';
import type { JsonValue } from '../../shared/api/client';
import type { MarketApi } from './marketApi';
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

function renderPanel(
  api: MarketWorkflowApi,
  marketApiClient = {
    searchInstruments: vi.fn(
      () => new Promise<readonly never[]>(() => undefined),
    ),
  } as unknown as MarketApi,
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <MarketOperationsPanel
            api={api}
            marketApiClient={marketApiClient}
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
        </MemoryRouter>
      </QueryClientProvider>,
    ),
    queryClient,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

it('links the visible market selection and date range to a refreshable backtest prefill', async () => {
  const user = userEvent.setup();
  renderPanel({
    getDailySchedule: vi.fn(() => Promise.reject(new Error('missing'))),
  } as unknown as MarketWorkflowApi);

  await user.clear(screen.getByLabelText('开始日期'));
  await user.type(screen.getByLabelText('开始日期'), '2024-02-10');
  await user.clear(screen.getByLabelText('结束日期'));
  await user.type(screen.getByLabelText('结束日期'), '2024-03-15');

  const link = screen.getByRole('link', { name: '回测当前股票' });
  expect(link).toHaveAttribute(
    'href',
    '/backtests?symbol=600000.SH&period=1d&adjustment=qfq&start=2024-02-10&end=2024-03-15',
  );
  expect(link.getAttribute('href')).not.toMatch(/formula|version|source/u);
});

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

it('keeps an in-flight create in one locked dialog session', async () => {
  const user = userEvent.setup();
  const pendingCreate = deferred<unknown>();
  const createPool = vi.fn(() => pendingCreate.promise);
  renderPanel({
    createPool,
    getDailySchedule: vi.fn(() => Promise.reject(new Error('missing'))),
  } as unknown as MarketWorkflowApi);

  await user.click(screen.getByRole('button', { name: '新建自定义池' }));
  await user.type(
    screen.getByRole('textbox', { name: '股票池名称' }),
    '旧会话草稿',
  );
  await user.click(screen.getByRole('button', { name: /加入浦发银行/u }));
  const submit = screen.getByRole('button', { name: '创建股票池' });
  fireEvent.click(submit);
  fireEvent.click(submit);

  await waitFor(() => expect(createPool).toHaveBeenCalledTimes(1));
  expect(createPool).toHaveBeenCalledWith(
    { name: '旧会话草稿', symbols: ['600000.SH'] },
    expect.objectContaining({ signal: expect.any(AbortSignal) as unknown }),
  );
  const status = screen.getByRole('status');
  expect(status).toHaveClass('visually-hidden');
  expect(status).toBeEmptyDOMElement();
  expect(status).toHaveFocus();
  expect(submit).toHaveAttribute('aria-busy', 'true');
  expect(submit).toHaveTextContent('创建股票池');
  expect(screen.getAllByTestId('async-action-spinner')).toHaveLength(1);
  expect(submit).toBeDisabled();

  await user.click(screen.getByRole('button', { name: '取消' }));
  expect(screen.getByRole('dialog', { name: '新建自定义池' })).toBeVisible();
  expect(
    screen.queryByRole('alertdialog', { name: '放弃新股票池草稿？' }),
  ).not.toBeInTheDocument();
  expect(status).toHaveFocus();
  await user.keyboard('{Escape}');
  expect(screen.getByRole('dialog', { name: '新建自定义池' })).toBeVisible();
  expect(status).toHaveFocus();
  expect(createPool).toHaveBeenCalledTimes(1);

  await act(async () => {
    pendingCreate.resolve({});
    await pendingCreate.promise;
  });
  await waitFor(() =>
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument(),
  );
  expect(createPool).toHaveBeenCalledTimes(1);
});

it('keeps an in-flight update in one locked dialog session', async () => {
  const user = userEvent.setup();
  const pendingUpdate = deferred<unknown>();
  const updatePool = vi.fn(() => pendingUpdate.promise);
  renderPanel({
    updatePool,
    getDailySchedule: vi.fn(() => Promise.reject(new Error('missing'))),
  } as unknown as MarketWorkflowApi);

  await user.click(screen.getByRole('button', { name: '编辑当前股票池' }));
  const oldName = screen.getByRole('textbox', { name: '股票池名称' });
  await user.clear(oldName);
  await user.type(oldName, '旧版本草稿');
  const submit = screen.getByRole('button', { name: '保存股票池' });
  fireEvent.click(submit);
  fireEvent.click(submit);

  await waitFor(() => expect(updatePool).toHaveBeenCalledTimes(1));
  expect(updatePool).toHaveBeenCalledWith(
    'custom-watch',
    {
      expectedRevision: 1,
      name: '旧版本草稿',
      symbols: ['600000.SH'],
    },
    expect.objectContaining({ signal: expect.any(AbortSignal) as unknown }),
  );
  const status = screen.getByRole('status');
  expect(status).toHaveClass('visually-hidden');
  expect(status).toBeEmptyDOMElement();
  expect(status).toHaveFocus();
  expect(submit).toHaveAttribute('aria-busy', 'true');
  expect(submit).toHaveTextContent('保存股票池');
  expect(screen.getAllByTestId('async-action-spinner')).toHaveLength(1);
  expect(submit).toBeDisabled();

  await user.click(screen.getByRole('button', { name: '取消' }));
  expect(screen.getByRole('dialog', { name: '编辑自定义池' })).toBeVisible();
  expect(
    screen.queryByRole('alertdialog', { name: '放弃股票池更改？' }),
  ).not.toBeInTheDocument();
  expect(status).toHaveFocus();
  await user.keyboard('{Escape}');
  expect(screen.getByRole('dialog', { name: '编辑自定义池' })).toBeVisible();
  expect(status).toHaveFocus();
  expect(updatePool).toHaveBeenCalledTimes(1);

  await act(async () => {
    pendingUpdate.resolve({});
    await pendingUpdate.promise;
  });
  await waitFor(() =>
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument(),
  );
  expect(updatePool).toHaveBeenCalledTimes(1);
});

it('protects a changed create draft and restores the confirmation origin on Escape', async () => {
  const user = userEvent.setup();
  renderPanel({
    getDailySchedule: vi.fn(() => Promise.reject(new Error('missing'))),
  } as unknown as MarketWorkflowApi);

  const trigger = screen.getByRole('button', { name: '新建自定义池' });
  await user.click(trigger);
  const name = screen.getByRole('textbox', { name: '股票池名称' });
  await user.type(name, '待确认草稿');
  const cancel = screen.getByRole('button', { name: '取消' });
  await user.click(cancel);

  expect(
    screen.getByRole('alertdialog', { name: '放弃新股票池草稿？' }),
  ).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '继续编辑' })).toHaveFocus();

  await user.keyboard('{Escape}');
  await waitFor(() => expect(cancel).toHaveFocus());
  expect(name).toHaveValue('待确认草稿');

  await user.click(cancel);
  await user.click(screen.getByRole('button', { name: '放弃更改' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await waitFor(() => expect(trigger).toHaveFocus());

  await user.click(trigger);
  expect(screen.getByRole('textbox', { name: '股票池名称' })).toHaveValue('');
});

it('protects edited pool fields, members, and search draft before closing', async () => {
  const user = userEvent.setup();
  renderPanel({
    getDailySchedule: vi.fn(() => Promise.reject(new Error('missing'))),
  } as unknown as MarketWorkflowApi);

  const trigger = screen.getByRole('button', {
    name: '编辑当前股票池',
  });
  await user.click(trigger);
  const name = screen.getByRole('textbox', { name: '股票池名称' });
  await user.clear(name);
  await user.type(name, '改名观察池');
  const search = screen.getByRole('textbox', { name: '编辑池搜索证券' });
  fireEvent.change(search, { target: { value: ' ' } });
  search.focus();
  await user.keyboard('{Escape}');

  expect(
    screen.getByRole('alertdialog', { name: '放弃股票池更改？' }),
  ).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '继续编辑' })).toHaveFocus();
  await user.keyboard('{Escape}');
  await waitFor(() => expect(search).toHaveFocus());
  expect(name).toHaveValue('改名观察池');
  expect(search).toHaveValue(' ');

  await user.click(screen.getByRole('button', { name: '取消' }));
  await user.click(screen.getByRole('button', { name: '放弃更改' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await waitFor(() => expect(trigger).toHaveFocus());
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
  const deleteTrigger = screen.getByRole('button', { name: '删除股票池' });
  await user.click(deleteTrigger);
  expect(deletePool).not.toHaveBeenCalled();
  expect(screen.getByRole('alertdialog')).toHaveTextContent('删除后无法撤销');
  expect(screen.getByRole('button', { name: '保留股票池' })).toHaveFocus();

  await user.keyboard('{Escape}');
  await waitFor(() => expect(deleteTrigger).toHaveFocus());
  expect(deletePool).not.toHaveBeenCalled();

  await user.click(deleteTrigger);

  await user.click(screen.getByRole('button', { name: '确认删除' }));
  await waitFor(() => expect(deletePool).toHaveBeenCalledTimes(1));
  expect(deletePool).toHaveBeenCalledWith(
    'custom-watch',
    1,
    expect.objectContaining({ signal: expect.any(AbortSignal) as unknown }),
  );
});

it('locks a pending delete, reports failure, and allows a safe retry', async () => {
  const user = userEvent.setup();
  const pendingDelete = deferred<void>();
  const deletePool = vi
    .fn()
    .mockImplementationOnce(() => pendingDelete.promise)
    .mockImplementationOnce(() => Promise.resolve());
  const updatePool = vi.fn();
  renderPanel({
    deletePool,
    updatePool,
    getDailySchedule: vi.fn(() => Promise.reject(new Error('missing'))),
  } as unknown as MarketWorkflowApi);

  await user.click(screen.getByRole('button', { name: '编辑当前股票池' }));
  await user.click(screen.getByRole('button', { name: '删除股票池' }));
  await user.click(screen.getByRole('button', { name: '确认删除' }));
  await waitFor(() => expect(deletePool).toHaveBeenCalledTimes(1));

  const status = screen.getByRole('status');
  expect(status).toHaveClass('visually-hidden');
  expect(status).toBeEmptyDOMElement();
  expect(status).toHaveFocus();
  expect(screen.getByRole('button', { name: '保留股票池' })).toBeDisabled();
  const confirmDelete = screen.getByRole('button', { name: '确认删除' });
  expect(confirmDelete).toHaveAttribute('aria-busy', 'true');
  expect(confirmDelete).toHaveTextContent('确认删除');
  expect(screen.getAllByTestId('async-action-spinner')).toHaveLength(1);
  expect(confirmDelete).toBeDisabled();

  await user.keyboard('{Escape}');
  expect(
    screen.getByRole('alertdialog', { name: '确认删除股票池？' }),
  ).toBeVisible();
  expect(status).toHaveFocus();
  expect(screen.queryByRole('textbox', { name: '股票池名称' })).toBeNull();
  expect(updatePool).not.toHaveBeenCalled();

  await act(async () => {
    pendingDelete.reject(new Error('offline'));
    await pendingDelete.promise.catch(() => undefined);
  });
  expect(await screen.findByRole('alert')).toHaveTextContent(
    '股票池删除失败，请重试或返回编辑。',
  );
  expect(screen.getByRole('button', { name: '保留股票池' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '确认删除' })).toBeEnabled();

  await user.click(screen.getByRole('button', { name: '保留股票池' }));
  const deleteTrigger = screen.getByRole('button', { name: '删除股票池' });
  await waitFor(() => expect(deleteTrigger).toHaveFocus());
  await user.click(deleteTrigger);
  await user.click(screen.getByRole('button', { name: '确认删除' }));
  await waitFor(() => expect(deletePool).toHaveBeenCalledTimes(2));
  await waitFor(() =>
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument(),
  );
});
