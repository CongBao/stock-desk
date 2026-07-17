import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { ApiError, createApiClient, type JsonValue } from './client';
import {
  decodeTaskListResponse,
  TaskApiError,
  type TaskStatus,
  type TaskView,
} from '../../features/tasks/taskApi';

const apiClient = createApiClient();
const REQUEST_TIMEOUT_MS = 5_000;
export type RecentTask = TaskView;
export type { TaskStatus };

export type OverallSystemState =
  'checking' | 'healthy' | 'degraded' | 'unavailable';

export type EndpointState =
  'checking' | 'available' | 'protocol' | 'unavailable';

export type WorkerState =
  'checking' | 'running' | 'not_detected' | 'unavailable' | 'api_offline';

export type SystemStatus = {
  readonly overall: OverallSystemState;
  readonly health: EndpointState;
  readonly tasks: EndpointState;
  readonly worker: WorkerState;
  readonly workerLastSeenAt: string | null;
  readonly recentTasks: readonly RecentTask[];
  readonly isRetrying: boolean;
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

function decodeTasks(value: JsonValue | undefined): readonly RecentTask[] {
  try {
    return decodeTaskListResponse(value, 5);
  } catch (error) {
    if (!(error instanceof TaskApiError)) throw error;
    throw new ProtocolError();
  }
}

function decodeWorkerStatus(value: JsonValue | undefined) {
  if (!isRecord(value)) {
    throw new ProtocolError();
  }
  const keys = Object.keys(value).sort();
  const lastSeenAt = value.last_seen_at;
  if (
    keys.length !== 2 ||
    keys[0] !== 'last_seen_at' ||
    keys[1] !== 'state' ||
    (value.state !== 'running' && value.state !== 'not_detected') ||
    (value.state === 'running' && lastSeenAt === null) ||
    (lastSeenAt !== null &&
      (typeof lastSeenAt !== 'string' ||
        !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/u.test(
          lastSeenAt,
        ) ||
        !Number.isFinite(Date.parse(lastSeenAt))))
  ) {
    throw new ProtocolError();
  }
  return {
    state: value.state,
    lastSeenAt,
  } as const;
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

function workerState(
  health: EndpointState,
  query: {
    readonly data: { readonly state: 'running' | 'not_detected' } | undefined;
    readonly error: unknown;
    readonly isError: boolean;
    readonly isPending: boolean;
  },
): WorkerState {
  if (
    health === 'unavailable' ||
    (query.isError &&
      (query.error instanceof RequestTimeoutError ||
        (query.error instanceof ApiError && query.error.kind === 'network')))
  ) {
    return 'api_offline';
  }
  const endpoint = endpointState(query);
  if (endpoint === 'checking') {
    return 'checking';
  }
  if (endpoint !== 'available' || query.data === undefined) {
    return 'unavailable';
  }
  return query.data.state;
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
      decodeTasks(await getWithTimeout('/tasks?view=safe&limit=5', signal)),
    retry: shouldRetry,
    retryDelay: 10,
    staleTime: 1_000,
    refetchInterval: 5_000,
    refetchIntervalInBackground: false,
  });
  const workerQuery = useQuery({
    queryKey: ['system-status', 'worker'],
    queryFn: async ({ signal }) =>
      decodeWorkerStatus(await getWithTimeout('/tasks/worker-status', signal)),
    retry: shouldRetry,
    retryDelay: 10,
    staleTime: 1_000,
    refetchInterval: 5_000,
    refetchIntervalInBackground: false,
  });
  const health = endpointState(healthQuery);
  const tasks = endpointState(tasksQuery);
  const isInitialPending =
    healthQuery.isPending || tasksQuery.isPending || workerQuery.isPending;
  const checkedAt = Math.max(
    healthQuery.dataUpdatedAt,
    healthQuery.errorUpdatedAt,
    tasksQuery.dataUpdatedAt,
    tasksQuery.errorUpdatedAt,
    workerQuery.dataUpdatedAt,
    workerQuery.errorUpdatedAt,
  );

  return {
    overall: overallState(health, tasks),
    health,
    tasks,
    worker: workerState(health, workerQuery),
    workerLastSeenAt: workerQuery.data?.lastSeenAt ?? null,
    recentTasks: tasksQuery.data ?? [],
    isRetrying: isManualRetrying,
    isRetryDisabled: isInitialPending || isManualRetrying,
    checkedAt: checkedAt > 0 ? checkedAt : null,
    retry: async () => {
      if (isInitialPending || isManualRetrying) {
        return;
      }
      setIsManualRetrying(true);
      try {
        await Promise.all([
          healthQuery.refetch(),
          tasksQuery.refetch(),
          workerQuery.refetch(),
        ]);
      } finally {
        setIsManualRetrying(false);
      }
    },
  };
}
