import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import theme from '../../app/theme.css?raw';
import type {
  BacktestReport,
  BacktestReportApi,
  BacktestTrade,
} from './backtestApi';
import { BacktestReportPage } from './BacktestReportPage';

const disclaimer = 'independent trade samples, not portfolio return';

it('uses shared theme surfaces and text tokens throughout the report', () => {
  const reportStyles = theme.slice(
    theme.indexOf('/* Conclusion-first backtest report */'),
    theme.indexOf('/* Intelligent analysis workspace */'),
  );

  expect(reportStyles).toContain('color: var(--text-primary)');
  expect(reportStyles).toContain('color: var(--text-muted)');
  expect(reportStyles).toContain('color: var(--accent)');
  expect(reportStyles).toContain('background: var(--surface-2)');
  expect(reportStyles).toContain('background: var(--surface-0)');
  expect(reportStyles).not.toContain('background: rgba(7, 17, 31');
});

function report(realizedCount = 2): BacktestReport {
  const hasRealized = realizedCount > 0;
  return {
    adjustment: 'qfq',
    backtestEngineVersion: 'backtest-engine-v1',
    compatibilityVersion: 'tdx-v1',
    costModelVersion: 'a-share-cost-v1',
    costs: {
      commissionBps: '2.5',
      minimumCommission: '5',
      sellTaxBps: '5',
      slippageBps: '1',
    },
    disclaimer,
    executionStatusEvidenceLevel: 'authoritative',
    executionRulesVersion: 'a-share-v1',
    formulaChecksum: `sha256:${'d'.repeat(64)}`,
    formulaEngineVersion: 'formula-engine-v1',
    formulaParameters: [{ kind: 'integer', name: 'FAST', value: '12' }],
    formulaVersionId: '11111111-1111-1111-1111-111111111111',
    metrics: {
      averageHoldingBars: hasRealized ? '3' : null,
      averageHoldingBarsReason: hasRealized ? null : 'no_realized_samples',
      averageHoldingDays: hasRealized ? '4' : null,
      averageHoldingDaysReason: hasRealized ? null : 'no_realized_samples',
      histogram: [
        'lt_neg_20pct',
        'neg_20_to_10pct',
        'neg_10_to_5pct',
        'neg_5_to_0pct',
        'zero',
        'pos_0_to_5pct',
        'pos_5_to_10pct',
        'pos_10_to_20pct',
        'gt_20pct',
      ].map((code) => ({ code, count: 0, share: hasRealized ? '0' : null })),
      label: disclaimer,
      maxLossReturn: hasRealized ? '-0.02' : null,
      maxLossReturnReason: hasRealized ? null : 'no_negative_returns',
      maxWinReturn: hasRealized ? '0.04' : null,
      maxWinReturnReason: hasRealized ? null : 'no_positive_returns',
      meanNetReturn: hasRealized ? '0.01' : null,
      meanNetReturnReason: hasRealized ? null : 'no_realized_samples',
      medianNetReturn: hasRealized ? '0.01' : null,
      medianNetReturnReason: hasRealized ? null : 'no_realized_samples',
      negativeCount: hasRealized ? 1 : 0,
      openTrades: {
        count: 1,
        floatingPnlTotal: '5',
        meanFloatingReturn: '0.005',
        meanFloatingReturnReason: null,
      },
      payoffRatio: hasRealized ? '2' : null,
      payoffRatioReason: hasRealized ? null : 'no_positive_or_negative_returns',
      positiveCount: hasRealized ? 1 : 0,
      realizedCount,
      realizedNetPnlTotal: hasRealized ? '20' : '0',
      reliability: {
        largestSymbolShare: hasRealized ? '0.5' : null,
        level: 'low',
        reason: hasRealized ? 'small_sample' : 'no_realized_samples',
        realizedCount,
      },
      winRate: hasRealized ? '0.5' : null,
      winRateDenominator: realizedCount,
      winRateReason: hasRealized ? null : 'no_realized_samples',
      zeroCount: 0,
    },
    overview: {
      createdAt: '2026-07-07T00:00:00Z',
      failed: 1,
      finishedAt: '2026-07-07T00:00:03Z',
      processed: 10,
      progress: 1,
      resultHash: `sha256:${'c'.repeat(64)}`,
      runId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
      snapshotId: `sha256:${'a'.repeat(64)}`,
      stage: 'completed',
      startedAt: '2026-07-07T00:00:01Z',
      status: 'partial_failed',
      taskId: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
      total: 10,
      updatedAt: '2026-07-07T00:00:03Z',
    },
    outcomes: {
      dataInsufficient: 1,
      failed: 0,
      succeeded: 9,
      total: 10,
      unprocessed: 0,
    },
    period: '1d',
    provenance: {
      digest: `sha256:${'f'.repeat(64)}`,
      gapCount: 1,
      instrumentDatasetVersion: `sha256:${'e'.repeat(64)}`,
      runnableCount: 9,
      sourceIds: {
        execution: ['akshare'],
        signal: ['tushare'],
        status: ['tdx_local'],
      },
      symbolCount: 10,
    },
    quantityShares: 1000,
    sizingVersion: 'fixed-lot-v1',
    warnings: [],
    warmupPolicyVersion: 'formula-warmup-v1',
  };
}

function api(): BacktestReportApi {
  return {
    getFailures: vi.fn().mockResolvedValue({ items: [], nextCursor: null }),
    getGroups: vi.fn(() => new Promise<never>(() => undefined)),
    getReport: vi.fn().mockResolvedValue(report()),
    getReplay: vi.fn(),
    getReportLogs: vi.fn().mockResolvedValue({ items: [], nextCursor: null }),
    getTrades: vi.fn().mockResolvedValue({ items: [], nextCursor: null }),
  };
}

function realizedTrade(): BacktestTrade {
  return {
    buyCommission: '5',
    entryFillAt: '2025-01-02T01:30:00Z',
    entrySignalAt: '2025-01-01T00:00:00Z',
    exitFillAt: '2025-01-04T01:30:00Z',
    exitSignalAt: '2025-01-03T00:00:00Z',
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
        eventType: 'OrderFilled',
        filledAt: '2025-01-02T01:30:00Z',
        price: '10.01',
        quantity: 1000,
        side: 'buy',
        signalAt: '2025-01-01T00:00:00Z',
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
}

it('shows conclusions before lazily loaded trade details without portfolio claims', () => {
  render(<BacktestReportPage api={api()} report={report()} />);

  const winRate = screen.getByText('胜率');
  const trades = screen.getByRole('tab', { name: '交易明细' });
  expect(
    winRate.compareDocumentPosition(trades) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  expect(screen.getByText('样本可靠性')).toBeVisible();
  expect(screen.getAllByText(disclaimer).length).toBeGreaterThan(1);
  expect(screen.queryByText(/权益曲线|组合收益|下单/u)).not.toBeInTheDocument();
});

it('keeps the basic execution limitation visible in the immutable report', () => {
  render(
    <BacktestReportPage
      api={api()}
      report={{
        ...report(),
        executionStatusEvidenceLevel: 'basic_no_price_limits',
        warnings: ['basic_execution_status'],
      }}
    />,
  );

  expect(
    screen.getByText(/基础成交假设：停牌依据 BaoStock tradestatus/u),
  ).toHaveTextContent(
    '未校验历史涨跌停。T+1、交易日和下一周期开盘仍按规则处理，结果可能高估可成交机会。',
  );
  expect(screen.getByText('成交状态证据').nextElementSibling).toHaveTextContent(
    '基础（未校验历史涨跌停）',
  );
});

it('shows every required realized and open-trade metric as separate server values', async () => {
  render(<BacktestReportPage api={api()} report={report()} />);

  expect(
    (await screen.findByText('已实现净盈亏')).nextElementSibling,
  ).toHaveTextContent('20');
  expect(screen.getByText('盈亏比').nextElementSibling).toHaveTextContent('2');
  expect(screen.getByText('最大单笔盈利').nextElementSibling).toHaveTextContent(
    '4%',
  );
  expect(screen.getByText('最大单笔亏损').nextElementSibling).toHaveTextContent(
    '-2%',
  );
  expect(
    screen.getByText('平均持有 K 线').nextElementSibling,
  ).toHaveTextContent('3');
  expect(screen.getByText('平均持有天数').nextElementSibling).toHaveTextContent(
    '4',
  );
  expect(screen.getByText('开放仓位样本').nextElementSibling).toHaveTextContent(
    '1',
  );
  expect(
    screen.getByText('开放仓位浮动盈亏').nextElementSibling,
  ).toHaveTextContent('5');
  expect(
    screen.getByText('开放仓位平均浮动收益').nextElementSibling,
  ).toHaveTextContent('0.5%');
});

it('renders an empty realized denominator as explicitly not calculable', () => {
  render(<BacktestReportPage api={api()} report={report(0)} />);

  expect(screen.getAllByText('不可计算').length).toBeGreaterThan(0);
  expect(screen.getByText('无已实现样本')).toBeVisible();
});

it('uses server-reconciled symbol outcomes without double-counting frozen gaps', () => {
  render(<BacktestReportPage api={api()} report={report()} />);
  expect(screen.getByText('成功处理').nextElementSibling).toHaveTextContent(
    '9',
  );
  expect(screen.getByText('失败').nextElementSibling).toHaveTextContent('0');
  expect(screen.getByText('数据不足').nextElementSibling).toHaveTextContent(
    '1',
  );
});

it('supports keyboard tab navigation and loads only the selected resource', async () => {
  const client = api();
  render(<BacktestReportPage api={client} report={report()} />);

  const overview = screen.getByRole('tab', { name: '结论概览' });
  overview.focus();
  fireEvent.keyDown(overview, { key: 'ArrowRight' });

  expect(screen.getByRole('tab', { name: '交易明细' })).toHaveFocus();
  expect(await screen.findByText('当前页排序')).toBeVisible();
  expect(client.getTrades).toHaveBeenCalledTimes(1);
  expect(client.getFailures).not.toHaveBeenCalled();
});

it('keeps one trade page and advances with the opaque server cursor', async () => {
  const user = userEvent.setup();
  const client = api();
  vi.mocked(client.getTrades)
    .mockResolvedValueOnce({ items: [], nextCursor: 'page-2' })
    .mockResolvedValueOnce({ items: [], nextCursor: null });
  render(<BacktestReportPage api={client} report={report()} />);

  await user.click(screen.getByRole('tab', { name: '交易明细' }));
  await user.click(await screen.findByRole('button', { name: '下一页' }));

  expect(client.getTrades).toHaveBeenLastCalledWith(
    report().overview.runId,
    'realized',
    expect.objectContaining({ cursor: 'page-2' }),
  );
});

it('never reuses a cursor from another report tab when the trade page is terminal', async () => {
  const user = userEvent.setup();
  const client = api();
  vi.mocked(client.getReportLogs).mockResolvedValue({
    items: [],
    nextCursor: 'logs-page-2',
  });
  vi.mocked(client.getTrades).mockResolvedValue({
    items: [],
    nextCursor: null,
  });
  render(<BacktestReportPage api={client} report={report()} />);

  await user.click(screen.getByRole('tab', { name: '运行日志' }));
  expect(await screen.findByRole('button', { name: '下一页' })).toBeEnabled();
  await user.click(screen.getByRole('tab', { name: '交易明细' }));
  await screen.findByText('当前页排序');

  expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled();
});

it('opens the selected pinned trade replay without requesting current market bars', async () => {
  const user = userEvent.setup();
  const client = api();
  vi.mocked(client.getTrades).mockResolvedValue({
    items: [realizedTrade()],
    nextCursor: null,
  });
  vi.mocked(client.getReplay).mockReturnValue(new Promise(() => undefined));
  render(<BacktestReportPage api={client} report={report()} />);

  await user.click(screen.getByRole('tab', { name: '交易明细' }));
  await user.click(await screen.findByRole('button', { name: '固定回放' }));

  expect(client.getReplay).toHaveBeenCalledWith(
    report().overview.runId,
    '600000.SH',
    0,
    expect.objectContaining({ cursor: null }),
  );
  expect(screen.getByText('正在重开固定行情、公式与成交证据…')).toBeVisible();
});

it('exposes only deterministic fixed-section export links', () => {
  render(<BacktestReportPage api={api()} report={report()} />);
  expect(screen.getByRole('link', { name: '导出交易 CSV' })).toHaveAttribute(
    'href',
    `/api/backtests/${report().overview.runId}/export/trades.csv`,
  );
  expect(
    screen.queryByRole('button', { name: /下单/u }),
  ).not.toBeInTheDocument();
});

it('keeps concrete reproducibility costs and sources visible across report tabs', async () => {
  const user = userEvent.setup();
  render(<BacktestReportPage api={api()} report={report()} />);

  expect(screen.getByText('佣金（bps）').nextElementSibling).toHaveTextContent(
    '2.5',
  );
  expect(screen.getByText('最低佣金').nextElementSibling).toHaveTextContent(
    '5',
  );
  expect(
    screen.getByText('卖出印花税（bps）').nextElementSibling,
  ).toHaveTextContent('5');
  expect(screen.getByText('滑点（bps）').nextElementSibling).toHaveTextContent(
    '1',
  );
  expect(screen.getByText('信号数据源').nextElementSibling).toHaveTextContent(
    'tushare',
  );
  expect(screen.getByText('执行数据源').nextElementSibling).toHaveTextContent(
    'akshare',
  );
  expect(screen.getByText('状态数据源').nextElementSibling).toHaveTextContent(
    'tdx_local',
  );
  expect(screen.getByText('公式参数').nextElementSibling).toHaveTextContent(
    'FAST=12',
  );
  expect(screen.getByText('公式引擎').nextElementSibling).toHaveTextContent(
    'formula-engine-v1',
  );
  expect(screen.getByText('回测引擎').nextElementSibling).toHaveTextContent(
    'backtest-engine-v1',
  );

  await user.click(screen.getByRole('tab', { name: '交易明细' }));
  await screen.findByText('当前页排序');
  expect(screen.getByText('佣金（bps）')).toBeVisible();
  expect(screen.getByText('信号数据源')).toBeVisible();
  expect(screen.getByText('公式参数')).toBeVisible();
});

it('does not show the previous run report while a reused route loads a new run', async () => {
  const first = report();
  let resolveSecond: ((value: BacktestReport) => void) | undefined;
  const client = api();
  vi.mocked(client.getReport)
    .mockResolvedValueOnce(first)
    .mockReturnValueOnce(
      new Promise<BacktestReport>((resolve) => {
        resolveSecond = resolve;
      }),
    );
  const mounted = render(
    <BacktestReportPage api={client} runId={first.overview.runId} />,
  );
  expect(await screen.findByText(first.overview.snapshotId)).toBeVisible();

  const secondRun = 'cccccccc-cccc-cccc-cccc-cccccccccccc';
  mounted.rerender(<BacktestReportPage api={client} runId={secondRun} />);

  expect(screen.getByText(/正在读取固定回测报告/u)).toBeVisible();
  expect(screen.queryByText(first.overview.snapshotId)).not.toBeInTheDocument();
  resolveSecond?.({
    ...first,
    overview: { ...first.overview, runId: secondRun },
  });
});

it('shows a bounded report-load error without stale conclusions', async () => {
  const client = api();
  vi.mocked(client.getReport).mockRejectedValue(new Error('offline'));
  render(
    <BacktestReportPage
      api={client}
      runId="dddddddd-dddd-dddd-dddd-dddddddddddd"
    />,
  );

  expect(await screen.findByRole('alert')).toHaveTextContent(
    '回测报告暂时无法读取',
  );
  expect(screen.queryByText('回测结论')).not.toBeInTheDocument();
});

it('renders failure rows and supports opaque next then previous navigation', async () => {
  const user = userEvent.setup();
  const client = api();
  vi.mocked(client.getFailures).mockResolvedValue({
    items: [
      {
        detail: {},
        ordinal: 0,
        reason: 'missing_signal_data',
        symbol: '600000.SH',
      },
    ],
    nextCursor: 'failure-page-2',
  });
  render(<BacktestReportPage api={client} report={report()} />);

  await user.click(screen.getByRole('tab', { name: '失败记录' }));
  expect(await screen.findByText('missing_signal_data')).toBeVisible();
  await user.click(screen.getByRole('button', { name: '下一页' }));
  expect(client.getFailures).toHaveBeenLastCalledWith(
    report().overview.runId,
    expect.objectContaining({ cursor: 'failure-page-2' }),
  );
  await user.click(screen.getByRole('button', { name: '上一页' }));
  expect(client.getFailures).toHaveBeenLastCalledWith(
    report().overview.runId,
    expect.objectContaining({ cursor: null }),
  );
});

it('shows persisted logs and isolated resource errors', async () => {
  const user = userEvent.setup();
  const client = api();
  vi.mocked(client.getReportLogs)
    .mockResolvedValueOnce({
      items: [{ detail: {}, level: 'info', message: '运行完成', ordinal: 1 }],
      nextCursor: null,
    })
    .mockRejectedValue(new Error('logs failed'));
  vi.mocked(client.getTrades).mockRejectedValue(new Error('page failed'));
  vi.mocked(client.getFailures).mockRejectedValue(
    new Error('failure page failed'),
  );
  render(<BacktestReportPage api={client} report={report()} />);

  await user.click(screen.getByRole('tab', { name: '运行日志' }));
  expect(await screen.findByText('运行完成')).toBeVisible();
  await user.click(screen.getByRole('tab', { name: '开放仓位' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('当前页读取失败');
  await user.click(screen.getByRole('tab', { name: '失败记录' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(
    '失败记录读取失败',
  );
  await user.click(screen.getByRole('tab', { name: '运行日志' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('日志读取失败');
});
