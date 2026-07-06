import { useEffect, useRef, useState } from 'react';
import { Link, useInRouterContext } from 'react-router-dom';

import type {
  MarketApi,
  MarketInstrument,
  MarketPoolSummary,
} from '../../market/marketApi';
import type { BacktestScope } from '../backtestApi';

export type ScopeStepProps = {
  readonly scope: BacktestScope;
  readonly pools: readonly MarketPoolSummary[];
  readonly marketApiClient: Pick<MarketApi, 'searchInstruments'>;
  readonly onChange: (scope: BacktestScope) => void;
};

export function ScopeStep({
  scope,
  pools,
  marketApiClient,
  onChange,
}: ScopeStepProps) {
  const inRouter = useInRouterContext();
  const [query, setQuery] = useState(
    scope.kind === 'single' ? scope.symbol : '',
  );
  const [matches, setMatches] = useState<readonly MarketInstrument[]>([]);
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [searchError, setSearchError] = useState(false);
  const searchController = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const preset = pools.filter((pool) => pool.kind === 'preset');
  const custom = pools.filter((pool) => pool.kind === 'custom');

  useEffect(() => () => searchController.current?.abort(), []);

  async function search() {
    searchController.current?.abort();
    const controller = new AbortController();
    searchController.current = controller;
    const currentGeneration = ++generation.current;
    setSearching(true);
    setSearchError(false);
    setSearched(false);
    try {
      const results = (
        await marketApiClient.searchInstruments({
          query,
          limit: 10,
          signal: controller.signal,
        })
      ).filter(
        (item) =>
          item.instrumentKind === 'stock' && item.listingStatus === 'listed',
      );
      if (
        !controller.signal.aborted &&
        generation.current === currentGeneration
      ) {
        setMatches(results);
        setSearched(true);
      }
    } catch {
      if (
        !controller.signal.aborted &&
        generation.current === currentGeneration
      )
        setSearchError(true);
    } finally {
      if (generation.current === currentGeneration) setSearching(false);
    }
  }

  function chooseKind(kind: BacktestScope['kind']) {
    if (kind === 'single') return onChange({ kind, symbol: query });
    const pool = (kind === 'preset' ? preset : custom)[0];
    if (pool === undefined) return;
    if (kind === 'preset' && pool.snapshotId !== null)
      onChange({ kind, poolId: pool.poolId, snapshotId: pool.snapshotId });
    if (kind === 'custom' && pool.revision !== null)
      onChange({ kind, poolId: pool.poolId, revision: pool.revision });
  }

  function choosePool(poolId: string) {
    const pool = pools.find((item) => item.poolId === poolId);
    if (pool?.kind === 'preset' && pool.snapshotId !== null)
      onChange({ kind: 'preset', poolId, snapshotId: pool.snapshotId });
    if (pool?.kind === 'custom' && pool.revision !== null)
      onChange({ kind: 'custom', poolId, revision: pool.revision });
  }

  return (
    <section className="backtest-step" aria-labelledby="backtest-scope-heading">
      <h3 id="backtest-scope-heading" tabIndex={-1}>
        2. 范围
      </h3>
      <fieldset className="scope-kind">
        <legend>回测范围</legend>
        <label>
          <input
            type="radio"
            name="scope-kind"
            checked={scope.kind === 'single'}
            onChange={() => chooseKind('single')}
          />
          单只证券
        </label>
        <label>
          <input
            type="radio"
            name="scope-kind"
            checked={scope.kind === 'preset'}
            disabled={preset.length === 0}
            onChange={() => chooseKind('preset')}
          />
          预设股票池
        </label>
        <label>
          <input
            type="radio"
            name="scope-kind"
            checked={scope.kind === 'custom'}
            disabled={custom.length === 0}
            onChange={() => chooseKind('custom')}
          />
          自定义股票池
        </label>
      </fieldset>
      {preset.length === 0 && custom.length === 0 ? (
        <p className="field-help">
          尚无可用股票池，可先回测单只证券，或{' '}
          {inRouter ? (
            <Link to="/market">前往行情工作区创建股票池</Link>
          ) : (
            <a href="/market">前往行情工作区创建股票池</a>
          )}
          。
        </p>
      ) : null}
      {scope.kind === 'single' ? (
        <>
          <label>
            证券
            <input
              value={query}
              placeholder="代码或名称"
              onChange={(event) => {
                searchController.current?.abort();
                generation.current += 1;
                setSearching(false);
                setQuery(event.target.value);
                setMatches([]);
                setSearched(false);
                setSearchError(false);
                onChange({ kind: 'single', symbol: '' });
              }}
            />
          </label>
          <button
            type="button"
            className="secondary-action"
            disabled={query.trim() === '' || searching}
            onClick={() => void search()}
          >
            {searching ? '搜索中…' : '搜索证券'}
          </button>
          {searchError ? (
            <p role="alert">证券搜索暂时不可用，请稍后重试。</p>
          ) : searched && matches.length === 0 ? (
            <p role="status">未找到可回测的已上市 A 股</p>
          ) : null}
          {matches.length > 0 ? (
            <ul className="instrument-matches">
              {matches.map((item) => (
                <li key={item.symbol}>
                  <button
                    type="button"
                    onClick={() => {
                      setQuery(item.symbol);
                      onChange({ kind: 'single', symbol: item.symbol });
                    }}
                  >
                    {item.symbol} · {item.name}
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </>
      ) : (
        <label>
          股票池
          <select
            value={scope.poolId}
            onChange={(event) => choosePool(event.target.value)}
          >
            {(scope.kind === 'preset' ? preset : custom).map((pool) => (
              <option key={pool.poolId} value={pool.poolId}>
                {pool.name} · {pool.memberCount} 只
              </option>
            ))}
          </select>
        </label>
      )}
    </section>
  );
}
