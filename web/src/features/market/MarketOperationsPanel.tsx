import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { ApiError, type JsonValue } from '../../shared/api/client';
import { AsyncActionButton } from '../../shared/components/AsyncActionButton';
import { ModalDialog } from '../../shared/ModalDialog';
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

type PoolDraft = {
  readonly name: string;
  readonly symbols: readonly string[];
  readonly search: string;
};

type PoolMutationToken = {
  readonly session: number;
  readonly revision: number;
};

type CreatePoolRequest = PoolMutationToken & {
  readonly draft: PoolDraft;
};

type UpdatePoolRequest = PoolMutationToken & {
  readonly draft: PoolDraft;
  readonly expectedRevision: number;
  readonly poolId: string;
};

type DeletePoolRequest = {
  readonly session: number;
  readonly expectedRevision: number;
  readonly poolId: string;
};

function tokensMatch(
  left: PoolMutationToken | null,
  right: PoolMutationToken,
): boolean {
  return (
    left !== null &&
    left.session === right.session &&
    left.revision === right.revision
  );
}

function poolDraft(
  name: string,
  symbols: readonly string[],
  search: string,
): PoolDraft {
  return { name, symbols: [...symbols], search };
}

function poolDraftsMatch(left: PoolDraft, right: PoolDraft): boolean {
  return (
    left.name === right.name &&
    left.search === right.search &&
    left.symbols.length === right.symbols.length &&
    left.symbols.every((symbol, index) => symbol === right.symbols[index])
  );
}

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
  const createTriggerRef = useRef<HTMLButtonElement>(null);
  const editTriggerRef = useRef<HTMLButtonElement>(null);
  const createNameRef = useRef<HTMLInputElement>(null);
  const editNameRef = useRef<HTMLInputElement>(null);
  const createContinueRef = useRef<HTMLButtonElement>(null);
  const editContinueRef = useRef<HTMLButtonElement>(null);
  const keepPoolRef = useRef<HTMLButtonElement>(null);
  const createStatusRef = useRef<HTMLParagraphElement>(null);
  const editStatusRef = useRef<HTMLParagraphElement>(null);
  const deleteStatusRef = useRef<HTMLParagraphElement>(null);
  const createConfirmationOriginRef = useRef<HTMLElement | null>(null);
  const editConfirmationOriginRef = useRef<HTMLElement | null>(null);
  const createBaselineRef = useRef<PoolDraft>(poolDraft('', [], ''));
  const editBaselineRef = useRef<PoolDraft>(poolDraft('', [], ''));
  const nextDialogSessionRef = useRef(0);
  const activeCreateSessionRef = useRef<number | null>(null);
  const activeEditSessionRef = useRef<number | null>(null);
  const activeEditPoolRef = useRef<{
    readonly id: string;
    readonly revision: number;
  } | null>(null);
  const createDraftRevisionRef = useRef(0);
  const editDraftRevisionRef = useRef(0);
  const createPendingRef = useRef<PoolMutationToken | null>(null);
  const editPendingRef = useRef<PoolMutationToken | null>(null);
  const deletePendingRef = useRef<DeletePoolRequest | null>(null);
  const [poolDialogOpen, setPoolDialogOpen] = useState(false);
  const [createDialogMode, setCreateDialogMode] = useState<
    'editor' | 'discard'
  >('editor');
  const [poolName, setPoolName] = useState('');
  const [poolSymbols, setPoolSymbols] = useState<string[]>([]);
  const [poolSearch, setPoolSearch] = useState('');
  const [createPendingToken, setCreatePendingToken] =
    useState<PoolMutationToken | null>(null);
  const [editPendingToken, setEditPendingToken] =
    useState<PoolMutationToken | null>(null);
  const [createIssue, setCreateIssue] = useState<unknown>(null);
  const [editIssue, setEditIssue] = useState<unknown>(null);
  const [deleteIssue, setDeleteIssue] = useState<unknown>(null);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [editDialogMode, setEditDialogMode] = useState<
    'editor' | 'discard' | 'delete'
  >('editor');
  const [editName, setEditName] = useState('');
  const [editSymbols, setEditSymbols] = useState<string[]>([]);
  const [scope, setScope] = useState<'instrument' | 'pool'>('instrument');
  const [start, setStart] = useState(() => dateInput(365));
  const [end, setEnd] = useState(() => dateInput(0));
  const [activeTask, setActiveTask] = useState<MarketTask | null>(null);
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [scheduleTime, setScheduleTime] = useState('18:00');
  const createBusy =
    createPendingToken !== null &&
    createPendingToken.session === activeCreateSessionRef.current;
  const editBusy =
    editPendingToken !== null &&
    editPendingToken.session === activeEditSessionRef.current;

  useEffect(
    () => () => {
      for (const controller of controllers.current) controller.abort();
      controllers.current.clear();
    },
    [],
  );
  useEffect(() => {
    if (createDialogMode === 'discard') createContinueRef.current?.focus();
  }, [createDialogMode]);
  useEffect(() => {
    if (editDialogMode === 'discard') editContinueRef.current?.focus();
    if (editDialogMode === 'delete') keepPoolRef.current?.focus();
  }, [editDialogMode]);
  useLayoutEffect(() => {
    if (createBusy) createStatusRef.current?.focus();
  }, [createBusy]);
  useLayoutEffect(() => {
    if (editBusy) editStatusRef.current?.focus();
  }, [editBusy]);

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

  function captureFocusedElement(): HTMLElement | null {
    return document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
  }

  function restoreConfirmationOrigin(
    originRef: { current: HTMLElement | null },
    fallbackRef: { readonly current: HTMLElement | null },
  ) {
    const origin = originRef.current;
    originRef.current = null;
    window.setTimeout(() => {
      if (origin?.isConnected && !origin.matches(':disabled')) origin.focus();
      if (document.activeElement !== origin) fallbackRef.current?.focus();
    }, 0);
  }

  function openCreateEditor() {
    if (createPendingRef.current !== null) {
      createStatusRef.current?.focus();
      return;
    }
    const session = nextDialogSessionRef.current + 1;
    nextDialogSessionRef.current = session;
    activeCreateSessionRef.current = session;
    createDraftRevisionRef.current = 0;
    createPendingRef.current = null;
    const baseline = poolDraft('', [], '');
    createBaselineRef.current = baseline;
    setPoolName(baseline.name);
    setPoolSymbols([...baseline.symbols]);
    setPoolSearch(baseline.search);
    setCreatePendingToken(null);
    setCreateIssue(null);
    setCreateDialogMode('editor');
    setPoolDialogOpen(true);
  }

  function reviseCreateDraft(change: () => void) {
    if (createPendingRef.current !== null) return;
    createDraftRevisionRef.current += 1;
    change();
  }

  function closeCreateEditor(writeSettled = false) {
    if (!writeSettled && createPendingRef.current !== null) {
      createStatusRef.current?.focus();
      return;
    }
    activeCreateSessionRef.current = null;
    createPendingRef.current = null;
    setCreatePendingToken(null);
    setPoolDialogOpen(false);
  }

  function requestCreateClose() {
    if (createPendingRef.current !== null) {
      createStatusRef.current?.focus();
      return;
    }
    if (
      !poolDraftsMatch(
        poolDraft(poolName, poolSymbols, poolSearch),
        createBaselineRef.current,
      )
    ) {
      createConfirmationOriginRef.current = captureFocusedElement();
      setCreateDialogMode('discard');
      return;
    }
    closeCreateEditor();
  }

  function returnToCreateEditor() {
    if (createPendingRef.current !== null) {
      createStatusRef.current?.focus();
      return;
    }
    setCreateDialogMode('editor');
    restoreConfirmationOrigin(createConfirmationOriginRef, createNameRef);
  }

  function discardCreateDraft() {
    if (createPendingRef.current !== null) {
      createStatusRef.current?.focus();
      return;
    }
    const baseline = createBaselineRef.current;
    setPoolName(baseline.name);
    setPoolSymbols([...baseline.symbols]);
    setPoolSearch(baseline.search);
    setCreateDialogMode('editor');
    closeCreateEditor();
  }

  function openEditEditor() {
    if (editPendingRef.current !== null || deletePendingRef.current !== null) {
      editStatusRef.current?.focus();
      return;
    }
    if (
      selectedPool?.kind !== 'custom' ||
      selectedPool.revision === null ||
      selectedPool.revision === undefined
    )
      return;
    const session = nextDialogSessionRef.current + 1;
    nextDialogSessionRef.current = session;
    activeEditSessionRef.current = session;
    activeEditPoolRef.current = {
      id: selectedPool.id,
      revision: selectedPool.revision,
    };
    editDraftRevisionRef.current = 0;
    editPendingRef.current = null;
    const baseline = poolDraft(selectedPool.name, selectedPool.symbols, '');
    editBaselineRef.current = baseline;
    setEditName(baseline.name);
    setEditSymbols([...baseline.symbols]);
    setPoolSearch(baseline.search);
    setEditPendingToken(null);
    setEditIssue(null);
    setDeleteIssue(null);
    setEditDialogMode('editor');
    setEditDialogOpen(true);
  }

  function reviseEditDraft(change: () => void) {
    if (editPendingRef.current !== null || deletePendingRef.current !== null)
      return;
    editDraftRevisionRef.current += 1;
    change();
  }

  function closeEditEditor(writeSettled = false) {
    if (!writeSettled && editPendingRef.current !== null) {
      editStatusRef.current?.focus();
      return;
    }
    if (!writeSettled && deletePendingRef.current !== null) {
      deleteStatusRef.current?.focus();
      return;
    }
    activeEditSessionRef.current = null;
    activeEditPoolRef.current = null;
    editPendingRef.current = null;
    deletePendingRef.current = null;
    setEditPendingToken(null);
    setEditDialogOpen(false);
  }

  function requestEditClose() {
    if (editPendingRef.current !== null) {
      editStatusRef.current?.focus();
      return;
    }
    if (deletePendingRef.current !== null) {
      deleteStatusRef.current?.focus();
      return;
    }
    if (
      !poolDraftsMatch(
        poolDraft(editName, editSymbols, poolSearch),
        editBaselineRef.current,
      )
    ) {
      editConfirmationOriginRef.current = captureFocusedElement();
      setEditDialogMode('discard');
      return;
    }
    closeEditEditor();
  }

  function returnToEditEditor() {
    if (deletePendingRef.current !== null) {
      deleteStatusRef.current?.focus();
      return;
    }
    setDeleteIssue(null);
    setEditDialogMode('editor');
    restoreConfirmationOrigin(editConfirmationOriginRef, editNameRef);
  }

  function discardEditDraft() {
    if (editPendingRef.current !== null) {
      editStatusRef.current?.focus();
      return;
    }
    if (deletePendingRef.current !== null) {
      deleteStatusRef.current?.focus();
      return;
    }
    const baseline = editBaselineRef.current;
    setEditName(baseline.name);
    setEditSymbols([...baseline.symbols]);
    setPoolSearch(baseline.search);
    setEditDialogMode('editor');
    closeEditEditor();
  }

  function requestDeletePool() {
    if (editPendingRef.current !== null || deletePendingRef.current !== null)
      return;
    editConfirmationOriginRef.current = captureFocusedElement();
    setDeleteIssue(null);
    setEditDialogMode('delete');
  }

  function handleCreateEscape() {
    if (createDialogMode === 'discard') {
      returnToCreateEditor();
      return;
    }
    requestCreateClose();
  }

  function handleEditEscape() {
    if (deletePendingRef.current !== null) {
      deleteStatusRef.current?.focus();
      return;
    }
    if (editPendingRef.current !== null) {
      editStatusRef.current?.focus();
      return;
    }
    if (editDialogMode !== 'editor') {
      returnToEditEditor();
      return;
    }
    requestEditClose();
  }

  function payload(): MarketUpdatePayload {
    return {
      symbols: scopeSymbols,
      period,
      adjustment,
      start: utcBoundary(start),
      end: utcBoundary(end),
    };
  }

  function submitCreatePool() {
    const session = activeCreateSessionRef.current;
    if (
      session === null ||
      createPendingRef.current !== null ||
      poolName.trim().length === 0 ||
      poolSymbols.length === 0
    )
      return;
    const request: CreatePoolRequest = {
      session,
      revision: createDraftRevisionRef.current,
      draft: poolDraft(poolName, poolSymbols, poolSearch),
    };
    createPendingRef.current = request;
    createStatusRef.current?.focus();
    setCreateIssue(null);
    setCreatePendingToken(request);
    createPool.mutate(request);
  }

  function submitUpdatePool() {
    const session = activeEditSessionRef.current;
    const pool = activeEditPoolRef.current;
    if (
      session === null ||
      pool === null ||
      editPendingRef.current !== null ||
      deletePendingRef.current !== null ||
      editName.trim().length === 0 ||
      editSymbols.length === 0
    )
      return;
    const request: UpdatePoolRequest = {
      session,
      revision: editDraftRevisionRef.current,
      draft: poolDraft(editName, editSymbols, poolSearch),
      expectedRevision: pool.revision,
      poolId: pool.id,
    };
    editPendingRef.current = request;
    editStatusRef.current?.focus();
    setEditIssue(null);
    setEditPendingToken(request);
    updatePool.mutate(request);
  }

  function submitDeletePool() {
    const session = activeEditSessionRef.current;
    const pool = activeEditPoolRef.current;
    if (
      session === null ||
      pool === null ||
      editPendingRef.current !== null ||
      deletePendingRef.current !== null
    )
      return;
    const request: DeletePoolRequest = {
      session,
      poolId: pool.id,
      expectedRevision: pool.revision,
    };
    deletePendingRef.current = request;
    setDeleteIssue(null);
    deleteStatusRef.current?.focus();
    deletePool.mutate(request);
  }

  const createPool = useMutation({
    mutationFn: (request: CreatePoolRequest) =>
      withSignal((signal) =>
        api.createPool(
          { name: request.draft.name, symbols: request.draft.symbols },
          { signal },
        ),
      ),
    onSuccess: async (_createdPool, request) => {
      if (
        activeCreateSessionRef.current === request.session &&
        createDraftRevisionRef.current === request.revision
      ) {
        setCreateDialogMode('editor');
        closeCreateEditor(true);
        setPoolName('');
        setPoolSymbols([]);
        setPoolSearch('');
      }
      await queryClient.invalidateQueries({ queryKey: ['market', 'pools'] });
    },
    onError: (error, request) => {
      if (
        activeCreateSessionRef.current === request.session &&
        createDraftRevisionRef.current === request.revision
      )
        setCreateIssue(error);
    },
    onSettled: (_data, _error, request) => {
      if (!tokensMatch(createPendingRef.current, request)) return;
      createPendingRef.current = null;
      setCreatePendingToken((current) =>
        tokensMatch(current, request) ? null : current,
      );
    },
  });
  const updatePool = useMutation({
    mutationFn: (request: UpdatePoolRequest) => {
      return withSignal((signal) =>
        api.updatePool(
          request.poolId,
          {
            expectedRevision: request.expectedRevision,
            name: request.draft.name,
            symbols: request.draft.symbols,
          },
          { signal },
        ),
      );
    },
    onSuccess: async (_updatedPool, request) => {
      if (
        activeEditSessionRef.current === request.session &&
        editDraftRevisionRef.current === request.revision &&
        activeEditPoolRef.current?.id === request.poolId &&
        activeEditPoolRef.current.revision === request.expectedRevision
      ) {
        setEditDialogMode('editor');
        closeEditEditor(true);
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['market', 'pools'] }),
        queryClient.invalidateQueries({
          queryKey: ['market', 'pool', request.poolId],
        }),
      ]);
    },
    onError: (error, request) => {
      if (
        activeEditSessionRef.current === request.session &&
        editDraftRevisionRef.current === request.revision &&
        activeEditPoolRef.current?.id === request.poolId &&
        activeEditPoolRef.current.revision === request.expectedRevision
      )
        setEditIssue(error);
    },
    onSettled: (_data, _error, request) => {
      if (!tokensMatch(editPendingRef.current, request)) return;
      editPendingRef.current = null;
      setEditPendingToken((current) =>
        tokensMatch(current, request) ? null : current,
      );
    },
  });
  const deletePool = useMutation({
    mutationFn: (request: DeletePoolRequest) => {
      return withSignal((signal) =>
        api.deletePool(request.poolId, request.expectedRevision, { signal }),
      );
    },
    onSuccess: async (_deleted, request) => {
      setEditDialogMode('editor');
      closeEditEditor(true);
      onPoolDeleted?.();
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['market', 'pools'] }),
        queryClient.invalidateQueries({
          queryKey: ['market', 'pool', request.poolId],
        }),
      ]);
    },
    onError: (error, request) => {
      if (
        activeEditSessionRef.current === request.session &&
        activeEditPoolRef.current?.id === request.poolId &&
        activeEditPoolRef.current.revision === request.expectedRevision
      )
        setDeleteIssue(error);
    },
    onSettled: (_data, _error, request) => {
      if (deletePendingRef.current !== request) return;
      deletePendingRef.current = null;
    },
  });
  useLayoutEffect(() => {
    if (deletePool.isPending) deleteStatusRef.current?.focus();
  }, [deletePool.isPending]);
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
        <button ref={createTriggerRef} type="button" onClick={openCreateEditor}>
          新建自定义池
        </button>
        {selectedPool?.kind === 'custom' ? (
          <button ref={editTriggerRef} type="button" onClick={openEditEditor}>
            编辑当前股票池
          </button>
        ) : null}
        <AsyncActionButton
          type="button"
          pending={createCatalog.isPending}
          disabled={createCatalog.isPending || activeNonterminal}
          onClick={() => createCatalog.mutate()}
        >
          更新证券目录
        </AsyncActionButton>
      </div>

      {poolDialogOpen ? (
        <ModalDialog
          backdropClassName="market-pool-backdrop"
          aria-busy={createBusy}
          aria-labelledby={
            createDialogMode === 'discard'
              ? 'create-pool-discard-title'
              : 'create-pool-title'
          }
          initialFocusRef={createNameRef}
          returnFocusRef={createTriggerRef}
          onEscape={handleCreateEscape}
        >
          <div
            hidden={createDialogMode !== 'editor'}
            inert={createDialogMode !== 'editor'}
            aria-hidden={createDialogMode !== 'editor'}
          >
            <h4 id="create-pool-title">新建自定义池</h4>
            <label>
              股票池名称
              <input
                aria-label="股票池名称"
                ref={createNameRef}
                value={poolName}
                maxLength={64}
                disabled={createBusy}
                onChange={(event) =>
                  reviseCreateDraft(() =>
                    setPoolName(event.currentTarget.value),
                  )
                }
              />
            </label>
            {selectedInstrument === null ? (
              <p>先搜索并选择证券，再加入股票池。</p>
            ) : (
              <button
                type="button"
                disabled={
                  createBusy || poolSymbols.includes(selectedInstrument.symbol)
                }
                onClick={() =>
                  reviseCreateDraft(() =>
                    setPoolSymbols((symbols) => [
                      ...symbols,
                      selectedInstrument.symbol,
                    ]),
                  )
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
                disabled={createBusy}
                onChange={(event) =>
                  reviseCreateDraft(() =>
                    setPoolSearch(event.currentTarget.value),
                  )
                }
              />
            </label>
            {poolSearchResults.data !== undefined ? (
              <ul aria-label="可加入证券">
                {poolSearchResults.data.slice(0, 20).map((instrument) => (
                  <li key={instrument.symbol}>
                    <button
                      type="button"
                      disabled={
                        createBusy || poolSymbols.includes(instrument.symbol)
                      }
                      onClick={() =>
                        reviseCreateDraft(() =>
                          setPoolSymbols((symbols) => [
                            ...symbols,
                            instrument.symbol,
                          ]),
                        )
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
                    disabled={createBusy}
                    onClick={() =>
                      reviseCreateDraft(() =>
                        setPoolSymbols((symbols) =>
                          symbols.filter(
                            (_item, itemIndex) => itemIndex !== index,
                          ),
                        ),
                      )
                    }
                  >
                    移除
                  </button>
                </li>
              ))}
            </ol>
            <p
              ref={createStatusRef}
              className="pool-mutation-status visually-hidden"
              role="status"
              tabIndex={-1}
            />
            {createIssue !== null ? (
              <p role="alert">
                股票池创建失败，请检查成员。
                {poolIssueText(createIssue) ?? ''}
              </p>
            ) : null}
            <AsyncActionButton
              type="button"
              pending={createBusy}
              disabled={
                createBusy ||
                poolName.trim().length === 0 ||
                poolSymbols.length === 0
              }
              onClick={submitCreatePool}
            >
              创建股票池
            </AsyncActionButton>
            <button type="button" onClick={requestCreateClose}>
              取消
            </button>
          </div>
          {createDialogMode === 'discard' ? (
            <section
              role="alertdialog"
              aria-labelledby="create-pool-discard-title"
              aria-describedby="create-pool-discard-description"
            >
              <h4 id="create-pool-discard-title">放弃新股票池草稿？</h4>
              <p id="create-pool-discard-description">
                名称、成员或搜索草稿尚未保存。继续编辑可保留这些更改。
              </p>
              <button
                ref={createContinueRef}
                type="button"
                onClick={returnToCreateEditor}
              >
                继续编辑
              </button>
              <button type="button" onClick={discardCreateDraft}>
                放弃更改
              </button>
            </section>
          ) : null}
        </ModalDialog>
      ) : null}

      {editDialogOpen && selectedPool?.kind === 'custom' ? (
        <ModalDialog
          backdropClassName="market-pool-backdrop"
          aria-busy={editBusy || deletePool.isPending}
          aria-labelledby={
            editDialogMode === 'discard'
              ? 'edit-pool-discard-title'
              : editDialogMode === 'delete'
                ? 'edit-pool-delete-title'
                : 'edit-pool-title'
          }
          initialFocusRef={editNameRef}
          returnFocusRef={editTriggerRef}
          onEscape={handleEditEscape}
        >
          <div
            hidden={editDialogMode !== 'editor'}
            inert={editDialogMode !== 'editor'}
            aria-hidden={editDialogMode !== 'editor'}
          >
            <h4 id="edit-pool-title">编辑自定义池</h4>
            <label>
              股票池名称
              <input
                ref={editNameRef}
                value={editName}
                disabled={editBusy || deletePool.isPending}
                onChange={(event) =>
                  reviseEditDraft(() => setEditName(event.currentTarget.value))
                }
              />
            </label>
            <ol aria-label="编辑股票池成员">
              {editSymbols.map((symbol, index) => (
                <li key={symbol}>
                  {symbol}
                  <button
                    type="button"
                    aria-label={`上移 ${symbol}`}
                    disabled={editBusy || deletePool.isPending || index === 0}
                    onClick={() =>
                      reviseEditDraft(() =>
                        setEditSymbols((symbols) => {
                          const next = [...symbols];
                          [next[index - 1], next[index]] = [
                            next[index],
                            next[index - 1],
                          ];
                          return next;
                        }),
                      )
                    }
                  >
                    上移
                  </button>
                  <button
                    type="button"
                    aria-label={`下移 ${symbol}`}
                    disabled={
                      editBusy ||
                      deletePool.isPending ||
                      index === editSymbols.length - 1
                    }
                    onClick={() =>
                      reviseEditDraft(() =>
                        setEditSymbols((symbols) => {
                          const next = [...symbols];
                          [next[index], next[index + 1]] = [
                            next[index + 1],
                            next[index],
                          ];
                          return next;
                        }),
                      )
                    }
                  >
                    下移
                  </button>
                  <button
                    type="button"
                    aria-label={`移除 ${symbol}`}
                    disabled={editBusy || deletePool.isPending}
                    onClick={() =>
                      reviseEditDraft(() =>
                        setEditSymbols((symbols) =>
                          symbols.filter(
                            (_item, itemIndex) => itemIndex !== index,
                          ),
                        ),
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
                disabled={editBusy || deletePool.isPending}
                onChange={(event) =>
                  reviseEditDraft(() =>
                    setPoolSearch(event.currentTarget.value),
                  )
                }
              />
            </label>
            {poolSearchResults.data !== undefined ? (
              <ul aria-label="编辑池可加入证券">
                {poolSearchResults.data.slice(0, 20).map((instrument) => (
                  <li key={instrument.symbol}>
                    <button
                      type="button"
                      disabled={
                        editBusy ||
                        deletePool.isPending ||
                        editSymbols.includes(instrument.symbol)
                      }
                      onClick={() =>
                        reviseEditDraft(() =>
                          setEditSymbols((symbols) => [
                            ...symbols,
                            instrument.symbol,
                          ]),
                        )
                      }
                    >
                      加入 {instrument.name} {instrument.symbol}
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
            <p
              ref={editStatusRef}
              className="pool-mutation-status visually-hidden"
              role="status"
              tabIndex={-1}
            />
            {editIssue !== null ? (
              <p role="alert">
                股票池保存失败，请检查成员。
                {poolIssueText(editIssue) ?? ''}
              </p>
            ) : null}
            <AsyncActionButton
              type="button"
              pending={editBusy}
              disabled={
                editBusy ||
                deletePool.isPending ||
                editName.trim().length === 0 ||
                editSymbols.length === 0
              }
              onClick={submitUpdatePool}
            >
              保存股票池
            </AsyncActionButton>
            <button
              type="button"
              disabled={editBusy || deletePool.isPending}
              onClick={requestDeletePool}
            >
              删除股票池
            </button>
            <button type="button" onClick={requestEditClose}>
              取消
            </button>
          </div>
          {editDialogMode === 'discard' ? (
            <section
              role="alertdialog"
              aria-labelledby="edit-pool-discard-title"
              aria-describedby="edit-pool-discard-description"
            >
              <h4 id="edit-pool-discard-title">放弃股票池更改？</h4>
              <p id="edit-pool-discard-description">
                名称、成员顺序或搜索草稿尚未保存。继续编辑可保留这些更改。
              </p>
              <button
                ref={editContinueRef}
                type="button"
                onClick={returnToEditEditor}
              >
                继续编辑
              </button>
              <button type="button" onClick={discardEditDraft}>
                放弃更改
              </button>
            </section>
          ) : null}
          {editDialogMode === 'delete' ? (
            <section
              role="alertdialog"
              className="pool-delete-confirmation"
              aria-labelledby="edit-pool-delete-title"
              aria-describedby="edit-pool-delete-description"
            >
              <h4 id="edit-pool-delete-title">确认删除股票池？</h4>
              <p id="edit-pool-delete-description">
                删除后无法撤销，确认删除“{selectedPool.name}”？
              </p>
              <p
                ref={deleteStatusRef}
                className="pool-mutation-status visually-hidden"
                role="status"
                tabIndex={-1}
              />
              {deleteIssue !== null ? (
                <p role="alert">
                  股票池删除失败，请重试或返回编辑。
                  {poolIssueText(deleteIssue) ?? ''}
                </p>
              ) : null}
              <button
                ref={keepPoolRef}
                type="button"
                disabled={deletePool.isPending}
                onClick={returnToEditEditor}
              >
                保留股票池
              </button>
              <AsyncActionButton
                type="button"
                pending={deletePool.isPending}
                disabled={deletePool.isPending}
                onClick={submitDeletePool}
              >
                确认删除
              </AsyncActionButton>
            </section>
          ) : null}
        </ModalDialog>
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
      <AsyncActionButton
        type="button"
        pending={createUpdate.isPending}
        disabled={rangeInvalid || createUpdate.isPending || activeNonterminal}
        onClick={() => createUpdate.mutate()}
      >
        启动更新
      </AsyncActionButton>
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
            <span>已请求取消，等待后台任务确认</span>
          ) : null}
          {!terminal.has(visibleTask.status) ? (
            <AsyncActionButton
              type="button"
              pending={cancel.isPending}
              disabled={cancel.isPending}
              onClick={() => cancel.mutate()}
            >
              取消更新
            </AsyncActionButton>
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
        <AsyncActionButton
          type="button"
          pending={schedule.isPending}
          disabled={rangeInvalid || schedule.isPending}
          onClick={() => schedule.mutate()}
        >
          保存每日计划
        </AsyncActionButton>
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
