import type { ApiClient, JsonValue } from '../../shared/api/client';

import backendBarsResponse from './fixtures/backend-bars-response.json';
import backendInstrumentsResponse from './fixtures/backend-instruments-response.json';
import backendPeriodBarsResponses from './fixtures/backend-period-bars-responses.json';
import backendPresetPoolResponse from './fixtures/backend-preset-pool-response.json';
import {
  createMarketApi,
  decodeRoutingManifest,
  isCanonicalBucketStart,
  MarketProtocolError,
} from './marketApi';

const DIGEST = `sha256:${'a'.repeat(64)}`;
const barsResponse = backendBarsResponse;

function clientReturning(value: JsonValue): {
  client: ApiClient;
  get: ReturnType<typeof vi.fn>;
} {
  const get = vi.fn(() => Promise.resolve(value));
  return { client: { get }, get };
}

it('encodes market queries and forwards AbortSignal', async () => {
  const { client, get } = clientReturning([]);
  const api = createMarketApi(client);
  const controller = new AbortController();

  await api.searchInstruments({
    query: '浦发 银行&600000',
    limit: 12,
    signal: controller.signal,
  });

  expect(get).toHaveBeenCalledWith(
    '/market/instruments?q=%E6%B5%A6%E5%8F%91+%E9%93%B6%E8%A1%8C%26600000&limit=12',
    { signal: controller.signal },
  );
});

it('decodes a bounded cached bar response and preserves routing evidence', async () => {
  const { client, get } = clientReturning(barsResponse);
  const api = createMarketApi(client);

  const result = await api.getBars({
    symbol: '600000.SH',
    period: '1d',
    adjustment: 'qfq',
  });

  expect(get).toHaveBeenCalledWith(
    '/market/bars?symbol=600000.SH&period=1d&adjustment=qfq',
    { signal: undefined },
  );
  expect(result.bars[0]).toMatchObject({
    close: 10.6,
    direction: 'rise',
    volume: 12345,
  });
  expect(result.routingManifest.attempts[0]).toMatchObject({
    source: 'tushare',
    reason: 'timeout',
  });
  expect(result.provenance.dataCutoff).toBe('2024-01-03T00:00:00Z');
});

it('accepts the fixed JSON emitted by the backend bar and routing models', async () => {
  const api = createMarketApi(clientReturning(backendBarsResponse).client);

  const result = await api.getBars({
    symbol: '600000.SH',
    period: '1d',
    adjustment: 'qfq',
  });

  expect(result.coverage).toEqual({
    start: result.query.start,
    end: result.query.end,
  });
  expect(result.bars).toHaveLength(1);
  expect(result.routingManifest.transition).toEqual({
    category: 'bars',
    fromSource: 'tushare',
    toSource: 'baostock',
    fromDatasetVersion: `sha256:${'1'.repeat(64)}`,
    toDatasetVersion: `sha256:${'2'.repeat(64)}`,
    fromRouteVersion: `sha256:${'3'.repeat(64)}`,
    effectiveAt: result.query.start,
    calendarStart: null,
    calendarEnd: null,
    reason: 'fallback_after_failure',
  });
});

it('rejects malformed upstream values as protocol failures', async () => {
  const malformed = structuredClone(barsResponse) as Record<string, unknown>;
  const bars = malformed['bars'] as Record<string, unknown>[];
  bars[0] = { ...bars[0], close: 10.6 };
  const { client } = clientReturning(malformed as JsonValue);

  await expect(
    createMarketApi(client).getBars({
      symbol: '600000.SH',
      period: '1d',
      adjustment: 'qfq',
    }),
  ).rejects.toBeInstanceOf(MarketProtocolError);
});

type MutableJsonRecord = Record<string, unknown>;

function nestedRecord(value: unknown): MutableJsonRecord {
  return value as MutableJsonRecord;
}

function nestedArray(value: unknown): MutableJsonRecord[] {
  return value as MutableJsonRecord[];
}

const auditMismatchCases: readonly [
  string,
  (payload: MutableJsonRecord) => void,
][] = [
  [
    'top query versus request',
    (payload) => {
      nestedRecord(payload['query'])['symbol'] = '000001.SZ';
    },
  ],
  [
    'attempt decision enum',
    (payload) => {
      const routing = nestedRecord(payload['routing_manifest']);
      nestedArray(routing['attempts'])[0]['decision'] = 'unknown_decision';
    },
  ],
  [
    'attempt reason enum',
    (payload) => {
      const routing = nestedRecord(payload['routing_manifest']);
      nestedArray(routing['attempts'])[0]['reason'] = 'unknown_reason';
    },
  ],
  [
    'attempt decision and reason combination',
    (payload) => {
      const routing = nestedRecord(payload['routing_manifest']);
      const attempt = nestedArray(routing['attempts'])[0];
      attempt['decision'] = 'registry_missing';
      attempt['reason'] = 'timeout';
    },
  ],
  [
    'attempt fixed detail',
    (payload) => {
      const routing = nestedRecord(payload['routing_manifest']);
      nestedArray(routing['attempts'])[0]['detail'] = 'raw upstream detail';
    },
  ],
  [
    'attempt no-provider terminal reason',
    (payload) => {
      const routing = nestedRecord(payload['routing_manifest']);
      nestedArray(routing['attempts'])[0]['reason'] = 'no_provider';
    },
  ],
  [
    'routing category',
    (payload) => {
      nestedRecord(payload['routing_manifest'])['category'] = 'instruments';
    },
  ],
  [
    'routing request query',
    (payload) => {
      const routing = nestedRecord(payload['routing_manifest']);
      nestedRecord(nestedRecord(routing['request'])['query'])['end'] =
        '2024-01-05T00:00:00Z';
    },
  ],
  [
    'routing upstream adjustment',
    (payload) => {
      nestedRecord(payload['routing_manifest'])['upstream_adjustment'] = 'hfq';
    },
  ],
  [
    'bar identity',
    (payload) => {
      nestedArray(payload['bars'])[0]['period'] = '1w';
    },
  ],
  [
    'bar timestamp ordering',
    (payload) => {
      const bars = nestedArray(payload['bars']);
      bars.push({ ...bars[0] });
    },
  ],
  [
    'bar timestamp query range',
    (payload) => {
      nestedArray(payload['bars'])[0]['timestamp'] = '2024-01-04T00:00:00Z';
    },
  ],
  [
    'bar OHLC containment',
    (payload) => {
      nestedArray(payload['bars'])[0]['high'] = '10.2';
    },
  ],
  [
    'bar trading status enum',
    (payload) => {
      nestedArray(payload['bars'])[0]['status'] = 'bogus';
    },
  ],
  [
    'nonempty bars',
    (payload) => {
      payload['bars'] = [];
    },
  ],
  [
    'coverage versus bars',
    (payload) => {
      nestedRecord(payload['coverage'])['end'] = '2024-01-02T00:00:00Z';
    },
  ],
  [
    'top dataset versus routing',
    (payload) => {
      payload['dataset_version'] = `sha256:${'b'.repeat(64)}`;
    },
  ],
  [
    'top route versus routing',
    (payload) => {
      payload['route_version'] = `sha256:${'b'.repeat(64)}`;
    },
  ],
  [
    'selected source versus provenance',
    (payload) => {
      nestedRecord(payload['provenance'])['source'] = 'tushare';
    },
  ],
  [
    'routing fetch time versus provenance',
    (payload) => {
      nestedRecord(payload['provenance'])['fetched_at'] =
        '2024-01-03T09:00:00Z';
    },
  ],
  [
    'cutoff after fetched time',
    (payload) => {
      nestedRecord(payload['provenance'])['data_cutoff'] =
        '2024-01-03T09:00:00Z';
    },
  ],
  [
    'cutoff before final bar',
    (payload) => {
      nestedRecord(payload['routing_manifest'])['upstream_data_cutoff'] =
        '2024-01-02T15:59:59Z';
      nestedRecord(payload['provenance'])['data_cutoff'] =
        '2024-01-02T15:59:59Z';
    },
  ],
  [
    'provenance adjustment',
    (payload) => {
      nestedRecord(payload['provenance'])['adjustment'] = 'hfq';
    },
  ],
  [
    'provenance dataset digest',
    (payload) => {
      nestedRecord(payload['provenance'])['dataset_version'] = 'not-a-digest';
    },
  ],
  [
    'attempt ordinals',
    (payload) => {
      const routing = nestedRecord(payload['routing_manifest']);
      nestedArray(routing['attempts'])[0]['ordinal'] = 2;
    },
  ],
  [
    'attempt priority source',
    (payload) => {
      const routing = nestedRecord(payload['routing_manifest']);
      nestedArray(routing['attempts'])[0]['source'] = 'baostock';
    },
  ],
  [
    'selected source priority index',
    (payload) => {
      nestedRecord(payload['routing_manifest'])['selected_source'] = 'tushare';
    },
  ],
];

const staleHashedAttemptCases = [
  ['registry_missing', 'provider_unavailable', 'provider is not registered'],
  [
    'capability_skip',
    'unsupported',
    'provider capability does not support this request',
  ],
  [
    'fetch_failure',
    'missing',
    'provider response does not cover the full request',
  ],
] as const;

it.each(staleHashedAttemptCases)(
  'rejects routing attempt %s/%s when its content hashes are stale',
  async (decision, reason, detail) => {
    const payload = structuredClone(backendBarsResponse) as MutableJsonRecord;
    const routing = nestedRecord(payload['routing_manifest']);
    const attempt = nestedArray(routing['attempts'])[0];
    attempt['decision'] = decision;
    attempt['reason'] = reason;
    attempt['detail'] = detail;

    await expect(
      createMarketApi(clientReturning(payload as JsonValue).client).getBars({
        symbol: '600000.SH',
        period: '1d',
        adjustment: 'qfq',
      }),
    ).rejects.toBeInstanceOf(MarketProtocolError);
  },
);

it('rejects a route version that does not hash its canonical route payload', async () => {
  const payload = structuredClone(backendBarsResponse) as MutableJsonRecord;
  const falseDigest = `sha256:${'f'.repeat(64)}`;
  payload['route_version'] = falseDigest;
  nestedRecord(payload['routing_manifest'])['route_version'] = falseDigest;

  await expect(
    createMarketApi(clientReturning(payload as JsonValue).client).getBars({
      symbol: '600000.SH',
      period: '1d',
      adjustment: 'qfq',
    }),
  ).rejects.toBeInstanceOf(MarketProtocolError);
});

it('rejects a manifest record id that is not the full manifest content hash', async () => {
  const payload = structuredClone(backendBarsResponse) as MutableJsonRecord;
  payload['manifest_record_id'] = `sha256:${'f'.repeat(64)}`;

  await expect(
    createMarketApi(clientReturning(payload as JsonValue).client).getBars({
      symbol: '600000.SH',
      period: '1d',
      adjustment: 'qfq',
    }),
  ).rejects.toBeInstanceOf(MarketProtocolError);
});

it('content-verifies every backend market API without WebCrypto subtle', async () => {
  const originalCrypto = globalThis.crypto;
  vi.stubGlobal('crypto', {});
  try {
    await expect(
      createMarketApi(clientReturning(backendBarsResponse).client).getBars({
        symbol: '600000.SH',
        period: '1d',
        adjustment: 'qfq',
      }),
    ).resolves.toMatchObject({ bars: [{ symbol: '600000.SH' }] });
    await expect(
      createMarketApi(
        clientReturning(backendInstrumentsResponse).client,
      ).searchInstruments({ query: '浦发' }),
    ).resolves.toMatchObject([{ symbol: '600000.SH' }]);
    await expect(
      createMarketApi(
        clientReturning(backendPresetPoolResponse.page).client,
      ).getPools(),
    ).resolves.toMatchObject({ items: [{ poolId: 'preset:all-a' }] });
    await expect(
      createMarketApi(
        clientReturning(backendPresetPoolResponse.detail).client,
      ).getPool('preset:all-a'),
    ).resolves.toMatchObject({ poolId: 'preset:all-a' });

    const forged = structuredClone(backendBarsResponse) as MutableJsonRecord;
    forged['manifest_record_id'] = `sha256:${'f'.repeat(64)}`;
    await expect(
      createMarketApi(clientReturning(forged as JsonValue).client).getBars({
        symbol: '600000.SH',
        period: '1d',
        adjustment: 'qfq',
      }),
    ).rejects.toBeInstanceOf(MarketProtocolError);
  } finally {
    vi.unstubAllGlobals();
  }
  expect(globalThis.crypto).toBe(originalCrypto);
});

it('accepts and content-verifies the backend instrument catalog fixture', async () => {
  await expect(
    createMarketApi(
      clientReturning(backendInstrumentsResponse).client,
    ).searchInstruments({
      query: '浦发',
    }),
  ).resolves.toMatchObject([{ symbol: '600000.SH' }]);
});

it('rejects a false instrument catalog manifest record id', async () => {
  const payload = structuredClone(
    backendInstrumentsResponse,
  ) as MutableJsonRecord[];
  nestedRecord(payload[0]?.['provenance'])['manifest_record_id'] =
    `sha256:${'f'.repeat(64)}`;

  await expect(
    createMarketApi(
      clientReturning(payload as JsonValue).client,
    ).searchInstruments({
      query: '浦发',
    }),
  ).rejects.toBeInstanceOf(MarketProtocolError);
});

it.each([
  ['17 integer digits', '12345678901234567'],
  ['9 fractional digits', '1.123456789'],
] as const)(
  'rejects backend-invalid price precision: %s',
  async (_name, price) => {
    const payload = structuredClone(backendBarsResponse) as MutableJsonRecord;
    const bar = nestedArray(payload['bars'])[0];
    bar['open'] = price;
    bar['high'] = price;
    bar['low'] = price;
    bar['close'] = price;

    await expect(
      createMarketApi(clientReturning(payload as JsonValue).client).getBars({
        symbol: '600000.SH',
        period: '1d',
        adjustment: 'qfq',
      }),
    ).rejects.toBeInstanceOf(MarketProtocolError);
  },
);

it('preserves a backend-valid 24-digit canonical price without display rounding', async () => {
  const payload = structuredClone(backendBarsResponse) as MutableJsonRecord;
  const bar = nestedArray(payload['bars'])[0];
  for (const field of ['open', 'high', 'low', 'close']) {
    bar[field] = '9999999999999999.99999999';
  }

  const result = await createMarketApi(
    clientReturning(payload as JsonValue).client,
  ).getBars({
    symbol: '600000.SH',
    period: '1d',
    adjustment: 'qfq',
  });

  expect(result.bars[0]).toMatchObject({
    priceText: {
      open: '9999999999999999.99999999',
      high: '9999999999999999.99999999',
      low: '9999999999999999.99999999',
      close: '9999999999999999.99999999',
    },
  });
});

it.each([
  ['1d', backendBarsResponse, '1d'],
  ['1w', backendPeriodBarsResponses.weekly, '1w'],
  ['60m', backendPeriodBarsResponses.min60, '60m'],
] as const)(
  'accepts the backend canonical %s bucket fixture',
  async (_name, payload, period) => {
    await expect(
      createMarketApi(clientReturning(payload).client).getBars({
        symbol: '600000.SH',
        period,
        adjustment: 'qfq',
      }),
    ).resolves.toMatchObject({ bars: [{ period }] });
  },
);

it.each([
  ['1d', '1988-05-31T15:00:00Z', '1d'],
  ['1w', '1988-06-05T15:00:00Z', '1w'],
  ['60m', '1988-06-01T00:30:00Z', '60m'],
] as const)(
  'uses historical Asia/Shanghai DST for backend-canonical %s buckets',
  (_name, timestamp, period) => {
    expect(isCanonicalBucketStart(timestamp, period)).toBe(true);
  },
);

it.each([
  [
    '1d local time is not midnight',
    backendBarsResponse,
    '2024-01-02T00:00:00Z',
  ],
  [
    '1w local time is not Monday midnight',
    backendPeriodBarsResponses.weekly,
    '2024-01-07T15:00:00Z',
  ],
  [
    '60m local time is not a configured bucket',
    backendPeriodBarsResponses.min60,
    '2024-01-03T00:30:00Z',
  ],
  ['nonzero seconds', backendBarsResponse, '2024-01-02T16:00:01Z'],
  ['nonzero milliseconds', backendBarsResponse, '2024-01-02T16:00:00.001Z'],
] as const)(
  'rejects a noncanonical bar bucket: %s',
  async (_name, fixture, timestamp) => {
    const payload = structuredClone(fixture) as MutableJsonRecord;
    nestedArray(payload['bars'])[0]['timestamp'] = timestamp;
    const period = nestedRecord(payload['query'])['period'];

    await expect(
      createMarketApi(clientReturning(payload as JsonValue).client).getBars({
        symbol: '600000.SH',
        period: period as '1d' | '1w' | '60m',
        adjustment: 'qfq',
      }),
    ).rejects.toBeInstanceOf(MarketProtocolError);
  },
);

const transitionMismatchCases: readonly [
  string,
  (transition: MutableJsonRecord) => void,
][] = [
  ['reason enum', (transition) => (transition['reason'] = 'unknown_reason')],
  [
    'distinct source',
    (transition) => (transition['from_source'] = transition['to_source']),
  ],
  [
    'distinct dataset',
    (transition) =>
      (transition['from_dataset_version'] = transition['to_dataset_version']),
  ],
  [
    'manifest category',
    (transition) => (transition['category'] = 'instruments'),
  ],
  ['selected source', (transition) => (transition['to_source'] = 'tushare')],
  [
    'upstream dataset',
    (transition) =>
      (transition['to_dataset_version'] = `sha256:${'4'.repeat(64)}`),
  ],
  [
    'bar effective boundary',
    (transition) => (transition['effective_at'] = '2024-01-02T01:00:00Z'),
  ],
  [
    'bar calendar boundary',
    (transition) => (transition['calendar_start'] = '2024-01-02'),
  ],
];

it.each(transitionMismatchCases)(
  'rejects source transition mismatch: %s',
  async (_name, mutate) => {
    const payload = structuredClone(backendBarsResponse) as MutableJsonRecord;
    const routing = nestedRecord(payload['routing_manifest']);
    const transition = nestedRecord(routing['transition']);
    mutate(transition);

    await expect(
      createMarketApi(clientReturning(payload as JsonValue).client).getBars({
        symbol: '600000.SH',
        period: '1d',
        adjustment: 'qfq',
      }),
    ).rejects.toBeInstanceOf(MarketProtocolError);
  },
);

function transitionManifest(
  category: 'instruments' | 'trading_calendar',
): MutableJsonRecord {
  const payload = structuredClone(backendBarsResponse) as MutableJsonRecord;
  const routing = nestedRecord(payload['routing_manifest']);
  const attempt = nestedArray(routing['attempts'])[0];
  const transition = nestedRecord(routing['transition']);
  routing['category'] = category;
  routing['request'] =
    category === 'instruments'
      ? {}
      : { exchange: 'SH', start: '2024-01-02', end: '2024-01-04' };
  routing['upstream_adjustment'] = null;
  attempt['category'] = category;
  transition['category'] = category;
  if (category === 'instruments') {
    transition['effective_at'] = routing['upstream_fetched_at'];
  } else {
    transition['effective_at'] = null;
    transition['calendar_start'] = '2024-01-02';
    transition['calendar_end'] = '2024-01-04';
  }
  return routing;
}

it.each(['instruments', 'trading_calendar'] as const)(
  'accepts the backend %s transition boundary contract',
  (category) => {
    expect(
      decodeRoutingManifest(
        transitionManifest(category) as JsonValue,
        'routing',
      ),
    ).toMatchObject({ category });
  },
);

it.each([
  [
    'instrument effective time',
    () => transitionManifest('instruments'),
    (transition: MutableJsonRecord) => {
      transition['effective_at'] = '2024-01-03T07:59:59Z';
    },
  ],
  [
    'calendar request boundary',
    () => transitionManifest('trading_calendar'),
    (transition: MutableJsonRecord) => {
      transition['calendar_end'] = '2024-01-05';
    },
  ],
  [
    'calendar nonempty boundary',
    () => transitionManifest('trading_calendar'),
    (transition: MutableJsonRecord) => {
      transition['calendar_end'] = transition['calendar_start'];
    },
  ],
] as const)(
  'rejects source transition mismatch: %s',
  (_name, createManifest, mutate) => {
    const manifest = createManifest();
    mutate(nestedRecord(manifest['transition']));
    expect(() =>
      decodeRoutingManifest(manifest as JsonValue, 'routing'),
    ).toThrow(MarketProtocolError);
  },
);

it.each(auditMismatchCases)(
  'rejects bars audit mismatch: %s',
  async (_name, mutate) => {
    const payload = structuredClone(barsResponse) as MutableJsonRecord;
    mutate(payload);

    await expect(
      createMarketApi(clientReturning(payload as JsonValue).client).getBars({
        symbol: '600000.SH',
        period: '1d',
        adjustment: 'qfq',
      }),
    ).rejects.toBeInstanceOf(MarketProtocolError);
  },
);

it('decodes backend preset details and keeps custom summaries explicit', async () => {
  const page = structuredClone(
    backendPresetPoolResponse.page,
  ) as MutableJsonRecord;
  const presetSummary = nestedArray(page['items'])[0];
  nestedArray(page['items']).push({
    ...structuredClone(presetSummary),
    pool_id: 'custom-watch',
    kind: 'custom',
    name: '自选观察',
    category: null,
    revision: 1,
    snapshot_id: null,
  });
  page['next_cursor'] = 'next/pool';
  const get = vi
    .fn<ApiClient['get']>()
    .mockResolvedValueOnce(page as JsonValue)
    .mockResolvedValueOnce(backendPresetPoolResponse.detail);
  const api = createMarketApi({ get });

  const result = await api.getPools({ cursor: '上一页/末项', limit: 20 });
  const detail = await api.getPool('preset:all-a');

  expect(get).toHaveBeenNthCalledWith(
    1,
    '/market/pools?limit=20&cursor=%E4%B8%8A%E4%B8%80%E9%A1%B5%2F%E6%9C%AB%E9%A1%B9',
    { signal: undefined },
  );
  expect(get).toHaveBeenNthCalledWith(2, '/market/pools/preset%3Aall-a', {
    signal: undefined,
  });
  expect(result.items[0]).toMatchObject({ kind: 'preset', category: 'all_a' });
  expect(result.items[1]).toMatchObject({
    kind: 'custom',
    category: null,
    revision: 1,
  });
  expect(detail.members[0]).toMatchObject({ symbol: '600000.SH' });
});

function catalogProvenancePayload(includeInstrumentDataset: boolean) {
  const fixture = backendInstrumentsResponse[0];
  if (fixture === undefined) throw new Error('Instrument fixture is empty');
  const provenance = structuredClone(fixture.provenance);
  return {
    ...provenance,
    ...(includeInstrumentDataset
      ? { instrument_dataset_version: provenance.dataset_version }
      : {}),
  };
}

function instrumentPayload() {
  const fixture = backendInstrumentsResponse[0];
  if (fixture === undefined) throw new Error('Instrument fixture is empty');
  return structuredClone(fixture);
}

function poolPagePayload() {
  return {
    items: [
      {
        pool_id: 'preset-all-a',
        kind: 'preset',
        name: '全量 A 股',
        category: 'all_a',
        revision: null,
        member_count: 1,
        snapshot_id: DIGEST,
        provenance: catalogProvenancePayload(true),
      },
    ],
    next_cursor: null,
  };
}

const catalogMismatchCases: readonly [
  string,
  (provenance: MutableJsonRecord) => void,
][] = [
  [
    'routing category',
    (catalog) => {
      const routing = nestedRecord(catalog['routing_manifest']);
      routing['category'] = 'trading_calendar';
      routing['request'] = {
        exchange: 'SH',
        start: '2024-01-02',
        end: '2024-01-04',
      };
    },
  ],
  ['selected source', (catalog) => (catalog['source'] = 'baostock')],
  [
    'upstream dataset',
    (catalog) => (catalog['dataset_version'] = `sha256:${'b'.repeat(64)}`),
  ],
  [
    'route version',
    (catalog) => (catalog['route_version'] = `sha256:${'b'.repeat(64)}`),
  ],
  [
    'fetched time',
    (catalog) => (catalog['fetched_at'] = '2024-01-03T09:00:00Z'),
  ],
  [
    'cutoff time',
    (catalog) => (catalog['data_cutoff'] = '2024-01-03T06:00:00Z'),
  ],
];

it.each(catalogMismatchCases)(
  'rejects instrument catalog provenance mismatch: %s',
  async (_name, mutate) => {
    const item = instrumentPayload() as MutableJsonRecord;
    mutate(nestedRecord(item['provenance']));

    await expect(
      createMarketApi(
        clientReturning([item] as JsonValue).client,
      ).searchInstruments({
        query: '浦发',
      }),
    ).rejects.toBeInstanceOf(MarketProtocolError);
  },
);

it.each(catalogMismatchCases)(
  'rejects pool catalog provenance mismatch: %s',
  async (_name, mutate) => {
    const page = poolPagePayload() as MutableJsonRecord;
    const summary = nestedArray(page['items'])[0];
    mutate(nestedRecord(summary['provenance']));

    await expect(
      createMarketApi(clientReturning(page as JsonValue).client).getPools(),
    ).rejects.toBeInstanceOf(MarketProtocolError);
  },
);

it('rejects a pool instrument dataset that differs from catalog provenance', async () => {
  const page = poolPagePayload() as MutableJsonRecord;
  const summary = nestedArray(page['items'])[0];
  nestedRecord(summary['provenance'])['instrument_dataset_version'] =
    `sha256:${'b'.repeat(64)}`;

  await expect(
    createMarketApi(clientReturning(page as JsonValue).client).getPools(),
  ).rejects.toBeInstanceOf(MarketProtocolError);
});

it('rejects a custom pool summary above the backend 5k member limit', async () => {
  const page = poolPagePayload() as MutableJsonRecord;
  const summary = nestedArray(page['items'])[0];
  summary['kind'] = 'custom';
  summary['category'] = null;
  summary['revision'] = 1;
  summary['member_count'] = 5_001;
  summary['snapshot_id'] = null;

  await expect(
    createMarketApi(clientReturning(page as JsonValue).client).getPools(),
  ).rejects.toBeInstanceOf(MarketProtocolError);
});

function poolDetailPayload(): MutableJsonRecord {
  const summary = structuredClone(
    poolPagePayload().items[0],
  ) as MutableJsonRecord;
  summary['pool_id'] = 'custom-watch';
  summary['kind'] = 'custom';
  summary['category'] = null;
  summary['revision'] = 1;
  summary['snapshot_id'] = null;
  summary['member_count'] = 2;
  summary['members'] = [
    {
      ordinal: 0,
      symbol: '600000.SH',
      name: '浦发银行',
      instrument_kind: 'stock',
      listing_status: 'listed',
    },
    {
      ordinal: 1,
      symbol: '000001.SZ',
      name: '平安银行',
      instrument_kind: 'stock',
      listing_status: 'listed',
    },
  ];
  return summary;
}

const poolDetailMismatchCases: readonly [
  string,
  (detail: MutableJsonRecord) => void,
][] = [
  ['member count mismatch', (detail) => (detail['member_count'] = 1)],
  [
    'empty pool detail',
    (detail) => {
      detail['member_count'] = 0;
      detail['members'] = [];
    },
  ],
  [
    'member ordinal',
    (detail) => (nestedArray(detail['members'])[1]['ordinal'] = 7),
  ],
  [
    'duplicate symbol',
    (detail) =>
      (nestedArray(detail['members'])[1]['symbol'] = nestedArray(
        detail['members'],
      )[0]['symbol']),
  ],
  [
    'bogus instrument kind',
    (detail) =>
      (nestedArray(detail['members'])[0]['instrument_kind'] = 'bogus'),
  ],
  [
    'bogus listing status',
    (detail) => (nestedArray(detail['members'])[0]['listing_status'] = 'bogus'),
  ],
  [
    'delisted member',
    (detail) =>
      (nestedArray(detail['members'])[0]['listing_status'] = 'delisted'),
  ],
];

it.each(poolDetailMismatchCases)(
  'rejects pool detail mismatch: %s',
  async (_name, mutate) => {
    const detail = poolDetailPayload();
    mutate(detail);

    await expect(
      createMarketApi(clientReturning(detail as JsonValue).client).getPool(
        'custom-watch',
      ),
    ).rejects.toBeInstanceOf(MarketProtocolError);
  },
);

function backendPresetDetail(): MutableJsonRecord {
  return structuredClone(backendPresetPoolResponse.detail);
}

it('accepts the backend DTO and repository-hashed preset summary and detail', async () => {
  const get = vi
    .fn<ApiClient['get']>()
    .mockResolvedValueOnce(backendPresetPoolResponse.page)
    .mockResolvedValueOnce(backendPresetPoolResponse.detail);
  const api = createMarketApi({ get });

  await expect(api.getPools()).resolves.toMatchObject({
    items: [{ poolId: 'preset:all-a' }],
  });
  await expect(api.getPool('preset:all-a')).resolves.toMatchObject({
    snapshotId:
      'sha256:498e863485711c2d1d81724280f5595f7a2445d7005ccbf076e8649aa9c218e2',
    provenance: {
      source: 'akshare',
      datasetVersion:
        'sha256:5555555555555555555555555555555555555555555555555555555555555555',
      fetchedAt: '2024-01-04T10:00:00Z',
      composition: {
        presetKey: 'all-a',
        symbols: ['600000.SH'],
        displayName: '全量 A 股',
        source: 'tushare',
        datasetVersion:
          'sha256:4444444444444444444444444444444444444444444444444444444444444444',
        fetchedAt: '2024-01-03T08:00:00Z',
      },
    },
  });
});

const presetCompositionMismatchCases: readonly [
  string,
  (detail: MutableJsonRecord, composition: MutableJsonRecord) => void,
][] = [
  [
    'ordered member symbols',
    (_detail, composition) => (composition['symbols'] = ['000001.SZ']),
  ],
  ['empty symbols', (_detail, composition) => (composition['symbols'] = [])],
  [
    'unique composition symbols',
    (_detail, composition) =>
      (composition['symbols'] = ['600000.SH', '600000.SH']),
  ],
  [
    'complete flag',
    (_detail, composition) => (composition['complete'] = false),
  ],
  [
    'preset key bounds',
    (_detail, composition) => (composition['preset_key'] = 'Bad Key'),
  ],
  [
    'display name bounds',
    (_detail, composition) => (composition['display_name'] = ' 全量 A 股'),
  ],
  [
    'composition cutoff',
    (_detail, composition) =>
      (composition['data_cutoff'] = '2024-01-03T09:00:00Z'),
  ],
  ['pool id', (detail) => (detail['pool_id'] = 'preset:other')],
  ['pool name', (detail) => (detail['name'] = '其他名称')],
  ['pool category', (detail) => (detail['category'] = 'industry')],
];

it.each(presetCompositionMismatchCases)(
  'rejects preset composition mismatch: %s',
  async (_name, mutate) => {
    const detail = backendPresetDetail();
    const provenance = nestedRecord(detail['provenance']);
    const composition = nestedRecord(provenance['composition']);
    mutate(detail, composition);

    await expect(
      createMarketApi(clientReturning(detail as JsonValue).client).getPool(
        'preset:all-a',
      ),
    ).rejects.toBeInstanceOf(MarketProtocolError);
  },
);

it('rejects an arbitrary preset snapshot id', async () => {
  const detail = backendPresetDetail();
  detail['snapshot_id'] = `sha256:${'f'.repeat(64)}`;

  await expect(
    createMarketApi(clientReturning(detail as JsonValue).client).getPool(
      'preset:all-a',
    ),
  ).rejects.toBeInstanceOf(MarketProtocolError);
});

it('requires composition on preset detail and forbids it on custom detail', async () => {
  const missing = backendPresetDetail();
  delete nestedRecord(missing['provenance'])['composition'];
  const custom = backendPresetDetail();
  custom['pool_id'] = 'custom-one';
  custom['kind'] = 'custom';
  custom['category'] = null;
  custom['revision'] = 1;
  custom['snapshot_id'] = null;

  const api = createMarketApi(clientReturning(missing as JsonValue).client);
  await expect(api.getPool('preset:all-a')).rejects.toBeInstanceOf(
    MarketProtocolError,
  );
  await expect(
    createMarketApi(clientReturning(custom as JsonValue).client).getPool(
      'custom-one',
    ),
  ).rejects.toBeInstanceOf(MarketProtocolError);
});

it('accepts a custom detail without preset composition or snapshot', async () => {
  const custom = poolDetailPayload();

  await expect(
    createMarketApi(clientReturning(custom as JsonValue).client).getPool(
      'custom-watch',
    ),
  ).resolves.toMatchObject({
    poolId: 'custom-watch',
    kind: 'custom',
    revision: 1,
    snapshotId: null,
    members: [{ ordinal: 0 }, { ordinal: 1 }],
  });
});
