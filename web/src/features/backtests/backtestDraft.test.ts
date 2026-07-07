import {
  BACKTEST_DRAFT_KEY,
  createBacktestDraft,
  loadBacktestDraft,
  parseBacktestPrefill,
  resolvedBacktestPrefill,
  saveBacktestDraft,
  type BacktestDraft,
} from './backtestDraft';

const draft: BacktestDraft = {
  adjustment: 'qfq',
  commissionBps: '2.5',
  endDate: '2026-01-02',
  formulaId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  formulaParameters: { FAST: 12 },
  formulaVersionId: '11111111-1111-1111-1111-111111111111',
  minimumCommission: '5',
  period: '1d',
  quantityShares: 1000,
  scope: { kind: 'single', symbol: '600000.SH' },
  sellTaxBps: '5',
  slippageBps: '1',
  startDate: '2025-01-02',
};

beforeEach(() => localStorage.clear());

it('parses only the exact refreshable market prefill contract', () => {
  expect(
    parseBacktestPrefill(
      '?symbol=600000.SH&period=1w&adjustment=hfq&start=2024-02-10&end=2024-03-15',
    ),
  ).toEqual({
    kind: 'valid',
    draft: createBacktestDraft({
      adjustment: 'hfq',
      endDate: '2024-03-15',
      period: '1w',
      scope: { kind: 'single', symbol: '600000.SH' },
      startDate: '2024-02-10',
    }),
  });
  expect(parseBacktestPrefill('')).toEqual({ kind: 'none' });
});

it.each([
  '?symbol=600000.SH&period=1d&adjustment=qfq&start=2024-02-10&end=2024-03-15&formula_id=secret',
  '?symbol=600000.SH&symbol=000001.SZ&period=1d&adjustment=qfq&start=2024-02-10&end=2024-03-15',
  '?symbol=600000.SH&period=5m&adjustment=qfq&start=2024-02-10&end=2024-03-15',
  '?symbol=600000.SH&period=1d&adjustment=qfq&start=2024-02-30&end=2024-03-15',
  '?symbol=600000.SH&period=1d&adjustment=qfq&start=2024-03-15&end=2024-03-15',
  '?symbol=600000.SH&period=1d&adjustment=qfq&start=2024-02-10',
])(
  'rejects unknown, duplicate, incomplete, or noncanonical prefill data',
  (search) => {
    expect(parseBacktestPrefill(search)).toEqual({ kind: 'invalid' });
  },
);

it.each([
  ['', { kind: 'none' } as const],
  [
    '?symbol=600000.SH&period=1d&adjustment=qfq&start=2024-02-10&end=2024-03-15&extra=1',
    { kind: 'invalid' } as const,
  ],
])(
  'never reuses a resolved draft after the URL becomes %s',
  (search, parsed) => {
    const oldSearch =
      '?symbol=600000.SH&period=1d&adjustment=qfq&start=2024-02-10&end=2024-03-15';
    expect(
      resolvedBacktestPrefill(
        parsed,
        {
          search: oldSearch,
          verified: true,
          draft: createBacktestDraft({
            scope: { kind: 'single', symbol: '600000.SH' },
            startDate: '2024-02-10',
            endDate: '2024-03-15',
          }),
        },
        search,
      ),
    ).toBeNull();
  },
);

it('round-trips only schema-validated non-sensitive user inputs', () => {
  saveBacktestDraft(draft);

  const raw = localStorage.getItem(BACKTEST_DRAFT_KEY) ?? '';
  expect(raw).toContain('"version":1');
  expect(raw).not.toMatch(/preflight|token|logs|formulaSource/u);
  expect(loadBacktestDraft()).toEqual(draft);
});

it.each([
  '{"version":2,"draft":{}}',
  '{"version":1,"draft":{"period":"1d"}}',
  'not json',
])('fails closed for old, incomplete, or malformed drafts', (raw) => {
  localStorage.setItem(BACKTEST_DRAFT_KEY, raw);
  expect(loadBacktestDraft()).toBeNull();
});

it.each([
  { ...draft, formulaId: 'not-a-uuid' },
  { ...draft, startDate: '2025-02-30' },
  { ...draft, endDate: '2024-01-01' },
  { ...draft, quantityShares: 100_000_100 },
  { ...draft, formulaParameters: { FAST: Number.POSITIVE_INFINITY } },
  { ...draft, commissionBps: '10001' },
  { ...draft, minimumCommission: '1'.repeat(65) },
  {
    ...draft,
    scope: {
      kind: 'preset',
      poolId: 'preset:_bad',
      snapshotId: `sha256:${'a'.repeat(64)}`,
    },
  },
])('refuses hostile or noncanonical draft data', (invalid) => {
  localStorage.setItem(
    BACKTEST_DRAFT_KEY,
    JSON.stringify({ version: 1, draft: invalid }),
  );
  expect(loadBacktestDraft()).toBeNull();
});

it('contains storage access failures without crashing the workspace', () => {
  const unavailable = {
    getItem() {
      throw new DOMException('blocked');
    },
    setItem() {
      throw new DOMException('blocked');
    },
    removeItem() {
      throw new DOMException('blocked');
    },
  } as unknown as Storage;
  expect(loadBacktestDraft(unavailable)).toBeNull();
  expect(saveBacktestDraft(draft, unavailable)).toBe(false);
});

it('keeps the last valid stored draft when an edit is incomplete', () => {
  expect(saveBacktestDraft(draft)).toBe(true);
  const stored = localStorage.getItem(BACKTEST_DRAFT_KEY);
  expect(saveBacktestDraft({ ...draft, endDate: '' })).toBe(false);
  expect(localStorage.getItem(BACKTEST_DRAFT_KEY)).toBe(stored);
});
