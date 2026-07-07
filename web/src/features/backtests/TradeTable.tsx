/* eslint-disable jsx-a11y/no-noninteractive-tabindex -- Scrollable table regions need keyboard focus. */
import { useMemo, useState } from 'react';

import type { BacktestTrade } from './backtestApi';

type SortKey = 'symbol' | 'entryFillAt' | 'result';

function result(trade: BacktestTrade) {
  return trade.realized ? trade.netReturn : trade.floatingReturn;
}

export function TradeTable({
  items,
  onReplay,
}: {
  readonly items: readonly BacktestTrade[];
  readonly onReplay?: (trade: BacktestTrade) => void;
}) {
  const [sort, setSort] = useState<SortKey>('symbol');
  const sorted = useMemo(
    () =>
      [...items].sort((left, right) => {
        if (sort === 'entryFillAt')
          return left.entryFillAt.localeCompare(right.entryFillAt);
        if (sort === 'result')
          return Number(result(right) ?? 0) - Number(result(left) ?? 0);
        return (
          left.symbol.localeCompare(right.symbol) ||
          left.ordinal - right.ordinal
        );
      }),
    [items, sort],
  );
  return (
    <section aria-label="交易明细当前页">
      <div className="table-toolbar">
        <strong>当前页排序</strong>
        <label>
          <span className="visually-hidden">排序字段</span>
          <select
            value={sort}
            onChange={(event) => setSort(event.currentTarget.value as SortKey)}
          >
            <option value="symbol">证券代码</option>
            <option value="entryFillAt">入场时间</option>
            <option value="result">收益率</option>
          </select>
        </label>
        <span>每页最多 100 条，不代表全局排序</span>
      </div>
      <div
        className="report-table-scroll"
        tabIndex={0}
        role="region"
        aria-label="可横向滚动的交易表"
      >
        <table>
          <thead>
            <tr>
              <th scope="col">证券</th>
              <th scope="col">入场成交</th>
              <th scope="col">退出/标记</th>
              <th scope="col">收益率</th>
              <th scope="col">持有</th>
              <th scope="col">成本与盈亏</th>
              {onReplay === undefined ? null : <th scope="col">回放</th>}
            </tr>
          </thead>
          <tbody>
            {sorted.map((trade) => (
              <tr key={`${trade.symbol}-${String(trade.ordinal)}`}>
                <th scope="row">{trade.symbol}</th>
                <td>
                  <time dateTime={trade.entryFillAt}>{trade.entryFillAt}</time>
                </td>
                <td>{trade.exitFillAt ?? trade.markAt ?? '开放'}</td>
                <td>{result(trade) ?? '不可计算'}</td>
                <td>
                  {trade.holdingBars} 根 / {trade.holdingDays} 天
                </td>
                <td>
                  <details className="trade-cost-bridge">
                    <summary>成本与盈亏桥接</summary>
                    <dl>
                      <div>
                        <dt>参考口径毛盈亏</dt>
                        <dd>{trade.referenceGrossPnl}</dd>
                      </div>
                      <div>
                        <dt>成交口径毛盈亏</dt>
                        <dd>{trade.fillGrossPnl}</dd>
                      </div>
                      <div>
                        <dt>买入佣金</dt>
                        <dd>{trade.buyCommission}</dd>
                      </div>
                      <div>
                        <dt>卖出佣金</dt>
                        <dd>{trade.sellCommission}</dd>
                      </div>
                      <div>
                        <dt>卖出印花税</dt>
                        <dd>{trade.sellTax}</dd>
                      </div>
                      <div>
                        <dt>滑点成本</dt>
                        <dd>{trade.slippageCost}</dd>
                      </div>
                      <div>
                        <dt>投入成本</dt>
                        <dd>{trade.investedCost}</dd>
                      </div>
                      <div>
                        <dt>净盈亏</dt>
                        <dd>{trade.netPnl ?? '开放仓位（未实现）'}</dd>
                      </div>
                      <div>
                        <dt>净收益率</dt>
                        <dd>{trade.netReturn ?? '开放仓位（未实现）'}</dd>
                      </div>
                      <div>
                        <dt>浮动盈亏</dt>
                        <dd>{trade.floatingPnl ?? '已实现（不适用）'}</dd>
                      </div>
                      <div>
                        <dt>浮动收益率</dt>
                        <dd>{trade.floatingReturn ?? '已实现（不适用）'}</dd>
                      </div>
                    </dl>
                  </details>
                </td>
                {onReplay === undefined ? null : (
                  <td>
                    <button
                      type="button"
                      className="secondary-action"
                      onClick={() => onReplay(trade)}
                    >
                      固定回放
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {items.length === 0 ? <p>当前页没有记录。</p> : null}
    </section>
  );
}
