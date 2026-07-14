import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  act,
  render,
  renderHook,
  screen,
  waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { ApiError } from '../../shared/api/client';
import { OnboardingDemoContext } from '../onboarding/demoMode';
import type {
  MarketApi,
  MarketBarsResponse,
  MarketInstrument,
} from './marketApi';
import { MarketPage } from './MarketPage';
import type {
  MarketNavigationApi,
  MarketNavigationState,
} from './marketNavigationApi';
import {
  resetMarketStore,
  useMarketStore,
  type MarketAdjustment,
  type MarketPeriod,
} from './marketStore';

vi.mock('./MarketChart', () => ({
  MarketChart: ({
    bars,
    errorMessage,
    formula,
    isLoading,
  }: {
    bars: readonly { symbol: string }[] | undefined;
    errorMessage?: string;
    formula?: {
      readonly placement: string;
      readonly numericOutputs: readonly unknown[];
    };
    isLoading?: boolean;
  }) => (
    <>
      <section aria-label="K 线与成交量">
        {isLoading
          ? '正在读取本地 K 线缓存'
          : (errorMessage ?? bars?.[0]?.symbol ?? '未选择证券')}
      </section>
      <section aria-label="公式结果副图">
        {formula === undefined
          ? '尚未启用公式副图'
          : `${formula.placement}:${String(formula.numericOutputs.length)}`}
      </section>
    </>
  ),
}));

const DIGEST = `sha256:${'a'.repeat(64)}`;
const instrument = {
  symbol: '600000.SH',
  name: '浦发银行',
  exchange: 'SH',
  instrumentKind: 'stock',
  listingStatus: 'listed',
  listedOn: '1999-11-10',
  delistedOn: null,
  provenance: {
    manifestRecordId: DIGEST,
    datasetVersion: DIGEST,
    routeVersion: DIGEST,
    source: 'tushare',
    fetchedAt: '2024-01-03T08:00:00Z',
    dataCutoff: '2024-01-03T07:00:00Z',
    routingManifest: {
      category: 'instruments',
      requestQuery: null,
      calendarRequest: null,
      priority: ['tushare'],
      attempts: [],
      selectedSource: 'tushare',
      upstreamDatasetVersion: DIGEST,
      upstreamFetchedAt: '2024-01-03T08:00:00Z',
      upstreamDataCutoff: '2024-01-03T07:00:00Z',
      upstreamAdjustment: null,
      routeVersion: DIGEST,
      transition: null,
    },
  },
} as const satisfies MarketInstrument;

function barsResponse(
  period: MarketPeriod,
  adjustment: MarketAdjustment,
): MarketBarsResponse {
  return {
    query: {
      symbol: '600000.SH',
      period,
      adjustment,
      start: '2024-01-02T00:00:00Z',
      end: '2024-01-04T00:00:00Z',
    },
    bars: [
      {
        symbol: '600000.SH',
        timestamp: '2024-01-02T16:00:00Z',
        period,
        adjustment,
        open: 10,
        high: 11,
        low: 9,
        close: 10.8,
        priceText: { open: '10', high: '11', low: '9', close: '10.8' },
        volume: 1000,
        status: 'normal',
        direction: 'rise',
      },
    ],
    coverage: {
      start: '2024-01-02T00:00:00Z',
      end: '2024-01-04T00:00:00Z',
    },
    manifestRecordId: DIGEST,
    datasetVersion: DIGEST,
    routeVersion: DIGEST,
    routingManifest: {
      category: 'bars',
      requestQuery: {
        symbol: '600000.SH',
        period,
        adjustment,
        start: '2024-01-02T00:00:00Z',
        end: '2024-01-04T00:00:00Z',
      },
      calendarRequest: null,
      priority: ['tushare', 'baostock'],
      attempts: [
        {
          ordinal: 1,
          source: 'tushare',
          decision: 'fetch_failure',
          reason: 'timeout',
          detail: 'provider request timed out',
          category: 'bars',
        },
      ],
      selectedSource: 'baostock',
      upstreamDatasetVersion: DIGEST,
      upstreamFetchedAt: '2024-01-03T08:00:00Z',
      upstreamDataCutoff: '2024-01-03T07:00:00Z',
      upstreamAdjustment: adjustment,
      routeVersion: DIGEST,
      transition: {
        category: 'bars',
        fromSource: 'tushare',
        toSource: 'baostock',
        fromDatasetVersion: `sha256:${'b'.repeat(64)}`,
        toDatasetVersion: DIGEST,
        fromRouteVersion: DIGEST,
        effectiveAt: '2024-01-02T00:00:00Z',
        calendarStart: null,
        calendarEnd: null,
        reason: 'fallback_after_failure',
      },
    },
    provenance: {
      source: 'baostock',
      fetchedAt: '2024-01-03T08:00:00Z',
      dataCutoff: '2024-01-03T07:00:00Z',
      adjustment,
      datasetVersion: DIGEST,
    },
  };
}

const emptyNavigation = {
  schemaVersion: 1,
  revision: 0,
  watchlist: [],
  recent: [],
  notice: null,
} as const satisfies MarketNavigationState;

function renderPage(
  api: MarketApi,
  navigationApi: MarketNavigationApi = {
    get: vi.fn(() => Promise.resolve(emptyNavigation)),
    put: vi.fn(() => Promise.resolve(emptyNavigation)),
  },
  readonlyDemo = false,
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <OnboardingDemoContext.Provider value={readonlyDemo}>
          <MarketPage
            api={api}
            navigationApi={navigationApi}
            searchDebounceMs={10}
          />
        </OnboardingDemoContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => resetMarketStore());

it('keeps bundled demo navigation visibly synthetic and free of writes', async () => {
  const api = {
    searchInstruments: vi.fn(() => Promise.resolve([])),
    getPools: vi.fn(() => Promise.resolve({ items: [], nextCursor: null })),
    getPool: vi.fn(),
    getBars: vi.fn(),
  } as unknown as MarketApi;
  const navigationApi = {
    get: vi.fn(() => Promise.resolve(emptyNavigation)),
    put: vi.fn(() => Promise.resolve(emptyNavigation)),
  } satisfies MarketNavigationApi;

  renderPage(api, navigationApi, true);

  expect(await screen.findByText('只读合成演示行情')).toBeVisible();
  expect(screen.getByText(/不是交易所真实行情/u)).toBeVisible();
  expect(screen.getByRole('button', { name: '打开股票池' })).toBeDisabled();
  expect(screen.queryByRole('button', { name: '添加第一只自选' })).toBeNull();
  expect(navigationApi.get).not.toHaveBeenCalled();
  expect(navigationApi.put).not.toHaveBeenCalled();
});

it('focuses prominent Market search and persists recent/watchlist operations', async () => {
  const user = userEvent.setup();
  const api = {
    searchInstruments: vi.fn(() => Promise.resolve([instrument])),
    getPools: vi.fn(() => Promise.resolve({ items: [], nextCursor: null })),
    getPool: vi.fn(),
    getBars: vi.fn(() => Promise.resolve(barsResponse('1d', 'qfq'))),
  } as unknown as MarketApi;
  const put = vi.fn<MarketNavigationApi['put']>().mockImplementation((value) =>
    Promise.resolve({
      schemaVersion: 1,
      revision: value.expectedRevision + 1,
      watchlist: value.watchlist,
      recent: value.recent,
      notice: null,
    }),
  );
  const navigationApi = {
    get: vi.fn(() => Promise.resolve(emptyNavigation)),
    put,
  } satisfies MarketNavigationApi;
  renderPage(api, navigationApi);

  const search = screen.getByRole('combobox', { name: '搜索证券' });
  expect(search).toHaveFocus();
  await user.type(search, '浦发');
  await user.click(
    await screen.findByRole('option', { name: /浦发银行.*600000\.SH/u }),
  );
  await waitFor(() =>
    expect(put).toHaveBeenCalledWith(
      {
        expectedRevision: 0,
        watchlist: [],
        recent: [
          {
            symbol: '600000.SH',
            name: '浦发银行',
            instrumentKind: 'stock',
          },
        ],
      },
      {},
    ),
  );

  await user.click(screen.getByRole('button', { name: '加入自选' }));
  await waitFor(() =>
    expect(put).toHaveBeenLastCalledWith(
      expect.objectContaining({
        expectedRevision: 1,
        watchlist: [
          {
            symbol: '600000.SH',
            name: '浦发银行',
            instrumentKind: 'stock',
          },
        ],
      }),
      {},
    ),
  );
});

it('opens stock pools as a trapped keyboard-dismissible workflow and restores its trigger', async () => {
  const user = userEvent.setup();
  const api = {
    searchInstruments: vi.fn(() => Promise.resolve([])),
    getPools: vi.fn(() => Promise.resolve({ items: [], nextCursor: null })),
    getPool: vi.fn(),
    getBars: vi.fn(),
  } as unknown as MarketApi;
  renderPage(api);

  const open = screen.getByRole('button', { name: '打开股票池' });
  await user.click(open);
  const dialog = screen.getByRole('dialog', {
    name: '选择或管理股票池',
  });
  const close = screen.getByRole('button', { name: '关闭股票池' });
  expect(close).toHaveFocus();

  const controls = Array.from(
    dialog.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => !element.hidden);
  const last = controls.at(-1);
  expect(last).toBeDefined();
  await user.keyboard('{Shift>}{Tab}{/Shift}');
  expect(last).toHaveFocus();
  await user.keyboard('{Tab}');
  expect(close).toHaveFocus();

  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await waitFor(() => expect(open).toHaveFocus());
});

it('does not request bars before selection and refetches by period and adjustment', async () => {
  const user = userEvent.setup();
  const getBars = vi.fn(
    ({
      period,
      adjustment,
    }: {
      period: MarketPeriod;
      adjustment: MarketAdjustment;
    }) => Promise.resolve(barsResponse(period, adjustment)),
  );
  const api = {
    searchInstruments: vi.fn(() => Promise.resolve([instrument])),
    getPools: vi.fn(() => Promise.resolve({ items: [], nextCursor: null })),
    getPool: vi.fn(),
    getBars,
  } as unknown as MarketApi;
  renderPage(api);

  expect(getBars).not.toHaveBeenCalled();
  await user.type(screen.getByRole('combobox', { name: '搜索证券' }), '浦发');
  await user.click(
    await screen.findByRole('option', { name: /浦发银行.*600000\.SH/u }),
  );

  await waitFor(() => expect(getBars).toHaveBeenCalledTimes(1));
  expect(getBars).toHaveBeenLastCalledWith({
    symbol: '600000.SH',
    period: '1d',
    adjustment: 'qfq',
    signal: expect.any(AbortSignal) as unknown,
  });
  expect(await screen.findByText(/数据来源：BaoStock/u)).toHaveAttribute(
    'title',
    'baostock',
  );
  expect(screen.getByText(/截至：2024-01-03T07:00:00Z/u)).toBeInTheDocument();
  expect(screen.getByText(/Tushare.*请求超时/u)).toHaveAttribute(
    'title',
    'tushare · timeout',
  );

  await user.click(screen.getByRole('radio', { name: '周线' }));
  await waitFor(() => expect(getBars).toHaveBeenCalledTimes(2));
  expect(getBars).toHaveBeenLastCalledWith(
    expect.objectContaining({ period: '1w', adjustment: 'qfq' }),
  );

  await user.click(screen.getByRole('radio', { name: '60 分钟' }));
  await user.selectOptions(
    screen.getByRole('combobox', { name: '复权方式' }),
    'hfq',
  );
  await waitFor(() =>
    expect(getBars).toHaveBeenLastCalledWith(
      expect.objectContaining({ period: '60m', adjustment: 'hfq' }),
    ),
  );
});

it('restores a saved formula version as the Market subchart and can safely disable it', async () => {
  const user = userEvent.setup();
  const store = renderHook(() => useMarketStore((state) => state));
  act(() =>
    store.result.current.setSubchart({
      kind: 'formula',
      formulaVersionId: 'version-1',
    }),
  );
  const response = {
    ...barsResponse('1d', 'qfq'),
    formula: {
      schemaVersion: 'stock-desk-signal-series-v1',
      signalSeriesId: `sha256:${'d'.repeat(64)}`,
      formulaId: 'formula-1',
      formulaVersionId: 'version-1',
      formulaVersion: 1,
      formulaChecksum: DIGEST,
      engineVersion: 'formula-engine-v1',
      compatibilityVersion: 'tdx-v1',
      symbol: '600000.SH',
      period: '1d',
      adjustment: 'qfq',
      source: 'baostock',
      datasetVersion: DIGEST,
      routeVersion: DIGEST,
      manifestRecordId: DIGEST,
      dataCutoff: '2024-01-03T07:00:00Z',
      queryStart: '2024-01-02T00:00:00Z',
      queryEnd: '2024-01-04T00:00:00Z',
      timestamps: ['2024-01-02T16:00:00Z'],
      parameters: [],
      numericOutputs: [{ name: 'MACD', values: [0.2], warmupNullCount: 0 }],
      signals: [
        { name: 'BUY', values: [true], warmupNullCount: 0 },
        { name: 'SELL', values: [false], warmupNullCount: 0 },
      ],
      runtimeDiagnostics: [],
    },
  } as const satisfies MarketBarsResponse;
  const getBars = vi.fn((options: { readonly formulaVersionId?: string }) =>
    Promise.resolve(
      options.formulaVersionId === undefined
        ? barsResponse('1d', 'qfq')
        : response,
    ),
  );
  const api = {
    searchInstruments: vi.fn(() => Promise.resolve([instrument])),
    getPools: vi.fn(() => Promise.resolve({ items: [], nextCursor: null })),
    getPool: vi.fn(),
    getBars,
  } as unknown as MarketApi;
  renderPage(api);

  await user.type(screen.getByRole('combobox', { name: '搜索证券' }), '浦发');
  await user.click(
    await screen.findByRole('option', { name: /浦发银行.*600000\.SH/u }),
  );

  await waitFor(() =>
    expect(getBars).toHaveBeenCalledWith({
      symbol: '600000.SH',
      period: '1d',
      adjustment: 'qfq',
      formulaVersionId: 'version-1',
      signal: expect.any(AbortSignal) as unknown,
    }),
  );
  expect(await screen.findByText('subchart:1')).toBeVisible();

  await user.click(screen.getByRole('button', { name: '关闭公式副图' }));
  await waitFor(() => expect(getBars).toHaveBeenCalledTimes(2));
  expect(getBars).toHaveBeenLastCalledWith({
    symbol: '600000.SH',
    period: '1d',
    adjustment: 'qfq',
    signal: expect.any(AbortSignal) as unknown,
  });
  expect(screen.getByText('尚未启用公式副图')).toBeVisible();
});

it('shows a cache-only 404 with an honest settings path', async () => {
  const user = userEvent.setup();
  const api = {
    searchInstruments: vi.fn(() => Promise.resolve([instrument])),
    getPools: vi.fn(() => Promise.resolve({ items: [], nextCursor: null })),
    getPool: vi.fn(),
    getBars: vi.fn(() =>
      Promise.reject(new ApiError('missing', { kind: 'http', status: 404 })),
    ),
  } as unknown as MarketApi;
  renderPage(api);

  await user.type(screen.getByRole('combobox', { name: '搜索证券' }), '浦发');
  await user.click(
    await screen.findByRole('option', { name: /浦发银行.*600000\.SH/u }),
  );

  expect(await screen.findByText(/本地暂无缓存/u)).toBeInTheDocument();
  expect(
    screen.getByRole('link', { name: '查看设置与数据入口' }),
  ).toHaveAttribute('href', '/settings');
  expect(screen.queryByText(/预览 K 线/u)).not.toBeInTheDocument();
});
