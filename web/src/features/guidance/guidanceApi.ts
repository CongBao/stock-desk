import {
  createApiClient,
  type ApiClient,
  type JsonValue,
} from '../../shared/api/client';

export type GuidancePage =
  'market' | 'formula' | 'backtest' | 'analysis' | 'tasks';
export type GuidanceStatus = 'completed' | 'dismissed';

export type GuidancePagePreference = {
  readonly contentVersion: number;
  readonly status: GuidanceStatus;
};

export type GuidancePreferences = {
  readonly schemaVersion: 1;
  readonly revision: number;
  readonly pages: Partial<Record<GuidancePage, GuidancePagePreference>>;
};

export type GuidanceUpdate = {
  readonly expectedRevision: number;
  readonly page: GuidancePage;
  readonly contentVersion: number;
  readonly status: GuidanceStatus;
};

export type GuidanceApi = {
  readonly get: (options?: {
    readonly signal?: AbortSignal;
  }) => Promise<GuidancePreferences>;
  readonly put: (
    update: GuidanceUpdate,
    options?: { readonly signal?: AbortSignal },
  ) => Promise<GuidancePreferences>;
};

export class GuidanceProtocolError extends Error {
  constructor() {
    super('Guidance preferences response is invalid');
    this.name = 'GuidanceProtocolError';
  }
}

const pages: readonly GuidancePage[] = [
  'market',
  'formula',
  'backtest',
  'analysis',
  'tasks',
];

function decode(value: JsonValue | undefined): GuidancePreferences {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new GuidanceProtocolError();
  }
  const record = value as Readonly<Record<string, JsonValue>>;
  if (
    record['schema_version'] !== 1 ||
    !Number.isSafeInteger(record['revision']) ||
    (record['revision'] as number) < 0
  ) {
    throw new GuidanceProtocolError();
  }
  const rawPages = record['pages'];
  if (
    rawPages === null ||
    typeof rawPages !== 'object' ||
    Array.isArray(rawPages)
  ) {
    throw new GuidanceProtocolError();
  }
  const decodedPages: Partial<Record<GuidancePage, GuidancePagePreference>> =
    {};
  for (const [key, item] of Object.entries(rawPages)) {
    if (!pages.includes(key as GuidancePage)) throw new GuidanceProtocolError();
    if (item === null || typeof item !== 'object' || Array.isArray(item)) {
      throw new GuidanceProtocolError();
    }
    const preference = item as Readonly<Record<string, JsonValue>>;
    const contentVersion = preference['content_version'];
    const status = preference['status'];
    if (
      !Number.isSafeInteger(contentVersion) ||
      (contentVersion as number) < 1 ||
      (status !== 'completed' && status !== 'dismissed')
    ) {
      throw new GuidanceProtocolError();
    }
    decodedPages[key as GuidancePage] = {
      contentVersion: contentVersion as number,
      status,
    };
  }
  return {
    schemaVersion: 1,
    revision: record['revision'] as number,
    pages: decodedPages,
  };
}

export function createGuidanceApi(
  client: Pick<ApiClient, 'get' | 'put'> = createApiClient(),
): GuidanceApi {
  return {
    async get(options = {}) {
      return decode(
        await client.get('/v1/guidance/preferences', {
          signal: options.signal,
          cache: 'no-store',
        }),
      );
    },
    async put(update, options = {}) {
      return decode(
        await client.put('/v1/guidance/preferences', {
          body: {
            expected_revision: update.expectedRevision,
            page: update.page,
            content_version: update.contentVersion,
            status: update.status,
          },
          signal: options.signal,
        }),
      );
    },
  };
}

export const guidanceApi = createGuidanceApi();
