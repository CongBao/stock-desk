import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { PropsWithChildren } from 'react';

import type { MarketApi, MarketPoolDetail, MarketPoolPage } from './marketApi';
import { StockPoolPanel } from './StockPoolPanel';

const DIGEST = `sha256:${'a'.repeat(64)}`;
const provenance = {
  manifestRecordId: DIGEST,
  datasetVersion: DIGEST,
  routeVersion: DIGEST,
  source: 'tushare',
  fetchedAt: '2024-01-03T08:00:00Z',
  dataCutoff: '2024-01-03T07:00:00Z',
  instrumentDatasetVersion: DIGEST,
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
} as const;
const page = {
  items: [
    {
      poolId: 'preset-all-a',
      kind: 'preset',
      name: '全量 A 股',
      category: 'all_a',
      revision: null,
      memberCount: 1,
      snapshotId: DIGEST,
      provenance,
    },
    {
      poolId: 'custom-watch',
      kind: 'custom',
      name: '我的观察池',
      category: null,
      revision: 2,
      memberCount: 1,
      snapshotId: null,
      provenance,
    },
  ],
  nextCursor: null,
} as const satisfies MarketPoolPage;
const detail = {
  ...page.items[0],
  provenance: {
    ...provenance,
    composition: {
      presetKey: 'all-a',
      category: 'all_a',
      displayName: '全量 A 股',
      symbols: ['600000.SH'],
      source: 'tushare',
      datasetVersion: DIGEST,
      routeVersion: DIGEST,
      fetchedAt: '2024-01-03T08:00:00Z',
      dataCutoff: '2024-01-03T07:00:00Z',
      complete: true,
    },
  },
  members: [
    {
      ordinal: 0,
      symbol: '600000.SH',
      name: '浦发银行',
      instrumentKind: 'stock',
      listingStatus: 'listed',
    },
  ],
} as const satisfies MarketPoolDetail;

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

it('labels preset/custom pools and selects a member from pool detail', async () => {
  const user = userEvent.setup();
  const getPools = vi.fn(() => Promise.resolve(page));
  const getPool = vi.fn(() => Promise.resolve(detail));
  const onSelectPool = vi.fn();
  const onSelectInstrument = vi.fn();

  render(
    <StockPoolPanel
      api={{ getPools, getPool } as unknown as MarketApi}
      onSelectInstrument={onSelectInstrument}
      onSelectPool={onSelectPool}
      selectedPoolId={null}
    />,
    { wrapper },
  );

  expect(await screen.findByText('预设')).toBeInTheDocument();
  expect(screen.getByText('自定义')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: /全量 A 股/u }));

  expect(onSelectPool).toHaveBeenCalledWith('preset-all-a');
  expect(getPool).toHaveBeenCalledWith('preset-all-a', {
    signal: expect.any(AbortSignal) as unknown,
  });
  const composition = await screen.findByRole('group', {
    name: '全量 A 股成分信息',
  });
  expect(within(composition).getByText('全 A')).toBeVisible();
  expect(within(composition).getByText('成分截至')).toBeVisible();
  expect(within(composition).getByText('更新于')).toBeVisible();
  expect(within(composition).getByText('来源 tushare')).toBeVisible();
  expect(
    composition.querySelector('time[datetime="2024-01-03T07:00:00Z"]'),
  ).toHaveAttribute('datetime', '2024-01-03T07:00:00Z');
  await user.click(
    await screen.findByRole('button', { name: /浦发银行.*600000\.SH/u }),
  );
  expect(onSelectInstrument).toHaveBeenCalledWith({
    symbol: '600000.SH',
    name: '浦发银行',
  });
});

function members(count: number, prefix = '测试证券') {
  return Array.from({ length: count }, (_, index) => ({
    ordinal: index,
    symbol: `${String(index).padStart(6, '0')}.SZ`,
    name: `${prefix}${String(index)}`,
    instrumentKind: 'stock' as const,
    listingStatus: 'listed' as const,
  }));
}

it('pages large pool details without accumulating more than 100 member rows', async () => {
  const user = userEvent.setup();
  const largeDetail = {
    ...detail,
    members: members(150),
  } satisfies MarketPoolDetail;
  render(
    <StockPoolPanel
      api={
        {
          getPools: vi.fn(() => Promise.resolve(page)),
          getPool: vi.fn(() => Promise.resolve(largeDetail)),
        } as unknown as MarketApi
      }
      onSelectInstrument={vi.fn()}
      onSelectPool={vi.fn()}
      selectedPoolId={null}
    />,
    { wrapper },
  );

  await user.click(await screen.findByRole('button', { name: /全量 A 股/u }));
  const memberList = await screen.findByRole('list', { name: '全量 A 股成员' });
  expect(within(memberList).getAllByRole('listitem')).toHaveLength(100);
  expect(screen.getByText('第 1 / 2 页')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '下一页' }));
  expect(within(memberList).getAllByRole('listitem')).toHaveLength(50);
  expect(screen.getByText('第 2 / 2 页')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '上一页' }));
  expect(within(memberList).getAllByRole('listitem')).toHaveLength(100);
});

it('jumps across a 10k pool with a bounded DOM and resets page on pool switch', async () => {
  const user = userEvent.setup();
  const presetDetail = {
    ...detail,
    memberCount: 10_000,
    members: members(10_000),
  } satisfies MarketPoolDetail;
  const customDetail = {
    ...page.items[1],
    memberCount: 150,
    members: members(150, '自选证券'),
  } satisfies MarketPoolDetail;
  const getPool = vi.fn((poolId: string) =>
    Promise.resolve(poolId === 'preset-all-a' ? presetDetail : customDetail),
  );
  render(
    <StockPoolPanel
      api={
        {
          getPools: vi.fn(() => Promise.resolve(page)),
          getPool,
        } as unknown as MarketApi
      }
      onSelectInstrument={vi.fn()}
      onSelectPool={vi.fn()}
      selectedPoolId={null}
    />,
    { wrapper },
  );

  await user.click(await screen.findByRole('button', { name: /全量 A 股/u }));
  const presetMembers = await screen.findByRole('list', {
    name: '全量 A 股成员',
  });
  expect(within(presetMembers).getAllByRole('listitem')).toHaveLength(100);
  await user.click(screen.getByRole('button', { name: '末页' }));
  expect(screen.getByText('第 100 / 100 页')).toBeInTheDocument();
  expect(within(presetMembers).getAllByRole('listitem')).toHaveLength(100);

  await user.click(screen.getByRole('button', { name: /我的观察池/u }));
  const customMembers = await screen.findByRole('list', {
    name: '我的观察池成员',
  });
  expect(screen.getByText('第 1 / 2 页')).toBeInTheDocument();
  expect(within(customMembers).getAllByRole('listitem')).toHaveLength(100);
});
