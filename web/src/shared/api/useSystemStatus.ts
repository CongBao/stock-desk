import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { ApiError, createApiClient, type JsonValue } from './client';

const apiClient = createApiClient();
const REQUEST_TIMEOUT_MS = 5_000;
const taskStatuses = new Set([
  'queued',
  'running',
  'succeeded',
  'failed',
  'cancelled',
]);

export type TaskStatus =
  'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export type RecentTask = {
  readonly id: string;
  readonly kind: string;
  readonly status: TaskStatus;
  readonly progress: number;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly finishedAt: string | null;
  readonly resultValue: boolean | number | string | null | undefined;
};

export type OverallSystemState =
  'checking' | 'healthy' | 'degraded' | 'unavailable';

export type EndpointState =
  'checking' | 'available' | 'protocol' | 'unavailable';

export type SystemStatus = {
  readonly overall: OverallSystemState;
  readonly health: EndpointState;
  readonly tasks: EndpointState;
  readonly recentTasks: readonly RecentTask[];
  readonly isRetryDisabled: boolean;
  readonly checkedAt: number | null;
  readonly retry: () => Promise<void>;
};

class ProtocolError extends Error {
  constructor() {
    super('API response did not match the public protocol');
    this.name = 'ProtocolError';
  }
}

class RequestTimeoutError extends Error {
  constructor() {
    super('API request timed out');
    this.name = 'RequestTimeoutError';
  }
}

async function getWithTimeout(
  path: string,
  querySignal: AbortSignal,
): Promise<JsonValue | undefined> {
  const controller = new AbortController();
  let timedOut = false;
  const cancelFromQuery = () => controller.abort();
  querySignal.addEventListener('abort', cancelFromQuery, { once: true });
  if (querySignal.aborted) {
    controller.abort();
  }
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, REQUEST_TIMEOUT_MS);

  try {
    return await apiClient.get(path, { signal: controller.signal });
  } catch (error) {
    if (timedOut && error instanceof ApiError && error.kind === 'abort') {
      throw new RequestTimeoutError();
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
    querySignal.removeEventListener('abort', cancelFromQuery);
  }
}

function isRecord(
  value: JsonValue | undefined,
): value is Record<string, JsonValue> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isTimestamp(value: JsonValue | undefined): value is string {
  return typeof value === 'string' && Number.isFinite(Date.parse(value));
}

function decodeHealth(value: JsonValue | undefined) {
  if (
    !isRecord(value) ||
    value.name !== 'stock-desk' ||
    value.status !== 'ok' ||
    value.api_version !== 'v1'
  ) {
    throw new ProtocolError();
  }

  return {
    apiVersion: value.api_version,
    name: value.name,
    status: value.status,
  } as const;
}

function decodeResultValue(
  value: JsonValue | undefined,
): boolean | number | string | null | undefined {
  if (value === null) {
    return undefined;
  }
  if (!isRecord(value)) {
    throw new ProtocolError();
  }
  const resultValue = value.value;
  if (
    resultValue === undefined ||
    resultValue === null ||
    typeof resultValue === 'boolean' ||
    typeof resultValue === 'string' ||
    (typeof resultValue === 'number' && Number.isFinite(resultValue))
  ) {
    return resultValue;
  }
  return undefined;
}

function decodeTask(value: JsonValue): RecentTask {
  if (
    !isRecord(value) ||
    typeof value.id !== 'string' ||
    value.id.length === 0 ||
    typeof value.kind !== 'string' ||
    value.kind.length === 0 ||
    typeof value.status !== 'string' ||
    !taskStatuses.has(value.status) ||
    typeof value.progress !== 'number' ||
    !Number.isFinite(value.progress) ||
    value.progress < 0 ||
    value.progress > 1 ||
    !isTimestamp(value.created_at) ||
    !isTimestamp(value.updated_at) ||
    (value.finished_at !== null && !isTimestamp(value.finished_at))
  ) {
    throw new ProtocolError();
  }

  return {
    id: value.id,
    kind: value.kind,
    status: value.status as TaskStatus,
    progress: value.progress,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
    finishedAt: value.finished_at,
    resultValue: decodeResultValue(value.result),
  };
}

function decodeTasks(value: JsonValue | undefined): readonly RecentTask[] {
  if (!Array.isArray(value) || value.length > 5) {
    throw new ProtocolError();
  }
  return value.map(decodeTask);
}

function endpointState(query: {
  readonly isError: boolean;
  readonly isPending: boolean;
  readonly error: unknown;
}): EndpointState {
  if (query.isPending) {
    return 'checking';
  }
  if (!query.isError) {
    return 'available';
  }
  if (
    query.error instanceof ProtocolError ||
    (query.error instanceof ApiError && query.error.kind === 'protocol')
  ) {
    return 'protocol';
  }
  return 'unavailable';
}

function overallState(
  health: EndpointState,
  tasks: EndpointState,
): OverallSystemState {
  if (health === 'checking' && tasks === 'checking') {
    return 'checking';
  }
  if (health === 'checking' || tasks === 'checking') {
    const knownState = health === 'checking' ? tasks : health;
    return knownState === 'available' || knownState === 'checking'
      ? 'checking'
      : 'degraded';
  }
  if (health === 'available' && tasks === 'available') {
    return 'healthy';
  }
  if (health === 'unavailable' && tasks === 'unavailable') {
    return 'unavailable';
  }
  return 'degraded';
}

function shouldRetry(failureCount: number, error: unknown): boolean {
  return (
    failureCount < 1 && !(error instanceof ApiError && error.kind === 'abort')
  );
}

export function useSystemStatus(): SystemStatus {
  const [isManualRetrying, setIsManualRetrying] = useState(false);
  const healthQuery = useQuery({
    queryKey: ['system-status', 'health'],
    queryFn: async ({ signal }) =>
      decodeHealth(await getWithTimeout('/health', signal)),
    retry: shouldRetry,
    retryDelay: 10,
    staleTime: 10_000,
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  });
  const tasksQuery = useQuery({
    queryKey: ['system-status', 'tasks', 5],
    queryFn: async ({ signal }) =>
      decodeTasks(await getWithTimeout('/tasks?limit=5', signal)),
    retry: shouldRetry,
    retryDelay: 10,
    staleTime: 1_000,
    refetchInterval: 5_000,
    refetchIntervalInBackground: false,
  });
  const health = endpointState(healthQuery);
  const tasks = endpointState(tasksQuery);
  const isInitialPending = healthQuery.isPending || tasksQuery.isPending;
  const checkedAt = Math.max(
    healthQuery.dataUpdatedAt,
    healthQuery.errorUpdatedAt,
    tasksQuery.dataUpdatedAt,
    tasksQuery.errorUpdatedAt,
  );

  return {
    overall: overallState(health, tasks),
    health,
    tasks,
    recentTasks: tasksQuery.data ?? [],
    isRetryDisabled: isInitialPending || isManualRetrying,
    checkedAt: checkedAt > 0 ? checkedAt : null,
    retry: async () => {
      if (isInitialPending || isManualRetrying) {
        return;
      }
      setIsManualRetrying(true);
      try {
        await Promise.all([healthQuery.refetch(), tasksQuery.refetch()]);
      } finally {
        setIsManualRetrying(false);
      }
    },
  };
}
