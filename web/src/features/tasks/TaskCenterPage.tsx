import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import {
  TaskApiError,
  taskApi as defaultTaskApi,
  type TaskApi,
  type TaskEventView,
  type TaskMetrics,
  type TaskStatus,
  type TaskView,
} from './taskApi';
import { mergeTaskSnapshots, updateTaskSnapshot } from './taskState';

type TaskCenterPageProps = {
  readonly api?: TaskApi;
  readonly pollIntervalMs?: number;
};

const statusLabels: Record<TaskStatus, string> = {
  queued: '排队中',
  running: '正在运行',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
};
const stageLabels = {
  queued: '排队',
  executing: '执行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
} as const;
const activeStatuses = new Set<TaskStatus>(['queued', 'running']);
const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  dateStyle: 'medium',
  timeStyle: 'medium',
});

function readableError(kind: TaskApiError['kind']): string {
  if (kind === 'storage') return '任务存储暂不可用，请稍后重试。';
  if (kind === 'protocol')
    return '任务服务返回了无法识别的数据，请刷新后重试。';
  if (kind === 'not_found') return '任务已不存在，请刷新列表。';
  return '任务服务暂时无法连接，请检查服务后重试。';
}

function formatDate(value: string): string {
  return dateFormatter.format(new Date(value));
}

function formatDuration(milliseconds: number): string {
  if (milliseconds < 1_000) return `${Math.round(milliseconds)} 毫秒`;
  const seconds = Math.floor(milliseconds / 1_000);
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} 分 ${seconds % 60} 秒`;
}

function runningDuration(task: TaskView, now: number): number | null {
  if (task.durationMs !== null) return task.durationMs;
  if (task.startedAt === null) return null;
  return Math.max(0, now - Date.parse(task.startedAt));
}

export function TaskCenterPage({
  api = defaultTaskApi,
  pollIntervalMs = 2_000,
}: TaskCenterPageProps) {
  const [tasks, setTasks] = useState<readonly TaskView[]>([]);
  const [metrics, setMetrics] = useState<TaskMetrics | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedTask, setSelectedTask] = useState<TaskView | null>(null);
  const [detailRefreshSequence, setDetailRefreshSequence] = useState(0);
  const [events, setEvents] = useState<readonly TaskEventView[]>([]);
  const [statusFilter, setStatusFilter] = useState<'all' | TaskStatus>('all');
  const [kindFilter, setKindFilter] = useState('all');
  const [isLoading, setIsLoading] = useState(true);
  const [hasLoadedTasks, setHasLoadedTasks] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [metricsError, setMetricsError] = useState<string | null>(null);
  const [eventsError, setEventsError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string>('正在载入任务');
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [cancelPending, setCancelPending] = useState(false);
  const [cancelUnknownId, setCancelUnknownId] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const tasksRef = useRef<readonly TaskView[]>(tasks);
  const selectedIdRef = useRef<string | null>(selectedId);
  const selectedTaskRef = useRef<TaskView | null>(selectedTask);
  const mountedRef = useRef(false);
  const pollTimerRef = useRef<number | null>(null);
  const refreshPromiseRef = useRef<Promise<void> | null>(null);
  const refreshControllersRef = useRef<Set<AbortController>>(new Set());
  const detailControllerRef = useRef<AbortController | null>(null);
  const cancelControllerRef = useRef<AbortController | null>(null);
  const refreshSequenceRef = useRef(0);
  const cancelUnknownRef = useRef<{
    readonly id: string;
    readonly afterRefreshSequence: number;
  } | null>(null);

  useEffect(() => {
    tasksRef.current = tasks;
  }, [tasks]);

  const selectTask = useCallback((task: TaskView | null) => {
    const taskId = task?.id ?? null;
    selectedIdRef.current = taskId;
    selectedTaskRef.current = task;
    setSelectedId(taskId);
    setSelectedTask(task);
  }, []);

  const commitTaskSnapshot = useCallback((replacement: TaskView) => {
    setTasks((current) => {
      if (!current.some((item) => item.id === replacement.id)) return current;
      const next = updateTaskSnapshot(current, replacement);
      tasksRef.current = next;
      return next;
    });
    if (selectedIdRef.current === replacement.id) {
      const current = selectedTaskRef.current;
      const next =
        current === null
          ? replacement
          : updateTaskSnapshot([current], replacement)[0];
      selectedTaskRef.current = next;
      setSelectedTask(next);
    }
  }, []);

  const clearUnknownCancellation = useCallback((taskId: string) => {
    if (cancelUnknownRef.current?.id !== taskId) return;
    cancelUnknownRef.current = null;
    setCancelUnknownId(null);
    setCancelError(null);
  }, []);

  const reconcileUnknownCancellation = useCallback(
    (snapshot: TaskView, refreshSequence?: number) => {
      const unknown = cancelUnknownRef.current;
      if (
        unknown === null ||
        unknown.id !== snapshot.id ||
        (refreshSequence !== undefined &&
          refreshSequence <= unknown.afterRefreshSequence)
      )
        return;
      cancelUnknownRef.current = null;
      setCancelUnknownId(null);
      setCancelError(
        snapshot.cancelRequested || !activeStatuses.has(snapshot.status)
          ? null
          : '取消请求未生效，可以重试。',
      );
    },
    [],
  );

  const refresh = useCallback((): Promise<void> => {
    if (refreshPromiseRef.current !== null) return refreshPromiseRef.current;
    const refreshSequence = ++refreshSequenceRef.current;
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    const controller = new AbortController();
    refreshControllersRef.current.add(controller);
    setIsRefreshing(true);
    const promise = (async () => {
      const [tasksResult, metricsResult] = await Promise.allSettled([
        api.listTasks({ signal: controller.signal }),
        api.getMetrics({ signal: controller.signal }),
      ]);
      if (!mountedRef.current || controller.signal.aborted) return;
      let nextTasks = tasksRef.current;
      if (tasksResult.status === 'fulfilled') {
        nextTasks = mergeTaskSnapshots(tasksRef.current, tasksResult.value);
        tasksRef.current = nextTasks;
        setTasks(nextTasks);
        setHasLoadedTasks(true);
        const unknown = cancelUnknownRef.current;
        const cancellationSnapshot =
          unknown === null
            ? undefined
            : nextTasks.find((item) => item.id === unknown.id);
        if (cancellationSnapshot !== undefined)
          reconcileUnknownCancellation(cancellationSnapshot, refreshSequence);
        setListError(null);
        const nextSelectedId =
          selectedIdRef.current ?? nextTasks[0]?.id ?? null;
        selectedIdRef.current = nextSelectedId;
        setSelectedId(nextSelectedId);
        const listedSelection = nextTasks.find(
          (item) => item.id === nextSelectedId,
        );
        if (listedSelection !== undefined) {
          const current = selectedTaskRef.current;
          const nextSelected =
            current === null || current.id !== listedSelection.id
              ? listedSelection
              : updateTaskSnapshot([current], listedSelection)[0];
          selectedTaskRef.current = nextSelected;
          setSelectedTask(nextSelected);
        }
        setDetailRefreshSequence(refreshSequence);
      } else if (!(
        tasksResult.reason instanceof TaskApiError &&
        tasksResult.reason.kind === 'abort'
      )) {
        const reason =
          tasksResult.reason instanceof TaskApiError
            ? tasksResult.reason.kind
            : 'protocol';
        setListError(`任务列表刷新失败。${readableError(reason)}`);
      }
      if (metricsResult.status === 'fulfilled') {
        setMetrics(metricsResult.value);
        setMetricsError(null);
      } else if (!(
        metricsResult.reason instanceof TaskApiError &&
        metricsResult.reason.kind === 'abort'
      )) {
        const reason =
          metricsResult.reason instanceof TaskApiError
            ? metricsResult.reason.kind
            : 'protocol';
        setMetricsError(`汇总指标刷新失败。${readableError(reason)}`);
      }
      if (tasksResult.status === 'fulfilled') {
        setNotice(
          nextTasks.some((item) => activeStatuses.has(item.status))
            ? '任务列表已更新，仍有任务正在运行'
            : '任务列表已更新',
        );
      }
    })().finally(() => {
      refreshControllersRef.current.delete(controller);
      if (mountedRef.current && !controller.signal.aborted) {
        setIsLoading(false);
        setIsRefreshing(false);
      }
      if (refreshPromiseRef.current === promise) {
        refreshPromiseRef.current = null;
      }
    });
    refreshPromiseRef.current = promise;
    return promise;
  }, [api, reconcileUnknownCancellation]);

  useEffect(() => {
    mountedRef.current = true;
    void refresh();
    return () => {
      mountedRef.current = false;
      if (pollTimerRef.current !== null)
        window.clearTimeout(pollTimerRef.current);
      for (const controller of refreshControllersRef.current)
        controller.abort();
      refreshPromiseRef.current = null;
      detailControllerRef.current?.abort();
      cancelControllerRef.current?.abort();
    };
  }, [refresh]);

  const hasActiveTasks =
    tasks.some((task) => activeStatuses.has(task.status)) ||
    (selectedTask !== null && activeStatuses.has(selectedTask.status));
  useEffect(() => {
    if (!hasActiveTasks || isLoading || isRefreshing) return undefined;
    pollTimerRef.current = window.setTimeout(() => {
      pollTimerRef.current = null;
      void refresh();
    }, pollIntervalMs);
    return () => {
      if (pollTimerRef.current !== null) {
        window.clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [hasActiveTasks, isLoading, isRefreshing, pollIntervalMs, refresh]);

  useEffect(() => {
    detailControllerRef.current?.abort();
    if (selectedId === null) {
      setEvents([]);
      setEventsError(null);
      return undefined;
    }
    const controller = new AbortController();
    detailControllerRef.current = controller;
    setEvents([]);
    setEventsError(null);
    void Promise.allSettled([
      api.getTask(selectedId, { signal: controller.signal }),
      api.listEvents(selectedId, { signal: controller.signal }),
    ]).then(([taskResult, eventResult]) => {
      if (!mountedRef.current || controller.signal.aborted) return;
      if (taskResult.status === 'fulfilled') {
        commitTaskSnapshot(taskResult.value);
        reconcileUnknownCancellation(taskResult.value, detailRefreshSequence);
      } else if (
        taskResult.reason instanceof TaskApiError &&
        taskResult.reason.kind === 'not_found' &&
        selectedIdRef.current === selectedId
      ) {
        clearUnknownCancellation(selectedId);
        selectTask(null);
        setEvents([]);
        setEventsError(null);
        setNotice('所选任务已不存在');
        return;
      }
      if (eventResult.status === 'fulfilled') {
        setEvents(eventResult.value);
        setEventsError(null);
      } else if (!(
        eventResult.reason instanceof TaskApiError &&
        eventResult.reason.kind === 'abort'
      )) {
        setEventsError('事件时间线暂不可用，任务元数据仍可查看。');
      }
    });
    return () => controller.abort();
  }, [
    api,
    clearUnknownCancellation,
    commitTaskSnapshot,
    detailRefreshSequence,
    reconcileUnknownCancellation,
    selectTask,
    selectedId,
    selectedTask?.status,
    selectedTask?.updatedAt,
  ]);

  useEffect(() => {
    if (selectedTask === null || !activeStatuses.has(selectedTask.status))
      return undefined;
    const timer = window.setTimeout(() => setNow(Date.now()), 1_000);
    return () => window.clearTimeout(timer);
  }, [now, selectedTask]);

  const kinds = useMemo(
    () => Array.from(new Set(tasks.map((task) => task.kind))).sort(),
    [tasks],
  );
  const filteredTasks = tasks.filter(
    (task) =>
      (statusFilter === 'all' || task.status === statusFilter) &&
      (kindFilter === 'all' || task.kind === kindFilter),
  );

  async function cancelSelected() {
    if (
      selectedTask === null ||
      !activeStatuses.has(selectedTask.status) ||
      selectedTask.cancelRequested ||
      cancelPending
    )
      return;
    setCancelPending(true);
    setCancelError(null);
    const taskId = selectedTask.id;
    const controller = new AbortController();
    cancelControllerRef.current?.abort();
    cancelControllerRef.current = controller;
    try {
      const cancelled = await api.cancelTask(taskId, {
        signal: controller.signal,
      });
      cancelUnknownRef.current = null;
      setCancelUnknownId(null);
      commitTaskSnapshot(cancelled);
      setNotice(cancelled.status === 'cancelled' ? '任务已取消' : '已请求取消');
    } catch (error) {
      if (error instanceof TaskApiError && error.kind === 'abort') return;
      if (error instanceof TaskApiError && error.kind === 'conflict') {
        setNotice('任务状态已变化，正在同步最新状态');
        try {
          const latest = await api.getTask(taskId, {
            signal: controller.signal,
          });
          commitTaskSnapshot(latest);
        } catch {
          setCancelError('任务状态已变化，请手动刷新。');
        }
      } else {
        cancelUnknownRef.current = {
          id: taskId,
          afterRefreshSequence: refreshSequenceRef.current,
        };
        setCancelUnknownId(taskId);
        try {
          const latest = await api.getTask(taskId, {
            signal: controller.signal,
          });
          commitTaskSnapshot(latest);
          reconcileUnknownCancellation(latest);
        } catch {
          setCancelError('取消结果未知。请先刷新任务状态，再决定是否重试。');
        }
      }
    } finally {
      if (!controller.signal.aborted) setCancelPending(false);
      if (cancelControllerRef.current === controller)
        cancelControllerRef.current = null;
    }
  }

  const duration =
    selectedTask === null ? null : runningDuration(selectedTask, now);
  const isPartial =
    listError !== null || metricsError !== null || eventsError !== null;

  return (
    <article className="task-center-page" aria-busy={isLoading || isRefreshing}>
      <header className="task-center-header">
        <div>
          <span className="page-kicker">v1.0.0 · Task Center</span>
          <h2 data-page-heading tabIndex={-1}>
            任务中心
          </h2>
          <p>查看最近 100 项任务的安全进度、生命周期与取消状态。</p>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={isRefreshing}
        >
          {isRefreshing ? '刷新中…' : '刷新任务'}
        </button>
      </header>

      <p
        className="visually-hidden"
        data-testid="task-live-status"
        aria-live="polite"
        aria-atomic="true"
      >
        {selectedTask === null
          ? notice
          : `${selectedTask.presentation.label}${statusLabels[selectedTask.status]}，进度 ${Math.round(selectedTask.progress * 100)}%${selectedTask.cancelRequested ? '，已请求取消' : ''}`}
      </p>

      {isPartial ? (
        <div className="task-degraded" role="alert">
          {[listError, metricsError, eventsError].filter(Boolean).join(' ')}
        </div>
      ) : null}
      {cancelError === null ? null : (
        <p role="alert" className="task-cancel-error">
          {cancelError}
        </p>
      )}
      <p className="task-action-status">{notice}</p>

      <section className="task-metrics" aria-label="全部任务汇总">
        <div>
          <span>全部任务</span>
          <strong>{metrics?.total ?? '—'}</strong>
        </div>
        <div>
          <span>排队 / 运行</span>
          <strong>
            {metrics === null
              ? '—'
              : metrics.byStatus.queued + metrics.byStatus.running}
          </strong>
        </div>
        <div>
          <span>成功</span>
          <strong>{metrics?.byStatus.succeeded ?? '—'}</strong>
        </div>
        <div>
          <span>失败</span>
          <strong>{metrics?.failureCount ?? '—'}</strong>
        </div>
      </section>

      <section className="task-filters" aria-label="最近任务筛选">
        <p>筛选范围：最近 100 项</p>
        <label>
          状态筛选
          <select
            value={statusFilter}
            onChange={(event) =>
              setStatusFilter(event.target.value as 'all' | TaskStatus)
            }
          >
            <option value="all">全部状态</option>
            {Object.entries(statusLabels).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          类型筛选
          <select
            value={kindFilter}
            onChange={(event) => setKindFilter(event.target.value)}
          >
            <option value="all">全部类型</option>
            {kinds.map((kind) => (
              <option key={kind} value={kind}>
                {kind}
              </option>
            ))}
          </select>
        </label>
      </section>

      {isLoading && tasks.length === 0 ? (
        <p className="task-center-empty" role="status">
          正在读取任务…
        </p>
      ) : !hasLoadedTasks && listError !== null ? (
        <div className="task-center-empty">
          <h3>任务列表暂不可用</h3>
          <p>尚未成功读取任务，请检查服务后重试。</p>
        </div>
      ) : tasks.length === 0 ? (
        <div className="task-center-empty">
          <h3>暂无任务</h3>
          <p>提交数据更新、回测或智能分析后，任务会显示在这里。</p>
        </div>
      ) : (
        <div className="task-center-layout">
          <section
            className="task-recent-panel"
            aria-labelledby="task-list-title"
          >
            <h3 id="task-list-title">最近任务</h3>
            {filteredTasks.length === 0 ? (
              <p>没有符合筛选条件的任务。</p>
            ) : (
              <ol className="task-center-list">
                {filteredTasks.map((task) => (
                  <li key={task.id}>
                    <button
                      type="button"
                      aria-current={task.id === selectedId ? 'true' : undefined}
                      aria-label={`${task.presentation.label} ${statusLabels[task.status]} ${task.id}`}
                      onClick={() => selectTask(task)}
                    >
                      <span>
                        <strong>{task.presentation.label}</strong>
                        <span data-status={task.status}>
                          {statusLabels[task.status]}
                        </span>
                      </span>
                      <span className="task-center-id">{task.id}</span>
                      <span>进度 {Math.round(task.progress * 100)}%</span>
                    </button>
                  </li>
                ))}
              </ol>
            )}
          </section>

          <section
            className="task-detail-panel"
            aria-labelledby="task-detail-title"
          >
            {selectedTask === null ? (
              <p>选择一个任务查看详情。</p>
            ) : (
              <>
                <header>
                  <div>
                    <span className="page-kicker">安全任务摘要</span>
                    <h3 id="task-detail-title">
                      {selectedTask.presentation.label}
                    </h3>
                  </div>
                  <span
                    className="task-status-badge"
                    data-status={selectedTask.status}
                  >
                    {statusLabels[selectedTask.status]}
                  </span>
                </header>
                <dl className="task-detail-metadata">
                  <div>
                    <dt>任务标识</dt>
                    <dd>{selectedTask.id}</dd>
                  </div>
                  <div>
                    <dt>创建时间</dt>
                    <dd>{formatDate(selectedTask.createdAt)}</dd>
                  </div>
                  <div>
                    <dt>最近更新</dt>
                    <dd>{formatDate(selectedTask.updatedAt)}</dd>
                  </div>
                  <div>
                    <dt>运行时长</dt>
                    <dd>
                      {duration === null
                        ? '尚未开始'
                        : formatDuration(duration)}
                    </dd>
                  </div>
                </dl>
                <div className="task-progress-block">
                  <div>
                    <span>总体进度</span>
                    <strong>{Math.round(selectedTask.progress * 100)}%</strong>
                  </div>
                  <progress
                    value={selectedTask.progress}
                    max={1}
                    aria-label="任务总体进度"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={Math.round(selectedTask.progress * 100)}
                  />
                </div>
                {selectedTask.presentation.stage === null ? null : (
                  <div
                    className="task-pool-progress"
                    aria-label="股票池回测进度"
                  >
                    <div>
                      <span>当前阶段</span>
                      <strong>
                        {stageLabels[selectedTask.presentation.stage]}
                      </strong>
                    </div>
                    <div>
                      <span>已处理 / 总数</span>
                      <strong>
                        {selectedTask.presentation.processed} /{' '}
                        {selectedTask.presentation.total}
                      </strong>
                    </div>
                    <div>
                      <span>失败</span>
                      <strong>失败 {selectedTask.presentation.failed}</strong>
                    </div>
                  </div>
                )}
                <div className="task-detail-actions">
                  {selectedTask.presentation.target?.type === 'backtest_run' ? (
                    <Link
                      to={`/backtests/${selectedTask.presentation.target.id}`}
                    >
                      打开回测报告
                    </Link>
                  ) : null}
                  {activeStatuses.has(selectedTask.status) ? (
                    <button
                      type="button"
                      onClick={() => void cancelSelected()}
                      disabled={
                        cancelPending ||
                        selectedTask.cancelRequested ||
                        cancelUnknownId === selectedTask.id
                      }
                    >
                      {selectedTask.cancelRequested
                        ? '已请求取消'
                        : cancelPending
                          ? '正在取消…'
                          : '取消任务'}
                    </button>
                  ) : null}
                </div>
                <section
                  className="task-timeline"
                  aria-labelledby="task-timeline-title"
                >
                  <h4 id="task-timeline-title">安全事件时间线</h4>
                  {events.length === 0 ? (
                    <p>暂无可显示事件。</p>
                  ) : (
                    <ol>
                      {events.map((event) => (
                        <li key={event.id} data-level={event.level}>
                          <div>
                            <strong>{event.presentation.label}</strong>
                            <time dateTime={event.occurredAt}>
                              {formatDate(event.occurredAt)}
                            </time>
                          </div>
                          {event.presentation.stage === null ? null : (
                            <p>
                              {stageLabels[event.presentation.stage]} ·{' '}
                              {event.presentation.processed} /{' '}
                              {event.presentation.total} · 失败{' '}
                              {event.presentation.failed}
                            </p>
                          )}
                        </li>
                      ))}
                    </ol>
                  )}
                </section>
              </>
            )}
          </section>
        </div>
      )}
    </article>
  );
}
