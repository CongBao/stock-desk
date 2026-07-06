/* eslint-disable jsx-a11y/no-noninteractive-tabindex -- Scrollable table regions need keyboard focus. */
import { useEffect, useState } from 'react';

import type { BacktestGroup, BacktestReportApi } from './backtestApi';

const dimensions = [
  ['symbol', '按股票'],
  ['entry_month', '按月'],
  ['entry_year', '按年'],
] as const;

export function GroupedMetrics({
  api,
  disclaimer,
  runId,
}: {
  readonly api: BacktestReportApi;
  readonly disclaimer: string;
  readonly runId: string;
}) {
  const [dimension, setDimension] =
    useState<BacktestGroup['dimension']>('symbol');
  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<readonly (string | null)[]>([]);
  const [items, setItems] = useState<readonly BacktestGroup[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(false);
    void api
      .getGroups(runId, dimension, { cursor, signal: controller.signal })
      .then((page) => {
        if (controller.signal.aborted) return;
        setItems(page.items);
        setNextCursor(page.nextCursor);
        setLoading(false);
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setItems([]);
        setNextCursor(null);
        setLoading(false);
        setError(true);
      });
    return () => controller.abort();
  }, [api, cursor, dimension, runId]);

  function changeDimension(value: BacktestGroup['dimension']) {
    setDimension(value);
    setCursor(null);
    setHistory([]);
  }

  return (
    <section
      className="grouped-metrics"
      aria-labelledby="grouped-metrics-title"
    >
      <header>
        <div>
          <span className="panel-kicker">GROUPED SAMPLES</span>
          <h4 id="grouped-metrics-title">分组表现</h4>
        </div>
        <div role="radiogroup" aria-label="分组维度">
          {dimensions.map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={dimension === value}
              onClick={() => changeDimension(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </header>
      <p className="backtest-disclaimer">{disclaimer}</p>
      {loading ? (
        <p role="status">正在读取分组当前页…</p>
      ) : error ? (
        <p role="alert">分组数据读取失败。</p>
      ) : items.length === 0 ? (
        <p>该维度没有已实现样本。</p>
      ) : (
        <div
          className="report-table-scroll"
          tabIndex={0}
          role="region"
          aria-label="可横向滚动的分组表现表"
        >
          <table>
            <thead>
              <tr>
                <th scope="col">分组</th>
                <th scope="col">样本</th>
                <th scope="col">胜率</th>
                <th scope="col">平均净收益</th>
                <th scope="col">净盈亏</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={`${item.dimension}-${item.key}`}>
                  <th scope="row">{item.key}</th>
                  <td>
                    {item.realizedCount} / {item.realizedDenominator}
                  </td>
                  <td>{item.winRate}</td>
                  <td>{item.meanNetReturn}</td>
                  <td>{item.netPnlTotal}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="cursor-controls" role="group" aria-label="分组当前页翻页">
        <button
          type="button"
          disabled={history.length === 0 || loading}
          onClick={() => {
            const previous = history.at(-1) ?? null;
            setHistory((current) => current.slice(0, -1));
            setCursor(previous);
          }}
        >
          上一页
        </button>
        <button
          type="button"
          disabled={nextCursor === null || loading}
          onClick={() => {
            if (nextCursor === null) return;
            setHistory((current) => [...current, cursor]);
            setCursor(nextCursor);
          }}
        >
          下一页
        </button>
      </div>
    </section>
  );
}
