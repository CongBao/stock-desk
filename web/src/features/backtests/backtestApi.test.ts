import {
  backtestExportUrl,
  createBacktestApi,
  type BacktestIntent,
} from './backtestApi';

const intent: BacktestIntent = {
  adjustment: 'qfq',
  commissionBps: '2.5',
  formulaParameters: { FAST: 12, SLOW: 26 },
  formulaVersionId: '11111111-1111-1111-1111-111111111111',
  minimumCommission: '5',
  period: '1d',
  quantityShares: 1000,
  scope: { kind: 'single', symbol: '600000.SH' },
  scoringEnd: '2026-01-02T00:00:00+08:00',
  scoringStart: '2025-01-02T00:00:00+08:00',
  sellTaxBps: '5',
  slippageBps: '1',
};

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function requestUrl(value: RequestInfo | URL | undefined) {
  if (value === undefined) return '';
  if (typeof value === 'string') return value;
  return value instanceof URL ? value.href : value.url;
}

function validPreflight() {
  return {
    preview_snapshot_id: `sha256:${'a'.repeat(64)}`,
    reservation: false,
    execution_status_evidence_level: 'basic_no_price_limits',
    formula: {
      formula_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
      formula_version_id: intent.formulaVersionId,
      formula_checksum: `sha256:${'b'.repeat(64)}`,
      engine_version: 'formula-engine-v1',
      compatibility_version: 'tdx-v1',
      normalized_parameters: [
        { name: 'FAST', kind: 'integer', value: '12' },
        { name: 'SLOW', kind: 'integer', value: '26' },
      ],
    },
    scope: {
      kind: 'single',
      symbol: '600000.SH',
      pool_id: null,
      revision_or_snapshot_id: null,
      total: 1,
      runnable: 1,
      gap_count: 0,
      gap_sample: [],
      gaps_truncated: false,
      warnings: ['basic_execution_status'],
    },
    period: '1d',
    adjustment: 'qfq',
    scoring_start: '2025-01-01T16:00:00Z',
    scoring_end: '2026-01-01T16:00:00Z',
    warmup: {
      policy_version: 'formula-warmup-v1',
      lookback_bars: 35,
      unbounded_dependency: false,
    },
    coverage: { signal: 1, execution: 1, status: 1 },
    rules: {
      execution_rules_version: 'a-share-v2',
      cost_model_version: 'a-share-cost-v1',
      sizing_version: 'fixed-lot-v1',
    },
    quantity_shares: 1000,
    costs: {
      commission_bps: '2.5',
      minimum_commission: '5',
      sell_tax_bps: '5',
      slippage_bps: '1',
    },
    estimated_workload: { symbols: 1, runnable_symbols: 1, formula_rows: 250 },
    disclaimer: '每只股票独立模拟，不代表组合收益',
  };
}

function validOverview(change: Readonly<Record<string, unknown>> = {}) {
  return {
    run_id: '11111111-1111-1111-1111-111111111111',
    task_id: '22222222-2222-2222-2222-222222222222',
    snapshot_id: `sha256:${'a'.repeat(64)}`,
    status: 'running',
    stage: 'executing',
    total: 10,
    processed: 2,
    failed: 0,
    progress: 0.2,
    result_hash: null,
    created_at: '2026-07-07T00:00:00Z',
    updated_at: '2026-07-07T00:00:02Z',
    started_at: '2026-07-07T00:00:01Z',
    finished_at: null,
    ...change,
  };
}

it('serializes exact decimal strings and aware timestamps without coercion', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    void input;
    void init;
    return Promise.resolve(
      response({
        preview_snapshot_id: `sha256:${'a'.repeat(64)}`,
        reservation: false,
        execution_status_evidence_level: 'authoritative',
        formula: {
          formula_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
          formula_version_id: intent.formulaVersionId,
          formula_checksum: `sha256:${'b'.repeat(64)}`,
          engine_version: 'formula-engine-v1',
          compatibility_version: 'tdx-v1',
          normalized_parameters: [
            { name: 'FAST', kind: 'integer', value: '12' },
            { name: 'SLOW', kind: 'integer', value: '26' },
          ],
        },
        scope: {
          kind: 'single',
          symbol: '600000.SH',
          pool_id: null,
          revision_or_snapshot_id: null,
          total: 1,
          runnable: 1,
          gap_count: 0,
          gap_sample: [],
          gaps_truncated: false,
          warnings: [],
        },
        period: '1d',
        adjustment: 'qfq',
        scoring_start: '2025-01-01T16:00:00Z',
        scoring_end: '2026-01-01T16:00:00Z',
        warmup: {
          policy_version: 'formula-warmup-v1',
          lookback_bars: 35,
          unbounded_dependency: false,
        },
        coverage: { signal: 1, execution: 1, status: 1 },
        rules: {
          execution_rules_version: 'a-share-v1',
          cost_model_version: 'a-share-cost-v1',
          sizing_version: 'fixed-lot-v1',
        },
        quantity_shares: 1000,
        costs: {
          commission_bps: '2.5',
          minimum_commission: '5',
          sell_tax_bps: '5',
          slippage_bps: '1',
        },
        estimated_workload: {
          symbols: 1,
          runnable_symbols: 1,
          formula_rows: 250,
        },
        disclaimer: '每只股票独立模拟，不代表组合收益',
      }),
    );
  });
  vi.stubGlobal('fetch', fetchMock);

  const result = await createBacktestApi().preflight(intent);

  const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
  expect(typeof init.body).toBe('string');
  expect(
    JSON.parse(typeof init.body === 'string' ? init.body : ''),
  ).toMatchObject({
    commission_bps: '2.5',
    minimum_commission: '5',
    scoring_start: '2025-01-02T00:00:00+08:00',
    scoring_end: '2026-01-02T00:00:00+08:00',
  });
  expect(result.scope.runnable).toBe(1);
  expect(result.costs.commissionBps).toBe('2.5');
  expect(result.executionStatusEvidenceLevel).toBe('authoritative');
});

it('uses an append cursor for the live log tail', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    void input;
    void init;
    return Promise.resolve(
      response({
        items: [{ ordinal: 7, level: 'info', message: '完成', detail: {} }],
        next_cursor: null,
        after_cursor: 'tail-7',
      }),
    );
  });
  vi.stubGlobal('fetch', fetchMock);

  const page = await createBacktestApi().getLogs(
    '11111111-1111-1111-1111-111111111111',
    { afterCursor: 'tail-6' },
  );

  const requested = fetchMock.mock.calls[0]?.[0];
  const requestedUrl =
    typeof requested === 'string'
      ? requested
      : requested instanceof URL
        ? requested.href
        : requested?.url;
  expect(requestedUrl).toContain('after_cursor=tail-6');
  expect(page.afterCursor).toBe('tail-7');
});

it.each([
  ['unknown warning', ['unknown_warning']],
  ['duplicate warning', ['basic_execution_status', 'basic_execution_status']],
])('rejects %s in a backtest submission', async (_label, warnings) => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        response({
          run_id: '11111111-1111-1111-1111-111111111111',
          task_id: '22222222-2222-2222-2222-222222222222',
          snapshot_id: `sha256:${'a'.repeat(64)}`,
          warnings,
        }),
      ),
    ),
  );

  await expect(createBacktestApi().create(intent)).rejects.toMatchObject({
    name: 'BacktestProtocolError',
  });
});

it.each([
  { period: '1w' },
  { quantity_shares: 2000 },
  { scoring_start: '2025-01-02T00:00:01+08:00' },
  { costs: { ...validPreflight().costs, commission_bps: '3' } },
  {
    formula: {
      ...validPreflight().formula,
      formula_version_id: '99999999-9999-9999-9999-999999999999',
    },
  },
])(
  'rejects a valid-shape preflight that is not bound to the request: %j',
  async (change) => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(response({ ...validPreflight(), ...change })),
      ),
    );
    await expect(createBacktestApi().preflight(intent)).rejects.toMatchObject({
      name: 'BacktestProtocolError',
    });
  },
);

it('rejects inconsistent server-authoritative coverage before review', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        response({
          ...validPreflight(),
          coverage: { signal: 0, execution: 1, status: 1 },
        }),
      ),
    ),
  );
  await expect(createBacktestApi().preflight(intent)).rejects.toMatchObject({
    name: 'BacktestProtocolError',
  });
});

it.each([
  { scope: { ...validPreflight().scope, warnings: [] } },
  {
    scope: {
      ...validPreflight().scope,
      warnings: ['partial_pool_gaps', 'basic_execution_status'],
    },
  },
  {
    scope: {
      ...validPreflight().scope,
      gap_count: 1,
      gap_sample: [
        { symbol: '600001.SH', reasons: ['missing_signal_coverage'] },
      ],
      warnings: ['basic_execution_status'],
    },
  },
  {
    rules: { ...validPreflight().rules, execution_rules_version: 'a-share-v1' },
  },
  {
    scope: {
      ...validPreflight().scope,
      warnings: ['basic_execution_status', 'basic_execution_status'],
    },
  },
  { scope: { ...validPreflight().scope, warnings: ['unknown_warning'] } },
])(
  'rejects inconsistent execution-status disclosure before review: %j',
  async (change) => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(response({ ...validPreflight(), ...change })),
      ),
    );
    await expect(createBacktestApi().preflight(intent)).rejects.toMatchObject({
      name: 'BacktestProtocolError',
    });
  },
);

it.each([
  { status: 'invented' },
  { stage: 'finished' },
  { progress: 1.2 },
  { processed: 11 },
  { snapshot_id: '/private/data.sqlite' },
])('fails closed on malformed run overview fields: %j', async (change) => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        response({
          items: [
            {
              run_id: '11111111-1111-1111-1111-111111111111',
              task_id: '22222222-2222-2222-2222-222222222222',
              snapshot_id: `sha256:${'a'.repeat(64)}`,
              status: 'running',
              stage: 'executing',
              total: 10,
              processed: 2,
              failed: 0,
              progress: 0.2,
              result_hash: null,
              created_at: '2026-07-07T00:00:00Z',
              updated_at: '2026-07-07T00:00:01Z',
              started_at: '2026-07-07T00:00:01Z',
              finished_at: null,
              ...change,
            },
          ],
          next_cursor: null,
        }),
      ),
    ),
  );
  await expect(createBacktestApi().listRuns()).rejects.toMatchObject({
    name: 'BacktestProtocolError',
  });
});

it.each([
  { progress: 0.3 },
  {
    status: 'queued',
    stage: 'queued',
    processed: 1,
    progress: 0.1,
    started_at: null,
  },
  {
    status: 'queued',
    stage: 'queued',
    processed: 0,
    progress: 0,
    started_at: '2026-07-07T00:00:01Z',
  },
  { status: 'running', stage: 'completed' },
  {
    status: 'running',
    stage: 'executing',
    finished_at: '2026-07-07T00:00:03Z',
  },
  {
    status: 'succeeded',
    stage: 'completed',
    processed: 10,
    progress: 1,
    failed: 1,
    finished_at: '2026-07-07T00:00:03Z',
    result_hash: `sha256:${'c'.repeat(64)}`,
  },
  {
    status: 'partial_failed',
    stage: 'completed',
    processed: 10,
    progress: 1,
    failed: 0,
    finished_at: '2026-07-07T00:00:03Z',
    result_hash: `sha256:${'c'.repeat(64)}`,
  },
  { status: 'failed', stage: 'failed', finished_at: null },
  {
    status: 'cancelled',
    stage: 'cancelled',
    finished_at: '2026-07-07T00:00:03Z',
    result_hash: `sha256:${'c'.repeat(64)}`,
  },
  { updated_at: '2026-07-06T23:59:59Z' },
  { finished_at: '2026-07-07T00:00:01Z' },
])('rejects contradictory overview state: %j', async (change) => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(response(validOverview(change)))),
  );
  await expect(
    createBacktestApi().getRun('11111111-1111-1111-1111-111111111111'),
  ).rejects.toMatchObject({ name: 'BacktestProtocolError' });
});

it('binds overview and cancellation responses to the requested run path', async () => {
  const foreign = '99999999-9999-9999-9999-999999999999';
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(response(validOverview({ run_id: foreign })))
    .mockResolvedValueOnce(
      response({
        run_id: foreign,
        task_id: '22222222-2222-2222-2222-222222222222',
        snapshot_id: `sha256:${'a'.repeat(64)}`,
        warnings: [],
      }),
    );
  vi.stubGlobal('fetch', fetchMock);
  const api = createBacktestApi();
  await expect(
    api.getRun('11111111-1111-1111-1111-111111111111'),
  ).rejects.toMatchObject({ name: 'BacktestProtocolError' });
  await expect(
    api.cancel('11111111-1111-1111-1111-111111111111'),
  ).rejects.toMatchObject({ name: 'BacktestProtocolError' });
});

function completedOverview() {
  return validOverview({
    failed: 1,
    finished_at: '2026-07-07T00:00:03Z',
    processed: 10,
    progress: 1,
    result_hash: `sha256:${'c'.repeat(64)}`,
    stage: 'completed',
    status: 'partial_failed',
  });
}

const histogramCodes = [
  'lt_neg_20pct',
  'neg_20_to_10pct',
  'neg_10_to_5pct',
  'neg_5_to_0pct',
  'zero',
  'pos_0_to_5pct',
  'pos_5_to_10pct',
  'pos_10_to_20pct',
  'gt_20pct',
] as const;

function validReport() {
  return {
    overview: completedOverview(),
    formula_version_id: intent.formulaVersionId,
    formula_checksum: `sha256:${'d'.repeat(64)}`,
    formula_engine_version: 'formula-engine-v1',
    compatibility_version: 'tdx-v1',
    backtest_engine_version: 'backtest-engine-v1',
    execution_status_evidence_level: 'basic_no_price_limits',
    warnings: ['partial_pool_gaps', 'basic_execution_status'],
    formula_parameters: [
      { name: 'FAST', kind: 'integer', value: '12' },
      { name: 'SLOW', kind: 'integer', value: '26' },
    ],
    provenance: {
      instrument_dataset_version: `sha256:${'e'.repeat(64)}`,
      symbol_count: 10,
      runnable_count: 9,
      gap_count: 1,
      source_ids: {
        signal: ['tushare'],
        execution: ['akshare'],
        status: ['tdx_local'],
      },
      digest: `sha256:${'f'.repeat(64)}`,
    },
    period: '1d',
    adjustment: 'qfq',
    quantity_shares: 1000,
    costs: {
      commission_bps: '2.5',
      minimum_commission: '5',
      sell_tax_bps: '5',
      slippage_bps: '1',
    },
    execution_rules_version: 'a-share-v2',
    cost_model_version: 'a-share-cost-v1',
    sizing_version: 'fixed-lot-v1',
    warmup_policy_version: 'formula-warmup-v1',
    metrics: {
      label: 'independent trade samples, not portfolio return',
      realized_count: 2,
      win_rate_denominator: 2,
      positive_count: 1,
      negative_count: 1,
      zero_count: 0,
      win_rate: '0.5',
      win_rate_reason: null,
      mean_net_return: '0.01',
      mean_net_return_reason: null,
      median_net_return: '0.01',
      median_net_return_reason: null,
      payoff_ratio: '2',
      payoff_ratio_reason: null,
      max_win_return: '0.04',
      max_win_return_reason: null,
      max_loss_return: '-0.02',
      max_loss_return_reason: null,
      realized_net_pnl_total: '20',
      average_holding_bars: '3',
      average_holding_bars_reason: null,
      average_holding_days: '4',
      average_holding_days_reason: null,
      histogram: histogramCodes.map((code, index) => ({
        code,
        lower_bound: index === 0 ? null : String(index - 5),
        upper_bound: index === 8 ? null : String(index - 4),
        lower_inclusive: index !== 0,
        upper_inclusive: code === 'zero',
        count: index === 3 || index === 5 ? 1 : 0,
        share: index === 3 || index === 5 ? '0.5' : '0',
        share_reason: null,
      })),
      open_trades: {
        count: 1,
        floating_pnl_total: '5',
        mean_floating_return: '0.005',
        mean_floating_return_reason: null,
      },
      reliability: {
        level: 'low',
        reason: 'small_sample',
        realized_count: 2,
        largest_symbol_share: '0.5',
      },
      equity_curve: null,
    },
    disclaimer: 'independent trade samples, not portfolio return',
    outcomes: {
      total: 10,
      succeeded: 9,
      failed: 0,
      data_insufficient: 1,
      unprocessed: 0,
    },
  };
}

it('strictly decodes a conclusion-first report bound to the requested run', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(response(validReport()))),
  );

  const report = await createBacktestApi().getReport(
    '11111111-1111-1111-1111-111111111111',
  );

  expect(report.metrics?.winRate).toBe('0.5');
  expect(report.metrics?.histogram).toHaveLength(9);
  expect(report.provenance.gapCount).toBe(1);
  expect(report.executionStatusEvidenceLevel).toBe('basic_no_price_limits');
  expect(report.warnings).toEqual([
    'partial_pool_gaps',
    'basic_execution_status',
  ]);
});

it('accepts the exact empty-realized metric semantics', async () => {
  const raw = validReport();
  const noRealized = 'no_realized_samples';
  const metrics = {
    ...raw.metrics,
    average_holding_bars: null,
    average_holding_bars_reason: noRealized,
    average_holding_days: null,
    average_holding_days_reason: noRealized,
    histogram: raw.metrics.histogram.map((bin) => ({
      ...bin,
      count: 0,
      share: null,
      share_reason: noRealized,
    })),
    max_loss_return: null,
    max_loss_return_reason: 'no_negative_returns',
    max_win_return: null,
    max_win_return_reason: 'no_positive_returns',
    mean_net_return: null,
    mean_net_return_reason: noRealized,
    median_net_return: null,
    median_net_return_reason: noRealized,
    negative_count: 0,
    payoff_ratio: null,
    payoff_ratio_reason: 'no_positive_or_negative_returns',
    positive_count: 0,
    realized_count: 0,
    realized_net_pnl_total: '0',
    reliability: {
      largest_symbol_share: null,
      level: 'low',
      realized_count: 0,
      reason: noRealized,
    },
    win_rate: null,
    win_rate_denominator: 0,
    win_rate_reason: noRealized,
    zero_count: 0,
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(response({ ...raw, metrics }))),
  );

  const decoded = await createBacktestApi().getReport(
    '11111111-1111-1111-1111-111111111111',
  );
  expect(decoded.metrics?.winRate).toBeNull();
  expect(decoded.metrics?.histogram.every((bin) => bin.share === null)).toBe(
    true,
  );
  expect(decoded.metrics?.reliability.reason).toBe(noRealized);

  for (const forged of [
    { mean_net_return: '0.1', mean_net_return_reason: null },
    { median_net_return: '0.1', median_net_return_reason: null },
    { average_holding_bars: '1', average_holding_bars_reason: null },
    { average_holding_days: '1', average_holding_days_reason: null },
    { max_win_return: '0.1', max_win_return_reason: null },
    { max_loss_return: '-0.1', max_loss_return_reason: null },
    { payoff_ratio: '1', payoff_ratio_reason: null },
  ]) {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          response({
            ...raw,
            metrics: { ...metrics, ...forged },
          }),
        ),
      ),
    );
    await expect(
      createBacktestApi().getReport('11111111-1111-1111-1111-111111111111'),
    ).rejects.toMatchObject({ name: 'BacktestProtocolError' });
  }
});

it.each([
  { metrics: { ...validReport().metrics, win_rate_denominator: 3 } },
  { metrics: { ...validReport().metrics, positive_count: 2 } },
  { metrics: { ...validReport().metrics, histogram: [] } },
  {
    overview: {
      ...completedOverview(),
      run_id: '99999999-9999-9999-9999-999999999999',
    },
  },
  { provenance: { ...validReport().provenance, gap_count: 2 } },
  { warnings: ['basic_execution_status'] },
  {
    provenance: { ...validReport().provenance, gap_count: 0 },
    warnings: ['partial_pool_gaps', 'basic_execution_status'],
  },
  { warnings: ['basic_execution_status', 'basic_execution_status'] },
  { warnings: ['unknown_warning'] },
  { execution_rules_version: 'a-share-v1' },
  { outcomes: { ...validReport().outcomes, succeeded: 8 } },
  { outcomes: { ...validReport().outcomes, unprocessed: 1 } },
  { outcomes: { ...validReport().outcomes, failed: 1 } },
])('rejects contradictory report fields: %j', async (change) => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(response({ ...validReport(), ...change }))),
  );
  await expect(
    createBacktestApi().getReport('11111111-1111-1111-1111-111111111111'),
  ).rejects.toMatchObject({ name: 'BacktestProtocolError' });
});

it.each([
  ['forged win-rate ratio', { win_rate: '0.4' }],
  [
    'forged histogram share',
    {
      histogram: validReport().metrics.histogram.map((bin, index) =>
        index === 3 ? { ...bin, share: '0.4' } : bin,
      ),
    },
  ],
  [
    'missing nonempty histogram share',
    {
      histogram: validReport().metrics.histogram.map((bin, index) =>
        index === 3
          ? { ...bin, share: null, share_reason: 'no_realized_samples' }
          : bin,
      ),
    },
  ],
  [
    'open count without a matching mean',
    { open_trades: { ...validReport().metrics.open_trades, count: 0 } },
  ],
  [
    'empty open set with the wrong reason',
    {
      open_trades: {
        count: 0,
        floating_pnl_total: '0',
        mean_floating_return: null,
        mean_floating_return_reason: 'other',
      },
    },
  ],
  [
    'nonempty reliability without concentration',
    {
      reliability: {
        ...validReport().metrics.reliability,
        largest_symbol_share: null,
      },
    },
  ],
  [
    'impossible reliability level',
    {
      reliability: {
        ...validReport().metrics.reliability,
        level: 'medium',
        reason: 'moderate_sample',
      },
    },
  ],
])('rejects %s', async (_label, metricsPatch) => {
  const raw = validReport();
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        response({ ...raw, metrics: { ...raw.metrics, ...metricsPatch } }),
      ),
    ),
  );

  await expect(
    createBacktestApi().getReport('11111111-1111-1111-1111-111111111111'),
  ).rejects.toMatchObject({ name: 'BacktestProtocolError' });
});

it('requests only one bounded cursor page and binds trade rows to the run', async () => {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
    response({
      items: [
        {
          symbol: '600000.SH',
          ordinal: 0,
          payload: {
            realized: true,
            entry_signal_at: '2025-01-01T00:00:00Z',
            entry_fill_at: '2025-01-02T01:30:00Z',
            exit_signal_at: '2025-01-03T00:00:00Z',
            exit_fill_at: '2025-01-04T01:30:00Z',
            mark_at: null,
            quantity: 1000,
            buy_commission: '5',
            sell_commission: '5',
            sell_tax: '1',
            slippage_cost: '2',
            reference_gross_pnl: '25.5',
            fill_gross_pnl: '23.5',
            invested_cost: '10010',
            net_pnl: '12.5',
            net_return: '0.0125',
            floating_pnl: null,
            floating_return: null,
            holding_bars: 2,
            holding_days: 2,
            order_events: [
              {
                event_type: 'OrderFilled',
                payload: {
                  side: 'buy',
                  signal_at: '2025-01-01T00:00:00Z',
                  filled_at: '2025-01-02T01:30:00Z',
                  price: '10.01',
                  quantity: 1000,
                },
              },
            ],
          },
        },
      ],
      next_cursor: 'next-safe',
    }),
  );
  vi.stubGlobal('fetch', fetchMock);

  const page = await createBacktestApi().getTrades(
    '11111111-1111-1111-1111-111111111111',
    'realized',
    { cursor: 'current-safe' },
  );

  expect(page.items).toHaveLength(1);
  expect(page.items[0]).toMatchObject({
    buyCommission: '5',
    fillGrossPnl: '23.5',
    investedCost: '10010',
    referenceGrossPnl: '25.5',
    sellCommission: '5',
    sellTax: '1',
    slippageCost: '2',
  });
  expect(page.nextCursor).toBe('next-safe');
  expect(requestUrl(fetchMock.mock.calls[0]?.[0])).toContain('limit=100');
  expect(requestUrl(fetchMock.mock.calls[0]?.[0])).toContain(
    'cursor=current-safe',
  );
});

it.each([
  ['realized', false],
  ['open', true],
] as const)(
  'binds the %s endpoint to the matching realized state',
  async (kind, realized) => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          response({
            items: [
              {
                symbol: '600000.SH',
                ordinal: 0,
                payload: {
                  buy_commission: '5',
                  entry_fill_at: '2025-01-02T01:30:00Z',
                  entry_signal_at: '2025-01-01T00:00:00Z',
                  exit_fill_at: realized ? '2025-01-04T01:30:00Z' : null,
                  exit_signal_at: realized ? '2025-01-03T00:00:00Z' : null,
                  fill_gross_pnl: '23.5',
                  floating_pnl: realized ? null : '12.5',
                  floating_return: realized ? null : '0.00125',
                  holding_bars: 2,
                  holding_days: 2,
                  invested_cost: '10010',
                  mark_at: realized ? null : '2025-01-05T00:00:00Z',
                  net_pnl: realized ? '12.5' : null,
                  net_return: realized ? '0.00125' : null,
                  order_events: [
                    {
                      event_type: 'OrderFilled',
                      payload: {
                        filled_at: '2025-01-02T01:30:00Z',
                        price: '10.01',
                        quantity: 1000,
                        side: 'buy',
                        signal_at: '2025-01-01T00:00:00Z',
                      },
                    },
                  ],
                  quantity: 1000,
                  realized,
                  reference_gross_pnl: '25.5',
                  sell_commission: realized ? '5' : '0',
                  sell_tax: realized ? '1' : '0',
                  slippage_cost: '2',
                },
              },
            ],
            next_cursor: null,
          }),
        ),
      ),
    );

    await expect(
      createBacktestApi().getTrades(
        '11111111-1111-1111-1111-111111111111',
        kind,
      ),
    ).rejects.toMatchObject({ name: 'BacktestProtocolError' });
  },
);

function rawBar(timestamp = '2025-01-01T16:00:00Z') {
  return {
    symbol: '600000.SH',
    timestamp,
    period: '1d',
    adjustment: 'qfq',
    open: '10',
    high: '11',
    low: '9',
    close: '10.5',
    volume: 1000,
    status: 'normal',
  };
}

const signalManifest = `sha256:${'1'.repeat(64)}`;
const executionManifest = `sha256:${'2'.repeat(64)}`;
const statusManifest = `sha256:${'3'.repeat(64)}`;
const signalSeriesId = `sha256:${'4'.repeat(64)}`;

function pinnedIdentity(manifestRecordId: string, suffix: string) {
  return {
    manifest_record_id: manifestRecordId,
    dataset_version: `sha256:${suffix.repeat(64)}`,
    route_version: `sha256:${suffix.repeat(64)}`,
    source: 'tushare',
    data_cutoff: '2025-01-02T00:00:00Z',
  };
}

function validReplay() {
  return {
    execution_status_evidence_level: 'basic_no_price_limits',
    warnings: ['basic_execution_status'],
    run_id: '11111111-1111-1111-1111-111111111111',
    snapshot_id: `sha256:${'a'.repeat(64)}`,
    result_hash: `sha256:${'c'.repeat(64)}`,
    symbol: '600000.SH',
    trade_ordinal: 0,
    period: '1d',
    adjustment: 'qfq',
    bars: [rawBar()],
    formula: {
      signal_series_id: signalSeriesId,
      formula_version_id: intent.formulaVersionId,
      formula_checksum: `sha256:${'d'.repeat(64)}`,
      engine_version: 'formula-engine-v1',
      compatibility_version: 'tdx-v1',
      numeric_outputs: [{ name: 'DIF', values: ['0.1'] }],
      signals: [
        { name: 'BUY', values: [true] },
        { name: 'SELL', values: [false] },
      ],
    },
    trade: {
      symbol: '600000.SH',
      realized: true,
      entry_signal_at: '2025-01-01T16:00:00Z',
      entry_fill_at: '2025-01-02T01:30:00Z',
      exit_signal_at: '2025-01-03T16:00:00Z',
      exit_fill_at: '2025-01-04T01:30:00Z',
      mark_at: null,
      quantity: 1000,
      buy_commission: '5',
      sell_commission: '5',
      sell_tax: '1',
      slippage_cost: '2',
      reference_gross_pnl: '25.5',
      fill_gross_pnl: '23.5',
      invested_cost: '10010',
      net_pnl: '12.5',
      net_return: '0.0125',
      floating_pnl: null,
      floating_return: null,
      holding_bars: 2,
      holding_days: 2,
      signal_series_id: signalSeriesId,
      formula_version_id: intent.formulaVersionId,
      market_manifest_ids: [signalManifest, executionManifest],
      status_manifest_ids: [statusManifest],
      order_events: [
        {
          event_type: 'OrderPending',
          payload: {
            side: 'buy',
            signal_at: '2025-01-01T16:00:00Z',
            eligible_at: '2025-01-02T01:00:00Z',
          },
        },
        {
          event_type: 'OrderBlocked',
          payload: {
            side: 'buy',
            at: '2025-01-02T01:00:00Z',
            reason: 'limit_up',
          },
        },
        {
          event_type: 'OrderFilled',
          payload: {
            side: 'buy',
            signal_at: '2025-01-01T16:00:00Z',
            filled_at: '2025-01-02T01:30:00Z',
            price: '10.01',
            quantity: 1000,
          },
        },
      ],
    },
    fill_markers: [
      {
        side: 'buy',
        signal_at: '2025-01-01T16:00:00Z',
        filled_at: '2025-01-02T01:30:00Z',
        anchor_ordinal: 0,
        reference_open: '10',
        fill_price: '10.01',
        quantity: 1000,
      },
    ],
    execution_evidence: [
      { side: 'buy', filled_at: '2025-01-02T01:30:00Z', bar: rawBar() },
    ],
    provenance: {
      signal: pinnedIdentity(signalManifest, '5'),
      execution: pinnedIdentity(executionManifest, '6'),
      status: pinnedIdentity(statusManifest, '7'),
    },
    next_cursor: null,
  };
}

it('decodes only the requested pinned trade replay and never calls market bars', async () => {
  const fetchMock = vi
    .fn<typeof fetch>()
    .mockResolvedValue(response(validReplay()));
  vi.stubGlobal('fetch', fetchMock);

  const replay = await createBacktestApi().getReplay(
    '11111111-1111-1111-1111-111111111111',
    '600000.SH',
    0,
    { cursor: 'window-1' },
  );

  expect(replay.formula.signalSeriesId).toBe(signalSeriesId);
  expect(replay.bars[0]?.priceText.open).toBe('10');
  expect(replay.trade.symbol).toBe('600000.SH');
  expect(replay.executionStatusEvidenceLevel).toBe('basic_no_price_limits');
  expect(replay.warnings).toEqual(['basic_execution_status']);
  const url = requestUrl(fetchMock.mock.calls[0]?.[0]);
  expect(url).toContain(
    '/backtests/11111111-1111-1111-1111-111111111111/trades/600000.SH/0/replay',
  );
  expect(url).toContain('limit=500');
  expect(url).not.toContain('/market/bars');
});

it.each([
  { event_type: 'UnknownEvent', payload: {} },
  {
    event_type: 'OrderBlocked',
    payload: { side: 'hold', at: '2025-01-02T01:00:00Z', reason: 'limit_up' },
  },
])(
  'rejects an unknown or malformed replay lifecycle event: %j',
  async (event) => {
    const raw = validReplay();
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          response({
            ...raw,
            trade: { ...raw.trade, order_events: [event] },
          }),
        ),
      ),
    );

    await expect(
      createBacktestApi().getReplay(
        '11111111-1111-1111-1111-111111111111',
        '600000.SH',
        0,
      ),
    ).rejects.toMatchObject({ name: 'BacktestProtocolError' });
  },
);

it.each([
  { run_id: '99999999-9999-9999-9999-999999999999' },
  { symbol: '000001.SZ' },
  { trade_ordinal: 1 },
  { bars: [{ ...rawBar(), period: '1w' }] },
  {
    formula: {
      ...validReplay().formula,
      numeric_outputs: [{ name: 'DIF', values: [] }],
    },
  },
  {
    trade: {
      ...validReplay().trade,
      signal_series_id: `sha256:${'9'.repeat(64)}`,
    },
  },
  { trade: { ...validReplay().trade, market_manifest_ids: [signalManifest] } },
  { trade: { ...validReplay().trade, buy_commission: null } },
  {
    execution_evidence: [
      { ...validReplay().execution_evidence[0], side: 'sell' },
    ],
  },
  {
    execution_evidence: [
      {
        ...validReplay().execution_evidence[0],
        filled_at: '2025-01-03T01:30:00Z',
      },
    ],
  },
  {
    execution_evidence: [
      {
        ...validReplay().execution_evidence[0],
        bar: { ...rawBar(), period: '1w' },
      },
    ],
  },
  { execution_status_evidence_level: 'mixed' },
  { warnings: ['partial_pool_gaps', 'basic_execution_status'] },
  { warnings: [] },
  { warnings: ['basic_execution_status', 'basic_execution_status'] },
  { warnings: ['unknown_warning'] },
])('rejects replay identity drift: %j', async (change) => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(response({ ...validReplay(), ...change }))),
  );
  await expect(
    createBacktestApi().getReplay(
      '11111111-1111-1111-1111-111111111111',
      '600000.SH',
      0,
    ),
  ).rejects.toMatchObject({ name: 'BacktestProtocolError' });
});

it.each([
  { result_hash: null },
  {
    fill_markers: [
      { ...validReplay().fill_markers[0], anchor_ordinal: 10_000 },
    ],
  },
])('accepts valid terminal/off-page replay evidence: %j', async (change) => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(response({ ...validReplay(), ...change }))),
  );
  await expect(
    createBacktestApi().getReplay(
      '11111111-1111-1111-1111-111111111111',
      '600000.SH',
      0,
    ),
  ).resolves.toMatchObject({ symbol: '600000.SH' });
});

it('accepts daily execution evidence for weekly signal bars', async () => {
  const weekly = validReplay();
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        response({
          ...weekly,
          period: '1w',
          bars: [{ ...rawBar(), period: '1w' }],
        }),
      ),
    ),
  );
  await expect(
    createBacktestApi().getReplay(
      '11111111-1111-1111-1111-111111111111',
      '600000.SH',
      0,
    ),
  ).resolves.toMatchObject({ period: '1w' });
});

it('uses bounded independent cursors for groups, failures, and terminal logs', async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(
      response({
        items: [
          {
            dimension: 'symbol',
            key: '600000.SH',
            payload: {
              realized_denominator: 2,
              realized_count: 2,
              positive_count: 1,
              negative_count: 1,
              zero_count: 0,
              share_of_all: '1',
              win_rate: '0.5',
              mean_net_return: '0.01',
              median_net_return: '0.01',
              payoff_ratio: '2',
              payoff_ratio_reason: null,
              net_pnl_total: '20',
              average_holding_days: '4',
            },
          },
        ],
        next_cursor: null,
      }),
    )
    .mockResolvedValueOnce(
      response({
        items: [
          {
            symbol: '600000.SH',
            ordinal: 0,
            reason: 'missing_signal_data',
            detail: {},
          },
        ],
        next_cursor: null,
      }),
    )
    .mockResolvedValueOnce(
      response({
        items: [{ ordinal: 1, level: 'info', message: '完成', detail: {} }],
        next_cursor: null,
        after_cursor: null,
      }),
    );
  vi.stubGlobal('fetch', fetchMock);
  const api = createBacktestApi();

  await api.getGroups('11111111-1111-1111-1111-111111111111', 'symbol');
  await api.getFailures('11111111-1111-1111-1111-111111111111');
  await api.getReportLogs('11111111-1111-1111-1111-111111111111');

  expect(String(fetchMock.mock.calls[0]?.[0])).toContain('dimension=symbol');
  expect(String(fetchMock.mock.calls[0]?.[0])).toContain('limit=100');
  expect(String(fetchMock.mock.calls[2]?.[0])).not.toContain('after_cursor');
});

it.each([
  ['share', { share_of_all: '0.5' }],
  ['win rate', { win_rate: '0.4' }],
  ['payoff reason', { payoff_ratio_reason: 'no_negative_returns' }],
])('rejects a group with a forged %s', async (_label, payloadPatch) => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        response({
          items: [
            {
              dimension: 'symbol',
              key: '600000.SH',
              payload: {
                average_holding_days: '4',
                mean_net_return: '0.01',
                median_net_return: '0.01',
                negative_count: 1,
                net_pnl_total: '20',
                payoff_ratio: '2',
                payoff_ratio_reason: null,
                positive_count: 1,
                realized_count: 2,
                realized_denominator: 2,
                share_of_all: '1',
                win_rate: '0.5',
                zero_count: 0,
                ...payloadPatch,
              },
            },
          ],
          next_cursor: null,
        }),
      ),
    ),
  );

  await expect(
    createBacktestApi().getGroups(
      '11111111-1111-1111-1111-111111111111',
      'symbol',
    ),
  ).rejects.toMatchObject({ name: 'BacktestProtocolError' });
});

it('builds deterministic export URLs only from fixed safe enums', () => {
  expect(
    backtestExportUrl('11111111-1111-1111-1111-111111111111', 'trades', 'csv'),
  ).toBe(
    '/api/backtests/11111111-1111-1111-1111-111111111111/export/trades.csv',
  );
  expect(() => backtestExportUrl('not-a-run', 'trades', 'csv')).toThrowError(
    /export/u,
  );
});
