import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useState } from 'react';

import { MarketChart } from './MarketChart';
import {
  isMarketNotFound,
  marketApi,
  type MarketApi,
  type MarketPoolDetail,
} from './marketApi';
import { MarketOperationsPanel } from './MarketOperationsPanel';
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
  readonly searchDebounceMs?: number;
  readonly workflowApi?: MarketWorkflowApi;
};

const periods: readonly { value: MarketPeriod; label: string }[] = [
  { value: '1d', label: '日线' },
  { value: '1w', label: '周线' },
  { value: '60m', label: '60 分钟' },
];

export function MarketPage({
  api = marketApi,
  searchDebounceMs,
  workflowApi = marketWorkflowApi,
}: MarketPageProps) {
  const readonlyDemo = useOnboardingDemoMode();
  const [selectedPool, setSelectedPool] = useState<MarketPoolDetail | null>(
    null,
  );
  const selectedInstrument = useMarketStore(
    (state) => state.selectedInstrument,
  );
  const selectedPoolId = useMarketStore((state) => state.selectedPoolId);
  const period = useMarketStore((state) => state.period);
  const adjustment = useMarketStore((state) => state.adjustment);
  const selectInstrument = useMarketStore((state) => state.selectInstrument);
  const selectPool = useMarketStore((state) => state.selectPool);
  const setPeriod = useMarketStore((state) => state.setPeriod);
  const setAdjustment = useMarketStore((state) => state.setAdjustment);

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

      <div className="market-terminal-grid">
        <aside className="market-terminal-left" aria-label="证券选择与股票池">
          <StockSearch
            api={api}
            debounceMs={searchDebounceMs}
            onSelect={(instrument) =>
              selectInstrument({
                symbol: instrument.symbol,
                name: instrument.name,
              })
            }
          />
          <StockPoolPanel
            api={api}
            selectedPoolId={selectedPoolId}
            onSelectPool={selectPool}
            onSelectInstrument={selectInstrument}
            onPoolDetail={setSelectedPool}
          />
        </aside>

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
              <div
                className="period-selector"
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
            </div>
          </div>

          <MarketChart
            bars={bars.data?.bars}
            isLoading={bars.isFetching && bars.data === undefined}
            errorMessage={errorMessage}
          />
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
    </article>
  );
}
