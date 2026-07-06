import {
  createApiClient,
  type ApiClient,
  type JsonValue,
} from '../../shared/api/client';
import type { MarketAdjustment, MarketPeriod } from './marketStore';
import { decodePoolDetail, type MarketPoolDetail } from './marketApi';

export class MarketWorkflowProtocolError extends Error {
  constructor(path: string) {
    super(`行情工作流 API 响应不符合协议：${path}`);
    this.name = 'MarketWorkflowProtocolError';
  }
}

export type MarketUpdatePayload = {
  readonly symbols: readonly string[];
  readonly period: MarketPeriod;
  readonly adjustment: MarketAdjustment;
  readonly start: string;
  readonly end: string;
};

export type MarketTask = {
  readonly id: string;
  readonly kind: 'market.catalog.update' | 'market.update';
  readonly status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  readonly progress: number;
  readonly payload: MarketUpdatePayload | Record<string, never>;
  readonly result: Readonly<Record<string, JsonValue>> | null;
  readonly error: Readonly<Record<string, JsonValue>> | null;
  readonly cancelRequested: boolean;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly startedAt: string | null;
  readonly finishedAt: string | null;
};

export type MarketTaskEvent = {
  readonly id: string;
  readonly taskId: string;
  readonly eventName: string;
  readonly progress: number | null;
  readonly detail: Readonly<Record<string, JsonValue>>;
  readonly occurredAt: string;
};

export type MarketUpdateItem = {
  readonly taskId: string;
  readonly ordinal: number;
  readonly symbol: string;
  readonly status: 'succeeded' | 'failed' | 'cancelled';
  readonly manifestRecordId: string | null;
  readonly datasetVersion: string | null;
  readonly reason: string | null;
  readonly createdAt: string;
};

export type DailyMarketSchedule = {
  readonly id: string;
  readonly enabled: boolean;
  readonly timezone: 'Asia/Shanghai';
  readonly localTime: string;
  readonly payload: MarketUpdatePayload;
  readonly symbolsFrozen: true;
  readonly lastEnqueuedLocalDate: string | null;
  readonly nextDueAt: string | null;
  readonly createdAt: string;
  readonly updatedAt: string;
};

type SignalOptions = { readonly signal?: AbortSignal };

export type MarketWorkflowApi = {
  createPool(
    value: { readonly name: string; readonly symbols: readonly string[] },
    options?: SignalOptions,
  ): Promise<MarketPoolDetail>;
  updatePool(
    poolId: string,
    value: {
      readonly expectedRevision: number;
      readonly name: string;
      readonly symbols: readonly string[];
    },
    options?: SignalOptions,
  ): Promise<MarketPoolDetail>;
  deletePool(
    poolId: string,
    expectedRevision: number,
    options?: SignalOptions,
  ): Promise<void>;
  createCatalogUpdate(options?: SignalOptions): Promise<MarketTask>;
  createUpdate(
    payload: MarketUpdatePayload,
    options?: SignalOptions,
  ): Promise<MarketTask>;
  getTask(taskId: string, options?: SignalOptions): Promise<MarketTask>;
  getTaskEvents(
    taskId: string,
    options?: SignalOptions,
  ): Promise<readonly MarketTaskEvent[]>;
  cancelTask(taskId: string, options?: SignalOptions): Promise<MarketTask>;
  getUpdateItems(
    taskId: string,
    options?: SignalOptions,
  ): Promise<readonly MarketUpdateItem[]>;
  getDailySchedule(options?: SignalOptions): Promise<DailyMarketSchedule>;
  saveDailySchedule(
    value: {
      readonly enabled: boolean;
      readonly localTime: string;
      readonly payload: MarketUpdatePayload;
    },
    options?: SignalOptions,
  ): Promise<DailyMarketSchedule>;
};

function fail(path: string): never {
  throw new MarketWorkflowProtocolError(path);
}

function exactRecord(
  value: JsonValue | undefined,
  path: string,
  keys: readonly string[],
): Record<string, JsonValue> {
  if (
    value === null ||
    value === undefined ||
    Array.isArray(value) ||
    typeof value !== 'object'
  )
    return fail(path);
  const record = value as Record<string, JsonValue>;
  const actual = Object.keys(record).sort();
  const expected = [...keys].sort();
  if (
    actual.length !== expected.length ||
    actual.some((key, index) => key !== expected[index])
  )
    return fail(`${path}.keys`);
  return record;
}

function text(value: JsonValue | undefined, path: string, max = 512): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > max)
    return fail(path);
  return value;
}

function timestamp(value: JsonValue | undefined, path: string): string {
  const resolved = text(value, path, 40);
  if (!/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/u.test(resolved))
    return fail(path);
  if (!Number.isFinite(Date.parse(resolved))) return fail(path);
  return resolved;
}

function nullableTimestamp(
  value: JsonValue | undefined,
  path: string,
): string | null {
  return value === null ? null : timestamp(value, path);
}

function boolean(value: JsonValue | undefined, path: string): boolean {
  if (typeof value !== 'boolean') return fail(path);
  return value;
}

function number(value: JsonValue | undefined, path: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return fail(path);
  return value;
}

function integer(
  value: JsonValue | undefined,
  path: string,
  minimum = 0,
  maximum = 10_000,
): number {
  const resolved = number(value, path);
  if (
    !Number.isSafeInteger(resolved) ||
    resolved < minimum ||
    resolved > maximum
  )
    return fail(path);
  return resolved;
}

const periods = new Set<MarketPeriod>(['1d', '1w', '60m']);
const adjustments = new Set<MarketAdjustment>(['none', 'qfq', 'hfq']);
const symbolPattern = /^\d{6}\.(?:SH|SZ|BJ)$/u;
const uuidPattern = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/u;
const digestPattern = /^sha256:[0-9a-f]{64}$/u;

function uuid(value: JsonValue | undefined, path: string): string {
  const resolved = text(value, path, 36);
  return uuidPattern.test(resolved) ? resolved : fail(path);
}

function nullableText(
  value: JsonValue | undefined,
  path: string,
): string | null {
  return value === null ? null : text(value, path);
}

function digest(value: JsonValue | undefined, path: string): string {
  const resolved = text(value, path, 71);
  return digestPattern.test(resolved) ? resolved : fail(path);
}

const providerIds = new Set([
  'tushare',
  'akshare',
  'baostock',
  'tdx_local',
  'eastmoney',
]);
const poolCategories = new Set(['all_a', 'index', 'industry']);
const presetKeyPattern = /^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$/u;

function presetOutcome(
  value: JsonValue,
  path: string,
  failed: boolean,
): Readonly<Record<string, JsonValue>> {
  const item = exactRecord(
    value,
    path,
    failed ? ['preset_key', 'category', 'reason'] : ['preset_key', 'category'],
  );
  const presetKey = text(item.preset_key, `${path}.preset_key`, 64);
  const category = text(item.category, `${path}.category`, 16);
  if (!presetKeyPattern.test(presetKey) || !poolCategories.has(category))
    return fail(path);
  if (failed) text(item.reason, `${path}.reason`, 64);
  return item;
}

function decodeTaskResult(
  value: JsonValue | undefined,
  kind: MarketTask['kind'],
): Readonly<Record<string, JsonValue>> | null {
  if (value === null) return null;
  if (kind === 'market.update') {
    const item = exactRecord(value, 'task.result', [
      'total',
      'succeeded',
      'failed',
      'cancelled',
      'configuration_fingerprint',
    ]);
    const total = integer(item.total, 'task.result.total', 1);
    const succeeded = integer(item.succeeded, 'task.result.succeeded');
    const failed = integer(item.failed, 'task.result.failed');
    const cancelled = integer(item.cancelled, 'task.result.cancelled');
    if (succeeded + failed + cancelled !== total)
      return fail('task.result.counts');
    digest(
      item.configuration_fingerprint,
      'task.result.configuration_fingerprint',
    );
    return item;
  }
  const item = exactRecord(value, 'task.result', [
    'source',
    'row_count',
    'manifest_record_id',
    'full_a_pool_id',
    'preset_successes',
    'preset_failures',
    'configuration_fingerprint',
  ]);
  const source = text(item.source, 'task.result.source', 32);
  if (!providerIds.has(source)) return fail('task.result.source');
  integer(item.row_count, 'task.result.row_count', 1, 100_000);
  digest(item.manifest_record_id, 'task.result.manifest_record_id');
  if (item.full_a_pool_id !== null && item.full_a_pool_id !== 'preset:all-a')
    return fail('task.result.full_a_pool_id');
  if (
    !Array.isArray(item.preset_successes) ||
    !Array.isArray(item.preset_failures) ||
    item.preset_successes.length > 1_000 ||
    item.preset_failures.length > 1_000
  )
    return fail('task.result.presets');
  const successes = item.preset_successes as JsonValue[];
  const failures = item.preset_failures as JsonValue[];
  successes.forEach((entry, index) =>
    presetOutcome(
      entry,
      `task.result.preset_successes[${String(index)}]`,
      false,
    ),
  );
  failures.forEach((entry, index) =>
    presetOutcome(entry, `task.result.preset_failures[${String(index)}]`, true),
  );
  digest(
    item.configuration_fingerprint,
    'task.result.configuration_fingerprint',
  );
  return item;
}

function decodeTaskError(
  value: JsonValue | undefined,
): Readonly<Record<string, JsonValue>> | null {
  if (value === null) return null;
  const item = exactRecord(value, 'task.error', ['code']);
  const code = text(item.code, 'task.error.code', 64);
  if (!['task_handler_failed', 'unknown_task_kind'].includes(code))
    return fail('task.error.code');
  return item;
}

function decodePayload(
  value: JsonValue | undefined,
  path: string,
): MarketUpdatePayload {
  const item = exactRecord(value, path, [
    'symbols',
    'period',
    'adjustment',
    'start',
    'end',
  ]);
  if (
    !Array.isArray(item.symbols) ||
    item.symbols.length < 1 ||
    item.symbols.length > 10_000
  )
    return fail(`${path}.symbols`);
  const rawSymbols = item.symbols as readonly JsonValue[];
  const symbols = rawSymbols.map((value, index) => {
    const symbol = text(value, `${path}.symbols[${String(index)}]`, 9);
    return symbolPattern.test(symbol) ? symbol : fail(`${path}.symbols`);
  });
  if (new Set(symbols).size !== symbols.length) return fail(`${path}.symbols`);
  const period = text(item.period, `${path}.period`) as MarketPeriod;
  const adjustment = text(
    item.adjustment,
    `${path}.adjustment`,
  ) as MarketAdjustment;
  if (!periods.has(period) || !adjustments.has(adjustment)) return fail(path);
  const start = timestamp(item.start, `${path}.start`);
  const end = timestamp(item.end, `${path}.end`);
  if (Date.parse(start) >= Date.parse(end)) return fail(`${path}.range`);
  return { symbols, period, adjustment, start, end };
}

const taskKeys = [
  'id',
  'correlation_id',
  'kind',
  'status',
  'progress',
  'payload',
  'result',
  'error',
  'cancel_requested',
  'worker_id',
  'created_at',
  'updated_at',
  'started_at',
  'finished_at',
  'duration_ms',
] as const;

function decodeTask(value: JsonValue | undefined): MarketTask {
  const item = exactRecord(value, 'task', taskKeys);
  const id = uuid(item.id, 'task.id');
  if (item.correlation_id !== id) return fail('task.correlation_id');
  const kind = text(item.kind, 'task.kind') as MarketTask['kind'];
  if (kind !== 'market.update' && kind !== 'market.catalog.update')
    return fail('task.kind');
  const status = text(item.status, 'task.status') as MarketTask['status'];
  if (
    !new Set(['queued', 'running', 'succeeded', 'failed', 'cancelled']).has(
      status,
    )
  )
    return fail('task.status');
  const progress = number(item.progress, 'task.progress');
  if (progress < 0 || progress > 1) return fail('task.progress');
  const payload =
    kind === 'market.update'
      ? decodePayload(item.payload, 'task.payload')
      : (exactRecord(item.payload, 'task.payload', []) as Record<
          string,
          never
        >);
  const result = decodeTaskResult(item.result, kind);
  const error = decodeTaskError(item.error);
  const workerId = nullableText(item.worker_id, 'task.worker_id');
  const durationMs =
    item.duration_ms === null
      ? null
      : number(item.duration_ms, 'task.duration_ms');
  if (durationMs !== null && durationMs < 0) return fail('task.duration_ms');
  const createdAt = timestamp(item.created_at, 'task.created_at');
  const updatedAt = timestamp(item.updated_at, 'task.updated_at');
  const startedAt = nullableTimestamp(item.started_at, 'task.started_at');
  const finishedAt = nullableTimestamp(item.finished_at, 'task.finished_at');
  const cancelRequested = boolean(
    item.cancel_requested,
    'task.cancel_requested',
  );
  const terminal = ['succeeded', 'failed', 'cancelled'].includes(status);
  if (
    (terminal && finishedAt === null) ||
    (!terminal && finishedAt !== null) ||
    (status === 'succeeded' &&
      (progress !== 1 || result === null || error !== null)) ||
    (status === 'failed' && error === null) ||
    (status === 'running' && (startedAt === null || workerId === null)) ||
    (status === 'queued' &&
      (startedAt !== null || workerId !== null || progress !== 0)) ||
    (status !== 'failed' && error !== null) ||
    (status !== 'succeeded' && result !== null) ||
    Date.parse(createdAt) > Date.parse(updatedAt) ||
    (startedAt !== null && Date.parse(createdAt) > Date.parse(startedAt)) ||
    (startedAt !== null && Date.parse(startedAt) > Date.parse(updatedAt)) ||
    (finishedAt !== null && Date.parse(updatedAt) > Date.parse(finishedAt)) ||
    (startedAt === null && durationMs !== null) ||
    (startedAt !== null && finishedAt !== null && durationMs === null) ||
    (['queued', 'succeeded', 'failed'].includes(status) && cancelRequested) ||
    (status === 'cancelled' && !cancelRequested)
  )
    return fail('task.state');
  return {
    id,
    kind,
    status,
    progress,
    payload,
    result,
    error,
    cancelRequested,
    createdAt,
    updatedAt,
    startedAt,
    finishedAt,
  };
}

function decodeItem(value: JsonValue, index: number): MarketUpdateItem {
  const path = `items[${String(index)}]`;
  const item = exactRecord(value, path, [
    'task_id',
    'ordinal',
    'symbol',
    'status',
    'manifest_record_id',
    'dataset_version',
    'reason',
    'created_at',
  ]);
  const ordinal = number(item.ordinal, `${path}.ordinal`);
  if (!Number.isSafeInteger(ordinal) || ordinal < 0)
    return fail(`${path}.ordinal`);
  const symbol = text(item.symbol, `${path}.symbol`, 9);
  if (!symbolPattern.test(symbol)) return fail(`${path}.symbol`);
  const status = text(
    item.status,
    `${path}.status`,
  ) as MarketUpdateItem['status'];
  if (!['succeeded', 'failed', 'cancelled'].includes(status))
    return fail(`${path}.status`);
  const nullable = (value: JsonValue, nestedPath: string) =>
    value === null ? null : text(value, nestedPath);
  const manifestRecordId = nullable(
    item.manifest_record_id,
    `${path}.manifest_record_id`,
  );
  const datasetVersion = nullable(
    item.dataset_version,
    `${path}.dataset_version`,
  );
  const reason = nullable(item.reason, `${path}.reason`);
  if (
    (status === 'succeeded' &&
      (manifestRecordId === null ||
        datasetVersion === null ||
        reason !== null)) ||
    (status !== 'succeeded' &&
      (manifestRecordId !== null || datasetVersion !== null || reason === null))
  )
    return fail(`${path}.state`);
  if (manifestRecordId !== null)
    digest(manifestRecordId, `${path}.manifest_record_id`);
  if (datasetVersion !== null)
    digest(datasetVersion, `${path}.dataset_version`);
  if (
    (status === 'failed' && !reason?.startsWith('routing:')) ||
    (status === 'cancelled' && reason !== 'cancel_requested')
  )
    return fail(`${path}.reason`);
  return {
    taskId: uuid(item.task_id, `${path}.task_id`),
    ordinal,
    symbol,
    status,
    manifestRecordId,
    datasetVersion,
    reason,
    createdAt: timestamp(item.created_at, `${path}.created_at`),
  };
}

function decodeSchedule(value: JsonValue | undefined): DailyMarketSchedule {
  const item = exactRecord(value, 'schedule', [
    'id',
    'enabled',
    'timezone',
    'local_time',
    'payload',
    'symbols_frozen',
    'last_enqueued_local_date',
    'next_due_at',
    'created_at',
    'updated_at',
  ]);
  if (item.timezone !== 'Asia/Shanghai' || item.symbols_frozen !== true)
    return fail('schedule.contract');
  const localTime = text(item.local_time, 'schedule.local_time', 5);
  if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/u.test(localTime))
    return fail('schedule.local_time');
  const last = item.last_enqueued_local_date;
  if (
    last !== null &&
    (typeof last !== 'string' || !/^\d{4}-\d{2}-\d{2}$/u.test(last))
  )
    return fail('schedule.last_enqueued_local_date');
  const enabled = boolean(item.enabled, 'schedule.enabled');
  const nextDueAt = nullableTimestamp(item.next_due_at, 'schedule.next_due_at');
  const createdAt = timestamp(item.created_at, 'schedule.created_at');
  const updatedAt = timestamp(item.updated_at, 'schedule.updated_at');
  if (
    enabled === (nextDueAt === null) ||
    Date.parse(createdAt) > Date.parse(updatedAt)
  )
    return fail('schedule.state');
  return {
    id: uuid(item.id, 'schedule.id'),
    enabled,
    timezone: 'Asia/Shanghai',
    localTime,
    payload: decodePayload(item.payload, 'schedule.payload'),
    symbolsFrozen: true,
    lastEnqueuedLocalDate: last,
    nextDueAt,
    createdAt,
    updatedAt,
  };
}

function decodeTaskEvent(value: JsonValue, index: number): MarketTaskEvent {
  const path = `events[${String(index)}]`;
  const item = exactRecord(value, path, [
    'id',
    'task_id',
    'correlation_id',
    'event_name',
    'level',
    'progress',
    'detail',
    'occurred_at',
  ]);
  const taskId = uuid(item.task_id, `${path}.task_id`);
  if (item.correlation_id !== taskId) return fail(`${path}.correlation_id`);
  const progress =
    item.progress === null ? null : number(item.progress, `${path}.progress`);
  if (progress !== null && (progress < 0 || progress > 1))
    return fail(`${path}.progress`);
  const eventName = text(item.event_name, `${path}.event_name`, 64);
  const level = text(item.level, `${path}.level`);
  let detail: Readonly<Record<string, JsonValue>>;
  if (eventName === 'task.created') {
    detail = exactRecord(item.detail, `${path}.detail`, ['kind']);
    const kind = text(detail['kind'], `${path}.detail.kind`, 64);
    if (kind !== 'market.update' && kind !== 'market.catalog.update')
      return fail(`${path}.detail.kind`);
  } else if (eventName === 'task.claimed') {
    detail = exactRecord(item.detail, `${path}.detail`, ['worker_id']);
    text(detail['worker_id'], `${path}.detail.worker_id`, 255);
  } else if (eventName === 'task.progressed') {
    detail = exactRecord(item.detail, `${path}.detail`, [
      'stage',
      'processed',
      'total',
      'current_symbol',
      'succeeded',
      'failed',
      'cancelled',
    ]);
    const stage = text(detail['stage'], `${path}.detail.stage`, 16);
    if (!['routing', 'persisting', 'finalizing'].includes(stage))
      return fail(`${path}.detail.stage`);
    const processed = integer(detail['processed'], `${path}.detail.processed`);
    const total = integer(detail['total'], `${path}.detail.total`, 1);
    const succeeded = integer(detail['succeeded'], `${path}.detail.succeeded`);
    const failed = integer(detail['failed'], `${path}.detail.failed`);
    const cancelled = integer(detail['cancelled'], `${path}.detail.cancelled`);
    const currentSymbol = detail['current_symbol'];
    if (
      processed > total ||
      succeeded + failed + cancelled !== processed ||
      (stage === 'finalizing' && currentSymbol !== null) ||
      (stage !== 'finalizing' &&
        (typeof currentSymbol !== 'string' ||
          !symbolPattern.test(currentSymbol)))
    )
      return fail(`${path}.detail.state`);
  } else if (eventName === 'task.failed') {
    detail = exactRecord(item.detail, `${path}.detail`, ['code']);
    if (detail['code'] !== 'task_failed') return fail(`${path}.detail.code`);
  } else if (
    ['task.cancel_requested', 'task.cancelled', 'task.succeeded'].includes(
      eventName,
    )
  ) {
    detail = exactRecord(item.detail, `${path}.detail`, []);
  } else {
    return fail(`${path}.event_name`);
  }
  if (
    (eventName === 'task.failed' && level !== 'error') ||
    (eventName !== 'task.failed' && level !== 'info') ||
    progress === null
  )
    return fail(`${path}.state`);
  return {
    id: uuid(item.id, `${path}.id`),
    taskId,
    eventName,
    progress,
    detail,
    occurredAt: timestamp(item.occurred_at, `${path}.occurred_at`),
  };
}

export function createMarketWorkflowApi(
  client: ApiClient = createApiClient(),
): MarketWorkflowApi {
  return {
    async createPool(value, { signal } = {}) {
      return decodePoolDetail(
        await client.post('/market/pools', {
          signal,
          body: { name: value.name, symbols: value.symbols },
        }),
      );
    },
    async updatePool(poolId, value, { signal } = {}) {
      return decodePoolDetail(
        await client.put(`/market/pools/${encodeURIComponent(poolId)}`, {
          signal,
          body: {
            expected_revision: value.expectedRevision,
            name: value.name,
            symbols: value.symbols,
          },
        }),
      );
    },
    async deletePool(poolId, expectedRevision, { signal } = {}) {
      if (client.delete === undefined) return fail('client.delete');
      const response = await client.delete(
        `/market/pools/${encodeURIComponent(poolId)}?expected_revision=${String(expectedRevision)}`,
        { signal },
      );
      if (response !== undefined) return fail('pool.delete');
    },
    async createCatalogUpdate({ signal } = {}) {
      return decodeTask(
        await client.post('/market/catalog/updates', { signal }),
      );
    },
    async createUpdate(payload, { signal } = {}) {
      return decodeTask(
        await client.post('/market/updates', { body: payload, signal }),
      );
    },
    async getTask(taskId, { signal } = {}) {
      return decodeTask(
        await client.get(`/tasks/${encodeURIComponent(taskId)}`, { signal }),
      );
    },
    async getTaskEvents(taskId, { signal } = {}) {
      const value = await client.get(
        `/tasks/${encodeURIComponent(taskId)}/events?limit=100`,
        { signal },
      );
      if (!Array.isArray(value) || value.length > 100) return fail('events');
      const events = value.map(decodeTaskEvent);
      if (
        events.some((event) => event.taskId !== taskId) ||
        events.some(
          (event, index) =>
            index > 0 &&
            Date.parse(events[index - 1]?.occurredAt ?? '') <
              Date.parse(event.occurredAt),
        )
      )
        return fail('events.task_id');
      return events;
    },
    async cancelTask(taskId, { signal } = {}) {
      return decodeTask(
        await client.post(`/tasks/${encodeURIComponent(taskId)}/cancel`, {
          signal,
        }),
      );
    },
    async getUpdateItems(taskId, { signal } = {}) {
      const value = await client.get(
        `/market/updates/${encodeURIComponent(taskId)}/items`,
        { signal },
      );
      if (!Array.isArray(value) || value.length > 10_000) return fail('items');
      const items = value.map(decodeItem);
      if (
        items.some((item) => item.taskId !== taskId) ||
        items.some((item, index) => item.ordinal !== index)
      )
        return fail('items.identity');
      return items;
    },
    async getDailySchedule({ signal } = {}) {
      return decodeSchedule(
        await client.get('/market/schedules/daily', { signal }),
      );
    },
    async saveDailySchedule(value, { signal } = {}) {
      return decodeSchedule(
        await client.put('/market/schedules/daily', {
          signal,
          body: {
            enabled: value.enabled,
            local_time: value.localTime,
            payload: value.payload,
          },
        }),
      );
    },
  };
}

export const marketWorkflowApi = createMarketWorkflowApi();
