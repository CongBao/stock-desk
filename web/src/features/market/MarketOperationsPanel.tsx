import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { ApiError, type JsonValue } from '../../shared/api/client';
import { marketApi, type MarketApi } from './marketApi';
import type { MarketInstrumentSelection } from './marketStore';
import {
  marketWorkflowApi,
  type MarketTask,
  type MarketUpdatePayload,
  type MarketWorkflowApi,
} from './marketWorkflowApi';
import type { MarketAdjustment, MarketPeriod } from './marketStore';

type PoolScope = {
  readonly id: string;
  readonly name: string;
  readonly symbols: readonly string[];
  readonly kind?: 'preset' | 'custom';
  readonly revision?: number | null;
};

type MarketOperationsPanelProps = {
  readonly api?: MarketWorkflowApi;
  readonly selectedInstrument: MarketInstrumentSelection | null;
  readonly selectedPool: PoolScope | null;
  readonly period: MarketPeriod;
  readonly adjustment: MarketAdjustment;
  readonly marketApiClient?: MarketApi;
  readonly onPoolDeleted?: () => void;
};

const terminal = new Set<MarketTask['status']>([
  'succeeded',
  'failed',
  'cancelled',
]);
const statusLabels: Record<MarketTask['status'], string> = {
  queued: '排队中',
  running: '更新中',
  succeeded: '已完成',
  failed: '更新失败',
  cancelled: '已取消',
};

function dateInput(daysAgo: number): string {
  const date = new Date();
  date.setUTCDate(date.getUTCDate() - daysAgo);
  return date.toISOString().slice(0, 10);
}

function utcBoundary(value: string): string {
  return new Date(`${value}T00:00:00+08:00`).toISOString();
}

function progressValue(value: unknown, fallback: string): string {
  return typeof value === 'string' || typeof value === 'number'
    ? String(value)
    : fallback;
}

function poolIssueText(error: unknown): string | null {
  if (
    !(error instanceof ApiError) ||
    typeof error.details !== 'object' ||
    error.details === null ||
    Array.isArray(error.details)
  )
    return null;
  const details = error.details as Readonly<Record<string, JsonValue>>;
  const issues = details['issues'];
  if (!Array.isArray(issues)) return null;
  return issues
    .slice(0, 20)
    .map((issue) => {
      if (typeof issue !== 'object' || issue === null || Array.isArray(issue))
        return '#? invalid';
      const issueRecord = issue as Readonly<Record<string, JsonValue>>;
      const ordinal = issueRecord['ordinal'];
      const code = issueRecord['code'];
      return `#${typeof ordinal === 'number' ? String(ordinal + 1) : '?'} ${typeof code === 'string' ? code : 'invalid'}`;
    })
    .join('；');
}

export function MarketOperationsPanel({
  api = marketWorkflowApi,
  selectedInstrument,
  selectedPool,
  period,
  adjustment,
  marketApiClient = marketApi,
  onPoolDeleted,
}: MarketOperationsPanelProps) {
  const queryClient = useQueryClient();
  const controllers = useRef(new Set<AbortController>());
  const createNameRef = useRef<HTMLInputElement>(null);
  const editNameRef = useRef<HTMLInputElement>(null);
  const createDialogRef = useRef<HTMLDialogElement>(null);
  const editDialogRef = useRef<HTMLDialogElement>(null);
  const [poolDialogOpen, setPoolDialogOpen] = useState(false);
  const [poolName, setPoolName] = useState('');
  const [poolSymbols, setPoolSymbols] = useState<string[]>([]);
  const [poolSearch, setPoolSearch] = useState('');
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [editName, setEditName] = useState('');
  const [editSymbols, setEditSymbols] = useState<string[]>([]);
  const [deleteConfirmation, setDeleteConfirmation] = useState(false);
  const [scope, setScope] = useState<'instrument' | 'pool'>('instrument');
  const [start, setStart] = useState(() => dateInput(365));
  const [end, setEnd] = useState(() => dateInput(0));
  const [activeTask, setActiveTask] = useState<MarketTask | null>(null);
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [scheduleTime, setScheduleTime] = useState('18:00');

  useEffect(
    () => () => {
      for (const controller of controllers.current) controller.abort();
      controllers.current.clear();
    },
    [],
  );
  useEffect(() => {
    const dialog = createDialogRef.current;
    if (!poolDialogOpen || dialog === null) return;
    if (!dialog.open) {
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
    }
    createNameRef.current?.focus();
  }, [poolDialogOpen]);
  useEffect(() => {
    const dialog = editDialogRef.current;
    if (!editDialogOpen || dialog === null) return;
    if (!dialog.open) {
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
    }
    editNameRef.current?.focus();
  }, [editDialogOpen]);

  async function withSignal<T>(
    operation: (signal: AbortSignal) => Promise<T>,
  ): Promise<T> {
    const controller = new AbortController();
    controllers.current.add(controller);
    try {
      return await operation(controller.signal);
    } finally {
      controllers.current.delete(controller);
    }
  }

  const scopeSymbols = useMemo(
    () =>
      scope === 'pool'
        ? (selectedPool?.symbols ?? [])
        : selectedInstrument === null
          ? []
          : [selectedInstrument.symbol],
    [scope, selectedInstrument, selectedPool],
  );
  const poolSearchResults = useQuery({
    queryKey: ['market', 'pool-member-search', poolSearch],
    enabled: (poolDialogOpen || editDialogOpen) && poolSearch.trim().length > 0,
    queryFn: ({ signal }) =>
      marketApiClient.searchInstruments({
        query: poolSearch.trim(),
        limit: 20,
        signal,
      }),
  });

  function payload(): MarketUpdatePayload {
    return {
      symbols: scopeSymbols,
      period,
      adjustment,
      start: utcBoundary(start),
      end: utcBoundary(end),
    };
  }

  const createPool = useMutation({
    mutationFn: () =>
      withSignal((signal) =>
        api.createPool({ name: poolName, symbols: poolSymbols }, { signal }),
      ),
    onSuccess: async () => {
      setPoolDialogOpen(false);
      setPoolName('');
      setPoolSymbols([]);
      await queryClient.invalidateQueries({ queryKey: ['market', 'pools'] });
    },
  });
  const updatePool = useMutation({
    mutationFn: () => {
      if (
        selectedPool?.kind !== 'custom' ||
        selectedPool.revision === null ||
        selectedPool.revision === undefined
      )
        throw new Error('Custom pool revision is missing');
      return withSignal((signal) =>
        api.updatePool(
          selectedPool.id,
          {
            expectedRevision: selectedPool.revision as number,
            name: editName,
            symbols: editSymbols,
          },
          { signal },
        ),
      );
    },
    onSuccess: async () => {
      setEditDialogOpen(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['market', 'pools'] }),
        queryClient.invalidateQueries({
          queryKey: ['market', 'pool', selectedPool?.id],
        }),
      ]);
    },
  });
  const deletePool = useMutation({
    mutationFn: () => {
      if (
        selectedPool?.kind !== 'custom' ||
        selectedPool.revision === null ||
        selectedPool.revision === undefined
      )
        throw new Error('Custom pool revision is missing');
      return withSignal((signal) =>
        api.deletePool(selectedPool.id, selectedPool.revision as number, {
          signal,
        }),
      );
    },
    onSuccess: async () => {
      setEditDialogOpen(false);
      onPoolDeleted?.();
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['market', 'pools'] }),
        queryClient.invalidateQueries({
          queryKey: ['market', 'pool', selectedPool?.id],
        }),
      ]);
    },
  });
  const createUpdate = useMutation({
    mutationFn: () =>
      withSignal((signal) => api.createUpdate(payload(), { signal })),
    onSuccess: setActiveTask,
  });
  const createCatalog = useMutation({
    mutationFn: () =>
      withSignal((signal) => api.createCatalogUpdate({ signal })),
    onSuccess: setActiveTask,
  });
  const task = useQuery({
    queryKey: ['market', 'update-task', activeTask?.id ?? null],
    enabled: activeTask !== null && !terminal.has(activeTask.status),
    queryFn: ({ signal }) => {
      if (activeTask === null) throw new Error('Task is missing');
      return api.getTask(activeTask.id, { signal });
    },
    refetchInterval: (query) => {
      const current = query.state.data;
      return current !== undefined && terminal.has(current.status)
        ? false
        : 500;
    },
  });
  const visibleTask = activeTask;
  useEffect(() => {
    if (
      task.data !== undefined &&
      (activeTask === null ||
        (task.data.id === activeTask.id &&
          Date.parse(task.data.updatedAt) >= Date.parse(activeTask.updatedAt)))
    )
      setActiveTask(task.data);
  }, [task.data]);
  const events = useQuery({
    queryKey: ['market', 'update-events', visibleTask?.id ?? null],
    enabled: visibleTask !== null,
    queryFn: ({ signal }) => {
      if (visibleTask === null) throw new Error('Task is missing');
      return api.getTaskEvents(visibleTask.id, { signal });
    },
    refetchInterval: () =>
      visibleTask !== null && !terminal.has(visibleTask.status) ? 500 : false,
  });
  const items = useQuery({
    queryKey: ['market', 'update-items', visibleTask?.id ?? null],
    enabled: visibleTask?.kind === 'market.update',
    queryFn: ({ signal }) => {
      if (visibleTask === null) throw new Error('Task is missing');
      return api.getUpdateItems(visibleTask.id, { signal });
    },
    refetchInterval: () =>
      visibleTask !== null && !terminal.has(visibleTask.status) ? 500 : false,
  });
  const cancel = useMutation({
    mutationFn: () => {
      if (visibleTask === null) throw new Error('Task is missing');
      return withSignal((signal) => api.cancelTask(visibleTask.id, { signal }));
    },
    onSuccess: (next) => {
      if (
        activeTask === null ||
        (next.id === activeTask.id &&
          Date.parse(next.updatedAt) >= Date.parse(activeTask.updatedAt))
      )
        setActiveTask(next);
    },
    onError: () => void task.refetch(),
  });
  const savedSchedule = useQuery({
    queryKey: ['market', 'daily-schedule'],
    queryFn: ({ signal }) => api.getDailySchedule({ signal }),
    retry: false,
  });
  const scheduleHydrated = useRef(false);
  const completedTaskEffects = useRef(new Set<string>());
  useEffect(() => {
    if (savedSchedule.data !== undefined && !scheduleHydrated.current) {
      scheduleHydrated.current = true;
      setScheduleEnabled(savedSchedule.data.enabled);
      setScheduleTime(savedSchedule.data.localTime);
    }
  }, [savedSchedule.data]);
  const schedule = useMutation({
    mutationFn: () =>
      withSignal((signal) =>
        api.saveDailySchedule(
          {
            enabled: scheduleEnabled,
            localTime: scheduleTime,
            payload: payload(),
          },
          { signal },
        ),
      ),
    onSuccess: (value) =>
      queryClient.setQueryData(['market', 'daily-schedule'], value),
  });

  const validDate = (value: string) =>
    /^\d{4}-\d{2}-\d{2}$/u.test(value) && Number.isFinite(Date.parse(value));
  const rangeInvalid =
    scopeSymbols.length === 0 ||
    !validDate(start) ||
    !validDate(end) ||
    Date.parse(start) >= Date.parse(end);
  const activeNonterminal =
    visibleTask !== null && !terminal.has(visibleTask.status);
  const backtestHref =
    selectedInstrument === null || rangeInvalid
      ? null
      : `/backtests?${new URLSearchParams({
          symbol: selectedInstrument.symbol,
          period,
          adjustment,
          start,
          end,
        }).toString()}`;
  const latestProgress = events.data?.find(
    (event) => event.eventName === 'task.progressed',
  );

  useEffect(() => {
    if (
      visibleTask?.status !== 'succeeded' ||
      completedTaskEffects.current.has(visibleTask.id)
    )
      return;
    completedTaskEffects.current.add(visibleTask.id);
    if (visibleTask.kind === 'market.catalog.update') {
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ['market', 'pools'] }),
        queryClient.invalidateQueries({
          queryKey: ['market', 'instrument-search'],
        }),
        queryClient.invalidateQueries({
          queryKey: ['market', 'pool-member-search'],
        }),
      ]);
      return;
    }
    void queryClient.invalidateQueries({ queryKey: ['market', 'bars'] });
  }, [queryClient, visibleTask]);

  return (
    <section
      className="market-operations"
      aria-labelledby="market-operations-title"
    >
      <span className="panel-kicker">UPDATE</span>
      <h3 id="market-operations-title">数据更新</h3>
      <p>更新只在明确启动后访问已配置数据源；图表始终只读本地缓存。</p>

      <div className="market-operation-actions">
        <button type="button" onClick={() => setPoolDialogOpen(true)}>
          新建自定义池
        </button>
        {selectedPool?.kind === 'custom' ? (
          <button
            type="button"
            onClick={() => {
              setEditName(selectedPool.name);
              setEditSymbols([...selectedPool.symbols]);
              setPoolSearch('');
              setDeleteConfirmation(false);
              setEditDialogOpen(true);
            }}
          >
            编辑当前股票池
          </button>
        ) : null}
        <button
          type="button"
          disabled={createCatalog.isPending || activeNonterminal}
          onClick={() => createCatalog.mutate()}
        >
          更新证券目录
        </button>
      </div>

      {poolDialogOpen ? (
        <dialog
          ref={createDialogRef}
          aria-labelledby="create-pool-title"
          onCancel={(event) => {
            event.preventDefault();
            setPoolDialogOpen(false);
          }}
        >
          <h4 id="create-pool-title">新建自定义池</h4>
          <label>
            股票池名称
            <input
              aria-label="股票池名称"
              ref={createNameRef}
              value={poolName}
              maxLength={64}
              onChange={(event) => setPoolName(event.currentTarget.value)}
            />
          </label>
          {selectedInstrument === null ? (
            <p>先搜索并选择证券，再加入股票池。</p>
          ) : (
            <button
              type="button"
              disabled={poolSymbols.includes(selectedInstrument.symbol)}
              onClick={() =>
                setPoolSymbols((symbols) => [
                  ...symbols,
                  selectedInstrument.symbol,
                ])
              }
            >
              加入{selectedInstrument.name} {selectedInstrument.symbol}
            </button>
          )}
          <label>
            搜索更多证券
            <input
              aria-label="搜索更多证券"
              value={poolSearch}
              onChange={(event) => setPoolSearch(event.currentTarget.value)}
            />
          </label>
          {poolSearchResults.data !== undefined ? (
            <ul aria-label="可加入证券">
              {poolSearchResults.data.slice(0, 20).map((instrument) => (
                <li key={instrument.symbol}>
                  <button
                    type="button"
                    disabled={poolSymbols.includes(instrument.symbol)}
                    onClick={() =>
                      setPoolSymbols((symbols) => [
                        ...symbols,
                        instrument.symbol,
                      ])
                    }
                  >
                    加入 {instrument.name} {instrument.symbol}
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          <ol aria-label="新股票池成员">
            {poolSymbols.map((symbol, index) => (
              <li key={symbol}>
                <span>{symbol}</span>
                <button
                  type="button"
                  aria-label={`移除 ${symbol}`}
                  onClick={() =>
                    setPoolSymbols((symbols) =>
                      symbols.filter((_item, itemIndex) => itemIndex !== index),
                    )
                  }
                >
                  移除
                </button>
              </li>
            ))}
          </ol>
          {createPool.isError ? (
            <p role="alert">
              股票池创建失败，请检查成员。
              {poolIssueText(createPool.error) ?? ''}
            </p>
          ) : null}
          <button
            type="button"
            disabled={poolName.trim().length === 0 || poolSymbols.length === 0}
            onClick={() => createPool.mutate()}
          >
            创建股票池
          </button>
          <button type="button" onClick={() => setPoolDialogOpen(false)}>
            取消
          </button>
        </dialog>
      ) : null}

      {editDialogOpen && selectedPool?.kind === 'custom' ? (
        <dialog
          ref={editDialogRef}
          aria-labelledby="edit-pool-title"
          onCancel={(event) => {
            event.preventDefault();
            setEditDialogOpen(false);
          }}
        >
          <h4 id="edit-pool-title">编辑自定义池</h4>
          <label>
            股票池名称
            <input
              ref={editNameRef}
              value={editName}
              onChange={(event) => setEditName(event.currentTarget.value)}
            />
          </label>
          <ol aria-label="编辑股票池成员">
            {editSymbols.map((symbol, index) => (
              <li key={symbol}>
                {symbol}
                <button
                  type="button"
                  aria-label={`上移 ${symbol}`}
                  disabled={index === 0}
                  onClick={() =>
                    setEditSymbols((symbols) => {
                      const next = [...symbols];
                      [next[index - 1], next[index]] = [
                        next[index],
                        next[index - 1],
                      ];
                      return next;
                    })
                  }
                >
                  上移
                </button>
                <button
                  type="button"
                  aria-label={`下移 ${symbol}`}
                  disabled={index === editSymbols.length - 1}
                  onClick={() =>
                    setEditSymbols((symbols) => {
                      const next = [...symbols];
                      [next[index], next[index + 1]] = [
                        next[index + 1],
                        next[index],
                      ];
                      return next;
                    })
                  }
                >
                  下移
                </button>
                <button
                  type="button"
                  aria-label={`移除 ${symbol}`}
                  onClick={() =>
                    setEditSymbols((symbols) =>
                      symbols.filter((_item, itemIndex) => itemIndex !== index),
                    )
                  }
                >
                  移除
                </button>
              </li>
            ))}
          </ol>
          <label>
            搜索并加入证券
            <input
              aria-label="编辑池搜索证券"
              value={poolSearch}
              onChange={(event) => setPoolSearch(event.currentTarget.value)}
            />
          </label>
          {poolSearchResults.data !== undefined ? (
            <ul aria-label="编辑池可加入证券">
              {poolSearchResults.data.slice(0, 20).map((instrument) => (
                <li key={instrument.symbol}>
                  <button
                    type="button"
                    disabled={editSymbols.includes(instrument.symbol)}
                    onClick={() =>
                      setEditSymbols((symbols) => [
                        ...symbols,
                        instrument.symbol,
                      ])
                    }
                  >
                    加入 {instrument.name} {instrument.symbol}
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          {updatePool.isError ? (
            <p role="alert">
              股票池保存失败，请检查成员。
              {poolIssueText(updatePool.error) ?? ''}
            </p>
          ) : null}
          <button
            type="button"
            disabled={editName.trim().length === 0 || editSymbols.length === 0}
            onClick={() => updatePool.mutate()}
          >
            保存股票池
          </button>
          {deleteConfirmation ? (
            <div role="alert" className="pool-delete-confirmation">
              <p>删除后无法撤销，确认删除“{selectedPool.name}”？</p>
              <button
                type="button"
                disabled={deletePool.isPending}
                onClick={() => deletePool.mutate()}
              >
                确认删除
              </button>
              <button
                type="button"
                disabled={deletePool.isPending}
                onClick={() => setDeleteConfirmation(false)}
              >
                保留股票池
              </button>
            </div>
          ) : (
            <button type="button" onClick={() => setDeleteConfirmation(true)}>
              删除股票池
            </button>
          )}
          <button type="button" onClick={() => setEditDialogOpen(false)}>
            取消
          </button>
        </dialog>
      ) : null}

      <fieldset>
        <legend>更新范围</legend>
        <label>
          <input
            type="radio"
            name="update-scope"
            checked={scope === 'instrument'}
            disabled={selectedInstrument === null}
            onChange={() => setScope('instrument')}
          />
          当前证券
        </label>
        <label>
          <input
            type="radio"
            name="update-scope"
            checked={scope === 'pool'}
            disabled={selectedPool === null}
            onChange={() => setScope('pool')}
          />
          当前股票池
        </label>
      </fieldset>
      <div className="market-date-range">
        <label>
          开始日期
          <input
            type="date"
            value={start}
            onChange={(event) => setStart(event.currentTarget.value)}
          />
        </label>
        <label>
          结束日期
          <input
            type="date"
            value={end}
            onChange={(event) => setEnd(event.currentTarget.value)}
          />
        </label>
      </div>
      <p>
        {period} · {adjustment} · {scopeSymbols.length} 只证券
      </p>
      <button
        type="button"
        disabled={rangeInvalid || createUpdate.isPending || activeNonterminal}
        onClick={() => createUpdate.mutate()}
      >
        启动更新
      </button>
      {backtestHref === null ? null : (
        <Link className="secondary-action" to={backtestHref}>
          回测当前股票
        </Link>
      )}
      {rangeInvalid ? (
        <p role="note">请选择有效范围，并确保结束日期晚于开始日期。</p>
      ) : null}

      {visibleTask !== null ? (
        <section aria-label="更新进度" aria-live="polite">
          <strong>{statusLabels[visibleTask.status]}</strong>
          <progress max={1} value={visibleTask.progress}>
            {Math.round(visibleTask.progress * 100)}%
          </progress>
          {latestProgress !== undefined ? (
            <p>
              {progressValue(latestProgress.detail['stage'], '处理中')} ·
              {progressValue(latestProgress.detail['current_symbol'], '批次')} ·
              {progressValue(latestProgress.detail['processed'], '0')}/
              {progressValue(latestProgress.detail['total'], '0')} · 成功
              {progressValue(latestProgress.detail['succeeded'], '0')} / 失败
              {progressValue(latestProgress.detail['failed'], '0')} / 取消
              {progressValue(latestProgress.detail['cancelled'], '0')}
            </p>
          ) : null}
          {visibleTask.status === 'succeeded' &&
          visibleTask.result !== null &&
          visibleTask.kind === 'market.update' ? (
            <p role="status">
              共 {progressValue(visibleTask.result['total'], '0')} · 成功{' '}
              {progressValue(visibleTask.result['succeeded'], '0')} · 失败{' '}
              {progressValue(visibleTask.result['failed'], '0')} · 取消{' '}
              {progressValue(visibleTask.result['cancelled'], '0')}
            </p>
          ) : null}
          {visibleTask.status === 'succeeded' &&
          visibleTask.result !== null &&
          visibleTask.kind === 'market.catalog.update' ? (
            <p role="status">
              目录 {progressValue(visibleTask.result['row_count'], '0')} 只 ·
              预设成功{' '}
              {Array.isArray(visibleTask.result['preset_successes'])
                ? visibleTask.result['preset_successes'].length
                : 0}{' '}
              · 预设失败{' '}
              {Array.isArray(visibleTask.result['preset_failures'])
                ? visibleTask.result['preset_failures'].length
                : 0}
            </p>
          ) : null}
          {visibleTask.cancelRequested && !terminal.has(visibleTask.status) ? (
            <span>已请求取消，等待 Worker 确认</span>
          ) : null}
          {!terminal.has(visibleTask.status) ? (
            <button
              type="button"
              disabled={cancel.isPending}
              onClick={() => cancel.mutate()}
            >
              取消更新
            </button>
          ) : null}
          {items.data !== undefined ? (
            <ul aria-label="逐证券更新结果">
              {items.data.slice(0, 100).map((item) => (
                <li key={`${item.ordinal}-${item.symbol}`}>
                  {item.symbol} · {item.status}
                  {item.reason === null ? '' : ` · ${item.reason}`}
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      ) : null}

      <fieldset>
        <legend>每日计划（Asia/Shanghai）</legend>
        <label>
          <input
            type="checkbox"
            checked={scheduleEnabled}
            onChange={(event) =>
              setScheduleEnabled(event.currentTarget.checked)
            }
          />
          启用每日更新
        </label>
        <label>
          每日更新时间
          <input
            aria-label="每日更新时间"
            type="time"
            value={scheduleTime}
            onChange={(event) => setScheduleTime(event.currentTarget.value)}
          />
        </label>
        <button
          type="button"
          disabled={rangeInvalid || schedule.isPending}
          onClick={() => schedule.mutate()}
        >
          保存每日计划
        </button>
        <p>计划保存的是当前证券列表快照，后续修改股票池不会静默改变范围。</p>
        {(schedule.data ?? savedSchedule.data) !== undefined ? (
          <p role="status">
            范围快照已冻结
            {(schedule.data ?? savedSchedule.data)?.lastEnqueuedLocalDate ===
            null
              ? ''
              : `；上次入队 ${(schedule.data ?? savedSchedule.data)?.lastEnqueuedLocalDate ?? ''}`}
            {(schedule.data ?? savedSchedule.data)?.nextDueAt === null
              ? '；计划已停用'
              : `；下次运行 ${(schedule.data ?? savedSchedule.data)?.nextDueAt ?? ''}`}
          </p>
        ) : null}
      </fieldset>
    </section>
  );
}
