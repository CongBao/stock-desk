import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { PropsWithChildren } from 'react';

import type { MarketApi, MarketInstrument } from './marketApi';
import { StockSearch } from './StockSearch';

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

const secondInstrument = {
  ...instrument,
  symbol: '000001.SZ',
  name: '平安银行',
  exchange: 'SZ',
} as const satisfies MarketInstrument;

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((fulfill) => {
    resolve = fulfill;
  });
  return { promise, resolve };
}

function wrapper({ children }: PropsWithChildren) {
  return (
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: { queries: { retry: false, gcTime: 0 } },
        })
      }
    >
      {children}
    </QueryClientProvider>
  );
}

it('debounces search and supports keyboard combobox selection', async () => {
  const user = userEvent.setup();
  const searchInstruments = vi.fn(() => Promise.resolve([instrument]));
  const onSelect = vi.fn();
  render(
    <StockSearch
      api={{ searchInstruments } as unknown as MarketApi}
      debounceMs={20}
      onSelect={onSelect}
    />,
    { wrapper },
  );

  const input = screen.getByRole('combobox', { name: '搜索证券' });
  await user.type(input, '浦发');

  expect(searchInstruments).not.toHaveBeenCalled();
  expect(
    await screen.findByRole('option', { name: /浦发银行.*600000\.SH/u }),
  ).toBeInTheDocument();
  expect(searchInstruments).toHaveBeenCalledWith({
    query: '浦发',
    limit: 20,
    signal: expect.any(AbortSignal) as unknown,
  });

  await user.keyboard('{ArrowDown}{Enter}');

  expect(onSelect).toHaveBeenCalledWith(instrument);
  expect(input).toHaveValue('浦发银行 · 600000.SH');
  expect(input).toHaveAttribute('aria-expanded', 'false');
});

it('searches a complete six-digit A-share code without waiting for name debounce', async () => {
  const user = userEvent.setup();
  const searchInstruments = vi.fn(() => Promise.resolve([instrument]));
  render(
    <StockSearch
      api={{ searchInstruments } as unknown as MarketApi}
      debounceMs={10_000}
      onSelect={vi.fn()}
    />,
    { wrapper },
  );

  await user.type(screen.getByRole('combobox', { name: '搜索证券' }), '600000');

  expect(
    await screen.findByRole(
      'option',
      { name: /浦发银行.*600000\.SH/u },
      { timeout: 500 },
    ),
  ).toBeInTheDocument();
  expect(searchInstruments).toHaveBeenCalledWith({
    query: '600000',
    limit: 20,
    signal: expect.any(AbortSignal) as unknown,
  });
});

it('announces empty and failed searches without inventing results', async () => {
  const user = userEvent.setup();
  const searchInstruments = vi
    .fn<MarketApi['searchInstruments']>()
    .mockResolvedValueOnce([])
    .mockRejectedValueOnce(new Error('offline'));
  render(
    <StockSearch
      api={{ searchInstruments } as unknown as MarketApi}
      debounceMs={20}
      onSelect={vi.fn()}
    />,
    { wrapper },
  );

  const input = screen.getByRole('combobox', { name: '搜索证券' });
  await user.type(input, '不存在');
  expect(await screen.findByText('未找到匹配的本地证券')).toBeInTheDocument();

  await user.clear(input);
  await user.type(input, '浦发');
  await waitFor(() => expect(searchInstruments).toHaveBeenCalledTimes(2));
  expect(await screen.findByRole('alert')).toHaveTextContent(
    '证券搜索暂不可用',
  );
});

it('does not select during touch movement and only commits the tap click', async () => {
  const user = userEvent.setup();
  const onSelect = vi.fn();
  render(
    <StockSearch
      api={
        {
          searchInstruments: vi.fn(() => Promise.resolve([instrument])),
        } as unknown as MarketApi
      }
      debounceMs={10}
      onSelect={onSelect}
    />,
    { wrapper },
  );

  await user.type(screen.getByRole('combobox', { name: '搜索证券' }), '浦发');
  const option = await screen.findByRole('option', {
    name: /浦发银行.*600000\.SH/u,
  });
  fireEvent.pointerDown(option, { pointerType: 'touch' });
  fireEvent.pointerMove(option, { pointerType: 'touch', clientY: 80 });
  expect(onSelect).not.toHaveBeenCalled();
  fireEvent.click(option);
  expect(onSelect).toHaveBeenCalledOnce();
});

it('hides stale results immediately and cannot select them during debounce', async () => {
  const user = userEvent.setup();
  const onSelect = vi.fn();
  render(
    <StockSearch
      api={
        {
          searchInstruments: vi.fn(() => Promise.resolve([instrument])),
        } as unknown as MarketApi
      }
      debounceMs={40}
      onSelect={onSelect}
    />,
    { wrapper },
  );

  const input = screen.getByRole('combobox', { name: '搜索证券' });
  await user.type(input, '浦发');
  expect(
    await screen.findByRole('option', { name: /浦发银行.*600000\.SH/u }),
  ).toBeInTheDocument();

  await user.type(input, '银行');
  expect(screen.queryByRole('option')).not.toBeInTheDocument();
  await user.keyboard('{ArrowDown}{Enter}');
  expect(onSelect).not.toHaveBeenCalled();
});

it('aborts the old request and ignores its late result after a new query wins', async () => {
  const user = userEvent.setup();
  const oldResult = deferred<readonly MarketInstrument[]>();
  const newResult = deferred<readonly MarketInstrument[]>();
  let oldSignal: AbortSignal | undefined;
  const searchInstruments = vi.fn<MarketApi['searchInstruments']>(
    ({ query, signal }) => {
      if (query === '浦发') {
        oldSignal = signal;
        return oldResult.promise;
      }
      return newResult.promise;
    },
  );
  render(
    <StockSearch
      api={{ searchInstruments } as unknown as MarketApi}
      debounceMs={20}
      onSelect={vi.fn()}
    />,
    { wrapper },
  );

  const input = screen.getByRole('combobox', { name: '搜索证券' });
  await user.type(input, '浦发');
  await waitFor(() => expect(searchInstruments).toHaveBeenCalledTimes(1));
  await user.clear(input);
  await user.type(input, '平安');
  await waitFor(() => expect(searchInstruments).toHaveBeenCalledTimes(2));
  expect(oldSignal?.aborted).toBe(true);

  oldResult.resolve([instrument]);
  newResult.resolve([secondInstrument]);
  expect(
    await screen.findByRole('option', { name: /平安银行.*000001\.SZ/u }),
  ).toBeInTheDocument();
  expect(screen.queryByText('浦发银行')).not.toBeInTheDocument();
});

it('uses click for mouse selection and reopens current results with ArrowUp', async () => {
  const user = userEvent.setup();
  const onSelect = vi.fn();
  render(
    <StockSearch
      api={
        {
          searchInstruments: vi.fn(() =>
            Promise.resolve([instrument, secondInstrument]),
          ),
        } as unknown as MarketApi
      }
      debounceMs={10}
      onSelect={onSelect}
    />,
    { wrapper },
  );

  const input = screen.getByRole('combobox', { name: '搜索证券' });
  await user.type(input, '银行');
  const option = await screen.findByRole('option', {
    name: /浦发银行.*600000\.SH/u,
  });
  fireEvent.mouseDown(option);
  expect(onSelect).not.toHaveBeenCalled();
  fireEvent.click(option);
  expect(onSelect).toHaveBeenCalledOnce();

  await user.clear(input);
  await user.type(input, '银行');
  await screen.findByRole('option', { name: /平安银行.*000001\.SZ/u });
  await user.keyboard('{Escape}{Enter}');
  expect(onSelect).toHaveBeenCalledOnce();
  await user.keyboard('{ArrowDown}');
  expect(input).toHaveAttribute('aria-expanded', 'true');
  expect(
    screen.getByRole('option', { name: /浦发银行.*600000\.SH/u }),
  ).toHaveAttribute('aria-selected', 'true');
  await user.keyboard('{Escape}');
  await user.keyboard('{ArrowUp}');
  expect(input).toHaveAttribute('aria-expanded', 'true');
  expect(
    screen.getByRole('option', { name: /平安银行.*000001\.SZ/u }),
  ).toHaveAttribute('aria-selected', 'true');
  await user.keyboard('{Enter}');
  expect(onSelect).toHaveBeenLastCalledWith(secondInstrument);
});
