import {
  createApiClient,
  type ApiClient,
  type JsonValue,
} from '../../shared/api/client';

export type SourceProvider =
  'akshare' | 'baostock' | 'eastmoney' | 'tdx_local' | 'tushare';

export type SourceCategory =
  | 'daily_bars'
  | 'execution_status'
  | 'instruments'
  | 'minute_bars'
  | 'trading_calendar'
  | 'weekly_bars';

export type DiagnosticState =
  | 'available'
  | 'permission_denied'
  | 'transient_failure'
  | 'unavailable'
  | 'unsupported';

export type FailureReason =
  | 'corrupt'
  | 'invalid_response'
  | 'missing'
  | 'no_data'
  | 'permission_denied'
  | 'provider_unavailable'
  | 'timeout'
  | 'transient_failure'
  | 'unsupported';

export type SourcePriorities = Readonly<
  Record<SourceCategory, readonly SourceProvider[]>
>;

export type TushareSourceStatus = {
  readonly source: 'tushare';
  readonly configured: boolean;
  readonly secure_storage_available: boolean;
  readonly masked_hint: string | null;
};

export type SourceSettings = {
  readonly priorities: SourcePriorities;
  readonly tdx_path: string | null;
  readonly tushare: TushareSourceStatus;
};

export type SourceDiagnostic = {
  readonly source: SourceProvider;
  readonly status: DiagnosticState;
  readonly capabilities: readonly (
    'bars' | 'execution_status' | 'instruments' | 'trading_calendar'
  )[];
  readonly permissions: readonly {
    readonly category: SourceCategory;
    readonly state: DiagnosticState;
  }[];
  readonly available_periods: readonly ('1d' | '1w' | '60m')[];
  readonly markets: readonly ('SH' | 'SZ')[];
  readonly gaps: readonly {
    readonly category: SourceCategory;
    readonly state: DiagnosticState;
    readonly reason: FailureReason;
    readonly detail: string;
  }[];
  readonly last_checked: string;
  readonly last_update: string | null;
  readonly data_cutoff: string | null;
  readonly fallback_reason: {
    readonly reason: FailureReason;
    readonly detail: string;
  } | null;
};

type SignalOptions = { readonly signal?: AbortSignal };

export type SourceSettingsApi = {
  getSettings(options?: SignalOptions): Promise<SourceSettings>;
  savePublic(
    value: {
      readonly priorities: SourcePriorities;
      readonly tdxPath: string | null;
    },
    options?: SignalOptions,
  ): Promise<SourceSettings>;
  saveTushare(
    token: string,
    options?: SignalOptions,
  ): Promise<TushareSourceStatus>;
  testSource(
    source: SourceProvider,
    options?: SignalOptions,
  ): Promise<SourceDiagnostic>;
};

const providers = new Set<SourceProvider>([
  'akshare',
  'baostock',
  'eastmoney',
  'tdx_local',
  'tushare',
]);
const categories = [
  'daily_bars',
  'weekly_bars',
  'minute_bars',
  'instruments',
  'trading_calendar',
  'execution_status',
] as const satisfies readonly SourceCategory[];
const diagnosticCategories = [
  'minute_bars',
  'daily_bars',
  'weekly_bars',
  'instruments',
  'trading_calendar',
  'execution_status',
] as const satisfies readonly SourceCategory[];
const states = new Set<DiagnosticState>([
  'available',
  'permission_denied',
  'transient_failure',
  'unavailable',
  'unsupported',
]);
const reasons = new Set<FailureReason>([
  'corrupt',
  'invalid_response',
  'missing',
  'no_data',
  'permission_denied',
  'provider_unavailable',
  'timeout',
  'transient_failure',
  'unsupported',
]);
const reasonStates: Readonly<
  Record<FailureReason, Exclude<DiagnosticState, 'available'>>
> = {
  corrupt: 'unavailable',
  invalid_response: 'unavailable',
  missing: 'unavailable',
  no_data: 'unavailable',
  permission_denied: 'permission_denied',
  provider_unavailable: 'unavailable',
  timeout: 'transient_failure',
  transient_failure: 'transient_failure',
  unsupported: 'unsupported',
};
const capabilities = new Set([
  'bars',
  'execution_status',
  'instruments',
  'trading_calendar',
] as const);
const periods = new Set(['1d', '1w', '60m'] as const);
const markets = new Set(['SH', 'SZ'] as const);
const usableProviders: Readonly<
  Record<SourceCategory, ReadonlySet<SourceProvider>>
> = {
  daily_bars: new Set(['tushare', 'akshare', 'baostock', 'tdx_local']),
  weekly_bars: new Set(['tushare', 'akshare', 'baostock']),
  minute_bars: new Set(['tushare', 'baostock']),
  instruments: new Set(['tushare', 'akshare', 'baostock']),
  trading_calendar: new Set(['tushare', 'baostock']),
  execution_status: new Set(['tushare']),
};

export class SourceSettingsProtocolError extends Error {
  constructor(path: string) {
    super(`数据源 API 响应不符合协议：${path}`);
    this.name = 'SourceSettingsProtocolError';
  }
}

function protocolAssert(condition: boolean, path: string): asserts condition {
  if (!condition) throw new SourceSettingsProtocolError(path);
}

function exactRecord(
  value: JsonValue | undefined,
  path: string,
  keys: readonly string[],
): Record<string, JsonValue> {
  protocolAssert(
    value !== null &&
      value !== undefined &&
      !Array.isArray(value) &&
      typeof value === 'object',
    path,
  );
  const item = value as Record<string, JsonValue>;
  const actual = Object.keys(item).sort();
  const expected = [...keys].sort();
  protocolAssert(
    actual.length === expected.length &&
      actual.every((key, index) => key === expected[index]),
    path,
  );
  return item;
}

function enumValue<T extends string>(
  value: JsonValue | undefined,
  allowed: ReadonlySet<T>,
  path: string,
): T {
  protocolAssert(typeof value === 'string' && allowed.has(value as T), path);
  return value as T;
}

function textValue(
  value: JsonValue | undefined,
  path: string,
  max = 512,
): string {
  protocolAssert(
    typeof value === 'string' && value.length > 0 && value.length <= max,
    path,
  );
  return value;
}

function arrayValue(
  value: JsonValue | undefined,
  path: string,
  max: number,
): readonly JsonValue[] {
  protocolAssert(Array.isArray(value) && value.length <= max, path);
  return value as readonly JsonValue[];
}

function timestamp(value: JsonValue | undefined, path: string): string {
  const result = textValue(value, path, 40);
  protocolAssert(
    /(?:Z|[+-]\d{2}:\d{2})$/u.test(result) &&
      Number.isFinite(Date.parse(result)),
    path,
  );
  return result;
}

function nullableTimestamp(
  value: JsonValue | undefined,
  path: string,
): string | null {
  return value === null ? null : timestamp(value, path);
}

function enumArray<T extends string>(
  value: JsonValue | undefined,
  allowed: ReadonlySet<T>,
  path: string,
  max: number,
  allowEmpty = true,
): readonly T[] {
  const items = arrayValue(value, path, max);
  protocolAssert(allowEmpty || items.length > 0, path);
  const decoded = items.map((item, index) =>
    enumValue(item, allowed, `${path}[${String(index)}]`),
  );
  protocolAssert(new Set(decoded).size === decoded.length, path);
  return decoded;
}

function decodePriorities(
  value: JsonValue | undefined,
  path: string,
): SourcePriorities {
  const item = exactRecord(value, path, categories);
  return Object.fromEntries(
    categories.map((category) => {
      const categoryPath = `${path}.${category}`;
      const order = enumArray(
        item[category],
        providers,
        categoryPath,
        5,
        false,
      );
      protocolAssert(
        order.some((source) => usableProviders[category].has(source)),
        categoryPath,
      );
      return [category, order];
    }),
  ) as SourcePriorities;
}

function absolutePath(value: JsonValue | undefined, path: string): string {
  const result = textValue(value, path, 2_048);
  protocolAssert(
    result.length >= 4 &&
      result === result.trim() &&
      [...result].every((character) => {
        const codePoint = character.codePointAt(0) ?? 0;
        return codePoint >= 32 && codePoint !== 127;
      }) &&
      /^(?:\/|[A-Za-z]:[\\/]|\\\\)/u.test(result),
    path,
  );
  return result;
}

function decodeTushareStatus(
  value: JsonValue | undefined,
  path: string,
): TushareSourceStatus {
  const item = exactRecord(value, path, [
    'source',
    'configured',
    'secure_storage_available',
    'masked_hint',
  ]);
  protocolAssert(item['source'] === 'tushare', `${path}.source`);
  protocolAssert(typeof item['configured'] === 'boolean', `${path}.configured`);
  protocolAssert(
    typeof item['secure_storage_available'] === 'boolean',
    `${path}.secure_storage_available`,
  );
  const maskedHint =
    item['masked_hint'] === null
      ? null
      : textValue(item['masked_hint'], `${path}.masked_hint`, 64);
  protocolAssert(
    item['configured'] === true || maskedHint === null,
    `${path}.masked_hint`,
  );
  return {
    source: 'tushare',
    configured: item['configured'],
    secure_storage_available: item['secure_storage_available'],
    masked_hint: maskedHint,
  };
}

function decodeSettings(value: JsonValue | undefined): SourceSettings {
  const item = exactRecord(value, 'settings', [
    'priorities',
    'tdx_path',
    'tushare',
  ]);
  const tdxPath =
    item['tdx_path'] === null
      ? null
      : absolutePath(item['tdx_path'], 'settings.tdx_path');
  return {
    priorities: decodePriorities(item['priorities'], 'settings.priorities'),
    tdx_path: tdxPath,
    tushare: decodeTushareStatus(item['tushare'], 'settings.tushare'),
  };
}

function decodeFallback(
  value: JsonValue | undefined,
  path: string,
): SourceDiagnostic['fallback_reason'] {
  if (value === null) return null;
  const item = exactRecord(value, path, ['reason', 'detail']);
  return {
    reason: enumValue(item['reason'], reasons, `${path}.reason`),
    detail: textValue(item['detail'], `${path}.detail`),
  };
}

function decodeDiagnostic(
  value: JsonValue | undefined,
  expectedSource: SourceProvider,
): SourceDiagnostic {
  const item = exactRecord(value, 'diagnostic', [
    'source',
    'status',
    'capabilities',
    'permissions',
    'available_periods',
    'markets',
    'gaps',
    'last_checked',
    'last_update',
    'data_cutoff',
    'fallback_reason',
  ]);
  const source = enumValue(item['source'], providers, 'diagnostic.source');
  protocolAssert(source === expectedSource, 'diagnostic.source');
  const status = enumValue(item['status'], states, 'diagnostic.status');
  const rawPermissions = arrayValue(
    item['permissions'],
    'diagnostic.permissions',
    6,
  );
  protocolAssert(
    rawPermissions.length === diagnosticCategories.length,
    'diagnostic.permissions',
  );
  const permissions = rawPermissions.map((value, index) => {
    const path = `diagnostic.permissions[${String(index)}]`;
    const permission = exactRecord(value, path, ['category', 'state']);
    const category = enumValue(
      permission['category'],
      new Set(diagnosticCategories),
      `${path}.category`,
    );
    protocolAssert(
      category === diagnosticCategories[index],
      `${path}.category`,
    );
    return {
      category,
      state: enumValue(permission['state'], states, `${path}.state`),
    };
  });
  protocolAssert(
    new Set(permissions.map((permission) => permission.category)).size ===
      permissions.length,
    'diagnostic.permissions',
  );
  const rawGaps = arrayValue(item['gaps'], 'diagnostic.gaps', 6);
  const gaps = rawGaps.map((value, index) => {
    const path = `diagnostic.gaps[${String(index)}]`;
    const gap = exactRecord(value, path, [
      'category',
      'state',
      'reason',
      'detail',
    ]);
    const category = enumValue(
      gap['category'],
      new Set(diagnosticCategories),
      `${path}.category`,
    );
    const state = enumValue(gap['state'], states, `${path}.state`);
    const reason = enumValue(gap['reason'], reasons, `${path}.reason`);
    protocolAssert(reasonStates[reason] === state, `${path}.state`);
    return {
      category,
      state,
      reason,
      detail: textValue(gap['detail'], `${path}.detail`),
    };
  });
  protocolAssert(
    new Set(gaps.map((gap) => gap.category)).size === gaps.length,
    'diagnostic.gaps',
  );
  const fallbackReason = decodeFallback(
    item['fallback_reason'],
    'diagnostic.fallback_reason',
  );
  const gapByCategory = new Map(gaps.map((gap) => [gap.category, gap]));
  for (const permission of permissions) {
    const gap = gapByCategory.get(permission.category);
    protocolAssert(
      permission.state === 'available'
        ? gap === undefined
        : gap !== undefined && gap.state === permission.state,
      'diagnostic.permissions',
    );
  }
  if (status === 'available') {
    protocolAssert(fallbackReason === null, 'diagnostic.fallback_reason');
    protocolAssert(
      gaps.every((gap) => gap.state === 'unsupported'),
      'diagnostic.status',
    );
  } else {
    protocolAssert(fallbackReason !== null, 'diagnostic.fallback_reason');
    protocolAssert(
      reasonStates[fallbackReason.reason] === status &&
        gaps.some(
          (gap) =>
            gap.state === status &&
            gap.reason === fallbackReason.reason &&
            gap.detail === fallbackReason.detail,
        ),
      'diagnostic.fallback_reason',
    );
  }

  const decodedCapabilities = enumArray(
    item['capabilities'],
    capabilities,
    'diagnostic.capabilities',
    4,
  );
  const decodedPeriods = enumArray(
    item['available_periods'],
    periods,
    'diagnostic.available_periods',
    3,
  );
  const decodedMarkets = enumArray(
    item['markets'],
    markets,
    'diagnostic.markets',
    2,
  );
  protocolAssert(
    status === 'available' || decodedMarkets.length === 0,
    'diagnostic.markets',
  );
  protocolAssert(
    source !== 'tdx_local' ||
      status !== 'available' ||
      decodedMarkets.length > 0,
    'diagnostic.markets',
  );
  const availableCategories = new Set(
    permissions
      .filter((permission) => permission.state === 'available')
      .map((permission) => permission.category),
  );
  const expectedCapabilities: SourceDiagnostic['capabilities'][number][] = [];
  if (
    ['minute_bars', 'daily_bars', 'weekly_bars'].some((category) =>
      availableCategories.has(category as SourceCategory),
    )
  )
    expectedCapabilities.push('bars');
  if (availableCategories.has('instruments'))
    expectedCapabilities.push('instruments');
  if (availableCategories.has('trading_calendar'))
    expectedCapabilities.push('trading_calendar');
  if (availableCategories.has('execution_status'))
    expectedCapabilities.push('execution_status');
  const expectedPeriods: SourceDiagnostic['available_periods'][number][] = [];
  if (availableCategories.has('daily_bars')) expectedPeriods.push('1d');
  if (availableCategories.has('weekly_bars')) expectedPeriods.push('1w');
  if (availableCategories.has('minute_bars')) expectedPeriods.push('60m');
  protocolAssert(
    [...decodedCapabilities].sort().join(',') ===
      expectedCapabilities.sort().join(','),
    'diagnostic.capabilities',
  );
  protocolAssert(
    [...decodedPeriods].sort().join(',') === expectedPeriods.sort().join(','),
    'diagnostic.available_periods',
  );

  const lastChecked = timestamp(
    item['last_checked'],
    'diagnostic.last_checked',
  );
  const lastUpdate = nullableTimestamp(
    item['last_update'],
    'diagnostic.last_update',
  );
  const dataCutoff = nullableTimestamp(
    item['data_cutoff'],
    'diagnostic.data_cutoff',
  );
  const checkedMilliseconds = Date.parse(lastChecked);
  protocolAssert(
    (lastUpdate === null || Date.parse(lastUpdate) <= checkedMilliseconds) &&
      (dataCutoff === null || Date.parse(dataCutoff) <= checkedMilliseconds) &&
      (lastUpdate === null ||
        dataCutoff === null ||
        Date.parse(dataCutoff) <= Date.parse(lastUpdate)),
    'diagnostic.last_checked',
  );
  return {
    source,
    status,
    capabilities: decodedCapabilities,
    permissions,
    available_periods: decodedPeriods,
    markets: decodedMarkets,
    gaps,
    last_checked: lastChecked,
    last_update: lastUpdate,
    data_cutoff: dataCutoff,
    fallback_reason: fallbackReason,
  };
}

export function createSourceSettingsApi(client: ApiClient): SourceSettingsApi {
  return {
    async getSettings({ signal } = {}) {
      return decodeSettings(await client.get('/settings/sources', { signal }));
    },
    async savePublic({ priorities, tdxPath }, { signal } = {}) {
      return decodeSettings(
        await client.put('/settings/sources', {
          body: { priorities, tdx_path: tdxPath },
          signal,
        }),
      );
    },
    async saveTushare(token, { signal } = {}) {
      return decodeTushareStatus(
        await client.put('/settings/sources/tushare', {
          body: { token },
          signal,
        }),
        'settings.tushare',
      );
    },
    async testSource(source, { signal } = {}) {
      return decodeDiagnostic(
        await client.post(`/settings/sources/${source}/test`, { signal }),
        source,
      );
    },
  };
}

export const sourceSettingsApi = createSourceSettingsApi(createApiClient());
