import {
  ApiError,
  type ApiClient,
  type JsonValue,
} from '../../shared/api/client';
import { createTaskApi, TaskApiError } from './taskApi';

const TASK_ID = '11111111-1111-4111-8111-111111111111';
const RUN_ID = '22222222-2222-4222-8222-222222222222';

const taskResponse = {
  id: TASK_ID,
  kind: 'backtest.run',
  status: 'running',
  progress: 0.4,
  cancel_requested: false,
  created_at: '2026-07-08T00:00:00Z',
  updated_at: '2026-07-08T00:00:02Z',
  started_at: '2026-07-08T00:00:01Z',
  finished_at: null,
  duration_ms: null,
  presentation: {
    label: '股票池回测',
    stage: 'executing',
    processed: 2,
    total: 5,
    failed: 1,
    target: { type: 'backtest_run', id: RUN_ID },
  },
} as const;

function stubClient(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    get: vi.fn(() => Promise.resolve(taskResponse as unknown as JsonValue)),
    post: vi.fn(() => Promise.resolve(taskResponse as unknown as JsonValue)),
    put: vi.fn(() => Promise.resolve(taskResponse as unknown as JsonValue)),
    ...overrides,
  };
}

it('strictly decodes the allowlisted safe task response', async () => {
  const api = createTaskApi(stubClient());

  const task = await api.getTask(TASK_ID);

  expect(task).toEqual({
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
  });
  expect(JSON.stringify(task)).not.toMatch(/SENTINEL|worker-secret/u);
});

it('rejects raw task JSON crossing the safe browser boundary', async () => {
  const api = createTaskApi(
    stubClient({
      get: vi.fn(() =>
        Promise.resolve({
          ...taskResponse,
          payload: { secret: 'PAYLOAD-SENTINEL' },
        } as unknown as JsonValue),
      ),
    }),
  );

  await expect(api.getTask(TASK_ID)).rejects.toMatchObject({
    kind: 'protocol',
  });
});

it.each([
  ['progress above one', { ...taskResponse, progress: 1.01 }],
  [
    'non-canonical target id',
    {
      ...taskResponse,
      presentation: {
        ...taskResponse.presentation,
        target: { type: 'backtest_run', id: 'not-a-run-id' },
      },
    },
  ],
  [
    'processed above total',
    {
      ...taskResponse,
      presentation: { ...taskResponse.presentation, processed: 6 },
    },
  ],
  [
    'failed above processed',
    {
      ...taskResponse,
      presentation: { ...taskResponse.presentation, failed: 3 },
    },
  ],
  [
    'unknown stage',
    {
      ...taskResponse,
      presentation: { ...taskResponse.presentation, stage: 'secret-stage' },
    },
  ],
  [
    'terminal task without finish time',
    { ...taskResponse, status: 'succeeded', progress: 1 },
  ],
  [
    'background task with backtest presentation',
    { ...taskResponse, kind: 'demo.task' },
  ],
  [
    'timestamps out of order',
    {
      ...taskResponse,
      created_at: '2026-07-08T00:00:03Z',
      updated_at: '2026-07-08T00:00:02Z',
    },
  ],
])('rejects invalid task protocol: %s', async (_name, response) => {
  const api = createTaskApi(
    stubClient({
      get: vi.fn(() => Promise.resolve(response as unknown as JsonValue)),
    }),
  );

  await expect(api.getTask(TASK_ID)).rejects.toMatchObject({
    kind: 'protocol',
  });
});

it('bounds recent tasks, metrics and chronological event history', async () => {
  const event = {
    id: '33333333-3333-4333-8333-333333333333',
    task_id: TASK_ID,
    level: 'info',
    progress: 0.4,
    occurred_at: '2026-07-08T00:00:02Z',
    presentation: {
      label: '已处理回测标的',
      stage: 'executing',
      processed: 2,
      total: 5,
      failed: 1,
    },
  };
  const client = stubClient({
    get: vi
      .fn()
      .mockResolvedValueOnce([taskResponse])
      .mockResolvedValueOnce({
        total: 10,
        by_status: {
          queued: 1,
          running: 2,
          succeeded: 3,
          failed: 2,
          cancelled: 2,
        },
        failure_count: 2,
        completed_count: 7,
        average_duration_ms: 120,
        min_duration_ms: 10,
        max_duration_ms: 500,
      })
      .mockResolvedValueOnce([event]),
  });
  const api = createTaskApi(client);

  expect(await api.listTasks()).toHaveLength(1);
  expect(await api.getMetrics()).toMatchObject({
    total: 10,
    completedCount: 7,
  });
  const events = await api.listEvents(TASK_ID);
  expect(events).toHaveLength(1);
  expect(JSON.stringify(events)).not.toContain('EVENT-SENTINEL');
  expect(client.get).toHaveBeenNthCalledWith(1, '/tasks?view=safe&limit=100', {
    signal: undefined,
  });
  expect(client.get).toHaveBeenNthCalledWith(
    3,
    `/tasks/${TASK_ID}/events?view=safe&limit=100`,
    { signal: undefined },
  );
});

it('accepts queued cancellations outside the duration sample count', async () => {
  const client = stubClient({
    get: vi.fn(() =>
      Promise.resolve({
        total: 1,
        by_status: {
          queued: 0,
          running: 0,
          succeeded: 0,
          failed: 0,
          cancelled: 1,
        },
        failure_count: 0,
        completed_count: 0,
        average_duration_ms: null,
        min_duration_ms: null,
        max_duration_ms: null,
      }),
    ),
  });

  await expect(createTaskApi(client).getMetrics()).resolves.toMatchObject({
    completedCount: 0,
    byStatus: { cancelled: 1 },
  });
});

it('forwards AbortSignal to every request', async () => {
  const client = stubClient();
  const api = createTaskApi(client);
  const controller = new AbortController();

  await api.getTask(TASK_ID, { signal: controller.signal });
  await api.cancelTask(TASK_ID, { signal: controller.signal });

  expect(client.get).toHaveBeenCalledWith(`/tasks/${TASK_ID}?view=safe`, {
    signal: controller.signal,
  });
  expect(client.post).toHaveBeenCalledWith(
    `/tasks/${TASK_ID}/cancel?view=safe`,
    {
      signal: controller.signal,
    },
  );
});

it.each([
  ['abort', new ApiError('unsafe', { kind: 'abort' }), 'abort'],
  ['network', new ApiError('unsafe', { kind: 'network' }), 'network'],
  ['storage', new ApiError('unsafe', { kind: 'http', status: 503 }), 'storage'],
  [
    'not_found',
    new ApiError('unsafe', { kind: 'http', status: 404 }),
    'not_found',
  ],
  [
    'conflict',
    new ApiError('unsafe', { kind: 'http', status: 409 }),
    'conflict',
  ],
  ['invalid', new ApiError('unsafe', { kind: 'http', status: 422 }), 'invalid'],
  ['protocol', new ApiError('unsafe', { kind: 'protocol' }), 'protocol'],
])('maps %s failures to safe task errors', async (_name, error, expected) => {
  const api = createTaskApi(
    stubClient({ get: vi.fn(async () => Promise.reject(error)) }),
  );

  const rejection = await api
    .getTask(TASK_ID)
    .catch((caught: unknown) => caught);
  expect(rejection).toBeInstanceOf(TaskApiError);
  expect(rejection).toMatchObject({ kind: expected });
  expect(String(rejection)).not.toContain('unsafe');
});
