import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type {
  BacktestReplay,
  BacktestReportApi,
  BacktestTrade,
} from './backtestApi';
import { TradeReplay } from './TradeReplay';

const chart = vi.hoisted(() => vi.fn());
vi.mock('../market/MarketChart', () => ({
  MarketChart: (props: unknown) => {
    chart(props);
    return <div data-testid="pinned-chart" />;
  },
}));

const trade: BacktestTrade = {
  buyCommission: '5',
  entryFillAt: '2025-01-02T01:30:00Z',
  entrySignalAt: '2025-01-01T16:00:00Z',
  exitFillAt: '2025-01-04T01:30:00Z',
  exitSignalAt: '2025-01-03T16:00:00Z',
  fillGrossPnl: '23.5',
  floatingPnl: null,
  floatingReturn: null,
  holdingBars: 2,
  holdingDays: 2,
  investedCost: '10010',
  markAt: null,
  netPnl: '12.5',
  netReturn: '0.0125',
  ordinal: 0,
  orderEvents: [
    {
      eligibleAt: '2025-01-02T01:00:00Z',
      eventType: 'OrderPending',
      side: 'buy',
      signalAt: '2025-01-01T16:00:00Z',
    },
    {
      at: '2025-01-02T01:00:00Z',
      eventType: 'OrderBlocked',
      reason: 'limit_up',
      side: 'buy',
    },
    {
      eventType: 'OrderFilled',
      filledAt: '2025-01-02T01:30:00Z',
      price: '10.01',
      quantity: 1000,
      side: 'buy',
      signalAt: '2025-01-01T16:00:00Z',
    },
  ],
  quantity: 1000,
  realized: true,
  referenceGrossPnl: '25.5',
  sellCommission: '5',
  sellTax: '1',
  slippageCost: '2',
  symbol: '600000.SH',
};

const bar = {
  adjustment: 'qfq' as const,
  close: 10.5,
  direction: 'rise' as const,
  high: 11,
  low: 9,
  open: 10,
  period: '1d' as const,
  priceText: { close: '10.5', high: '11', low: '9', open: '10' },
  status: 'normal' as const,
  symbol: '600000.SH',
  timestamp: '2025-01-01T16:00:00Z',
  volume: 1000,
};

function replay(): BacktestReplay {
  const pinned = {
    dataCutoff: '2025-01-02T00:00:00Z',
    datasetVersion: `sha256:${'5'.repeat(64)}`,
    manifestRecordId: `sha256:${'1'.repeat(64)}`,
    routeVersion: `sha256:${'6'.repeat(64)}`,
    source: 'tushare',
  };
  return {
    adjustment: 'qfq',
    bars: [bar],
    executionStatusEvidenceLevel: 'authoritative',
    executionEvidence: [{ bar, filledAt: trade.entryFillAt, side: 'buy' }],
    fillMarkers: [
      {
        anchorOrdinal: 0,
        fillPrice: '10.01',
        filledAt: trade.entryFillAt,
        quantity: 1000,
        referenceOpen: '10',
        side: 'buy',
        signalAt: trade.entrySignalAt,
      },
    ],
    formula: {
      compatibilityVersion: 'tdx-v1',
      engineVersion: 'formula-engine-v1',
      formulaChecksum: `sha256:${'d'.repeat(64)}`,
      formulaVersionId: '11111111-1111-1111-1111-111111111111',
      numericOutputs: [{ name: 'DIF', values: [0.1] }],
      signalSeriesId: `sha256:${'4'.repeat(64)}`,
      signals: [
        { name: 'BUY', values: [true] },
        { name: 'SELL', values: [false] },
      ],
    },
    nextCursor: null,
    period: '1d',
    provenance: { execution: pinned, signal: pinned, status: pinned },
    resultHash: `sha256:${'c'.repeat(64)}`,
    runId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    snapshotId: `sha256:${'a'.repeat(64)}`,
    symbol: trade.symbol,
    trade,
    tradeOrdinal: trade.ordinal,
    warnings: [],
  };
}

it('forces the pinned formula into a subchart and repeats fill evidence outside canvas', async () => {
  const api = {
    getReplay: vi.fn().mockResolvedValue(replay()),
  } as unknown as BacktestReportApi;
  render(
    <TradeReplay
      api={api}
      runId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
      trade={trade}
    />,
  );

  expect(await screen.findByTestId('pinned-chart')).toBeVisible();
  const chartProps: unknown = chart.mock.calls[0]?.[0];
  expect(chartProps).toMatchObject({ bars: [bar] });
  expect(chartProps).toHaveProperty('formula.placement', 'subchart');
  expect(screen.getByText('买入成交')).toBeVisible();
  expect(screen.getAllByText(/10.01/u).length).toBeGreaterThan(1);
  expect(screen.getByText(/固定 SignalSeries/u)).toBeVisible();
  expect(screen.getByText('委托待执行')).toBeVisible();
  expect(screen.getByText('执行受阻')).toBeVisible();
  expect(screen.getByText('委托成交')).toBeVisible();
  expect(screen.getByText(/limit_up/u)).toBeVisible();
});

it('keeps the basic execution limitation visible in pinned replay', async () => {
  const api = {
    getReplay: vi.fn().mockResolvedValue({
      ...replay(),
      executionStatusEvidenceLevel: 'basic_no_price_limits',
      warnings: ['basic_execution_status'],
    }),
  } as unknown as BacktestReportApi;
  render(
    <TradeReplay
      api={api}
      runId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
      trade={trade}
    />,
  );

  expect(
    await screen.findByText(/基础成交假设：停牌依据 BaoStock tradestatus/u),
  ).toBeVisible();
  expect(screen.getByText('成交状态证据').nextElementSibling).toHaveTextContent(
    '基础（未校验历史涨跌停）',
  );
});

it('renders cancelled and unfilled lifecycle evidence as accessible text', async () => {
  const lifecycle = replay();
  const api = {
    getReplay: vi.fn().mockResolvedValue({
      ...lifecycle,
      trade: {
        ...lifecycle.trade,
        orderEvents: [
          {
            at: '2025-01-03T16:00:00Z',
            eventType: 'OrderCancelled',
            reason: 'opposite_signal',
            side: 'sell',
          },
          {
            eligibleAt: '2025-01-04T01:30:00Z',
            endedAt: '2025-01-05T16:00:00Z',
            eventType: 'OrderUnfilled',
            reason: 'range_ended_unfilled',
            side: 'sell',
            signalAt: '2025-01-03T16:00:00Z',
          },
        ],
      },
    }),
  } as unknown as BacktestReportApi;
  render(
    <TradeReplay
      api={api}
      runId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
      trade={trade}
    />,
  );

  expect(await screen.findByText('委托已撤销')).toBeVisible();
  expect(screen.getByText('区间结束未成交')).toBeVisible();
  expect(screen.getByText(/opposite_signal/u)).toBeVisible();
  expect(screen.getByText(/range_ended_unfilled/u)).toBeVisible();
});

it('renders pinned daily execution OHLC evidence separately for a weekly signal replay', async () => {
  const weekly = replay();
  const api = {
    getReplay: vi.fn().mockResolvedValue({
      ...weekly,
      bars: [{ ...bar, period: '1w' }],
      period: '1w',
      executionEvidence: [
        {
          bar: { ...bar, period: '1d', status: 'suspended' },
          filledAt: trade.entryFillAt,
          side: 'buy',
        },
      ],
    }),
  } as unknown as BacktestReportApi;
  render(
    <TradeReplay
      api={api}
      runId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
      trade={trade}
    />,
  );

  expect(await screen.findByText('固定执行行情证据')).toBeVisible();
  expect(screen.getByText(/买入 · 1d · suspended/u)).toBeVisible();
  expect(screen.getAllByText(bar.timestamp).length).toBeGreaterThan(1);
  expect(screen.getByText(/开 10 · 高 11 · 低 9 · 收 10.5/u)).toBeVisible();
});

it('aborts a pinned replay read when removed', () => {
  let signal: AbortSignal | undefined;
  const api = {
    getReplay: vi.fn<BacktestReportApi['getReplay']>(
      (_runId, _symbol, _ordinal, options) => {
        signal = options?.signal;
        return new Promise<never>(() => undefined);
      },
    ),
  } as unknown as BacktestReportApi;
  const mounted = render(
    <TradeReplay
      api={api}
      runId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
      trade={trade}
    />,
  );
  mounted.unmount();
  expect(signal?.aborted).toBe(true);
});

it('never labels a previous trade replay as the newly selected trade', async () => {
  const pending = new Promise<BacktestReplay>(() => undefined);
  const api = {
    getReplay: vi
      .fn()
      .mockResolvedValueOnce(replay())
      .mockReturnValueOnce(pending),
  } as unknown as BacktestReportApi;
  const mounted = render(
    <TradeReplay
      api={api}
      runId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
      trade={trade}
    />,
  );
  expect((await screen.findAllByText(/10.01/u)).length).toBeGreaterThan(1);

  mounted.rerender(
    <TradeReplay
      api={api}
      runId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
      trade={{ ...trade, ordinal: 1 }}
    />,
  );

  expect(screen.getByText(/正在重开固定行情/u)).toBeVisible();
  expect(screen.queryByText(/10.01/u)).not.toBeInTheDocument();
});

it('shows a pinned replay error without falling back to current market data', async () => {
  const api = {
    getReplay: vi.fn().mockRejectedValue(new Error('unavailable')),
  } as unknown as BacktestReportApi;
  render(
    <TradeReplay
      api={api}
      runId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
      trade={trade}
    />,
  );

  expect(await screen.findByRole('alert')).toHaveTextContent(
    '未改用当前最新行情',
  );
});

it('navigates pinned replay windows forward and backward with opaque cursors', async () => {
  const user = userEvent.setup();
  const first = { ...replay(), nextCursor: 'window-2' };
  const api = {
    getReplay: vi
      .fn<BacktestReportApi['getReplay']>()
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce({ ...first, nextCursor: null })
      .mockResolvedValueOnce(first),
  } as unknown as BacktestReportApi;
  render(
    <TradeReplay
      api={api}
      runId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
      trade={trade}
    />,
  );

  await user.click(await screen.findByRole('button', { name: '下一段' }));
  await waitFor(() =>
    expect(api.getReplay).toHaveBeenLastCalledWith(
      'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
      trade.symbol,
      trade.ordinal,
      expect.objectContaining({ cursor: 'window-2' }),
    ),
  );
  await user.click(screen.getByRole('button', { name: '上一段' }));
  await waitFor(() =>
    expect(api.getReplay).toHaveBeenLastCalledWith(
      'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
      trade.symbol,
      trade.ordinal,
      expect.objectContaining({ cursor: null }),
    ),
  );
});

it('renders ignored signals and open marks in the textual lifecycle', async () => {
  const value = replay();
  const api = {
    getReplay: vi.fn().mockResolvedValue({
      ...value,
      trade: {
        ...value.trade,
        orderEvents: [
          {
            at: '2025-01-01T16:00:00Z',
            eventType: 'IgnoredSignal',
            reason: 'conflicting_signals',
            signal: null,
          },
          {
            entryAt: trade.entryFillAt,
            entryPrice: '10.01',
            eventType: 'OpenTradeMarked',
            floatingPnl: '8',
            markAt: '2025-01-05T16:00:00Z',
            markPrice: '10.02',
            quantity: 1000,
          },
        ],
      },
    }),
  } as unknown as BacktestReportApi;
  render(
    <TradeReplay
      api={api}
      runId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
      trade={trade}
    />,
  );

  expect(await screen.findByText('信号已忽略')).toBeVisible();
  expect(screen.getByText('开放仓位标记')).toBeVisible();
  expect(screen.getByText(/标记价 10.02/u)).toBeVisible();
});
