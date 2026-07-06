import { createBacktestApi, type BacktestIntent } from './backtestApi';

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

function validPreflight() {
  return {
    preview_snapshot_id: `sha256:${'a'.repeat(64)}`,
    reservation: false,
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
