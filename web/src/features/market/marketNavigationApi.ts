import {
  createApiClient,
  type ApiClient,
  type JsonValue,
} from '../../shared/api/client';
import type { MarketInstrument } from './marketApi';

const NAVIGATION_PATH = '/v1/market/navigation';
const SYMBOL_PATTERN = /^[0-9]{6}\.(?:SS|SH|SZ|BJ)$/u;
const MAX_WATCHLIST = 100;
const MAX_RECENT = 20;

export type MarketNavigationState = {
  readonly schemaVersion: 1;
  readonly revision: number;
  readonly watchlist: readonly MarketNavigationInstrument[];
  readonly recent: readonly MarketNavigationInstrument[];
  readonly notice: MarketNavigationNotice;
};

export type MarketNavigationInstrument = {
  readonly symbol: string;
  readonly name: string;
  readonly instrumentKind: MarketInstrument['instrumentKind'];
};

export type MarketNavigationNotice = null | {
  readonly code: 'market_navigation_state_reset';
  readonly reason: 'corrupt' | 'unsupported_schema';
};

export type MarketNavigationApi = {
  readonly get: (options?: {
    readonly signal?: AbortSignal;
  }) => Promise<MarketNavigationState>;
  readonly put: (
    value: {
      readonly expectedRevision: number;
      readonly watchlist: readonly MarketNavigationInstrument[];
      readonly recent: readonly MarketNavigationInstrument[];
    },
    options?: { readonly signal?: AbortSignal },
  ) => Promise<MarketNavigationState>;
};

export class MarketNavigationProtocolError extends Error {
  constructor(path: string) {
    super(`行情导航 API 响应不符合协议：${path}`);
    this.name = 'MarketNavigationProtocolError';
  }
}

function record(
  value: JsonValue | undefined,
  path: string,
): Record<string, JsonValue> {
  if (
    value === undefined ||
    value === null ||
    Array.isArray(value) ||
    typeof value !== 'object'
  ) {
    throw new MarketNavigationProtocolError(path);
  }
  return value as Record<string, JsonValue>;
}

function exactKeys(
  value: Record<string, JsonValue>,
  expected: readonly string[],
  path: string,
) {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (
    actual.length !== wanted.length ||
    actual.some((key, index) => key !== wanted[index])
  ) {
    throw new MarketNavigationProtocolError(path);
  }
}

function decodeInstruments(
  value: JsonValue | undefined,
  path: string,
  maximum: number,
): readonly MarketNavigationInstrument[] {
  if (!Array.isArray(value) || value.length > maximum) {
    throw new MarketNavigationProtocolError(path);
  }
  const entries = value as readonly JsonValue[];
  const symbols = new Set<string>();
  return entries.map((entry, index) => {
    const item = record(entry, `${path}[${String(index)}]`);
    exactKeys(
      item,
      ['symbol', 'name', 'instrument_kind'],
      `${path}[${String(index)}]`,
    );
    const symbol = item['symbol'];
    const name = item['name'];
    const instrumentKind = item['instrument_kind'];
    if (
      typeof symbol !== 'string' ||
      !SYMBOL_PATTERN.test(symbol) ||
      symbols.has(symbol) ||
      typeof name !== 'string' ||
      name.length === 0 ||
      name.length > 255 ||
      typeof instrumentKind !== 'string' ||
      !['stock', 'index', 'etf', 'fund', 'bond'].includes(instrumentKind)
    ) {
      throw new MarketNavigationProtocolError(`${path}[${String(index)}]`);
    }
    symbols.add(symbol);
    return {
      symbol,
      name,
      instrumentKind:
        instrumentKind as MarketNavigationInstrument['instrumentKind'],
    };
  });
}

function decodeNotice(value: JsonValue | undefined): MarketNavigationNotice {
  if (value === null) return null;
  const item = record(value, 'response.notice');
  exactKeys(item, ['code', 'reason'], 'response.notice');
  if (
    item['code'] !== 'market_navigation_state_reset' ||
    (item['reason'] !== 'corrupt' && item['reason'] !== 'unsupported_schema')
  ) {
    throw new MarketNavigationProtocolError('response.notice');
  }
  return { code: item['code'], reason: item['reason'] };
}

function decodeState(value: JsonValue | undefined): MarketNavigationState {
  const item = record(value, 'response');
  exactKeys(
    item,
    ['schema_version', 'revision', 'watchlist', 'recent', 'notice'],
    'response',
  );
  if (item['schema_version'] !== 1) {
    throw new MarketNavigationProtocolError('response.schema_version');
  }
  const revision = item['revision'];
  if (
    typeof revision !== 'number' ||
    !Number.isSafeInteger(revision) ||
    revision < 0
  ) {
    throw new MarketNavigationProtocolError('response.revision');
  }
  return {
    schemaVersion: 1,
    revision,
    watchlist: decodeInstruments(
      item['watchlist'],
      'response.watchlist',
      MAX_WATCHLIST,
    ),
    recent: decodeInstruments(item['recent'], 'response.recent', MAX_RECENT),
    notice: decodeNotice(item['notice']),
  };
}

export function prependRecentInstrument(
  recent: readonly MarketNavigationInstrument[],
  instrument: MarketNavigationInstrument,
): readonly MarketNavigationInstrument[] {
  return [
    instrument,
    ...recent.filter((item) => item.symbol !== instrument.symbol),
  ].slice(0, MAX_RECENT);
}

export function createMarketNavigationApi(
  client: Pick<ApiClient, 'get' | 'put'> = createApiClient(),
): MarketNavigationApi {
  return {
    async get(options = {}) {
      return decodeState(
        await client.get(NAVIGATION_PATH, { signal: options.signal }),
      );
    },
    async put(value, options = {}) {
      return decodeState(
        await client.put(NAVIGATION_PATH, {
          body: {
            expected_revision: value.expectedRevision,
            watchlist: value.watchlist.map((item) => ({
              symbol: item.symbol,
              name: item.name,
              instrument_kind: item.instrumentKind,
            })),
            recent: value.recent.map((item) => ({
              symbol: item.symbol,
              name: item.name,
              instrument_kind: item.instrumentKind,
            })),
          },
          signal: options.signal,
        }),
      );
    },
  };
}

export const marketNavigationApi = createMarketNavigationApi();
