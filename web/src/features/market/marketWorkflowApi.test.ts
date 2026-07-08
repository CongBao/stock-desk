import type { ApiClient, JsonValue } from '../../shared/api/client';
import {
  createMarketWorkflowApi,
  MarketWorkflowProtocolError,
} from './marketWorkflowApi';

const task = {
  id: '11111111-1111-1111-1111-111111111111',
  correlation_id: '11111111-1111-1111-1111-111111111111',
  kind: 'market.update',
  status: 'running',
  progress: 0.5,
  payload: {
    symbols: ['600000.SH'],
    period: '1d',
    adjustment: 'qfq',
    start: '2024-01-01T00:00:00Z',
    end: '2024-01-03T00:00:00Z',
  },
  result: null,
  error: null,
  cancel_requested: false,
  worker_id: 'worker-1',
  created_at: '2026-07-06T08:00:00Z',
  updated_at: '2026-07-06T08:00:01Z',
  started_at: '2026-07-06T08:00:00Z',
  finished_at: null,
  duration_ms: null,
  presentation: {
    label: '数据更新',
    stage: null,
    processed: null,
    total: null,
    failed: null,
    target: null,
  },
} as const;

function client(
  responses: Partial<Record<keyof ApiClient, JsonValue | undefined>>,
) {
  return {
    delete: vi.fn(() => Promise.resolve(responses.delete)),
    get: vi.fn(() => Promise.resolve(responses.get)),
    post: vi.fn(() => Promise.resolve(responses.post)),
    put: vi.fn(() => Promise.resolve(responses.put)),
  } satisfies ApiClient;
}

it('strictly decodes update task items and the frozen daily schedule', async () => {
  const transport = client({
    post: task,
    get: [
      {
        task_id: task.id,
        ordinal: 0,
        symbol: '600000.SH',
        status: 'failed',
        manifest_record_id: null,
        dataset_version: null,
        reason: 'routing:no_provider',
        created_at: '2026-07-06T08:00:02Z',
      },
    ],
    put: {
      id: '00000000-0000-0000-0000-000000000001',
      enabled: true,
      timezone: 'Asia/Shanghai',
      local_time: '18:30',
      payload: task.payload,
      symbols_frozen: true,
      last_enqueued_local_date: null,
      next_due_at: '2026-07-06T10:30:00Z',
      created_at: '2026-07-06T08:00:00Z',
      updated_at: '2026-07-06T08:00:00Z',
    },
  });
  const api = createMarketWorkflowApi(transport);

  const created = await api.createUpdate(task.payload);
  expect(created.status).toBe('running');
  const items = await api.getUpdateItems(task.id);
  expect(items[0]).toMatchObject({ symbol: '600000.SH', status: 'failed' });
  const schedule = await api.saveDailySchedule({
    enabled: true,
    localTime: '18:30',
    payload: task.payload,
  });
  expect(schedule.symbolsFrozen).toBe(true);
  expect(schedule.nextDueAt).toBe('2026-07-06T10:30:00Z');
});

it('accepts the browser-safe presentation included by current task responses', async () => {
  const transport = client({
    get: {
      ...task,
      presentation: {
        label: '数据更新',
        stage: null,
        processed: null,
        total: null,
        failed: null,
        target: null,
      },
    },
  });

  await expect(
    createMarketWorkflowApi(transport).getTask(task.id),
  ).resolves.toMatchObject({ status: 'running', progress: 0.5 });
});

it('rejects unknown task keys and impossible terminal task state', async () => {
  const extra = client({ get: { ...task, unexpected: true } });
  await expect(
    createMarketWorkflowApi(extra).getTask(task.id),
  ).rejects.toBeInstanceOf(MarketWorkflowProtocolError);

  const impossible = client({
    get: { ...task, status: 'succeeded', progress: 0.5, finished_at: null },
  });
  await expect(
    createMarketWorkflowApi(impossible).getTask(task.id),
  ).rejects.toBeInstanceOf(MarketWorkflowProtocolError);
});

it('accepts the durable queued-to-cancelled state without a duration', async () => {
  const cancelled = client({
    get: {
      ...task,
      status: 'cancelled',
      progress: 0,
      cancel_requested: true,
      worker_id: null,
      started_at: null,
      finished_at: '2026-07-06T08:00:01Z',
      duration_ms: null,
    },
  });

  await expect(
    createMarketWorkflowApi(cancelled).getTask(task.id),
  ).resolves.toMatchObject({ status: 'cancelled', cancelRequested: true });
});

it('rejects unknown market result fields and malformed durable event details', async () => {
  const succeeded = {
    ...task,
    status: 'succeeded',
    progress: 1,
    result: {
      total: 1,
      succeeded: 1,
      failed: 0,
      cancelled: 0,
      configuration_fingerprint: `sha256:${'a'.repeat(64)}`,
      unexpected: true,
    },
    updated_at: '2026-07-06T08:00:02Z',
    finished_at: '2026-07-06T08:00:02Z',
    duration_ms: 2_000,
  };
  await expect(
    createMarketWorkflowApi(client({ get: succeeded })).getTask(task.id),
  ).rejects.toBeInstanceOf(MarketWorkflowProtocolError);

  const malformedEvent = {
    id: '22222222-2222-2222-2222-222222222222',
    task_id: task.id,
    correlation_id: task.id,
    event_name: 'task.progressed',
    level: 'info',
    progress: 0.5,
    detail: {
      stage: 'routing',
      processed: 0,
      total: 1,
      current_symbol: '600000.SH',
      succeeded: 0,
      failed: 0,
      cancelled: 0,
      unexpected: true,
    },
    occurred_at: '2026-07-06T08:00:01Z',
  };
  await expect(
    createMarketWorkflowApi(client({ get: [malformedEvent] })).getTaskEvents(
      task.id,
    ),
  ).rejects.toBeInstanceOf(MarketWorkflowProtocolError);
});

it('requires the durable event feed to remain newest-first', async () => {
  const event = (id: string, occurredAt: string) => ({
    id,
    task_id: task.id,
    correlation_id: task.id,
    event_name: 'task.claimed',
    level: 'info',
    progress: 0,
    detail: { worker_id: 'worker-1' },
    occurred_at: occurredAt,
  });
  const transport = client({
    get: [
      event('22222222-2222-2222-2222-222222222222', '2026-07-06T08:00:00Z'),
      event('33333333-3333-3333-3333-333333333333', '2026-07-06T08:00:01Z'),
    ],
  });

  await expect(
    createMarketWorkflowApi(transport).getTaskEvents(task.id),
  ).rejects.toBeInstanceOf(MarketWorkflowProtocolError);
});

it('accepts a next due time before monotonic updated_at after wall-clock rollback', async () => {
  const schedule = {
    id: '00000000-0000-0000-0000-000000000001',
    enabled: true,
    timezone: 'Asia/Shanghai',
    local_time: '18:30',
    payload: task.payload,
    symbols_frozen: true,
    last_enqueued_local_date: '2026-07-05',
    next_due_at: '2026-07-06T10:30:00Z',
    created_at: '2026-07-01T08:00:00Z',
    updated_at: '2027-07-01T08:00:00Z',
  };

  await expect(
    createMarketWorkflowApi(client({ get: schedule })).getDailySchedule(),
  ).resolves.toMatchObject({ nextDueAt: '2026-07-06T10:30:00Z' });
});
