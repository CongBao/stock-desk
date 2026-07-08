import {
  ApiError,
  createApiClient,
  type ApiClient,
  type JsonValue,
} from '../../shared/api/client';

export type TaskStatus =
  'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
export type TaskStage =
  'queued' | 'executing' | 'completed' | 'failed' | 'cancelled';
export type TaskApiErrorKind =
  | 'abort'
  | 'network'
  | 'storage'
  | 'not_found'
  | 'conflict'
  | 'invalid'
  | 'protocol';

export type TaskPresentation = {
  readonly label: '股票池回测' | '智能分析' | '数据更新' | '后台任务';
  readonly stage: TaskStage | null;
  readonly processed: number | null;
  readonly total: number | null;
  readonly failed: number | null;
  readonly target: {
    readonly type: 'backtest_run';
    readonly id: string;
  } | null;
};

export type TaskView = {
  readonly id: string;
  readonly kind: string;
  readonly status: TaskStatus;
  readonly progress: number;
  readonly cancelRequested: boolean;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly startedAt: string | null;
  readonly finishedAt: string | null;
  readonly durationMs: number | null;
  readonly presentation: TaskPresentation;
};

export type TaskEventView = {
  readonly id: string;
  readonly taskId: string;
  readonly level: 'info' | 'warning' | 'error';
  readonly progress: number | null;
  readonly occurredAt: string;
  readonly presentation: Omit<TaskPresentation, 'target' | 'label'> & {
    readonly label:
      | '任务已创建'
      | '任务已开始'
      | '任务进度已更新'
      | '已处理回测标的'
      | '已请求取消'
      | '任务已取消'
      | '任务已完成'
      | '任务失败'
      | '任务事件';
  };
};

export type TaskMetrics = {
  readonly total: number;
  readonly byStatus: Readonly<Record<TaskStatus, number>>;
  readonly failureCount: number;
  readonly completedCount: number;
  readonly averageDurationMs: number | null;
  readonly minDurationMs: number | null;
  readonly maxDurationMs: number | null;
};

export type TaskRequestOptions = { readonly signal?: AbortSignal };

export type TaskApi = {
  readonly listTasks: (
    options?: TaskRequestOptions,
  ) => Promise<readonly TaskView[]>;
  readonly getMetrics: (options?: TaskRequestOptions) => Promise<TaskMetrics>;
  readonly getTask: (
    id: string,
    options?: TaskRequestOptions,
  ) => Promise<TaskView>;
  readonly listEvents: (
    id: string,
    options?: TaskRequestOptions,
  ) => Promise<readonly TaskEventView[]>;
  readonly cancelTask: (
    id: string,
    options?: TaskRequestOptions,
  ) => Promise<TaskView>;
};

const statuses = [
  'queued',
  'running',
  'succeeded',
  'failed',
  'cancelled',
] as const;
const statusSet = new Set<string>(statuses);
const stageSet = new Set<string>([
  'queued',
  'executing',
  'completed',
  'failed',
  'cancelled',
]);
const taskLabels = new Set<string>([
  '股票池回测',
  '智能分析',
  '数据更新',
  '后台任务',
]);
const eventLabels = new Set<string>([
  '任务已创建',
  '任务已开始',
  '任务进度已更新',
  '已处理回测标的',
  '已请求取消',
  '任务已取消',
  '任务已完成',
  '任务失败',
  '任务事件',
]);
const levels = new Set<string>(['info', 'warning', 'error']);
const canonicalUuid =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const isoTimestamp =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/u;

export class TaskApiError extends Error {
  constructor(readonly kind: TaskApiErrorKind) {
    super('任务服务请求失败');
    this.name = 'TaskApiError';
  }
}

function record(value: JsonValue | undefined): Record<string, JsonValue> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TaskApiError('protocol');
  }
  return value as Record<string, JsonValue>;
}

function exactKeys(
  value: Record<string, JsonValue>,
  expected: readonly string[],
) {
  const keys = Object.keys(value);
  if (
    keys.length !== expected.length ||
    expected.some((key) => !Object.hasOwn(value, key))
  ) {
    throw new TaskApiError('protocol');
  }
}

function timestamp(value: JsonValue | undefined): value is string {
  return (
    typeof value === 'string' &&
    isoTimestamp.test(value) &&
    Number.isFinite(Date.parse(value))
  );
}

function nullableTimestamp(
  value: JsonValue | undefined,
): value is string | null {
  return value === null || timestamp(value);
}

function boundedInteger(
  value: JsonValue | undefined,
  max = Number.MAX_SAFE_INTEGER,
): value is number {
  return (
    typeof value === 'number' &&
    Number.isInteger(value) &&
    value >= 0 &&
    value <= max
  );
}

function nullableDuration(
  value: JsonValue | undefined,
): value is number | null {
  return (
    value === null ||
    (typeof value === 'number' && Number.isFinite(value) && value >= 0)
  );
}

function decodeCounts(value: Record<string, JsonValue>) {
  const { stage, processed, total, failed } = value;
  const allNull =
    stage === null && processed === null && total === null && failed === null;
  if (allNull)
    return { stage: null, processed: null, total: null, failed: null } as const;
  if (
    typeof stage !== 'string' ||
    !stageSet.has(stage) ||
    !boundedInteger(processed, 10_000) ||
    !boundedInteger(total, 10_000) ||
    !boundedInteger(failed, 10_000) ||
    failed > processed ||
    processed > total
  ) {
    throw new TaskApiError('protocol');
  }
  return {
    stage: stage as TaskStage,
    processed,
    total,
    failed,
  };
}

function decodePresentation(value: JsonValue | undefined): TaskPresentation {
  const source = record(value);
  exactKeys(source, [
    'label',
    'stage',
    'processed',
    'total',
    'failed',
    'target',
  ]);
  if (typeof source.label !== 'string' || !taskLabels.has(source.label)) {
    throw new TaskApiError('protocol');
  }
  const counts = decodeCounts(source);
  let target: TaskPresentation['target'] = null;
  if (source.target !== null) {
    const rawTarget = record(source.target);
    exactKeys(rawTarget, ['type', 'id']);
    if (
      rawTarget.type !== 'backtest_run' ||
      typeof rawTarget.id !== 'string' ||
      !canonicalUuid.test(rawTarget.id)
    ) {
      throw new TaskApiError('protocol');
    }
    target = { type: 'backtest_run', id: rawTarget.id };
  }
  const presentation = {
    label: source.label as TaskPresentation['label'],
    ...counts,
    target,
  };
  if ((counts.stage === null) !== (target === null)) {
    throw new TaskApiError('protocol');
  }
  return presentation;
}

export function decodeTaskResponse(value: JsonValue): TaskView {
  const source = record(value);
  exactKeys(source, [
    'id',
    'kind',
    'status',
    'progress',
    'cancel_requested',
    'created_at',
    'updated_at',
    'started_at',
    'finished_at',
    'duration_ms',
    'presentation',
  ]);
  if (
    typeof source.id !== 'string' ||
    !canonicalUuid.test(source.id) ||
    typeof source.kind !== 'string' ||
    source.kind.length < 1 ||
    source.kind.length > 64 ||
    source.kind.trim() !== source.kind ||
    typeof source.status !== 'string' ||
    !statusSet.has(source.status) ||
    typeof source.progress !== 'number' ||
    !Number.isFinite(source.progress) ||
    source.progress < 0 ||
    source.progress > 1 ||
    typeof source.cancel_requested !== 'boolean' ||
    !timestamp(source.created_at) ||
    !timestamp(source.updated_at) ||
    !nullableTimestamp(source.started_at) ||
    !nullableTimestamp(source.finished_at) ||
    !nullableDuration(source.duration_ms)
  ) {
    throw new TaskApiError('protocol');
  }
  const presentation = decodePresentation(source.presentation);
  const status = source.status as TaskStatus;
  const created = Date.parse(source.created_at);
  const updated = Date.parse(source.updated_at);
  const started =
    source.started_at === null ? null : Date.parse(source.started_at);
  const finished =
    source.finished_at === null ? null : Date.parse(source.finished_at);
  const terminal =
    status === 'succeeded' || status === 'failed' || status === 'cancelled';
  const expectedLabel =
    source.kind === 'backtest.run'
      ? '股票池回测'
      : source.kind === 'analysis.run'
        ? '智能分析'
        : source.kind === 'market.update' ||
            source.kind === 'market.catalog.update'
          ? '数据更新'
          : '后台任务';
  const stageForStatus = {
    queued: 'queued',
    running: 'executing',
    succeeded: 'completed',
    failed: 'failed',
    cancelled: 'cancelled',
  } as const;
  if (
    created > updated ||
    (started !== null && (started < created || started > updated)) ||
    (finished !== null && (finished < created || finished > updated)) ||
    (status === 'queued' && (started !== null || finished !== null)) ||
    (status === 'running' && (started === null || finished !== null)) ||
    (terminal && finished === null) ||
    (started === null || finished === null) !== (source.duration_ms === null) ||
    ((status === 'succeeded' || status === 'failed') && started === null) ||
    (status === 'succeeded' && source.progress !== 1) ||
    (status === 'cancelled' && source.cancel_requested !== true) ||
    (source.duration_ms !== null &&
      started !== null &&
      finished !== null &&
      Math.abs(finished - started - source.duration_ms) > 2) ||
    presentation.label !== expectedLabel ||
    (presentation.stage !== null &&
      (source.kind !== 'backtest.run' ||
        (presentation.stage !== stageForStatus[status] &&
          !(status === 'running' && presentation.stage === 'queued'))))
  ) {
    throw new TaskApiError('protocol');
  }
  return {
    id: source.id,
    kind: source.kind,
    status,
    progress: source.progress,
    cancelRequested: source.cancel_requested,
    createdAt: source.created_at,
    updatedAt: source.updated_at,
    startedAt: source.started_at,
    finishedAt: source.finished_at,
    durationMs: source.duration_ms,
    presentation,
  };
}

export function decodeTaskListResponse(
  value: JsonValue | undefined,
  limit = 100,
): readonly TaskView[] {
  if (!Array.isArray(value) || value.length > limit)
    throw new TaskApiError('protocol');
  const decoded = value.map(decodeTaskResponse);
  if (new Set(decoded.map((item) => item.id)).size !== decoded.length) {
    throw new TaskApiError('protocol');
  }
  return decoded;
}

function decodeMetrics(value: JsonValue | undefined): TaskMetrics {
  const source = record(value);
  exactKeys(source, [
    'total',
    'by_status',
    'failure_count',
    'completed_count',
    'average_duration_ms',
    'min_duration_ms',
    'max_duration_ms',
  ]);
  const rawByStatus = record(source.by_status);
  if (
    !boundedInteger(source.total) ||
    !boundedInteger(source.failure_count) ||
    !boundedInteger(source.completed_count) ||
    !nullableDuration(source.average_duration_ms) ||
    !nullableDuration(source.min_duration_ms) ||
    !nullableDuration(source.max_duration_ms)
  ) {
    throw new TaskApiError('protocol');
  }
  const byStatus = Object.fromEntries(
    statuses.map((status) => {
      const count = rawByStatus[status];
      if (!boundedInteger(count)) throw new TaskApiError('protocol');
      return [status, count];
    }),
  ) as Record<TaskStatus, number>;
  if (
    Object.keys(rawByStatus).length !== statuses.length ||
    Object.values(byStatus).reduce((sum, count) => sum + count, 0) !==
      source.total ||
    source.failure_count !== byStatus.failed ||
    source.completed_count >
      byStatus.succeeded + byStatus.failed + byStatus.cancelled
  ) {
    throw new TaskApiError('protocol');
  }
  const durations = [
    source.min_duration_ms,
    source.average_duration_ms,
    source.max_duration_ms,
  ];
  if (
    (source.completed_count === 0) !==
      durations.every((item) => item === null) ||
    durations.some((item) => item === null) !==
      durations.every((item) => item === null) ||
    (durations[0] !== null &&
      durations[1] !== null &&
      durations[2] !== null &&
      (durations[0] > durations[1] || durations[1] > durations[2]))
  ) {
    throw new TaskApiError('protocol');
  }
  return {
    total: source.total,
    byStatus,
    failureCount: source.failure_count,
    completedCount: source.completed_count,
    averageDurationMs: source.average_duration_ms,
    minDurationMs: source.min_duration_ms,
    maxDurationMs: source.max_duration_ms,
  };
}

function decodeEvent(value: JsonValue, taskId: string): TaskEventView {
  const source = record(value);
  exactKeys(source, [
    'id',
    'task_id',
    'level',
    'progress',
    'occurred_at',
    'presentation',
  ]);
  const presentation = record(source.presentation);
  exactKeys(presentation, ['label', 'stage', 'processed', 'total', 'failed']);
  if (
    typeof source.id !== 'string' ||
    !canonicalUuid.test(source.id) ||
    source.task_id !== taskId ||
    typeof source.level !== 'string' ||
    !levels.has(source.level) ||
    (source.progress !== null &&
      (typeof source.progress !== 'number' ||
        source.progress < 0 ||
        source.progress > 1)) ||
    !timestamp(source.occurred_at) ||
    typeof presentation.label !== 'string' ||
    !eventLabels.has(presentation.label)
  ) {
    throw new TaskApiError('protocol');
  }
  const counts = decodeCounts(presentation);
  if ((presentation.label === '已处理回测标的') !== (counts.stage !== null)) {
    throw new TaskApiError('protocol');
  }
  return {
    id: source.id,
    taskId,
    level: source.level as TaskEventView['level'],
    progress: source.progress,
    occurredAt: source.occurred_at,
    presentation: {
      label: presentation.label as TaskEventView['presentation']['label'],
      ...counts,
    },
  };
}

function decodeEvents(
  value: JsonValue | undefined,
  taskId: string,
): readonly TaskEventView[] {
  if (!Array.isArray(value) || value.length > 100)
    throw new TaskApiError('protocol');
  const rawEvents = value as readonly JsonValue[];
  const events = rawEvents.map((item) => decodeEvent(item, taskId));
  if (new Set(events.map((event) => event.id)).size !== events.length) {
    throw new TaskApiError('protocol');
  }
  for (let index = 1; index < events.length; index += 1) {
    if (
      Date.parse(events[index].occurredAt) <
      Date.parse(events[index - 1].occurredAt)
    ) {
      throw new TaskApiError('protocol');
    }
  }
  return events;
}

function safeError(error: unknown): TaskApiError {
  if (error instanceof TaskApiError) return error;
  if (!(error instanceof ApiError)) return new TaskApiError('protocol');
  if (error.kind === 'abort') return new TaskApiError('abort');
  if (error.kind === 'network') return new TaskApiError('network');
  if (error.kind === 'protocol') return new TaskApiError('protocol');
  if (error.status === 503) return new TaskApiError('storage');
  if (error.status === 404) return new TaskApiError('not_found');
  if (error.status === 409) return new TaskApiError('conflict');
  if (error.status === 422) return new TaskApiError('invalid');
  return new TaskApiError('protocol');
}

async function safely<T>(operation: () => Promise<T>): Promise<T> {
  try {
    return await operation();
  } catch (error) {
    throw safeError(error);
  }
}

export function createTaskApi(client: ApiClient = createApiClient()): TaskApi {
  return {
    listTasks: (options = {}) =>
      safely(async () =>
        decodeTaskListResponse(
          await client.get('/tasks?view=safe&limit=100', {
            signal: options.signal,
          }),
        ),
      ),
    getMetrics: (options = {}) =>
      safely(async () =>
        decodeMetrics(await client.get('/tasks/metrics', options)),
      ),
    getTask: (id, options = {}) =>
      safely(async () =>
        decodeTaskResponse(
          (await client.get(`/tasks/${id}?view=safe`, options)) as JsonValue,
        ),
      ),
    listEvents: (id, options = {}) =>
      safely(async () =>
        decodeEvents(
          await client.get(`/tasks/${id}/events?view=safe&limit=100`, options),
          id,
        ),
      ),
    cancelTask: (id, options = {}) =>
      safely(async () =>
        decodeTaskResponse(
          (await client.post(
            `/tasks/${id}/cancel?view=safe`,
            options,
          )) as JsonValue,
        ),
      ),
  };
}

export const taskApi = createTaskApi();
