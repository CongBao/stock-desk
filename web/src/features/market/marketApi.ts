import {
  ApiError,
  createApiClient,
  type ApiClient,
  type JsonValue,
} from '../../shared/api/client';
import type { MarketAdjustment, MarketPeriod } from './marketStore';
import { sha256Hex } from './sha256';
import {
  decodeFormulaPreview,
  type FormulaPreview,
} from '../formulas/formulaApi';

const MAX_INSTRUMENTS = 100;
const MAX_POOLS = 50;
const MAX_POOL_MEMBERS = 10_000;
const MAX_BARS = 100_000;
const MAX_ROUTING_SOURCES = 32;
const MAX_TEXT = 512;
const SYMBOL_PATTERN = /^[0-9]{6}\.(?:SH|SZ|BJ)$/u;
const DIGEST_PATTERN = /^sha256:[0-9a-f]{64}$/u;
const DECIMAL_PATTERN = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/u;

const periods = new Set<MarketPeriod>(['1d', '1w', '60m']);
const adjustments = new Set<MarketAdjustment>(['none', 'qfq', 'hfq']);
const providers = new Set([
  'akshare',
  'baostock',
  'eastmoney',
  'tdx_local',
  'tushare',
]);
type MarketCapability = 'bars' | 'instruments' | 'trading_calendar';
type RoutingDecision =
  | 'registry_missing'
  | 'capability_skip'
  | 'capability_failure'
  | 'fetch_failure';
type FailureReason =
  | 'permission_denied'
  | 'unsupported'
  | 'missing'
  | 'no_data'
  | 'provider_unavailable'
  | 'transient_failure'
  | 'timeout'
  | 'corrupt'
  | 'invalid_response'
  | 'no_provider';
type TransitionReason =
  'fallback_after_failure' | 'higher_priority_recovered' | 'priority_changed';

const capabilities = new Set<MarketCapability>([
  'bars',
  'instruments',
  'trading_calendar',
]);
const routingDecisions = new Set<RoutingDecision>([
  'registry_missing',
  'capability_skip',
  'capability_failure',
  'fetch_failure',
]);
const failureReasons = new Set<FailureReason>([
  'permission_denied',
  'unsupported',
  'missing',
  'no_data',
  'provider_unavailable',
  'transient_failure',
  'timeout',
  'corrupt',
  'invalid_response',
  'no_provider',
]);
const transitionReasons = new Set<TransitionReason>([
  'fallback_after_failure',
  'higher_priority_recovered',
  'priority_changed',
]);
const exchanges = new Set(['SH', 'SZ', 'BJ'] as const);
const instrumentKinds = new Set([
  'stock',
  'index',
  'etf',
  'fund',
  'bond',
] as const);
const listingStatuses = new Set(['unknown', 'listed', 'delisted'] as const);
const poolCategories = new Set(['all_a', 'index', 'industry'] as const);
const tradingStatuses = new Set([
  'unknown',
  'normal',
  'suspended',
  'limit_up',
  'limit_down',
] as const);
const shanghaiBucketFormatter = new Intl.DateTimeFormat(
  'en-GB-u-ca-iso8601-nu-latn',
  {
    timeZone: 'Asia/Shanghai',
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  },
);
const attemptDetails: Readonly<
  Record<Exclude<FailureReason, 'no_provider'>, string>
> = {
  permission_denied: 'provider permission was denied',
  unsupported: 'provider does not support this request',
  missing: 'provider response does not cover the full request',
  no_data: 'provider returned no data',
  provider_unavailable: 'provider is unavailable',
  transient_failure: 'provider failed transiently',
  timeout: 'provider request timed out',
  corrupt: 'provider data is corrupt',
  invalid_response: 'provider response is invalid',
};

export class MarketProtocolError extends Error {
  constructor(path: string) {
    super(`行情 API 响应不符合协议：${path}`);
    this.name = 'MarketProtocolError';
  }
}

export type RoutingAttempt = {
  readonly ordinal: number;
  readonly source: string;
  readonly decision: RoutingDecision;
  readonly reason: FailureReason;
  readonly detail: string;
  readonly category: MarketCapability;
};

export type MarketBarsQuery = {
  readonly symbol: string;
  readonly period: MarketPeriod;
  readonly adjustment: MarketAdjustment;
  readonly start: string;
  readonly end: string;
};

export type RoutingTransition = {
  readonly category: MarketCapability;
  readonly fromSource: string;
  readonly toSource: string;
  readonly fromDatasetVersion: string;
  readonly toDatasetVersion: string;
  readonly fromRouteVersion: string;
  readonly effectiveAt: string | null;
  readonly calendarStart: string | null;
  readonly calendarEnd: string | null;
  readonly reason: TransitionReason;
} | null;

export type CalendarRoutingRequest = {
  readonly exchange: 'SH' | 'SZ' | 'BJ';
  readonly start: string;
  readonly end: string;
};

export type RoutingManifest = {
  readonly category: MarketCapability;
  readonly requestQuery: MarketBarsQuery | null;
  readonly calendarRequest: CalendarRoutingRequest | null;
  readonly priority: readonly string[];
  readonly attempts: readonly RoutingAttempt[];
  readonly selectedSource: string;
  readonly upstreamDatasetVersion: string;
  readonly upstreamFetchedAt: string;
  readonly upstreamDataCutoff: string;
  readonly upstreamAdjustment: MarketAdjustment | null;
  readonly routeVersion: string;
  readonly transition: RoutingTransition;
};

export type CatalogProvenance = {
  readonly manifestRecordId: string;
  readonly datasetVersion: string;
  readonly routeVersion: string;
  readonly source: string;
  readonly fetchedAt: string;
  readonly dataCutoff: string;
  readonly routingManifest: RoutingManifest;
  readonly instrumentDatasetVersion?: string;
  readonly composition?: MarketPoolComposition;
};

export type MarketPoolComposition = {
  readonly presetKey: string;
  readonly category: 'all_a' | 'index' | 'industry';
  readonly displayName: string;
  readonly symbols: readonly string[];
  readonly source: string;
  readonly datasetVersion: string;
  readonly routeVersion: string;
  readonly fetchedAt: string;
  readonly dataCutoff: string;
  readonly complete: true;
};

export type MarketInstrument = {
  readonly symbol: string;
  readonly name: string;
  readonly exchange: 'SH' | 'SZ' | 'BJ';
  readonly instrumentKind: 'stock' | 'index' | 'etf' | 'fund' | 'bond';
  readonly listingStatus: 'unknown' | 'listed' | 'delisted';
  readonly listedOn: string | null;
  readonly delistedOn: string | null;
  readonly provenance: CatalogProvenance;
};

export type MarketPoolSummary = {
  readonly poolId: string;
  readonly kind: 'preset' | 'custom';
  readonly name: string;
  readonly category: 'all_a' | 'index' | 'industry' | null;
  readonly revision: number | null;
  readonly memberCount: number;
  readonly snapshotId: string | null;
  readonly provenance: CatalogProvenance;
};

export type MarketPoolMember = {
  readonly ordinal: number;
  readonly symbol: string;
  readonly name: string;
  readonly instrumentKind: MarketInstrument['instrumentKind'];
  readonly listingStatus: MarketInstrument['listingStatus'];
};

export type MarketPoolDetail = MarketPoolSummary & {
  readonly members: readonly MarketPoolMember[];
};

export type MarketPoolPage = {
  readonly items: readonly MarketPoolSummary[];
  readonly nextCursor: string | null;
};

export type MarketBar = {
  readonly symbol: string;
  readonly timestamp: string;
  readonly period: MarketPeriod;
  readonly adjustment: MarketAdjustment;
  readonly open: number;
  readonly high: number;
  readonly low: number;
  readonly close: number;
  readonly priceText: {
    readonly open: string;
    readonly high: string;
    readonly low: string;
    readonly close: string;
  };
  readonly volume: number;
  readonly status:
    'unknown' | 'normal' | 'suspended' | 'limit_up' | 'limit_down';
  readonly direction: 'rise' | 'fall' | 'flat';
};

export type MarketBarsResponse = {
  readonly query: MarketBarsQuery;
  readonly bars: readonly MarketBar[];
  readonly coverage: { readonly start: string; readonly end: string };
  readonly manifestRecordId: string;
  readonly datasetVersion: string;
  readonly routeVersion: string;
  readonly routingManifest: RoutingManifest;
  readonly provenance: {
    readonly source: string;
    readonly fetchedAt: string;
    readonly dataCutoff: string;
    readonly adjustment: MarketAdjustment;
    readonly datasetVersion: string;
  };
  readonly formula?: FormulaPreview;
};

type SignalOptions = { readonly signal?: AbortSignal };

export type MarketApi = {
  searchInstruments(options: {
    readonly query: string;
    readonly limit?: number;
    readonly signal?: AbortSignal;
  }): Promise<readonly MarketInstrument[]>;
  getPools(options?: {
    readonly cursor?: string;
    readonly limit?: number;
    readonly signal?: AbortSignal;
  }): Promise<MarketPoolPage>;
  getPool(poolId: string, options?: SignalOptions): Promise<MarketPoolDetail>;
  getBars(
    options: {
      readonly symbol: string;
      readonly period: MarketPeriod;
      readonly adjustment: MarketAdjustment;
      readonly signal?: AbortSignal;
    } & (
      | {
          readonly formulaVersionId?: never;
          readonly formulaParameters?: never;
        }
      | {
          readonly formulaVersionId: string;
          readonly formulaParameters: Readonly<Record<string, number>>;
        }
    ),
  ): Promise<MarketBarsResponse>;
};

function record(
  value: JsonValue | undefined,
  path: string,
): Record<string, JsonValue> {
  if (
    value === null ||
    value === undefined ||
    Array.isArray(value) ||
    typeof value !== 'object'
  ) {
    throw new MarketProtocolError(path);
  }
  return value as Record<string, JsonValue>;
}

function textValue(
  value: JsonValue | undefined,
  path: string,
  max = MAX_TEXT,
): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > max) {
    throw new MarketProtocolError(path);
  }
  return value;
}

function nullableText(
  value: JsonValue | undefined,
  path: string,
  max = MAX_TEXT,
): string | null {
  if (value === null) return null;
  return textValue(value, path, max);
}

function timestamp(value: JsonValue | undefined, path: string): string {
  const result = textValue(value, path, 40);
  if (
    !/(?:Z|[+-]\d{2}:\d{2})$/u.test(result) ||
    !Number.isFinite(Date.parse(result))
  ) {
    throw new MarketProtocolError(path);
  }
  return result;
}

function calendarDate(value: JsonValue | undefined, path: string): string {
  const result = textValue(value, path, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/u.test(result)) {
    throw new MarketProtocolError(path);
  }
  const parsed = new Date(`${result}T00:00:00Z`);
  if (
    !Number.isFinite(parsed.valueOf()) ||
    parsed.toISOString().slice(0, 10) !== result
  ) {
    throw new MarketProtocolError(path);
  }
  return result;
}

export function isCanonicalBucketStart(
  value: string,
  period: MarketPeriod,
): boolean {
  const parsed = new Date(value);
  const secondsMatch = /T\d{2}:\d{2}:(\d{2})(?:\.(\d+))?/u.exec(value);
  if (
    !Number.isFinite(parsed.valueOf()) ||
    secondsMatch?.[1] !== '00' ||
    (secondsMatch[2] !== undefined && /[1-9]/u.test(secondsMatch[2])) ||
    parsed.getUTCMilliseconds() !== 0
  ) {
    return false;
  }
  const localParts = Object.fromEntries(
    shanghaiBucketFormatter
      .formatToParts(parsed)
      .filter((part) => part.type !== 'literal')
      .map((part) => [part.type, part.value]),
  );
  const hour = Number(localParts['hour']);
  const minute = Number(localParts['minute']);
  const second = Number(localParts['second']);
  if (second !== 0) return false;
  if (period === '1d') return hour === 0 && minute === 0;
  if (period === '1w') {
    return localParts['weekday'] === 'Mon' && hour === 0 && minute === 0;
  }
  return (
    (hour === 9 && minute === 30) ||
    (hour === 10 && minute === 30) ||
    (hour === 13 && minute === 0) ||
    (hour === 14 && minute === 0)
  );
}

function digest(value: JsonValue | undefined, path: string): string {
  const result = textValue(value, path, 71);
  if (!DIGEST_PATTERN.test(result)) throw new MarketProtocolError(path);
  return result;
}

function symbol(value: JsonValue | undefined, path: string): string {
  const result = textValue(value, path, 9);
  if (!SYMBOL_PATTERN.test(result)) throw new MarketProtocolError(path);
  return result;
}

function integer(
  value: JsonValue | undefined,
  path: string,
  max = Number.MAX_SAFE_INTEGER,
): number {
  if (
    typeof value !== 'number' ||
    !Number.isSafeInteger(value) ||
    value < 0 ||
    value > max
  ) {
    throw new MarketProtocolError(path);
  }
  return value;
}

function enumValue<T extends string>(
  value: JsonValue | undefined,
  allowed: ReadonlySet<T>,
  path: string,
): T {
  if (typeof value !== 'string' || !allowed.has(value as T)) {
    throw new MarketProtocolError(path);
  }
  return value as T;
}

function arrayValue(
  value: JsonValue | undefined,
  path: string,
  max: number,
): readonly JsonValue[] {
  if (!Array.isArray(value) || value.length > max)
    throw new MarketProtocolError(path);
  return value as readonly JsonValue[];
}

function provider(value: JsonValue | undefined, path: string): string {
  return enumValue(value, providers, path);
}

function protocolAssert(condition: boolean, path: string): asserts condition {
  if (!condition) throw new MarketProtocolError(path);
}

function sortJsonKeys(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map(sortJsonKeys);
  if (value === null || typeof value !== 'object') return value;
  const object = value as { readonly [key: string]: JsonValue };
  const result: Record<string, JsonValue> = {};
  for (const key of Object.keys(object).sort()) {
    const item = object[key];
    if (item !== undefined) result[key] = sortJsonKeys(item);
  }
  return result;
}

function canonicalJson(value: JsonValue, ensureAscii: boolean): string {
  const encoded = JSON.stringify(sortJsonKeys(value));
  if (!ensureAscii) return encoded;
  let ascii = '';
  for (let index = 0; index < encoded.length; index += 1) {
    const codeUnit = encoded.charCodeAt(index);
    ascii +=
      codeUnit <= 0x7f
        ? encoded.charAt(index)
        : `\\u${codeUnit.toString(16).padStart(4, '0')}`;
  }
  return ascii;
}

async function canonicalDigest(
  value: JsonValue,
  ensureAscii: boolean,
): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalJson(value, ensureAscii));
  return `sha256:${await sha256Hex(bytes)}`;
}

function routingRequestJson(manifest: RoutingManifest): JsonValue {
  if (manifest.category === 'bars') {
    protocolAssert(
      manifest.requestQuery !== null,
      'routing_manifest.request.query',
    );
    return {
      query: {
        symbol: manifest.requestQuery.symbol,
        period: manifest.requestQuery.period,
        adjustment: manifest.requestQuery.adjustment,
        start: manifest.requestQuery.start,
        end: manifest.requestQuery.end,
      },
    };
  }
  if (manifest.category === 'instruments') return {};
  protocolAssert(manifest.calendarRequest !== null, 'routing_manifest.request');
  return {
    exchange: manifest.calendarRequest.exchange,
    start: manifest.calendarRequest.start,
    end: manifest.calendarRequest.end,
  };
}

function transitionJson(transition: RoutingTransition): JsonValue {
  if (transition === null) return null;
  return {
    category: transition.category,
    from_source: transition.fromSource,
    to_source: transition.toSource,
    from_dataset_version: transition.fromDatasetVersion,
    to_dataset_version: transition.toDatasetVersion,
    from_route_version: transition.fromRouteVersion,
    effective_at: transition.effectiveAt,
    calendar_start: transition.calendarStart,
    calendar_end: transition.calendarEnd,
    reason: transition.reason,
  };
}

function routingManifestJson(manifest: RoutingManifest): JsonValue {
  return {
    schema_version: 'stock-desk-routing-manifest-v1',
    category: manifest.category,
    request: routingRequestJson(manifest),
    priority: [...manifest.priority],
    attempts: manifest.attempts.map((attempt) => ({
      ordinal: attempt.ordinal,
      source: attempt.source,
      category: attempt.category,
      decision: attempt.decision,
      reason: attempt.reason,
      detail: attempt.detail,
    })),
    selected_source: manifest.selectedSource,
    upstream_dataset_version: manifest.upstreamDatasetVersion,
    upstream_fetched_at: manifest.upstreamFetchedAt,
    upstream_data_cutoff: manifest.upstreamDataCutoff,
    upstream_adjustment: manifest.upstreamAdjustment,
    route_version: manifest.routeVersion,
    transition: transitionJson(manifest.transition),
  };
}

function routingPayloadJson(manifest: RoutingManifest): JsonValue {
  return {
    schema_version: 'stock-desk-routing-manifest-v1',
    category: manifest.category,
    request: routingRequestJson(manifest),
    priority: [...manifest.priority],
    attempts: manifest.attempts.map((attempt) => ({
      ordinal: attempt.ordinal,
      source: attempt.source,
      category: attempt.category,
      decision: attempt.decision,
      reason: attempt.reason,
      detail: attempt.detail,
    })),
    selected_source: manifest.selectedSource,
    upstream_dataset_version: manifest.upstreamDatasetVersion,
    upstream_data_cutoff: manifest.upstreamDataCutoff,
    upstream_adjustment: manifest.upstreamAdjustment,
    transition: transitionJson(manifest.transition),
  };
}

async function verifyRoutingContentHashes(
  manifest: RoutingManifest,
  manifestRecordId: string,
  path: string,
): Promise<void> {
  protocolAssert(
    (await canonicalDigest(routingPayloadJson(manifest), true)) ===
      manifest.routeVersion,
    `${path}.route_version`,
  );
  protocolAssert(
    (await canonicalDigest(routingManifestJson(manifest), true)) ===
      manifestRecordId,
    `${path}.manifest_record_id`,
  );
}

function fixedAttemptDetail(
  decision: RoutingDecision,
  reason: FailureReason,
  path: string,
): string {
  if (decision === 'registry_missing') {
    protocolAssert(reason === 'provider_unavailable', path);
    return 'provider is not registered';
  }
  if (decision === 'capability_skip') {
    protocolAssert(reason === 'unsupported', path);
    return 'provider capability does not support this request';
  }
  protocolAssert(reason !== 'no_provider', path);
  return attemptDetails[reason];
}

function decodeTransition(
  value: JsonValue | undefined,
  path: string,
  context: {
    readonly category: MarketCapability;
    readonly requestQuery: MarketBarsQuery | null;
    readonly calendarRequest: CalendarRoutingRequest | null;
    readonly selectedSource: string;
    readonly upstreamDatasetVersion: string;
    readonly upstreamFetchedAt: string;
  },
): RoutingTransition {
  if (value === null) return null;
  const item = record(value, path);
  const result: Exclude<RoutingTransition, null> = {
    category: enumValue(item['category'], capabilities, `${path}.category`),
    fromSource: provider(item['from_source'], `${path}.from_source`),
    toSource: provider(item['to_source'], `${path}.to_source`),
    fromDatasetVersion: digest(
      item['from_dataset_version'],
      `${path}.from_dataset_version`,
    ),
    toDatasetVersion: digest(
      item['to_dataset_version'],
      `${path}.to_dataset_version`,
    ),
    fromRouteVersion: digest(
      item['from_route_version'],
      `${path}.from_route_version`,
    ),
    effectiveAt:
      item['effective_at'] === null
        ? null
        : timestamp(item['effective_at'], `${path}.effective_at`),
    calendarStart:
      item['calendar_start'] === null
        ? null
        : calendarDate(item['calendar_start'], `${path}.calendar_start`),
    calendarEnd:
      item['calendar_end'] === null
        ? null
        : calendarDate(item['calendar_end'], `${path}.calendar_end`),
    reason: enumValue(item['reason'], transitionReasons, `${path}.reason`),
  };
  protocolAssert(result.fromSource !== result.toSource, `${path}.from_source`);
  protocolAssert(
    result.fromDatasetVersion !== result.toDatasetVersion,
    `${path}.from_dataset_version`,
  );
  if (result.category === 'trading_calendar') {
    protocolAssert(result.effectiveAt === null, `${path}.effective_at`);
    protocolAssert(
      result.calendarStart !== null &&
        result.calendarEnd !== null &&
        result.calendarStart < result.calendarEnd,
      `${path}.calendar_start`,
    );
  } else {
    protocolAssert(result.effectiveAt !== null, `${path}.effective_at`);
    protocolAssert(
      result.calendarStart === null && result.calendarEnd === null,
      `${path}.calendar_start`,
    );
  }
  protocolAssert(result.category === context.category, `${path}.category`);
  protocolAssert(
    result.toSource === context.selectedSource,
    `${path}.to_source`,
  );
  protocolAssert(
    result.toDatasetVersion === context.upstreamDatasetVersion,
    `${path}.to_dataset_version`,
  );
  if (context.category === 'bars') {
    protocolAssert(
      context.requestQuery !== null &&
        result.effectiveAt === context.requestQuery.start,
      `${path}.effective_at`,
    );
  } else if (context.category === 'instruments') {
    protocolAssert(
      result.effectiveAt === context.upstreamFetchedAt,
      `${path}.effective_at`,
    );
  } else {
    protocolAssert(
      context.calendarRequest !== null &&
        result.calendarStart === context.calendarRequest.start &&
        result.calendarEnd === context.calendarRequest.end,
      `${path}.calendar_start`,
    );
  }
  return result;
}

export function decodeRoutingManifest(
  value: JsonValue | undefined,
  path: string,
): RoutingManifest {
  const item = record(value, path);
  if (item['schema_version'] !== 'stock-desk-routing-manifest-v1') {
    throw new MarketProtocolError(`${path}.schema_version`);
  }
  const category = enumValue(
    item['category'],
    capabilities,
    `${path}.category`,
  );
  const request = record(item['request'], `${path}.request`);
  let requestQuery: MarketBarsQuery | null = null;
  let calendarRequest: CalendarRoutingRequest | null = null;
  if (category === 'bars') {
    requestQuery = decodeQuery(request['query'], `${path}.request.query`);
  } else if (category === 'instruments') {
    protocolAssert(Object.keys(request).length === 0, `${path}.request`);
  } else {
    calendarRequest = {
      exchange: enumValue(
        request['exchange'],
        exchanges,
        `${path}.request.exchange`,
      ),
      start: calendarDate(request['start'], `${path}.request.start`),
      end: calendarDate(request['end'], `${path}.request.end`),
    };
    protocolAssert(
      calendarRequest.start < calendarRequest.end,
      `${path}.request`,
    );
  }
  const priority = arrayValue(
    item['priority'],
    `${path}.priority`,
    MAX_ROUTING_SOURCES,
  ).map((source, index) =>
    provider(source, `${path}.priority[${String(index)}]`),
  );
  const attempts = arrayValue(
    item['attempts'],
    `${path}.attempts`,
    MAX_ROUTING_SOURCES,
  ).map((attempt, index): RoutingAttempt => {
    const attemptPath = `${path}.attempts[${String(index)}]`;
    const decoded = record(attempt, attemptPath);
    const decision = enumValue(
      decoded['decision'],
      routingDecisions,
      `${attemptPath}.decision`,
    );
    const reason = enumValue(
      decoded['reason'],
      failureReasons,
      `${attemptPath}.reason`,
    );
    const detail = textValue(decoded['detail'], `${attemptPath}.detail`);
    protocolAssert(
      detail === fixedAttemptDetail(decision, reason, `${attemptPath}.reason`),
      `${attemptPath}.detail`,
    );
    return {
      ordinal: integer(
        decoded['ordinal'],
        `${attemptPath}.ordinal`,
        MAX_ROUTING_SOURCES,
      ),
      source: provider(decoded['source'], `${attemptPath}.source`),
      category: enumValue(
        decoded['category'],
        capabilities,
        `${attemptPath}.category`,
      ),
      decision,
      reason,
      detail,
    };
  });
  const selectedSource = provider(
    item['selected_source'],
    `${path}.selected_source`,
  );
  const upstreamFetchedAt = timestamp(
    item['upstream_fetched_at'],
    `${path}.upstream_fetched_at`,
  );
  const upstreamDataCutoff = timestamp(
    item['upstream_data_cutoff'],
    `${path}.upstream_data_cutoff`,
  );
  const upstreamDatasetVersion = digest(
    item['upstream_dataset_version'],
    `${path}.upstream_dataset_version`,
  );
  const upstreamAdjustment =
    item['upstream_adjustment'] === null
      ? null
      : enumValue(
          item['upstream_adjustment'],
          adjustments,
          `${path}.upstream_adjustment`,
        );
  protocolAssert(priority.length > 0, `${path}.priority`);
  protocolAssert(
    new Set(priority).size === priority.length,
    `${path}.priority`,
  );
  protocolAssert(attempts.length < priority.length, `${path}.attempts`);
  attempts.forEach((attempt, index) => {
    protocolAssert(attempt.ordinal === index + 1, `${path}.attempts.ordinal`);
    protocolAssert(
      attempt.source === priority[index],
      `${path}.attempts.source`,
    );
    protocolAssert(attempt.category === category, `${path}.attempts.category`);
  });
  protocolAssert(
    selectedSource === priority[attempts.length],
    `${path}.selected_source`,
  );
  protocolAssert(
    Date.parse(upstreamDataCutoff) <= Date.parse(upstreamFetchedAt),
    `${path}.upstream_data_cutoff`,
  );
  if (category === 'bars') {
    protocolAssert(requestQuery !== null, `${path}.request.query`);
    protocolAssert(
      upstreamAdjustment === requestQuery.adjustment,
      `${path}.upstream_adjustment`,
    );
  } else {
    protocolAssert(upstreamAdjustment === null, `${path}.upstream_adjustment`);
  }
  const transition = decodeTransition(
    item['transition'],
    `${path}.transition`,
    {
      category,
      requestQuery,
      calendarRequest,
      selectedSource,
      upstreamDatasetVersion,
      upstreamFetchedAt,
    },
  );
  return {
    category,
    requestQuery,
    calendarRequest,
    priority,
    attempts,
    selectedSource,
    upstreamDatasetVersion,
    upstreamFetchedAt,
    upstreamDataCutoff,
    upstreamAdjustment,
    routeVersion: digest(item['route_version'], `${path}.route_version`),
    transition,
  };
}

function decodePoolComposition(
  value: JsonValue | undefined,
  path: string,
): MarketPoolComposition {
  const item = record(value, path);
  const presetKey = textValue(item['preset_key'], `${path}.preset_key`, 64);
  protocolAssert(
    /^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$/u.test(presetKey),
    `${path}.preset_key`,
  );
  const displayName = textValue(
    item['display_name'],
    `${path}.display_name`,
    64,
  );
  protocolAssert(
    /^\S(?:.{0,62}\S)?$/u.test(displayName) &&
      [...displayName].every((character) => {
        const codePoint = character.codePointAt(0);
        return codePoint !== undefined && codePoint >= 32 && codePoint !== 127;
      }),
    `${path}.display_name`,
  );
  const symbols = arrayValue(
    item['symbols'],
    `${path}.symbols`,
    MAX_POOL_MEMBERS,
  ).map((itemSymbol, index) =>
    symbol(itemSymbol, `${path}.symbols[${String(index)}]`),
  );
  protocolAssert(symbols.length > 0, `${path}.symbols`);
  protocolAssert(new Set(symbols).size === symbols.length, `${path}.symbols`);
  const fetchedAt = timestamp(item['fetched_at'], `${path}.fetched_at`);
  const dataCutoff = timestamp(item['data_cutoff'], `${path}.data_cutoff`);
  protocolAssert(
    Date.parse(dataCutoff) <= Date.parse(fetchedAt),
    `${path}.data_cutoff`,
  );
  protocolAssert(item['complete'] === true, `${path}.complete`);
  return {
    presetKey,
    category: enumValue(item['category'], poolCategories, `${path}.category`),
    displayName,
    symbols,
    source: provider(item['source'], `${path}.source`),
    datasetVersion: digest(item['dataset_version'], `${path}.dataset_version`),
    routeVersion: digest(item['route_version'], `${path}.route_version`),
    fetchedAt,
    dataCutoff,
    complete: true,
  };
}

function decodeCatalogProvenance(
  value: JsonValue | undefined,
  path: string,
): CatalogProvenance {
  const item = record(value, path);
  const result: CatalogProvenance = {
    manifestRecordId: digest(
      item['manifest_record_id'],
      `${path}.manifest_record_id`,
    ),
    datasetVersion: digest(item['dataset_version'], `${path}.dataset_version`),
    routeVersion: digest(item['route_version'], `${path}.route_version`),
    source: provider(item['source'], `${path}.source`),
    fetchedAt: timestamp(item['fetched_at'], `${path}.fetched_at`),
    dataCutoff: timestamp(item['data_cutoff'], `${path}.data_cutoff`),
    routingManifest: decodeRoutingManifest(
      item['routing_manifest'],
      `${path}.routing_manifest`,
    ),
  };
  protocolAssert(
    result.routingManifest.category === 'instruments',
    `${path}.routing_manifest.category`,
  );
  protocolAssert(
    result.source === result.routingManifest.selectedSource,
    `${path}.source`,
  );
  protocolAssert(
    result.datasetVersion === result.routingManifest.upstreamDatasetVersion,
    `${path}.dataset_version`,
  );
  protocolAssert(
    result.routeVersion === result.routingManifest.routeVersion,
    `${path}.route_version`,
  );
  protocolAssert(
    result.fetchedAt === result.routingManifest.upstreamFetchedAt,
    `${path}.fetched_at`,
  );
  protocolAssert(
    result.dataCutoff === result.routingManifest.upstreamDataCutoff,
    `${path}.data_cutoff`,
  );
  const instrumentDatasetVersion =
    item['instrument_dataset_version'] === undefined
      ? undefined
      : digest(
          item['instrument_dataset_version'],
          `${path}.instrument_dataset_version`,
        );
  if (instrumentDatasetVersion !== undefined) {
    protocolAssert(
      instrumentDatasetVersion === result.datasetVersion,
      `${path}.instrument_dataset_version`,
    );
  }
  const composition =
    item['composition'] === undefined
      ? undefined
      : decodePoolComposition(item['composition'], `${path}.composition`);
  return {
    ...result,
    ...(instrumentDatasetVersion === undefined
      ? {}
      : { instrumentDatasetVersion }),
    ...(composition === undefined ? {} : { composition }),
  };
}

function decodeInstrument(value: JsonValue, path: string): MarketInstrument {
  const item = record(value, path);
  return {
    symbol: symbol(item['symbol'], `${path}.symbol`),
    name: textValue(item['name'], `${path}.name`, 255),
    exchange: enumValue(item['exchange'], exchanges, `${path}.exchange`),
    instrumentKind: enumValue(
      item['instrument_kind'],
      instrumentKinds,
      `${path}.instrument_kind`,
    ),
    listingStatus: enumValue(
      item['listing_status'],
      listingStatuses,
      `${path}.listing_status`,
    ),
    listedOn: nullableText(item['listed_on'], `${path}.listed_on`, 10),
    delistedOn: nullableText(item['delisted_on'], `${path}.delisted_on`, 10),
    provenance: decodeCatalogProvenance(
      item['provenance'],
      `${path}.provenance`,
    ),
  };
}

function decodePoolSummary(value: JsonValue, path: string): MarketPoolSummary {
  const item = record(value, path);
  const kind = enumValue(
    item['kind'],
    new Set(['preset', 'custom'] as const),
    `${path}.kind`,
  );
  const category =
    item['category'] === null
      ? null
      : enumValue(
          item['category'],
          new Set(['all_a', 'index', 'industry'] as const),
          `${path}.category`,
        );
  const revision =
    item['revision'] === null
      ? null
      : integer(item['revision'], `${path}.revision`, Number.MAX_SAFE_INTEGER);
  const snapshotId =
    item['snapshot_id'] === null
      ? null
      : digest(item['snapshot_id'], `${path}.snapshot_id`);
  if (
    (kind === 'preset' &&
      (category === null || revision !== null || snapshotId === null)) ||
    (kind === 'custom' &&
      (category !== null || revision === null || snapshotId !== null))
  ) {
    throw new MarketProtocolError(path);
  }
  const result: MarketPoolSummary = {
    poolId: textValue(item['pool_id'], `${path}.pool_id`, 255),
    kind,
    name: textValue(item['name'], `${path}.name`, 64),
    category,
    revision,
    memberCount: integer(
      item['member_count'],
      `${path}.member_count`,
      MAX_POOL_MEMBERS,
    ),
    snapshotId,
    provenance: decodeCatalogProvenance(
      item['provenance'],
      `${path}.provenance`,
    ),
  };
  protocolAssert(result.memberCount >= 1, `${path}.member_count`);
  protocolAssert(
    kind === 'preset' || result.memberCount <= 5_000,
    `${path}.member_count`,
  );
  return result;
}

export function decodePoolDetail(
  value: JsonValue | undefined,
): MarketPoolDetail {
  const item = record(value, 'pool');
  const summary = decodePoolSummary(item, 'pool');
  const members = arrayValue(
    item['members'],
    'pool.members',
    MAX_POOL_MEMBERS,
  ).map((member, index): MarketPoolMember => {
    const path = `pool.members[${String(index)}]`;
    const decoded = record(member, path);
    const ordinal = integer(
      decoded['ordinal'],
      `${path}.ordinal`,
      MAX_POOL_MEMBERS,
    );
    const instrumentKind = enumValue(
      decoded['instrument_kind'],
      instrumentKinds,
      `${path}.instrument_kind`,
    );
    const listingStatus = enumValue(
      decoded['listing_status'],
      listingStatuses,
      `${path}.listing_status`,
    );
    protocolAssert(ordinal === index, `${path}.ordinal`);
    protocolAssert(instrumentKind === 'stock', `${path}.instrument_kind`);
    protocolAssert(listingStatus !== 'delisted', `${path}.listing_status`);
    return {
      ordinal,
      symbol: symbol(decoded['symbol'], `${path}.symbol`),
      name: textValue(decoded['name'], `${path}.name`, 255),
      instrumentKind,
      listingStatus,
    };
  });
  protocolAssert(members.length === summary.memberCount, 'pool.member_count');
  protocolAssert(
    new Set(members.map((member) => member.symbol)).size === members.length,
    'pool.members.symbol',
  );
  const composition = summary.provenance.composition;
  if (summary.kind === 'preset') {
    protocolAssert(composition !== undefined, 'pool.provenance.composition');
    protocolAssert(
      summary.provenance.instrumentDatasetVersion !== undefined,
      'pool.provenance.instrument_dataset_version',
    );
    protocolAssert(
      summary.poolId === `preset:${composition.presetKey}`,
      'pool.pool_id',
    );
    protocolAssert(summary.name === composition.displayName, 'pool.name');
    protocolAssert(summary.category === composition.category, 'pool.category');
    protocolAssert(
      composition.symbols.length === members.length &&
        composition.symbols.every(
          (compositionSymbol, index) =>
            compositionSymbol === members[index]?.symbol,
        ),
      'pool.provenance.composition.symbols',
    );
  } else {
    protocolAssert(composition === undefined, 'pool.provenance.composition');
  }
  return { ...summary, members };
}

function poolCompositionJson(composition: MarketPoolComposition): JsonValue {
  return {
    preset_key: composition.presetKey,
    category: composition.category,
    display_name: composition.displayName,
    symbols: [...composition.symbols],
    source: composition.source,
    dataset_version: composition.datasetVersion,
    route_version: composition.routeVersion,
    fetched_at: composition.fetchedAt,
    data_cutoff: composition.dataCutoff,
    complete: composition.complete,
  };
}

async function verifyPresetPoolSnapshot(pool: MarketPoolDetail): Promise<void> {
  if (pool.kind !== 'preset') return;
  const composition = pool.provenance.composition;
  const instrumentDatasetVersion = pool.provenance.instrumentDatasetVersion;
  protocolAssert(composition !== undefined, 'pool.provenance.composition');
  protocolAssert(
    instrumentDatasetVersion !== undefined,
    'pool.provenance.instrument_dataset_version',
  );
  protocolAssert(pool.snapshotId !== null, 'pool.snapshot_id');
  const expectedSnapshotId = await canonicalDigest(
    {
      composition: poolCompositionJson(composition),
      instrument_dataset_version: instrumentDatasetVersion,
      instrument_manifest_record_id: pool.provenance.manifestRecordId,
      schema_version: 'stock-desk-preset-pool-v1',
    },
    false,
  );
  protocolAssert(expectedSnapshotId === pool.snapshotId, 'pool.snapshot_id');
}

type DecodedDecimal = {
  readonly canonical: string;
  readonly value: number;
};

function decodeDecimal(
  value: JsonValue | undefined,
  path: string,
): DecodedDecimal {
  const source = textValue(value, path, 26);
  if (!DECIMAL_PATTERN.test(source)) throw new MarketProtocolError(path);
  const negative = source.startsWith('-');
  const unsigned = negative ? source.slice(1) : source;
  const [integerPart = '', fractionalPart = ''] = unsigned.split('.');
  let digits = `${integerPart}${fractionalPart}`.replace(/^0+/u, '') || '0';
  let exponent = -fractionalPart.length;
  while (digits.length > 1 && digits.endsWith('0')) {
    digits = digits.slice(0, -1);
    exponent += 1;
  }
  const integerDigits = Math.max(digits.length + exponent, 0);
  const decimalPlaces = Math.max(-exponent, 0);
  protocolAssert(
    integerDigits <= 16 && decimalPlaces <= 8 && digits.length <= 24,
    path,
  );
  const normalizedFraction = fractionalPart.replace(/0+$/u, '');
  const isZero = digits === '0';
  const canonical = isZero
    ? '0'
    : `${negative ? '-' : ''}${integerPart}${
        normalizedFraction.length > 0 ? `.${normalizedFraction}` : ''
      }`;
  const result = Number(source);
  if (!Number.isFinite(result)) throw new MarketProtocolError(path);
  return { canonical, value: result };
}

function compareCanonicalDecimals(left: string, right: string): number {
  if (left === right) return 0;
  const leftNegative = left.startsWith('-');
  const rightNegative = right.startsWith('-');
  if (leftNegative !== rightNegative) return leftNegative ? -1 : 1;
  const leftUnsigned = leftNegative ? left.slice(1) : left;
  const rightUnsigned = rightNegative ? right.slice(1) : right;
  const [leftInteger = '', leftFraction = ''] = leftUnsigned.split('.');
  const [rightInteger = '', rightFraction = ''] = rightUnsigned.split('.');
  let magnitude = leftInteger.length - rightInteger.length;
  if (magnitude === 0 && leftInteger !== rightInteger) {
    magnitude = leftInteger < rightInteger ? -1 : 1;
  }
  if (magnitude === 0) {
    const length = Math.max(leftFraction.length, rightFraction.length);
    const paddedLeft = leftFraction.padEnd(length, '0');
    const paddedRight = rightFraction.padEnd(length, '0');
    if (paddedLeft !== paddedRight)
      magnitude = paddedLeft < paddedRight ? -1 : 1;
  }
  return leftNegative ? -magnitude : magnitude;
}

function decodeQuery(value: JsonValue | undefined, path: string) {
  const item = record(value, path);
  const result: MarketBarsQuery = {
    symbol: symbol(item['symbol'], `${path}.symbol`),
    period: enumValue(item['period'], periods, `${path}.period`),
    adjustment: enumValue(
      item['adjustment'],
      adjustments,
      `${path}.adjustment`,
    ),
    start: timestamp(item['start'], `${path}.start`),
    end: timestamp(item['end'], `${path}.end`),
  };
  protocolAssert(Date.parse(result.start) < Date.parse(result.end), path);
  return result;
}

type ExpectedFormulaParameters = Readonly<Record<string, number>>;

type ExpectedBarsRequest = Pick<
  MarketBarsQuery,
  'symbol' | 'period' | 'adjustment'
> & {
  readonly formulaVersionId?: string;
  readonly formulaParameters?: ExpectedFormulaParameters;
};

function sameQuery(left: MarketBarsQuery, right: MarketBarsQuery): boolean {
  return (
    left.symbol === right.symbol &&
    left.period === right.period &&
    left.adjustment === right.adjustment &&
    left.start === right.start &&
    left.end === right.end
  );
}

function validFormulaParameters(
  parameters: ExpectedFormulaParameters,
): boolean {
  const entries = Object.entries(parameters);
  return (
    entries.length <= 64 &&
    entries.every(
      ([name, value]) =>
        /^[A-Z][A-Z0-9_]{0,63}$/u.test(name) &&
        typeof value === 'number' &&
        Number.isFinite(value),
    )
  );
}

function sameFormulaParameters(
  expected: ExpectedFormulaParameters,
  actual: FormulaPreview['parameters'],
): boolean {
  const expectedNames = Object.keys(expected).sort();
  if (
    expectedNames.length !== actual.length ||
    actual.some((parameter, index) => parameter.name !== expectedNames[index])
  ) {
    return false;
  }
  return actual.every((parameter) => {
    const expectedValue = expected[parameter.name];
    if (expectedValue === undefined) return false;
    const actualValue = Number(parameter.value);
    if (parameter.kind === 'integer') {
      return (
        Number.isSafeInteger(expectedValue) && actualValue === expectedValue
      );
    }
    return (
      Number.isFinite(expectedValue) &&
      actualValue === (expectedValue === 0 ? 0 : expectedValue)
    );
  });
}

function decodeBarsResponse(
  value: JsonValue | undefined,
  expectedRequest: ExpectedBarsRequest,
): MarketBarsResponse {
  const item = record(value, 'bars');
  const bars = arrayValue(item['bars'], 'bars.bars', MAX_BARS).map(
    (bar, index): MarketBar => {
      const path = `bars.bars[${String(index)}]`;
      const decoded = record(bar, path);
      const open = decodeDecimal(decoded['open'], `${path}.open`);
      const high = decodeDecimal(decoded['high'], `${path}.high`);
      const low = decodeDecimal(decoded['low'], `${path}.low`);
      const close = decodeDecimal(decoded['close'], `${path}.close`);
      const directionComparison = compareCanonicalDecimals(
        close.canonical,
        open.canonical,
      );
      return {
        symbol: symbol(decoded['symbol'], `${path}.symbol`),
        timestamp: timestamp(decoded['timestamp'], `${path}.timestamp`),
        period: enumValue(decoded['period'], periods, `${path}.period`),
        adjustment: enumValue(
          decoded['adjustment'],
          adjustments,
          `${path}.adjustment`,
        ),
        open: open.value,
        high: high.value,
        low: low.value,
        close: close.value,
        priceText: {
          open: open.canonical,
          high: high.canonical,
          low: low.canonical,
          close: close.canonical,
        },
        volume: integer(decoded['volume'], `${path}.volume`),
        status:
          decoded['status'] === undefined
            ? 'unknown'
            : enumValue(decoded['status'], tradingStatuses, `${path}.status`),
        direction:
          directionComparison > 0
            ? 'rise'
            : directionComparison < 0
              ? 'fall'
              : 'flat',
      };
    },
  );
  protocolAssert(bars.length > 0, 'bars.bars');
  const coverage = record(item['coverage'], 'bars.coverage');
  const rawProvenance = record(item['provenance'], 'bars.provenance');
  const result: MarketBarsResponse = {
    query: decodeQuery(item['query'], 'bars.query'),
    bars,
    coverage: {
      start: timestamp(coverage['start'], 'bars.coverage.start'),
      end: timestamp(coverage['end'], 'bars.coverage.end'),
    },
    manifestRecordId: digest(
      item['manifest_record_id'],
      'bars.manifest_record_id',
    ),
    datasetVersion: digest(item['dataset_version'], 'bars.dataset_version'),
    routeVersion: digest(item['route_version'], 'bars.route_version'),
    routingManifest: decodeRoutingManifest(
      item['routing_manifest'],
      'bars.routing_manifest',
    ),
    provenance: {
      source: provider(rawProvenance['source'], 'bars.provenance.source'),
      fetchedAt: timestamp(
        rawProvenance['fetched_at'],
        'bars.provenance.fetched_at',
      ),
      dataCutoff: timestamp(
        rawProvenance['data_cutoff'],
        'bars.provenance.data_cutoff',
      ),
      adjustment: enumValue(
        rawProvenance['adjustment'],
        adjustments,
        'bars.provenance.adjustment',
      ),
      datasetVersion: digest(
        rawProvenance['dataset_version'],
        'bars.provenance.dataset_version',
      ),
    },
  };
  protocolAssert(
    result.query.symbol === expectedRequest.symbol &&
      result.query.period === expectedRequest.period &&
      result.query.adjustment === expectedRequest.adjustment,
    'bars.query.expected',
  );
  protocolAssert(
    result.routingManifest.category === 'bars',
    'bars.routing_manifest.category',
  );
  protocolAssert(
    result.routingManifest.requestQuery !== null &&
      sameQuery(result.routingManifest.requestQuery, result.query),
    'bars.routing_manifest.request.query',
  );
  let previousTimestamp = Number.NEGATIVE_INFINITY;
  const queryStart = Date.parse(result.query.start);
  const queryEnd = Date.parse(result.query.end);
  result.bars.forEach((bar, index) => {
    const barPath = `bars.bars[${String(index)}]`;
    protocolAssert(
      bar.symbol === result.query.symbol &&
        bar.period === result.query.period &&
        bar.adjustment === result.query.adjustment,
      `${barPath}.identity`,
    );
    const barTimestamp = Date.parse(bar.timestamp);
    protocolAssert(
      isCanonicalBucketStart(bar.timestamp, bar.period),
      `${barPath}.timestamp.bucket`,
    );
    protocolAssert(
      barTimestamp > previousTimestamp,
      `${barPath}.timestamp.order`,
    );
    protocolAssert(
      barTimestamp >= queryStart && barTimestamp < queryEnd,
      `${barPath}.timestamp.range`,
    );
    protocolAssert(
      compareCanonicalDecimals(bar.priceText.high, bar.priceText.open) >= 0 &&
        compareCanonicalDecimals(bar.priceText.high, bar.priceText.close) >=
          0 &&
        compareCanonicalDecimals(bar.priceText.low, bar.priceText.open) <= 0 &&
        compareCanonicalDecimals(bar.priceText.low, bar.priceText.close) <= 0 &&
        compareCanonicalDecimals(bar.priceText.low, bar.priceText.high) <= 0,
      `${barPath}.ohlc`,
    );
    if (bar.adjustment === 'none') {
      protocolAssert(
        compareCanonicalDecimals(bar.priceText.open, '0') > 0 &&
          compareCanonicalDecimals(bar.priceText.high, '0') > 0 &&
          compareCanonicalDecimals(bar.priceText.low, '0') > 0 &&
          compareCanonicalDecimals(bar.priceText.close, '0') > 0,
        `${barPath}.ohlc`,
      );
    }
    previousTimestamp = barTimestamp;
  });
  const lastBar = result.bars.at(-1);
  protocolAssert(lastBar !== undefined, 'bars.bars');
  protocolAssert(
    result.coverage.start === result.query.start &&
      result.coverage.end === result.query.end,
    'bars.coverage',
  );
  protocolAssert(
    result.datasetVersion === result.routingManifest.upstreamDatasetVersion &&
      result.datasetVersion === result.provenance.datasetVersion,
    'bars.dataset_version',
  );
  protocolAssert(
    result.routeVersion === result.routingManifest.routeVersion,
    'bars.route_version',
  );
  protocolAssert(
    result.routingManifest.selectedSource === result.provenance.source,
    'bars.provenance.source',
  );
  protocolAssert(
    result.routingManifest.upstreamFetchedAt === result.provenance.fetchedAt,
    'bars.provenance.fetched_at',
  );
  protocolAssert(
    result.routingManifest.upstreamDataCutoff === result.provenance.dataCutoff,
    'bars.provenance.data_cutoff',
  );
  protocolAssert(
    result.routingManifest.upstreamAdjustment === result.provenance.adjustment,
    'bars.provenance.adjustment',
  );
  protocolAssert(
    Date.parse(result.provenance.dataCutoff) <=
      Date.parse(result.provenance.fetchedAt),
    'bars.provenance.data_cutoff',
  );
  protocolAssert(
    Date.parse(result.provenance.dataCutoff) >= Date.parse(lastBar.timestamp),
    'bars.provenance.data_cutoff',
  );
  if (expectedRequest.formulaVersionId === undefined) return result;
  const formula = decodeFormulaPreview(item['formula']);
  protocolAssert(
    formula.formulaVersionId === expectedRequest.formulaVersionId,
    'bars.formula.formula_version_id',
  );
  protocolAssert(
    formula.symbol === result.query.symbol &&
      formula.period === result.query.period &&
      formula.adjustment === result.query.adjustment &&
      formula.queryStart === result.query.start &&
      formula.queryEnd === result.query.end,
    'bars.formula.query',
  );
  protocolAssert(
    formula.source === result.provenance.source &&
      formula.datasetVersion === result.datasetVersion &&
      formula.routeVersion === result.routeVersion &&
      formula.manifestRecordId === result.manifestRecordId &&
      formula.dataCutoff === result.provenance.dataCutoff,
    'bars.formula.provenance',
  );
  protocolAssert(
    formula.timestamps.length === result.bars.length &&
      formula.timestamps.every(
        (formulaTimestamp, index) =>
          formulaTimestamp === result.bars[index]?.timestamp,
      ),
    'bars.formula.timestamps',
  );
  protocolAssert(
    sameFormulaParameters(
      expectedRequest.formulaParameters ?? {},
      formula.parameters,
    ),
    'bars.formula.parameters',
  );
  return { ...result, formula };
}

function queryPath(
  path: string,
  parameters: Readonly<Record<string, string | number | undefined>>,
) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(parameters)) {
    if (value !== undefined) query.set(key, String(value));
  }
  return `${path}?${query.toString()}`;
}

export function createMarketApi(
  client: Pick<ApiClient, 'get'> = createApiClient(),
): MarketApi {
  return {
    async searchInstruments({ query, limit = 20, signal }) {
      const value = await client.get(
        queryPath('/market/instruments', { q: query, limit }),
        { signal },
      );
      const instruments = arrayValue(value, 'instruments', MAX_INSTRUMENTS).map(
        (item, index) =>
          decodeInstrument(item, `instruments[${String(index)}]`),
      );
      await Promise.all(
        instruments.map((instrument, index) =>
          verifyRoutingContentHashes(
            instrument.provenance.routingManifest,
            instrument.provenance.manifestRecordId,
            `instruments[${String(index)}].provenance`,
          ),
        ),
      );
      return instruments;
    },
    async getPools({ cursor, limit = 20, signal } = {}) {
      const value = record(
        await client.get(queryPath('/market/pools', { limit, cursor }), {
          signal,
        }),
        'pools',
      );
      const items = arrayValue(value['items'], 'pools.items', MAX_POOLS).map(
        (item, index) =>
          decodePoolSummary(item, `pools.items[${String(index)}]`),
      );
      await Promise.all(
        items.map((pool, index) =>
          verifyRoutingContentHashes(
            pool.provenance.routingManifest,
            pool.provenance.manifestRecordId,
            `pools.items[${String(index)}].provenance`,
          ),
        ),
      );
      return {
        items,
        nextCursor:
          value['next_cursor'] === null
            ? null
            : textValue(value['next_cursor'], 'pools.next_cursor', 255),
      };
    },
    async getPool(poolId, { signal } = {}) {
      const pool = decodePoolDetail(
        await client.get(`/market/pools/${encodeURIComponent(poolId)}`, {
          signal,
        }),
      );
      await verifyRoutingContentHashes(
        pool.provenance.routingManifest,
        pool.provenance.manifestRecordId,
        'pool.provenance',
      );
      await verifyPresetPoolSnapshot(pool);
      return pool;
    },
    async getBars({
      symbol: selectedSymbol,
      period,
      adjustment,
      formulaVersionId,
      formulaParameters,
      signal,
    }) {
      if (
        (formulaParameters === undefined) !==
        (formulaVersionId === undefined)
      ) {
        throw new MarketProtocolError('request.formula_version_id');
      }
      if (
        formulaParameters !== undefined &&
        !validFormulaParameters(formulaParameters)
      ) {
        throw new MarketProtocolError('request.formula_parameters');
      }
      const response = decodeBarsResponse(
        await client.get(
          queryPath('/market/bars', {
            symbol: selectedSymbol,
            period,
            adjustment,
            formula_version_id: formulaVersionId,
            formula_parameters:
              formulaParameters === undefined
                ? undefined
                : JSON.stringify(formulaParameters),
          }),
          { signal },
        ),
        {
          symbol: selectedSymbol,
          period,
          adjustment,
          ...(formulaVersionId === undefined ? {} : { formulaVersionId }),
          ...(formulaParameters === undefined ? {} : { formulaParameters }),
        },
      );
      await verifyRoutingContentHashes(
        response.routingManifest,
        response.manifestRecordId,
        'bars',
      );
      return response;
    },
  };
}

export const marketApi = createMarketApi();

export function isMarketNotFound(error: unknown): boolean {
  return (
    error instanceof ApiError && error.kind === 'http' && error.status === 404
  );
}
