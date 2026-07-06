import { useEffect, useState } from 'react';

import { MarketChart, type FormulaChartLayer } from '../market/MarketChart';
import type {
  BacktestOrderEvent,
  BacktestReplay,
  BacktestReportApi,
  BacktestTrade,
} from './backtestApi';

const eventLabels: Record<BacktestOrderEvent['eventType'], string> = {
  IgnoredSignal: '信号已忽略',
  OpenTradeMarked: '开放仓位标记',
  OrderBlocked: '执行受阻',
  OrderCancelled: '委托已撤销',
  OrderFilled: '委托成交',
  OrderPending: '委托待执行',
  OrderUnfilled: '区间结束未成交',
};

function eventSummary(event: BacktestOrderEvent) {
  switch (event.eventType) {
    case 'OrderPending':
      return {
        at: event.signalAt,
        detail: `可执行时间 ${event.eligibleAt}`,
        reason: null,
        side: event.side,
      };
    case 'IgnoredSignal':
      return {
        at: event.at,
        detail: null,
        reason: event.reason,
        side: event.signal,
      };
    case 'OrderCancelled':
    case 'OrderBlocked':
      return {
        at: event.at,
        detail: null,
        reason: event.reason,
        side: event.side,
      };
    case 'OrderFilled':
      return {
        at: event.filledAt,
        detail: `${event.price} · ${String(event.quantity)} 股`,
        reason: null,
        side: event.side,
      };
    case 'OrderUnfilled':
      return {
        at: event.endedAt,
        detail: `可执行时间 ${event.eligibleAt}`,
        reason: event.reason,
        side: event.side,
      };
    case 'OpenTradeMarked':
      return {
        at: event.markAt,
        detail: `标记价 ${event.markPrice} · 浮动盈亏 ${event.floatingPnl}`,
        reason: null,
        side: null,
      };
  }
}

export function TradeReplay({
  api,
  runId,
  trade,
}: {
  readonly api: BacktestReportApi;
  readonly runId: string;
  readonly trade: BacktestTrade;
}) {
  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<readonly (string | null)[]>([]);
  const requestKey = `${runId}/${trade.symbol}/${String(trade.ordinal)}/${cursor ?? ''}`;
  const [loaded, setLoaded] = useState<{
    readonly key: string;
    readonly replay: BacktestReplay;
  } | null>(null);
  const [errorKey, setErrorKey] = useState<string | null>(null);

  useEffect(() => {
    setCursor(null);
    setHistory([]);
  }, [runId, trade.ordinal, trade.symbol]);

  useEffect(() => {
    const controller = new AbortController();
    setErrorKey(null);
    void api
      .getReplay(runId, trade.symbol, trade.ordinal, {
        cursor,
        signal: controller.signal,
      })
      .then((value) => {
        if (!controller.signal.aborted)
          setLoaded({ key: requestKey, replay: value });
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setErrorKey(requestKey);
        }
      });
    return () => controller.abort();
  }, [api, cursor, requestKey, runId, trade.ordinal, trade.symbol]);

  if (errorKey === requestKey)
    return (
      <p className="backtest-error-summary" role="alert">
        固定交易回放读取失败；未改用当前最新行情。
      </p>
    );
  if (loaded?.key !== requestKey)
    return <p role="status">正在重开固定行情、公式与成交证据…</p>;
  const replay = loaded.replay;

  const formula: FormulaChartLayer = {
    placement: 'subchart',
    timestamps: replay.bars.map((bar) => bar.timestamp),
    numericOutputs: replay.formula.numericOutputs,
    signals: replay.formula.signals,
  };
  return (
    <section className="trade-replay" aria-labelledby="trade-replay-title">
      <header>
        <div>
          <span className="panel-kicker">PINNED REPLAY</span>
          <h4 id="trade-replay-title">
            {trade.symbol} · 第 {trade.ordinal + 1} 笔固定回放
          </h4>
        </div>
        <span>固定 SignalSeries：{replay.formula.signalSeriesId}</span>
      </header>
      <MarketChart bars={replay.bars} formula={formula} />
      {replay.period === '1w' ? (
        <p className="partial-result-note">
          周线信号保持周线坐标；日线成交证据在下方单独披露，不伪装为周线时间点。
        </p>
      ) : null}
      <section
        className="order-lifecycle"
        aria-labelledby="order-lifecycle-title"
      >
        <h5 id="order-lifecycle-title">订单生命周期</h5>
        <ol className="order-event-timeline">
          {replay.trade.orderEvents.map((event, index) => {
            const summary = eventSummary(event);
            return (
              <li key={`${event.eventType}-${summary.at}-${String(index)}`}>
                <strong>{eventLabels[event.eventType]}</strong>
                <time dateTime={summary.at}>{summary.at}</time>
                <span>
                  方向{' '}
                  {summary.side === null
                    ? '—'
                    : summary.side === 'buy'
                      ? '买入'
                      : '卖出'}
                </span>
                <span>原因 {summary.reason ?? '—'}</span>
                {summary.detail === null ? null : (
                  <small>{summary.detail}</small>
                )}
              </li>
            );
          })}
        </ol>
      </section>
      <ol className="fill-evidence" aria-label="成交与执行证据">
        {replay.fillMarkers.map((marker) => (
          <li key={`${marker.side}-${marker.filledAt}`}>
            <strong>{marker.side === 'buy' ? '买入成交' : '卖出成交'}</strong>
            <span>
              信号 <time dateTime={marker.signalAt}>{marker.signalAt}</time>
            </span>
            <span>
              成交 <time dateTime={marker.filledAt}>{marker.filledAt}</time>
            </span>
            <span>
              参考开盘 {marker.referenceOpen} → 成交价 {marker.fillPrice}
            </span>
            <span>{marker.quantity} 股</span>
          </li>
        ))}
      </ol>
      <section
        className="execution-evidence"
        aria-labelledby="execution-evidence-title"
      >
        <h5 id="execution-evidence-title">固定执行行情证据</h5>
        <ol>
          {replay.executionEvidence.map((evidence) => (
            <li
              key={`${evidence.side}-${evidence.filledAt}-${evidence.bar.timestamp}`}
            >
              <strong>
                {evidence.side === 'buy' ? '买入' : '卖出'} ·{' '}
                {evidence.bar.period} · {evidence.bar.status}
              </strong>
              <span>
                行情时间{' '}
                <time dateTime={evidence.bar.timestamp}>
                  {evidence.bar.timestamp}
                </time>
              </span>
              <span>
                成交时间{' '}
                <time dateTime={evidence.filledAt}>{evidence.filledAt}</time>
              </span>
              <span>
                开 {evidence.bar.priceText.open} · 高{' '}
                {evidence.bar.priceText.high} · 低 {evidence.bar.priceText.low}{' '}
                · 收 {evidence.bar.priceText.close}
              </span>
            </li>
          ))}
        </ol>
      </section>
      <dl className="replay-identities">
        <div>
          <dt>信号行情</dt>
          <dd>{replay.provenance.signal.manifestRecordId}</dd>
        </div>
        <div>
          <dt>执行行情</dt>
          <dd>{replay.provenance.execution.manifestRecordId}</dd>
        </div>
        <div>
          <dt>交易状态</dt>
          <dd>{replay.provenance.status.manifestRecordId}</dd>
        </div>
      </dl>
      <div
        className="cursor-controls"
        role="group"
        aria-label="固定回放窗口翻页"
      >
        <button
          type="button"
          disabled={history.length === 0}
          onClick={() => {
            setCursor(history.at(-1) ?? null);
            setHistory((current) => current.slice(0, -1));
          }}
        >
          上一段
        </button>
        <button
          type="button"
          disabled={replay.nextCursor === null}
          onClick={() => {
            if (replay.nextCursor === null) return;
            setHistory((current) => [...current, cursor]);
            setCursor(replay.nextCursor);
          }}
        >
          下一段
        </button>
      </div>
    </section>
  );
}
