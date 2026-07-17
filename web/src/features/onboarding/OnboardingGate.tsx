import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { useMarketStore } from '../market/marketStore';
import { AsyncActionButton } from '../../shared/components/AsyncActionButton';
import {
  onboardingApi,
  type OnboardingAction,
  type OnboardingApi,
  type OnboardingInstrument,
  type OnboardingSource,
  type OnboardingState,
} from './onboardingApi';
import { OnboardingDemoContext } from './demoMode';

const DEFAULT_INSTRUMENT: OnboardingInstrument = {
  symbol: '000001.SS',
  name: '上证指数',
  exchange: 'SH',
  instrumentKind: 'index',
};

const stepOrder = [
  'welcome',
  'data_preparation',
  'instrument_selection',
  'synchronization',
] as const;

const stepLabels = ['开始', '数据源', '股票', '完成'] as const;

function safeDate(value: string | null): string {
  if (value === null) return '首次加载后显示';
  const time = Date.parse(value);
  return Number.isNaN(time)
    ? '已记录'
    : new Intl.DateTimeFormat('zh-CN', {
        dateStyle: 'medium',
        timeStyle: 'short',
        timeZone: 'Asia/Shanghai',
      }).format(time);
}

function LoadingCard() {
  return (
    <main className="onboarding-shell">
      <section className="onboarding-card onboarding-status-card" role="status">
        <h1>正在打开 Stock Desk</h1>
        <p>正在加载设置…</p>
      </section>
    </main>
  );
}

function LoadError({
  busy,
  onRetry,
  onDiagnostics,
}: {
  readonly busy: boolean;
  readonly onRetry: () => void;
  readonly onDiagnostics: () => void;
}) {
  return (
    <main className="onboarding-shell">
      <section className="onboarding-card onboarding-status-card" role="alert">
        <h1>暂时无法打开</h1>
        <p>请重试。如果问题仍然存在，可以查看帮助。</p>
        <div className="onboarding-actions">
          <AsyncActionButton pending={busy} type="button" onClick={onRetry}>
            重试
          </AsyncActionButton>
          <button className="secondary" type="button" onClick={onDiagnostics}>
            查看帮助
          </button>
        </div>
      </section>
    </main>
  );
}

function Stepper({ state }: { readonly state: OnboardingState }) {
  const current = Math.max(0, stepOrder.indexOf(state.currentStep as never));
  return (
    <ol className="onboarding-stepper" aria-label="首次设置进度">
      {stepLabels.map((label, index) => (
        <li
          key={label}
          data-active={index === current}
          data-complete={index < current}
          aria-current={index === current ? 'step' : undefined}
        >
          <span>{index + 1}</span>
          <strong>{label}</strong>
        </li>
      ))}
    </ol>
  );
}

function InstrumentSearch({
  api,
  selected,
  onSelect,
}: {
  readonly api: OnboardingApi;
  readonly selected: OnboardingInstrument;
  readonly onSelect: (instrument: OnboardingInstrument) => void;
}) {
  const listboxId = useId();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<readonly OnboardingInstrument[]>([]);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [status, setStatus] = useState<'idle' | 'loading' | 'error'>('idle');

  useEffect(() => {
    const normalized = query.trim();
    if (normalized.length === 0) {
      setResults([]);
      setStatus('idle');
      return undefined;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(
      () => {
        setStatus('loading');
        void api
          .searchInstruments({ query: normalized, signal: controller.signal })
          .then((items) => {
            setResults(items);
            setActiveIndex(items.length > 0 ? 0 : -1);
            setStatus('idle');
          })
          .catch(() => {
            if (!controller.signal.aborted) setStatus('error');
          });
      },
      /^\d{6}(?:\.(?:SS|SH|SZ|BJ))?$/u.test(normalized) ? 0 : 180,
    );
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [api, query]);

  function choose(item: OnboardingInstrument) {
    onSelect(item);
    setQuery('');
    setResults([]);
    setActiveIndex(-1);
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Escape') {
      setResults([]);
      setActiveIndex(-1);
      return;
    }
    if (results.length === 0) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      setActiveIndex((current) => {
        const delta = event.key === 'ArrowDown' ? 1 : -1;
        return (Math.max(current, 0) + delta + results.length) % results.length;
      });
    } else if (event.key === 'Enter' && activeIndex >= 0) {
      event.preventDefault();
      const item = results[activeIndex];
      if (item !== undefined) choose(item);
    }
  }

  return (
    <div className="onboarding-search">
      <label htmlFor={`${listboxId}-input`}>搜索证券</label>
      <input
        id={`${listboxId}-input`}
        role="combobox"
        aria-label="按代码、中文或拼音搜索证券"
        aria-controls={listboxId}
        aria-expanded={results.length > 0}
        aria-activedescendant={
          activeIndex < 0 ? undefined : `${listboxId}-${String(activeIndex)}`
        }
        autoComplete="off"
        placeholder="例如：000001、贵州茅台、gzmt"
        value={query}
        onChange={(event) => setQuery(event.currentTarget.value)}
        onKeyDown={onKeyDown}
      />
      {status === 'loading' ? <p role="status">正在搜索…</p> : null}
      {status === 'error' ? (
        <p role="alert">搜索失败，仍可直接使用上证指数。</p>
      ) : null}
      {results.length > 0 ? (
        <ul id={listboxId} role="listbox" aria-label="首次设置证券搜索结果">
          {results.map((item, index) => (
            <li
              id={`${listboxId}-${String(index)}`}
              key={`${item.instrumentKind}:${item.symbol}`}
              role="option"
              tabIndex={-1}
              aria-selected={item.symbol === selected.symbol}
              data-active={index === activeIndex}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => choose(item)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') choose(item);
              }}
            >
              <span>
                <strong>{item.name}</strong>
                <small>
                  {item.instrumentKind === 'index' ? '指数' : '证券'}
                </small>
              </span>
              <code>{item.symbol}</code>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

type WizardProps = {
  readonly api: OnboardingApi;
  readonly initialState: OnboardingState;
  readonly onCompleted: (instrument: OnboardingInstrument) => void;
  readonly onDemo: (
    instrument: OnboardingInstrument,
    state: OnboardingState,
  ) => void;
  readonly onAdvanced: (state: OnboardingState) => void;
};

const PROGRESS_RECOVERY_ATTEMPTS = 30;
const PROGRESS_RECOVERY_INTERVAL_MS = 1_000;

async function recoverAdvancedProgress(
  api: OnboardingApi,
  revision: number,
  isTarget: (state: OnboardingState) => boolean,
): Promise<OnboardingState | null> {
  for (let attempt = 0; attempt < PROGRESS_RECOVERY_ATTEMPTS; attempt += 1) {
    try {
      const recovered = await api.getState();
      if (
        recovered.revision > revision &&
        (isTarget(recovered) || recovered.error !== null)
      ) {
        return recovered;
      }
    } catch {
      // The desktop sidecar may still be completing the original request.
    }
    if (attempt + 1 < PROGRESS_RECOVERY_ATTEMPTS) {
      await new Promise<void>((resolve) =>
        window.setTimeout(resolve, PROGRESS_RECOVERY_INTERVAL_MS),
      );
    }
  }
  return null;
}

function OnboardingWizard({
  api,
  initialState,
  onCompleted,
  onDemo,
  onAdvanced,
}: WizardProps) {
  const [state, setState] = useState(initialState);
  const [sources, setSources] = useState<readonly OnboardingSource[]>([]);
  const [selectedSourceId, setSelectedSourceId] = useState(
    initialState.source?.id ?? '',
  );
  const [instrument, setInstrument] = useState(
    initialState.instrument ?? DEFAULT_INSTRUMENT,
  );
  const [busy, setBusy] = useState(false);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState(false);
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => headingRef.current?.focus(), [state.currentStep]);

  useEffect(() => {
    if (state.currentStep !== 'data_preparation') return undefined;
    const controller = new AbortController();
    void api
      .getSources(controller.signal)
      .then((items) => {
        setSources(items);
        setSelectedSourceId((current) =>
          current.length > 0
            ? current
            : (items.find(
                (item) => item.recommended && item.status !== 'unavailable',
              )?.id ??
              items.find((item) => item.status !== 'unavailable')?.id ??
              ''),
        );
      })
      .catch(() => {
        if (!controller.signal.aborted) setActionError(true);
      });
    return () => controller.abort();
  }, [api, state.currentStep]);

  const perform = useCallback(
    async (
      actionId: string,
      operation: () => Promise<OnboardingState>,
      isRecoveryTarget: (state: OnboardingState) => boolean,
    ) => {
      setBusy(true);
      setPendingAction(actionId);
      setActionError(false);
      try {
        setState(await operation());
      } catch {
        const recovered = await recoverAdvancedProgress(
          api,
          state.revision,
          isRecoveryTarget,
        );
        if (recovered === null) {
          setActionError(true);
        } else {
          setState(recovered);
        }
      } finally {
        setBusy(false);
        setPendingAction(null);
      }
    },
    [api, state.revision],
  );

  async function runAction(action: OnboardingAction) {
    setBusy(true);
    setPendingAction(action);
    setActionError(false);
    try {
      const next = await api.runAction(action);
      setState(next);
      if (action === 'demo') onDemo(next.instrument ?? instrument, next);
      if (action === 'advanced') onAdvanced(next);
    } catch {
      setActionError(true);
    } finally {
      setBusy(false);
      setPendingAction(null);
    }
  }

  const selectedSource =
    sources.find((source) => source.id === selectedSourceId) ?? null;
  const dataPreparationRetrying = pendingAction === 'data-retry';
  const dataPreparationNeedsRetry =
    state.error !== null || actionError || dataPreparationRetrying;

  return (
    <main className="onboarding-shell">
      <section className="onboarding-card" aria-labelledby="onboarding-title">
        <header className="onboarding-header">
          <div>
            <strong>Stock Desk</strong>
            <p>首次设置</p>
          </div>
        </header>
        <Stepper state={state} />

        <div className="onboarding-content">
          {state.currentStep === 'welcome' ? (
            <>
              <h1 id="onboarding-title" ref={headingRef} tabIndex={-1}>
                欢迎使用 Stock Desk
              </h1>
              <p className="onboarding-lead">选择一只股票后，就能查看行情。</p>
              <div className="onboarding-actions">
                <AsyncActionButton
                  type="button"
                  pending={pendingAction === 'welcome-start'}
                  disabled={busy}
                  onClick={() =>
                    void perform(
                      'welcome-start',
                      () =>
                        api.saveProgress({ currentStep: 'data_preparation' }),
                      (recovered) =>
                        recovered.currentStep === 'data_preparation',
                    )
                  }
                >
                  开始
                </AsyncActionButton>
                <AsyncActionButton
                  className="secondary"
                  type="button"
                  pending={pendingAction === 'demo'}
                  disabled={busy}
                  onClick={() => void runAction('demo')}
                >
                  进入演示模式
                </AsyncActionButton>
              </div>
            </>
          ) : null}

          {state.currentStep === 'data_preparation' ? (
            <>
              <h1 id="onboarding-title" ref={headingRef} tabIndex={-1}>
                选择数据源
              </h1>
              <fieldset className="onboarding-source-list">
                <legend>选择数据来源</legend>
                {sources.length === 0 && !actionError ? (
                  <p role="status">正在加载…</p>
                ) : null}
                {sources.map((source) => (
                  <label
                    key={source.id}
                    data-selected={source.id === selectedSourceId}
                  >
                    <input
                      type="radio"
                      name="onboarding-source"
                      value={source.id}
                      checked={source.id === selectedSourceId}
                      disabled={source.status === 'unavailable'}
                      onChange={() => setSelectedSourceId(source.id)}
                    />
                    <span>
                      <strong>{source.label}</strong>
                      {source.recommended ? <em>推荐</em> : null}
                      <small>{source.description}</small>
                      {source.status === 'unavailable' ? (
                        <small>暂时不可用</small>
                      ) : source.status === 'ready' ? (
                        <small>可用</small>
                      ) : null}
                    </span>
                  </label>
                ))}
              </fieldset>
              <div className="onboarding-actions">
                <AsyncActionButton
                  type="button"
                  pending={
                    pendingAction ===
                    (state.error !== null
                      ? 'retry'
                      : dataPreparationNeedsRetry
                        ? 'data-retry'
                        : 'data-continue')
                  }
                  disabled={
                    busy ||
                    (state.error === null &&
                      (selectedSource === null ||
                        selectedSource.status === 'unavailable'))
                  }
                  onClick={() => {
                    if (state.error !== null) {
                      void runAction('retry');
                      return;
                    }
                    void perform(
                      actionError ? 'data-retry' : 'data-continue',
                      () =>
                        api.saveProgress({
                          currentStep: 'instrument_selection',
                          sourceId: selectedSourceId,
                        }),
                      (recovered) =>
                        recovered.currentStep === 'instrument_selection',
                    );
                  }}
                >
                  {dataPreparationNeedsRetry ? '重试' : '继续'}
                </AsyncActionButton>
                <AsyncActionButton
                  className="secondary"
                  type="button"
                  pending={pendingAction === 'advanced'}
                  disabled={busy}
                  onClick={() => void runAction('advanced')}
                >
                  数据源设置
                </AsyncActionButton>
              </div>
            </>
          ) : null}

          {state.currentStep === 'instrument_selection' ? (
            <>
              <h1 id="onboarding-title" ref={headingRef} tabIndex={-1}>
                选择一只股票
              </h1>
              <p className="onboarding-lead">
                不知道选什么？直接使用上证指数。
              </p>
              <div className="onboarding-selection" aria-live="polite">
                <span>
                  {instrument.instrumentKind === 'index' ? '指数' : '证券'}
                </span>
                <strong>{instrument.name}</strong>
                <code>{instrument.symbol}</code>
              </div>
              <InstrumentSearch
                api={api}
                selected={instrument}
                onSelect={setInstrument}
              />
              <div className="onboarding-actions">
                <AsyncActionButton
                  type="button"
                  pending={pendingAction === 'instrument-sync'}
                  disabled={
                    busy || (state.source?.id ?? selectedSourceId).length === 0
                  }
                  onClick={() =>
                    void perform(
                      'instrument-sync',
                      () =>
                        api.synchronize({
                          sourceId: state.source?.id ?? selectedSourceId,
                          symbol: instrument.symbol,
                        }),
                      (recovered) =>
                        recovered.currentStep === 'synchronization' &&
                        recovered.sync?.status === 'verified',
                    )
                  }
                >
                  加载行情
                </AsyncActionButton>
              </div>
            </>
          ) : null}

          {state.currentStep === 'synchronization' ? (
            <>
              <span className="onboarding-success-icon" aria-hidden="true">
                ✓
              </span>
              <h1 id="onboarding-title" ref={headingRef} tabIndex={-1}>
                {state.sync?.status === 'verified'
                  ? '可以开始使用了'
                  : '正在准备行情'}
              </h1>
              {state.sync?.status === 'verified' ? (
                <div className="onboarding-ready-summary">
                  <p>
                    <strong>{state.instrument?.name ?? instrument.name}</strong>{' '}
                    {state.instrument?.symbol ?? instrument.symbol}
                  </p>
                  <dl>
                    <div>
                      <dt>来源</dt>
                      <dd>
                        {state.source?.label ??
                          state.sync.providerId ??
                          '免费数据源'}
                      </dd>
                    </div>
                    <div>
                      <dt>更新到</dt>
                      <dd>{safeDate(state.sync.dataCutoff)}</dd>
                    </div>
                    <div>
                      <dt>行情数量</dt>
                      <dd>{state.sync.rowCount.toLocaleString('zh-CN')} 条</dd>
                    </div>
                  </dl>
                </div>
              ) : (
                <p role="status">行情还没有准备好，请重试。</p>
              )}
              <div className="onboarding-actions">
                <AsyncActionButton
                  type="button"
                  pending={pendingAction === 'complete'}
                  disabled={busy || state.sync?.status !== 'verified'}
                  onClick={() => {
                    setBusy(true);
                    setPendingAction('complete');
                    setActionError(false);
                    void api
                      .complete(state.instrument?.symbol ?? instrument.symbol)
                      .then((next) => {
                        setState(next);
                        onCompleted(
                          next.instrument ?? state.instrument ?? instrument,
                        );
                      })
                      .catch(() => setActionError(true))
                      .finally(() => {
                        setBusy(false);
                        setPendingAction(null);
                      });
                  }}
                >
                  打开行情
                </AsyncActionButton>
              </div>
            </>
          ) : null}

          {state.error !== null ? (
            <section className="onboarding-inline-error" role="alert">
              <strong>暂时无法加载行情</strong>
              <p>请重试。</p>
            </section>
          ) : null}
          {actionError ? (
            <p className="onboarding-action-error" role="alert">
              操作失败，请重试。
            </p>
          ) : null}
        </div>
      </section>
    </main>
  );
}

export function OnboardingGate({
  api = onboardingApi,
  children,
  onDiagnostics,
}: {
  readonly api?: OnboardingApi;
  readonly children: ReactNode;
  readonly onDiagnostics?: () => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const selectInstrument = useMarketStore((state) => state.selectInstrument);
  const [state, setState] = useState<OnboardingState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [recoveryBusy, setRecoveryBusy] = useState(false);
  const [recoveryFailed, setRecoveryFailed] = useState(false);
  const requestIdRef = useRef(0);

  const load = useCallback(() => {
    const controller = new AbortController();
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    setLoading(true);
    void api
      .getState(controller.signal)
      .then((next) => {
        if (requestIdRef.current === requestId) {
          setState(next);
          setLoadFailed(false);
        }
      })
      .catch(() => {
        if (requestIdRef.current === requestId && !controller.signal.aborted) {
          setLoadFailed(true);
        }
      })
      .finally(() => {
        if (requestIdRef.current === requestId) setLoading(false);
      });
    return () => controller.abort();
  }, [api]);

  useEffect(() => load(), [load]);

  useEffect(() => {
    if (
      (state?.status === 'completed' || state?.demoMode === true) &&
      state.instrument !== null
    ) {
      selectInstrument({
        symbol: state.instrument.symbol,
        name: state.instrument.name,
        exchange: state.instrument.exchange,
        instrumentKind: state.instrument.instrumentKind,
      });
    }
  }, [selectInstrument, state]);

  function openMarket(instrument: OnboardingInstrument) {
    selectInstrument({
      symbol: instrument.symbol,
      name: instrument.name,
      exchange: instrument.exchange,
      instrumentKind: instrument.instrumentKind,
    });
    void navigate('/market', { replace: true });
  }

  if (loading && !loadFailed) return <LoadingCard />;
  if (loadFailed || state === null) {
    return (
      <LoadError
        busy={loading}
        onRetry={() => load()}
        onDiagnostics={onDiagnostics ?? (() => undefined)}
      />
    );
  }
  const advancedMode =
    location.pathname === '/settings' &&
    state.error?.code === 'advanced_configuration_required';
  if (advancedMode) {
    return (
      <OnboardingDemoContext.Provider value={false}>
        <div className="onboarding-notice-frame">
          <div className="onboarding-demo-banner" role="status">
            <span>
              高级数据设置 · 可在此配置 Tushare Token 或通达信本地 vipdoc 目录
            </span>
            <AsyncActionButton
              type="button"
              pending={recoveryBusy}
              disabled={recoveryBusy}
              onClick={() => {
                setRecoveryBusy(true);
                setRecoveryFailed(false);
                void api
                  .saveProgress({ currentStep: 'data_preparation' })
                  .then((next) => {
                    setState(next);
                    void navigate('/market', { replace: true });
                  })
                  .catch(() => setRecoveryFailed(true))
                  .finally(() => setRecoveryBusy(false));
              }}
            >
              返回首次设置
            </AsyncActionButton>
            {recoveryFailed ? (
              <span role="alert">暂时无法返回，请重试。</span>
            ) : null}
          </div>
          {children}
        </div>
      </OnboardingDemoContext.Provider>
    );
  }
  if (
    state.status === 'completed' ||
    state.currentStep === 'completed' ||
    state.demoMode
  ) {
    return (
      <OnboardingDemoContext.Provider value={state.demoMode}>
        {state.demoMode ? (
          <div className="onboarding-notice-frame">
            <div className="onboarding-demo-banner" role="status">
              <span>演示模式 · 当前显示示例数据</span>
              <AsyncActionButton
                type="button"
                pending={recoveryBusy}
                disabled={recoveryBusy}
                onClick={() => {
                  setRecoveryBusy(true);
                  setRecoveryFailed(false);
                  void api
                    .runAction('exit_demo')
                    .then((next) => {
                      setState(next);
                      void navigate('/market', { replace: true });
                    })
                    .catch(() => setRecoveryFailed(true))
                    .finally(() => setRecoveryBusy(false));
                }}
              >
                设置真实行情
              </AsyncActionButton>
              {recoveryFailed ? (
                <span role="alert">退出演示失败，请重试。</span>
              ) : null}
            </div>
            {children}
          </div>
        ) : (
          children
        )}
      </OnboardingDemoContext.Provider>
    );
  }
  return (
    <OnboardingWizard
      key={state.revision}
      api={api}
      initialState={state}
      onCompleted={(instrument) => {
        openMarket(instrument);
        setState((current) =>
          current === null
            ? current
            : {
                ...current,
                status: 'completed',
                currentStep: 'completed',
                instrument,
              },
        );
      }}
      onDemo={(instrument, next) => {
        setState(next);
        openMarket(instrument);
      }}
      onAdvanced={(next) => {
        setState(next);
        void navigate('/settings?focus=data-sources');
      }}
    />
  );
}
