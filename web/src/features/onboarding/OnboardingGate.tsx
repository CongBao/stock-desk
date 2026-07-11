import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react';
import { useNavigate } from 'react-router-dom';

import { useMarketStore } from '../market/marketStore';
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

const stepLabels = ['欢迎', '准备数据', '选择证券', '同步完成'] as const;

function safeDate(value: string | null): string {
  if (value === null) return '由首次同步确定';
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
        <span className="panel-kicker">STOCK DESK / FIRST RUN</span>
        <h1>正在读取首次设置</h1>
        <p>马上就好，正在恢复上次的设置进度…</p>
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
        <span className="panel-kicker">STOCK DESK / SETUP RECOVERY</span>
        <h1>首次设置暂时无法读取</h1>
        <p>本地服务没有返回可用的设置状态。你的配置和密钥不会显示在这里。</p>
        <div className="onboarding-actions">
          <button type="button" disabled={busy} onClick={onRetry}>
            {busy ? '正在重试…' : '重试读取'}
          </button>
          <button className="secondary" type="button" onClick={onDiagnostics}>
            查看安全诊断
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
      {status === 'loading' ? <p role="status">正在搜索本地证券目录…</p> : null}
      {status === 'error' ? (
        <p role="alert">证券目录暂时不可用，请重试或保留默认上证指数。</p>
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
  readonly onDemo: (instrument: OnboardingInstrument) => void;
};

function OnboardingWizard({
  api,
  initialState,
  onCompleted,
  onDemo,
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
            : (items.find((item) => item.recommended && item.status === 'ready')
                ?.id ??
              items.find((item) => item.status === 'ready')?.id ??
              ''),
        );
      })
      .catch(() => {
        if (!controller.signal.aborted) setActionError(true);
      });
    return () => controller.abort();
  }, [api, state.currentStep]);

  const perform = useCallback(
    async (operation: () => Promise<OnboardingState>) => {
      setBusy(true);
      setActionError(false);
      try {
        setState(await operation());
      } catch {
        setActionError(true);
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  async function runAction(action: OnboardingAction) {
    setBusy(true);
    setActionError(false);
    try {
      const next = await api.runAction(action);
      setState(next);
      if (action === 'demo') onDemo(instrument);
    } catch {
      setActionError(true);
    } finally {
      setBusy(false);
    }
  }

  const selectedSource =
    sources.find((source) => source.id === selectedSourceId) ?? null;

  return (
    <main className="onboarding-shell">
      <section className="onboarding-card" aria-labelledby="onboarding-title">
        <header className="onboarding-header">
          <div>
            <span className="panel-kicker">STOCK DESK / FIRST RUN</span>
            <p>约 1 分钟 · 无需编程</p>
          </div>
          <span className="onboarding-local-badge">仅保存在本机</span>
        </header>
        <Stepper state={state} />

        <div className="onboarding-content">
          {state.currentStep === 'welcome' ? (
            <>
              <span className="onboarding-hero-icon" aria-hidden="true">
                ⌁
              </span>
              <h1 id="onboarding-title" ref={headingRef} tabIndex={-1}>
                欢迎使用 stock-desk
              </h1>
              <p className="onboarding-lead">
                我们会准备最基本的 A 股数据，并打开一张马上可用的 K 线图。
              </p>
              <ul className="onboarding-benefits">
                <li>自动选择无需密钥的数据源</li>
                <li>默认打开上证指数 000001.SS</li>
                <li>稍后可在设置中更换来源</li>
              </ul>
              <div className="onboarding-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void perform(() =>
                      api.saveProgress({ currentStep: 'data_preparation' }),
                    )
                  }
                >
                  开始设置
                </button>
                <button
                  className="secondary"
                  type="button"
                  disabled={busy}
                  onClick={() => void runAction('demo')}
                >
                  先看只读演示
                </button>
              </div>
              <p className="onboarding-demo-note">
                演示不会完成设置；下次启动仍会回到这里。
              </p>
            </>
          ) : null}

          {state.currentStep === 'data_preparation' ? (
            <>
              <h1 id="onboarding-title" ref={headingRef} tabIndex={-1}>
                准备行情数据
              </h1>
              <p className="onboarding-lead">
                推荐来源不需要 API 密钥。stock-desk 只会在你确认后同步。
              </p>
              <fieldset className="onboarding-source-list">
                <legend>选择数据来源</legend>
                {sources.length === 0 && !actionError ? (
                  <p role="status">正在检测可用来源…</p>
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
                      <small>
                        {source.requiresToken ? '需要密钥' : '无需密钥'} ·
                        数据截至 {safeDate(source.dataCutoff)}
                      </small>
                    </span>
                  </label>
                ))}
              </fieldset>
              <div className="onboarding-actions">
                <button
                  type="button"
                  disabled={busy || selectedSource === null}
                  onClick={() =>
                    void perform(() =>
                      api.saveProgress({
                        currentStep: 'instrument_selection',
                        sourceId: selectedSourceId,
                      }),
                    )
                  }
                >
                  使用此来源并继续
                </button>
                <button
                  className="secondary"
                  type="button"
                  disabled={busy}
                  onClick={() => void runAction('advanced')}
                >
                  高级数据设置
                </button>
              </div>
            </>
          ) : null}

          {state.currentStep === 'instrument_selection' ? (
            <>
              <h1 id="onboarding-title" ref={headingRef} tabIndex={-1}>
                选择打开后的第一只证券
              </h1>
              <p className="onboarding-lead">
                不确定选什么时，保留默认的上证指数即可。
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
                <button
                  type="button"
                  disabled={
                    busy || (state.source?.id ?? selectedSourceId).length === 0
                  }
                  onClick={() =>
                    void perform(() =>
                      api.synchronize({
                        sourceId: state.source?.id ?? selectedSourceId,
                        symbol: instrument.symbol,
                      }),
                    )
                  }
                >
                  同步并继续
                </button>
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
                  ? '数据已准备好'
                  : '正在确认数据'}
              </h1>
              {state.sync?.status === 'verified' ? (
                <div className="onboarding-ready-summary">
                  <p>
                    <strong>{state.instrument?.name ?? instrument.name}</strong>{' '}
                    {state.instrument?.symbol ?? instrument.symbol}
                  </p>
                  <dl>
                    <div>
                      <dt>数据来源</dt>
                      <dd>{state.source?.label ?? state.sync.providerId}</dd>
                    </div>
                    <div>
                      <dt>数据截至</dt>
                      <dd>{safeDate(state.sync.dataCutoff)}</dd>
                    </div>
                    <div>
                      <dt>已验证数据</dt>
                      <dd>{state.sync.rowCount.toLocaleString('zh-CN')} 条</dd>
                    </div>
                  </dl>
                </div>
              ) : (
                <p role="status">同步尚未通过验证，请使用下方恢复操作。</p>
              )}
              <div className="onboarding-actions">
                <button
                  type="button"
                  disabled={busy || state.sync?.status !== 'verified'}
                  onClick={() => {
                    setBusy(true);
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
                      .finally(() => setBusy(false));
                  }}
                >
                  进入行情工作区
                </button>
              </div>
            </>
          ) : null}

          {state.error !== null ? (
            <section className="onboarding-inline-error" role="alert">
              <strong>数据准备没有完成</strong>
              <p>你可以安全重试、更换来源，或查看不含隐私信息的诊断。</p>
              <div>
                {state.error.actions.includes('retry') ? (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void runAction('retry')}
                  >
                    重试
                  </button>
                ) : null}
                {state.error.actions.includes('switch_provider') ? (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void runAction('switch_provider')}
                  >
                    更换数据源
                  </button>
                ) : null}
                {state.error.actions.includes('advanced') ? (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void runAction('advanced')}
                  >
                    高级设置
                  </button>
                ) : null}
                {state.error.actions.includes('demo') ? (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void runAction('demo')}
                  >
                    进入只读演示
                  </button>
                ) : null}
              </div>
            </section>
          ) : null}
          {actionError ? (
            <p className="onboarding-action-error" role="alert">
              操作没有完成。请检查本地服务后重试；详细技术信息不会显示在此页面。
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
  const selectInstrument = useMarketStore((state) => state.selectInstrument);
  const [state, setState] = useState<OnboardingState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [demoMode, setDemoMode] = useState(false);
  const requestIdRef = useRef(0);

  const load = useCallback(() => {
    const controller = new AbortController();
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    setLoading(true);
    setLoadFailed(false);
    void api
      .getState(controller.signal)
      .then((next) => {
        if (requestIdRef.current === requestId) setState(next);
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
    if (state?.status === 'completed' && state.instrument !== null) {
      selectInstrument({
        symbol: state.instrument.symbol,
        name: state.instrument.name,
      });
    }
  }, [selectInstrument, state]);

  function openMarket(instrument: OnboardingInstrument) {
    selectInstrument({ symbol: instrument.symbol, name: instrument.name });
    void navigate('/market', { replace: true });
  }

  if (loading) return <LoadingCard />;
  if (loadFailed || state === null) {
    return (
      <LoadError
        busy={loading}
        onRetry={() => load()}
        onDiagnostics={onDiagnostics ?? (() => undefined)}
      />
    );
  }
  if (
    state.status === 'completed' ||
    state.currentStep === 'completed' ||
    demoMode
  ) {
    return (
      <OnboardingDemoContext.Provider value={demoMode}>
        {demoMode ? (
          <div className="onboarding-demo-banner" role="status">
            只读演示 · 设置尚未完成，重新启动后会继续首次向导
          </div>
        ) : null}
        {children}
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
      onDemo={(instrument) => {
        openMarket(instrument);
        setDemoMode(true);
      }}
    />
  );
}
