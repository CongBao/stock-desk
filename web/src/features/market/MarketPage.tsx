import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useCallback, useEffect, useRef, useState } from 'react';

import { MarketChart } from './MarketChart';
import {
  isMarketNotFound,
  marketApi,
  type MarketApi,
  type MarketPoolDetail,
} from './marketApi';
import { MarketOperationsPanel } from './MarketOperationsPanel';
import { MarketInstrumentRail } from './MarketInstrumentRail';
import {
  marketNavigationApi,
  prependRecentInstrument,
  type MarketNavigationApi,
  type MarketNavigationInstrument,
  type MarketNavigationState,
} from './marketNavigationApi';
import { marketWorkflowApi, type MarketWorkflowApi } from './marketWorkflowApi';
import {
  useMarketStore,
  type MarketAdjustment,
  type MarketPeriod,
} from './marketStore';
import { ProvenancePanel } from './ProvenancePanel';
import { StockPoolPanel } from './StockPoolPanel';
import { StockSearch } from './StockSearch';
import { useOnboardingDemoMode } from '../onboarding/demoMode';

type MarketPageProps = {
  readonly api?: MarketApi;
  readonly navigationApi?: MarketNavigationApi;
  readonly searchDebounceMs?: number;
  readonly workflowApi?: MarketWorkflowApi;
};

const periods: readonly { value: MarketPeriod; label: string }[] = [
  { value: '1d', label: '日线' },
  { value: '1w', label: '周线' },
  { value: '60m', label: '60 分钟' },
];

const EMPTY_NAVIGATION: MarketNavigationState = {
  schemaVersion: 1,
  revision: 0,
  watchlist: [],
  recent: [],
  notice: null,
};

function asNavigationInstrument(instrument: {
  readonly symbol: string;
  readonly name: string;
  readonly instrumentKind?: MarketNavigationInstrument['instrumentKind'];
}): MarketNavigationInstrument {
  return {
    symbol: instrument.symbol,
    name: instrument.name,
    instrumentKind: instrument.instrumentKind ?? 'stock',
  };
}

export function MarketPage({
  api = marketApi,
  navigationApi = marketNavigationApi,
  searchDebounceMs,
  workflowApi = marketWorkflowApi,
}: MarketPageProps) {
  const readonlyDemo = useOnboardingDemoMode();
  const queryClient = useQueryClient();
  const [selectedPool, setSelectedPool] = useState<MarketPoolDetail | null>(
    null,
  );
  const [isPoolWorkflowOpen, setIsPoolWorkflowOpen] = useState(false);
  const [navigationDraft, setNavigationDraft] =
    useState<MarketNavigationState | null>(null);
  const [navigationMessage, setNavigationMessage] = useState<string | null>(
    null,
  );
  const [isNarrowRail, setIsNarrowRail] = useState(() =>
    typeof window.matchMedia === 'function'
      ? window.matchMedia('(max-width: 900px)').matches
      : false,
  );
  const [isRailCollapsed, setIsRailCollapsed] = useState(isNarrowRail);
  const poolWorkflowButtonRef = useRef<HTMLButtonElement>(null);
  const poolWorkflowCloseRef = useRef<HTMLButtonElement>(null);
  const marketRailToggleRef = useRef<HTMLButtonElement>(null);
  const selectedInstrument = useMarketStore(
    (state) => state.selectedInstrument,
  );
  const selectedPoolId = useMarketStore((state) => state.selectedPoolId);
  const period = useMarketStore((state) => state.period);
  const adjustment = useMarketStore((state) => state.adjustment);
  const zoom = useMarketStore((state) => state.zoom);
  const selectInstrument = useMarketStore((state) => state.selectInstrument);
  const selectPool = useMarketStore((state) => state.selectPool);
  const setPeriod = useMarketStore((state) => state.setPeriod);
  const setAdjustment = useMarketStore((state) => state.setAdjustment);
  const setZoom = useMarketStore((state) => state.setZoom);

  const navigation = useQuery({
    queryKey: ['market', 'navigation'],
    queryFn: ({ signal }) => navigationApi.get({ signal }),
    retry: false,
  });
  const navigationState =
    navigationDraft ?? navigation.data ?? EMPTY_NAVIGATION;

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const query = window.matchMedia('(max-width: 900px)');
    const handleChange = (event: MediaQueryListEvent) => {
      setIsNarrowRail(event.matches);
      if (event.matches) setIsRailCollapsed(true);
    };
    query.addEventListener('change', handleChange);
    return () => query.removeEventListener('change', handleChange);
  }, []);

  useEffect(() => {
    if (!isPoolWorkflowOpen) return;
    poolWorkflowCloseRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      setIsPoolWorkflowOpen(false);
      window.setTimeout(() => poolWorkflowButtonRef.current?.focus(), 0);
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isPoolWorkflowOpen]);

  useEffect(() => {
    if (!isNarrowRail || isRailCollapsed) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        setIsRailCollapsed(true);
        window.setTimeout(() => marketRailToggleRef.current?.focus(), 0);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isNarrowRail, isRailCollapsed]);

  const persistNavigation = useCallback(
    async (
      next: Pick<MarketNavigationState, 'watchlist' | 'recent'>,
      expectedRevision: number,
    ) => {
      const optimistic: MarketNavigationState = {
        schemaVersion: 1,
        revision: expectedRevision,
        watchlist: next.watchlist,
        recent: next.recent,
        notice: null,
      };
      setNavigationDraft(optimistic);
      setNavigationMessage(null);
      try {
        const saved = await navigationApi.put(
          {
            expectedRevision,
            watchlist: next.watchlist,
            recent: next.recent,
          },
          {},
        );
        queryClient.setQueryData(['market', 'navigation'], saved);
        setNavigationDraft(null);
      } catch {
        setNavigationMessage('自选与最近访问暂未同步，请重试。');
      }
    },
    [navigationApi, queryClient],
  );

  const chooseInstrument = useCallback(
    (instrument: MarketNavigationInstrument) => {
      selectInstrument({
        symbol: instrument.symbol,
        name: instrument.name,
        instrumentKind: instrument.instrumentKind,
      });
      void persistNavigation(
        {
          watchlist: navigationState.watchlist,
          recent: prependRecentInstrument(navigationState.recent, instrument),
        },
        navigationState.revision,
      );
    },
    [navigationState, persistNavigation, selectInstrument],
  );

  const addToWatchlist = useCallback(
    (instrument: MarketNavigationInstrument) => {
      if (
        navigationState.watchlist.some(
          (item) => item.symbol === instrument.symbol,
        )
      ) {
        return;
      }
      void persistNavigation(
        {
          watchlist: [...navigationState.watchlist, instrument],
          recent: navigationState.recent,
        },
        navigationState.revision,
      );
    },
    [navigationState, persistNavigation],
  );

  const removeFromWatchlist = useCallback(
    (instrument: MarketNavigationInstrument) => {
      void persistNavigation(
        {
          watchlist: navigationState.watchlist.filter(
            (item) => item.symbol !== instrument.symbol,
          ),
          recent: navigationState.recent,
        },
        navigationState.revision,
      );
    },
    [navigationState, persistNavigation],
  );

  const bars = useQuery({
    queryKey: [
      'market',
      'bars',
      selectedInstrument?.symbol ?? null,
      period,
      adjustment,
    ],
    enabled: selectedInstrument !== null,
    queryFn: ({ signal }) => {
      if (selectedInstrument === null)
        throw new Error('Instrument selection is missing');
      return api.getBars({
        symbol: selectedInstrument.symbol,
        period,
        adjustment,
        signal,
      });
    },
  });
  const isCacheMiss = bars.isError && isMarketNotFound(bars.error);
  const errorMessage = bars.isError
    ? isCacheMiss
      ? '本地暂无缓存：当前周期与复权组合尚未落盘。'
      : '行情数据读取失败，请检查本地服务或响应协议。'
    : undefined;

  return (
    <article className="market-page market-terminal-page">
      <header className="page-heading market-heading">
        <div>
          <span className="page-kicker">MARKET / LOCAL CACHE</span>
          <h2 data-page-heading tabIndex={-1}>
            行情工作区
          </h2>
          <p>搜索或从股票池选择证券，查看可追溯的本地 K 线与成交量。</p>
        </div>
        <span className="release-badge">v0.2.0 · 行情数据</span>
      </header>

      <section
        className="market-search-hero"
        aria-label="搜索并选择证券"
        data-guidance-target="market-search"
      >
        <div>
          <span className="panel-kicker">FIND INSTRUMENT</span>
          <p>输入代码、中文名或拼音，选择后立即加载真实 K 线。</p>
        </div>
        <StockSearch
          api={api}
          focusOnMount
          debounceMs={searchDebounceMs}
          onSelect={(instrument) =>
            chooseInstrument(asNavigationInstrument(instrument))
          }
        />
      </section>

      {navigation.isError || navigationMessage !== null ? (
        <div className="market-navigation-status" role="alert">
          <span>
            {navigationMessage ?? '自选与最近访问暂不可用，行情查看仍可继续。'}
          </span>
          <button
            type="button"
            onClick={() => {
              setNavigationDraft(null);
              setNavigationMessage(null);
              void navigation.refetch();
            }}
          >
            重试同步
          </button>
        </div>
      ) : navigationState.notice === null ? null : (
        <p className="market-navigation-status" role="status">
          已安全重置无法读取的自选与最近访问。
        </p>
      )}

      <div
        className="market-terminal-grid"
        data-market-rail-collapsed={isRailCollapsed}
      >
        <div
          className="guidance-anchor-contents"
          data-guidance-target="market-watchlist"
        >
          <MarketInstrumentRail
            collapsed={isRailCollapsed}
            onAdd={addToWatchlist}
            onRemove={removeFromWatchlist}
            onSelect={chooseInstrument}
            onToggle={() => setIsRailCollapsed((collapsed) => !collapsed)}
            recent={navigationState.recent}
            selectedSymbol={selectedInstrument?.symbol ?? null}
            toggleRef={marketRailToggleRef}
            watchlist={navigationState.watchlist}
          />
        </div>

        <section className="market-terminal-center" aria-label="行情图表工作区">
          <div className="market-command-bar">
            <div className="selected-instrument" aria-live="polite">
              <span>当前证券</span>
              {selectedInstrument === null ? (
                <strong>尚未选择</strong>
              ) : (
                <strong>
                  {selectedInstrument.name} · {selectedInstrument.symbol}
                </strong>
              )}
            </div>
            <div className="market-control-row">
              <button
                ref={poolWorkflowButtonRef}
                className="market-pool-entry"
                type="button"
                onClick={() => setIsPoolWorkflowOpen(true)}
              >
                <svg
                  aria-hidden="true"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                >
                  <rect x="4" y="4" width="6" height="6" rx="1" />
                  <rect x="14" y="4" width="6" height="6" rx="1" />
                  <rect x="4" y="14" width="6" height="6" rx="1" />
                  <rect x="14" y="14" width="6" height="6" rx="1" />
                </svg>
                打开股票池
              </button>
              <div
                className="period-selector"
                data-guidance-target="market-period"
                role="radiogroup"
                aria-label="K 线周期"
              >
                {periods.map((item) => (
                  <button
                    key={item.value}
                    type="button"
                    role="radio"
                    aria-checked={period === item.value}
                    onClick={() => setPeriod(item.value)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
              <label className="adjustment-selector">
                <span>复权方式</span>
                <select
                  aria-label="复权方式"
                  value={adjustment}
                  onChange={(event) =>
                    setAdjustment(event.currentTarget.value as MarketAdjustment)
                  }
                >
                  <option value="none">不复权</option>
                  <option value="qfq">前复权</option>
                  <option value="hfq">后复权</option>
                </select>
              </label>
              {selectedInstrument ===
              null ? null : navigationState.watchlist.some(
                  (item) => item.symbol === selectedInstrument.symbol,
                ) ? (
                <button
                  className="market-watchlist-action"
                  type="button"
                  onClick={() =>
                    removeFromWatchlist(
                      asNavigationInstrument(selectedInstrument),
                    )
                  }
                >
                  移出自选
                </button>
              ) : (
                <button
                  className="market-watchlist-action"
                  type="button"
                  onClick={() =>
                    addToWatchlist(asNavigationInstrument(selectedInstrument))
                  }
                >
                  加入自选
                </button>
              )}
            </div>
          </div>

          <div
            className="guidance-anchor-contents"
            data-guidance-target="market-chart"
          >
            <MarketChart
              bars={bars.data?.bars}
              isLoading={bars.isFetching && bars.data === undefined}
              errorMessage={errorMessage}
              initialZoom={zoom}
              onZoomChange={setZoom}
            />
          </div>
          {isCacheMiss ? (
            <div className="cache-miss-guidance" role="note">
              <p>此页面只读取本地缓存，不会在浏览时静默访问外部行情源。</p>
              <Link to="/settings">查看设置与数据入口</Link>
            </div>
          ) : null}
        </section>

        <aside
          className="market-terminal-right"
          aria-label="数据证据与快捷操作"
        >
          <ProvenancePanel data={bars.data} />
          {readonlyDemo ? (
            <section
              className="market-quick-actions"
              aria-labelledby="readonly-demo-title"
            >
              <span className="panel-kicker">READ ONLY</span>
              <h3 id="readonly-demo-title">只读演示</h3>
              <p>
                演示模式只允许浏览行情，不会更新数据、保存股票池或完成首次设置。
              </p>
            </section>
          ) : (
            <>
              <MarketOperationsPanel
                api={workflowApi}
                marketApiClient={api}
                onPoolDeleted={() => {
                  setSelectedPool(null);
                  selectPool(null);
                }}
                selectedInstrument={selectedInstrument}
                selectedPool={
                  selectedPool === null
                    ? null
                    : {
                        id: selectedPool.poolId,
                        name: selectedPool.name,
                        symbols: selectedPool.members.map(
                          (member) => member.symbol,
                        ),
                        kind: selectedPool.kind,
                        revision: selectedPool.revision,
                      }
                }
                period={period}
                adjustment={adjustment}
              />
              <section
                className="market-quick-actions"
                aria-labelledby="quick-actions-title"
              >
                <span className="panel-kicker">ACTIONS</span>
                <h3 id="quick-actions-title">快捷操作</h3>
                <Link to="/settings">数据源与设置</Link>
                <Link to="/tasks">查看更新任务</Link>
                <p>
                  可在本页明确启动目录或行情更新；浏览图表不会静默访问外部数据源。
                </p>
              </section>
            </>
          )}
        </aside>
      </div>

      {isPoolWorkflowOpen ? (
        <div className="market-pool-backdrop" role="presentation">
          <section
            className="market-pool-workflow"
            role="dialog"
            aria-modal="true"
            aria-label="股票池独立流程"
          >
            <header>
              <div>
                <span className="panel-kicker">STOCK POOL WORKFLOW</span>
                <h2>选择或管理股票池</h2>
              </div>
              <button
                ref={poolWorkflowCloseRef}
                type="button"
                aria-label="关闭股票池"
                onClick={() => {
                  setIsPoolWorkflowOpen(false);
                  window.setTimeout(
                    () => poolWorkflowButtonRef.current?.focus(),
                    0,
                  );
                }}
              >
                ×
              </button>
            </header>
            <StockPoolPanel
              api={api}
              selectedPoolId={selectedPoolId}
              onSelectPool={selectPool}
              onSelectInstrument={(instrument) => {
                chooseInstrument(asNavigationInstrument(instrument));
                setIsPoolWorkflowOpen(false);
              }}
              onPoolDetail={setSelectedPool}
            />
          </section>
        </div>
      ) : null}
    </article>
  );
}
