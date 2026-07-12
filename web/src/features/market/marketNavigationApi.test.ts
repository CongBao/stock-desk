import type { ApiClient } from '../../shared/api/client';
import {
  createMarketNavigationApi,
  prependRecentInstrument,
  type MarketNavigationState,
} from './marketNavigationApi';

const emptyState = {
  schemaVersion: 1,
  revision: 4,
  watchlist: [],
  recent: [],
  notice: null,
} as const satisfies MarketNavigationState;

it('loads a closed, ordered market-navigation document', async () => {
  const get = vi.fn(() =>
    Promise.resolve({
      schema_version: 1,
      revision: 4,
      watchlist: [
        { symbol: '600000.SH', name: '浦发银行', instrument_kind: 'stock' },
      ],
      recent: [
        { symbol: '000001.SS', name: '上证指数', instrument_kind: 'index' },
      ],
      notice: null,
    }),
  );

  await expect(
    createMarketNavigationApi({ get } as unknown as ApiClient).get(),
  ).resolves.toEqual({
    ...emptyState,
    watchlist: [
      { symbol: '600000.SH', name: '浦发银行', instrumentKind: 'stock' },
    ],
    recent: [
      { symbol: '000001.SS', name: '上证指数', instrumentKind: 'index' },
    ],
  });
  expect(get).toHaveBeenCalledWith('/v1/market/navigation', {
    signal: undefined,
  });
});

it('saves the complete ordered document with compare-and-swap revision', async () => {
  const put = vi.fn(() =>
    Promise.resolve({
      schema_version: 1,
      revision: 5,
      watchlist: [
        { symbol: '600000.SH', name: '浦发银行', instrument_kind: 'stock' },
      ],
      recent: [],
      notice: null,
    }),
  );
  const api = createMarketNavigationApi({ put } as unknown as ApiClient);

  await expect(
    api.put({
      expectedRevision: 4,
      watchlist: [
        { symbol: '600000.SH', name: '浦发银行', instrumentKind: 'stock' },
      ],
      recent: [],
    }),
  ).resolves.toMatchObject({ revision: 5 });
  expect(put).toHaveBeenCalledWith('/v1/market/navigation', {
    body: {
      expected_revision: 4,
      watchlist: [
        { symbol: '600000.SH', name: '浦发银行', instrument_kind: 'stock' },
      ],
      recent: [],
    },
    signal: undefined,
  });
});

it('rejects unknown fields and duplicate symbols instead of normalizing server data', async () => {
  const api = createMarketNavigationApi({
    get: vi
      .fn()
      .mockResolvedValueOnce({ ...emptyState, schema_version: 1, extra: true })
      .mockResolvedValueOnce({
        schema_version: 1,
        revision: 1,
        watchlist: [
          { symbol: '600000.SH', name: '浦发银行', instrument_kind: 'stock' },
          { symbol: '600000.SH', name: '重复', instrument_kind: 'stock' },
        ],
        recent: [],
        notice: null,
      }),
  } as unknown as ApiClient);

  await expect(api.get()).rejects.toThrow('行情导航 API 响应不符合协议');
  await expect(api.get()).rejects.toThrow('行情导航 API 响应不符合协议');
});

it('moves a selected instrument to the front, deduplicates, and bounds recent items', () => {
  const existing = Array.from({ length: 20 }, (_, index) => ({
    symbol: `${String(600000 + index).padStart(6, '0')}.SH`,
    name: `证券 ${String(index)}`,
    instrumentKind: 'stock' as const,
  }));

  const result = prependRecentInstrument(existing, existing[5]);

  expect(result).toHaveLength(20);
  expect(result[0]).toEqual(existing[5]);
  expect(
    result.filter((item) => item.symbol === existing[5].symbol),
  ).toHaveLength(1);
  expect(result.at(-1)).toEqual(existing[19]);
});
