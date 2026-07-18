import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { BacktestWorkspacePage } from './BacktestWorkspacePage';
import { BACKTEST_DRAFT_KEY, type BacktestDraft } from './backtestDraft';
import type { BacktestApi } from './backtestApi';
import type { FormulaApi, FormulaVersion } from '../formulas/formulaApi';
import type { MarketApi, MarketInstrument } from '../market/marketApi';

beforeEach(() => localStorage.clear());

const version: FormulaVersion = {
  checksum: `sha256:${'b'.repeat(64)}`,
  compatibilityVersion: 'tdx-v1',
  createdAt: '2026-07-07T00:00:00Z',
  engineVersion: 'formula-engine-v1',
  formulaId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  formulaType: 'trading',
  id: '11111111-1111-1111-1111-111111111111',
  name: 'MACD 金叉',
  parameterSchema: {
    FAST: { default: 12, kind: 'integer', label: '快线周期' },
  },
  placement: 'subchart',
  source: 'BUY:CROSS(DIF,DEA);',
  version: 2,
};

const formulaClient = {
  listFormulas: vi.fn().mockResolvedValue({
    items: [
      {
        createdAt: '2026-07-07T00:00:00Z',
        formulaType: 'trading',
        id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
        latestVersion: 2,
        name: 'MACD 金叉',
        placement: 'subchart',
        updatedAt: '2026-07-07T00:00:00Z',
      },
      {
        createdAt: '2026-07-07T00:00:00Z',
        formulaType: 'indicator',
        id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
        latestVersion: 1,
        name: '仅指标',
        placement: 'subchart',
        updatedAt: '2026-07-07T00:00:00Z',
      },
    ],
    nextCursor: null,
  }),
  listVersions: vi.fn().mockResolvedValue([version]),
} as unknown as FormulaApi;

const marketClient = {
  getPools: vi.fn().mockResolvedValue({ items: [], nextCursor: null }),
  searchInstruments: vi.fn().mockImplementation(({ query }) =>
    Promise.resolve(
      query === '600000.SH'
        ? ([
            {
              symbol: '600000.SH',
              name: '浦发银行',
              instrumentKind: 'stock',
              listingStatus: 'listed',
            } as unknown as MarketInstrument,
          ] as const)
        : [],
    ),
  ),
} as unknown as MarketApi;

function api(items: readonly unknown[] = []): BacktestApi {
  return {
    cancel: vi.fn(),
    create: vi.fn(),
    getLogs: vi.fn(),
    getRun: vi.fn(),
    listRuns: vi.fn().mockResolvedValue({ items, nextCursor: null }),
    preflight: vi.fn(),
  };
}

it('loads executable trading formulas and recent runs without exposing UUID entry', async () => {
  render(
    <MemoryRouter>
      <BacktestWorkspacePage
        api={api([
          {
            createdAt: '2026-07-07T00:00:00Z',
            failed: 0,
            finishedAt: null,
            processed: 2,
            progress: 0.2,
            resultHash: null,
            runId: '33333333-3333-3333-3333-333333333333',
            snapshotId: `sha256:${'c'.repeat(64)}`,
            stage: 'executing',
            startedAt: '2026-07-07T00:00:01Z',
            status: 'running',
            taskId: '44444444-4444-4444-4444-444444444444',
            total: 10,
            updatedAt: '2026-07-07T00:00:02Z',
          },
        ])}
        formulaClient={formulaClient}
        marketClient={marketClient}
      />
    </MemoryRouter>,
  );

  expect(
    await screen.findByRole('option', { name: 'MACD 金叉' }),
  ).toBeVisible();
  expect(
    screen.queryByRole('option', { name: '仅指标' }),
  ).not.toBeInTheDocument();
  expect(screen.queryByLabelText(/ID/u)).not.toBeInTheDocument();
  expect(
    screen.getByRole('link', { name: /运行中.*2 \/ 10/u }),
  ).toHaveAttribute('href', '/backtests/33333333-3333-3333-3333-333333333333');
});

it('keeps the wizard usable in empty and history-error states', async () => {
  const client = api();
  vi.mocked(client.listRuns).mockRejectedValue(new Error('private secret'));
  render(
    <MemoryRouter>
      <BacktestWorkspacePage
        api={client}
        formulaClient={formulaClient}
        marketClient={marketClient}
      />
    </MemoryRouter>,
  );

  expect(await screen.findByText('暂时无法读取最近回测')).toBeVisible();
  expect(screen.queryByText('private secret')).not.toBeInTheDocument();
  expect(screen.getByRole('heading', { name: '1. 公式' })).toBeVisible();
  vi.mocked(client.listRuns).mockResolvedValue({ items: [], nextCursor: null });
  await userEvent
    .setup()
    .click(screen.getByRole('button', { name: '刷新公式、股票池与历史' }));
  expect(
    await screen.findByText('还没有回测记录，完成上方配置即可创建第一条。'),
  ).toBeVisible();
  expect(screen.queryByText('暂时无法读取最近回测')).not.toBeInTheDocument();
});

it('offers explicit validated draft restoration and preflights again', async () => {
  const user = userEvent.setup();
  const draft: BacktestDraft = {
    adjustment: 'qfq',
    commissionBps: '2.5',
    endDate: '2026-01-02',
    formulaId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    formulaParameters: { FAST: 12 },
    formulaVersionId: version.id,
    minimumCommission: '5',
    period: '1d',
    quantityShares: 1000,
    scope: { kind: 'single', symbol: '600000.SH' },
    sellTaxBps: '5',
    slippageBps: '1',
    startDate: '2025-01-02',
  };
  localStorage.setItem(
    BACKTEST_DRAFT_KEY,
    JSON.stringify({ version: 1, draft }),
  );
  const originalRaw = localStorage.getItem(BACKTEST_DRAFT_KEY);
  const client = api();
  render(
    <MemoryRouter>
      <BacktestWorkspacePage
        api={client}
        formulaClient={formulaClient}
        marketClient={marketClient}
      />
    </MemoryRouter>,
  );

  expect(
    await screen.findByRole('button', { name: '恢复上次草稿' }),
  ).toBeVisible();
  expect(localStorage.getItem(BACKTEST_DRAFT_KEY)).toBe(originalRaw);
  expect(
    screen.getByRole('complementary', { name: '当前配置摘要' }),
  ).toHaveTextContent('未选择');
  await user.click(screen.getByRole('button', { name: '恢复上次草稿' }));
  await user.click(screen.getByRole('button', { name: '2. 范围' }));
  await waitFor(() =>
    expect(screen.getByLabelText('证券')).toHaveValue('600000.SH'),
  );
  expect(client.preflight).not.toHaveBeenCalled();
});

it('does not offer a stored symbol that no longer resolves to a listed stock', async () => {
  const draft: BacktestDraft = {
    adjustment: 'qfq',
    commissionBps: '2.5',
    endDate: '2026-01-02',
    formulaId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    formulaParameters: { FAST: 12 },
    formulaVersionId: version.id,
    minimumCommission: '5',
    period: '1d',
    quantityShares: 1000,
    scope: { kind: 'single', symbol: '600000.SH' },
    sellTaxBps: '5',
    slippageBps: '1',
    startDate: '2025-01-02',
  };
  localStorage.setItem(
    BACKTEST_DRAFT_KEY,
    JSON.stringify({ version: 1, draft }),
  );
  const unavailableMarketClient = {
    ...marketClient,
    searchInstruments: vi.fn().mockResolvedValue([
      {
        symbol: '600000.SH',
        name: '浦发银行',
        instrumentKind: 'stock',
        listingStatus: 'delisted',
      },
    ]),
  };
  render(
    <MemoryRouter>
      <BacktestWorkspacePage
        api={api()}
        formulaClient={formulaClient}
        marketClient={unavailableMarketClient}
      />
    </MemoryRouter>,
  );
  expect(
    await screen.findByRole('option', { name: 'MACD 金叉' }),
  ).toBeVisible();
  await waitFor(() =>
    expect(
      screen.queryByRole('button', { name: '恢复上次草稿' }),
    ).not.toBeInTheDocument(),
  );
});

it('re-resolves and applies a valid market prefill before mounting the wizard', async () => {
  render(
    <MemoryRouter
      initialEntries={[
        '/backtests?symbol=600000.SH&period=1w&adjustment=hfq&start=2024-02-10&end=2024-03-15',
      ]}
    >
      <BacktestWorkspacePage
        api={api()}
        formulaClient={formulaClient}
        marketClient={marketClient}
      />
    </MemoryRouter>,
  );

  expect(screen.getByText('正在核对行情预填…')).toBeVisible();
  expect(await screen.findByText('600000.SH')).toBeVisible();
  expect(screen.getByText('周线 · 后复权')).toBeVisible();
  expect(screen.getByText('2024-02-10 → 2024-03-15')).toBeVisible();
});

it('fails closed for unknown query keys without leaking them or applying a prefill', async () => {
  render(
    <MemoryRouter
      initialEntries={[
        '/backtests?symbol=600000.SH&period=1d&adjustment=qfq&start=2024-02-10&end=2024-03-15&formula_id=private-value',
      ]}
    >
      <BacktestWorkspacePage
        api={api()}
        formulaClient={formulaClient}
        marketClient={marketClient}
      />
    </MemoryRouter>,
  );

  expect(
    await screen.findByText('行情预填参数无效或已失效，未应用任何预填内容。'),
  ).toBeVisible();
  expect(screen.queryByText('private-value')).not.toBeInTheDocument();
  expect(
    screen.getByRole('complementary', { name: '当前配置摘要' }),
  ).toHaveTextContent('未选择');
});

it('accepts real provider instruments with unknown listing status in market prefill', async () => {
  const unknownStatusMarketClient = {
    ...marketClient,
    searchInstruments: vi.fn().mockResolvedValue([
      {
        symbol: '600519.SH',
        name: '贵州茅台',
        instrumentKind: 'stock',
        listingStatus: 'unknown',
      },
    ]),
  } as unknown as MarketApi;
  render(
    <MemoryRouter
      initialEntries={[
        '/backtests?symbol=600519.SH&period=1d&adjustment=none&start=2025-07-18&end=2026-07-18',
      ]}
    >
      <BacktestWorkspacePage
        api={api()}
        formulaClient={formulaClient}
        marketClient={unknownStatusMarketClient}
      />
    </MemoryRouter>,
  );

  expect(await screen.findByText('600519.SH')).toBeVisible();
  expect(screen.getByText('日线 · 不复权')).toBeVisible();
  expect(screen.getByText('2025-07-18 → 2026-07-18')).toBeVisible();
  expect(
    screen.queryByText('行情预填参数无效或已失效，未应用任何预填内容。'),
  ).not.toBeInTheDocument();
});

it('fails closed when a prefilled symbol resolves to a delisted stock', async () => {
  const staleMarketClient = {
    ...marketClient,
    searchInstruments: vi.fn().mockResolvedValue([
      {
        symbol: '600000.SH',
        name: '浦发银行',
        instrumentKind: 'stock',
        listingStatus: 'delisted',
      },
    ]),
  } as unknown as MarketApi;
  render(
    <MemoryRouter
      initialEntries={[
        '/backtests?symbol=600000.SH&period=1d&adjustment=qfq&start=2024-02-10&end=2024-03-15',
      ]}
    >
      <BacktestWorkspacePage
        api={api()}
        formulaClient={formulaClient}
        marketClient={staleMarketClient}
      />
    </MemoryRouter>,
  );

  expect(
    await screen.findByText('行情预填参数无效或已失效，未应用任何预填内容。'),
  ).toBeVisible();
  expect(
    screen.getByRole('complementary', { name: '当前配置摘要' }),
  ).toHaveTextContent('未选择');
});

it('prefill wins until the user explicitly restores a stored draft', async () => {
  const stored: BacktestDraft = {
    adjustment: 'qfq',
    commissionBps: '2.5',
    endDate: '2026-01-02',
    formulaId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    formulaParameters: { FAST: 12 },
    formulaVersionId: version.id,
    minimumCommission: '5',
    period: '1d',
    quantityShares: 1000,
    scope: { kind: 'single', symbol: '600000.SH' },
    sellTaxBps: '5',
    slippageBps: '1',
    startDate: '2025-01-02',
  };
  localStorage.setItem(
    BACKTEST_DRAFT_KEY,
    JSON.stringify({ version: 1, draft: stored }),
  );
  const user = userEvent.setup();
  render(
    <MemoryRouter
      initialEntries={[
        '/backtests?symbol=600000.SH&period=1w&adjustment=hfq&start=2024-02-10&end=2024-03-15',
      ]}
    >
      <BacktestWorkspacePage
        api={api()}
        formulaClient={formulaClient}
        marketClient={marketClient}
      />
    </MemoryRouter>,
  );

  expect(await screen.findByText('周线 · 后复权')).toBeVisible();
  await user.click(screen.getByRole('button', { name: '恢复上次草稿' }));
  expect(screen.getByText('日线 · 前复权')).toBeVisible();
  expect(screen.getByText('2025-01-02 → 2026-01-02')).toBeVisible();
});
