import {
  createApiClient,
  type ApiClient,
  type JsonValue,
} from '../shared/api/client';
import type {
  MarketAdjustment,
  MarketChartPreference,
  MarketInstrumentSelection,
  MarketPeriod,
  MarketSubchartPreference,
  MarketZoom,
} from '../features/market/marketStore';

const WORKSPACE_PATH = '/v1/workspace';
const ROUTES = [
  '/market',
  '/formulas',
  '/backtests',
  '/analysis',
  '/tasks',
  '/settings',
] as const;
const PERIODS = ['1d', '1w', '60m'] as const;
const ADJUSTMENTS = ['none', 'qfq', 'hfq'] as const;
const EXCHANGES = ['SH', 'SZ', 'BJ'] as const;
const INSTRUMENT_KINDS = ['stock', 'index', 'etf', 'fund', 'bond'] as const;
const NOTICES = [
  'workspace_missing',
  'workspace_corrupt',
  'workspace_schema_unsupported',
  'workspace_expired',
  'workspace_route_invalid',
  'workspace_instrument_unavailable',
  'workspace_chart_unavailable',
] as const;
const SYMBOL_PATTERN = /^(?:[0-9]{6}\.(?:SH|SZ|BJ)|000001\.SS)$/u;
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;

export type WorkspaceRoute = (typeof ROUTES)[number];
export type WorkspaceNotice = (typeof NOTICES)[number];

export type WorkspaceInstrument = MarketInstrumentSelection & {
  readonly exchange: (typeof EXCHANGES)[number];
  readonly instrumentKind: (typeof INSTRUMENT_KINDS)[number];
};

export type WorkspaceValue = {
  readonly currentPage: WorkspaceRoute;
  readonly instrument: WorkspaceInstrument;
  readonly period: MarketPeriod;
  readonly adjustment: MarketAdjustment;
  readonly zoom: MarketZoom;
  readonly mainChart: MarketChartPreference;
  readonly subchart: MarketSubchartPreference;
};

export type WorkspaceState = {
  readonly schemaVersion: 1;
  readonly revision: number;
  readonly updatedAt: string | null;
  readonly expiresAt: string | null;
  readonly restored: boolean;
  readonly notice: WorkspaceNotice | null;
  readonly workspace: WorkspaceValue;
};

export type WorkspaceApi = {
  readonly get: (options?: {
    readonly signal?: AbortSignal;
  }) => Promise<WorkspaceState>;
  readonly put: (
    request: {
      readonly expectedRevision: number;
      readonly workspace: WorkspaceValue;
    },
    options?: { readonly signal?: AbortSignal },
  ) => Promise<WorkspaceState>;
};

export class WorkspaceProtocolError extends Error {
  constructor(path: string) {
    super(`工作区 API 响应不符合协议：${path}`);
    this.name = 'WorkspaceProtocolError';
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
    throw new WorkspaceProtocolError(path);
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
    throw new WorkspaceProtocolError(path);
  }
}

function enumeration<T extends string>(
  value: JsonValue | undefined,
  values: readonly T[],
  path: string,
): T {
  if (typeof value !== 'string' || !values.includes(value as T)) {
    throw new WorkspaceProtocolError(path);
  }
  return value as T;
}

function timestamp(value: JsonValue | undefined, path: string): string | null {
  if (value === null) return null;
  if (
    typeof value !== 'string' ||
    !/^\d{4}-\d{2}-\d{2}T/u.test(value) ||
    Number.isNaN(Date.parse(value))
  ) {
    throw new WorkspaceProtocolError(path);
  }
  return value;
}

function decodeInstrument(value: JsonValue | undefined): WorkspaceInstrument {
  const item = record(value, 'response.workspace.instrument');
  exactKeys(
    item,
    ['symbol', 'name', 'exchange', 'kind'],
    'response.workspace.instrument',
  );
  const symbol = item['symbol'];
  const name = item['name'];
  if (
    typeof symbol !== 'string' ||
    !SYMBOL_PATTERN.test(symbol) ||
    typeof name !== 'string' ||
    name.length === 0 ||
    name.length > 255 ||
    name.trim() !== name
  ) {
    throw new WorkspaceProtocolError('response.workspace.instrument');
  }
  return {
    symbol,
    name,
    exchange: enumeration(
      item['exchange'],
      EXCHANGES,
      'response.workspace.instrument.exchange',
    ),
    instrumentKind: enumeration(
      item['kind'],
      INSTRUMENT_KINDS,
      'response.workspace.instrument.kind',
    ),
  };
}

function decodeZoom(value: JsonValue | undefined): MarketZoom {
  const item = record(value, 'response.workspace.zoom');
  exactKeys(item, ['start', 'end'], 'response.workspace.zoom');
  const start = item['start'];
  const end = item['end'];
  if (
    typeof start !== 'number' ||
    typeof end !== 'number' ||
    !Number.isFinite(start) ||
    !Number.isFinite(end) ||
    start < 0 ||
    start >= end ||
    end > 100
  ) {
    throw new WorkspaceProtocolError('response.workspace.zoom');
  }
  return { start, end };
}

function decodeSubchart(
  value: JsonValue | undefined,
): MarketSubchartPreference {
  const item = record(value, 'response.workspace.subchart');
  const kind = item['kind'];
  if (kind === 'none' || kind === 'volume') {
    exactKeys(item, ['kind'], 'response.workspace.subchart');
    return { kind };
  }
  if (kind === 'formula') {
    exactKeys(
      item,
      ['kind', 'formula_version_id'],
      'response.workspace.subchart',
    );
    const formulaVersionId = item['formula_version_id'];
    if (
      typeof formulaVersionId !== 'string' ||
      !UUID_PATTERN.test(formulaVersionId)
    ) {
      throw new WorkspaceProtocolError(
        'response.workspace.subchart.formula_version_id',
      );
    }
    return { kind, formulaVersionId };
  }
  throw new WorkspaceProtocolError('response.workspace.subchart.kind');
}

function decodeWorkspace(value: JsonValue | undefined): WorkspaceValue {
  const item = record(value, 'response.workspace');
  exactKeys(
    item,
    [
      'current_page',
      'instrument',
      'period',
      'adjustment',
      'zoom',
      'main_chart',
      'subchart',
    ],
    'response.workspace',
  );
  return {
    currentPage: enumeration(
      item['current_page'],
      ROUTES,
      'response.workspace.current_page',
    ),
    instrument: decodeInstrument(item['instrument']),
    period: enumeration(item['period'], PERIODS, 'response.workspace.period'),
    adjustment: enumeration(
      item['adjustment'],
      ADJUSTMENTS,
      'response.workspace.adjustment',
    ),
    zoom: decodeZoom(item['zoom']),
    mainChart: enumeration(
      item['main_chart'],
      ['candlestick'] as const,
      'response.workspace.main_chart',
    ),
    subchart: decodeSubchart(item['subchart']),
  };
}

function decodeState(value: JsonValue | undefined): WorkspaceState {
  const item = record(value, 'response');
  exactKeys(
    item,
    [
      'schema_version',
      'revision',
      'updated_at',
      'expires_at',
      'restored',
      'notice',
      'workspace',
    ],
    'response',
  );
  const revision = item['revision'];
  const restored = item['restored'];
  const notice = item['notice'];
  if (
    item['schema_version'] !== 1 ||
    typeof revision !== 'number' ||
    !Number.isSafeInteger(revision) ||
    revision < 0 ||
    typeof restored !== 'boolean' ||
    (notice !== null &&
      (typeof notice !== 'string' ||
        !NOTICES.includes(notice as WorkspaceNotice)))
  ) {
    throw new WorkspaceProtocolError('response');
  }
  return {
    schemaVersion: 1,
    revision,
    updatedAt: timestamp(item['updated_at'], 'response.updated_at'),
    expiresAt: timestamp(item['expires_at'], 'response.expires_at'),
    restored,
    notice: notice as WorkspaceNotice | null,
    workspace: decodeWorkspace(item['workspace']),
  };
}

function encodeSubchart(value: MarketSubchartPreference): JsonValue {
  return value.kind === 'formula'
    ? {
        kind: value.kind,
        formula_version_id: value.formulaVersionId,
      }
    : { kind: value.kind };
}

export function createWorkspaceApi(
  client: Pick<ApiClient, 'get' | 'put'> = createApiClient(),
): WorkspaceApi {
  return {
    async get(options = {}) {
      return decodeState(
        await client.get(WORKSPACE_PATH, { signal: options.signal }),
      );
    },
    async put(request, options = {}) {
      const workspace = request.workspace;
      return decodeState(
        await client.put(WORKSPACE_PATH, {
          body: {
            expected_revision: request.expectedRevision,
            current_page: workspace.currentPage,
            instrument: {
              symbol: workspace.instrument.symbol,
              name: workspace.instrument.name,
              exchange: workspace.instrument.exchange,
              kind: workspace.instrument.instrumentKind,
            },
            period: workspace.period,
            adjustment: workspace.adjustment,
            zoom: workspace.zoom,
            main_chart: workspace.mainChart,
            subchart: encodeSubchart(workspace.subchart),
          },
          signal: options.signal,
        }),
      );
    },
  };
}

export const workspaceApi = createWorkspaceApi();
