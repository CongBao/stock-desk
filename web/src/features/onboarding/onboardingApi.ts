import {
  createApiClient,
  type ApiClient,
  type JsonValue,
} from '../../shared/api/client';

export type OnboardingStep =
  | 'welcome'
  | 'data_preparation'
  | 'instrument_selection'
  | 'synchronization'
  | 'completed';

export type OnboardingInstrument = {
  readonly symbol: string;
  readonly name: string;
  readonly exchange: 'SH' | 'SZ' | 'BJ';
  readonly instrumentKind: 'index' | 'stock' | 'etf' | 'fund' | 'bond';
};

export type OnboardingSource = {
  readonly id: string;
  readonly label: string;
  readonly description: string;
  readonly recommended: boolean;
  readonly requiresToken: boolean;
  readonly status: 'unknown' | 'ready' | 'unavailable';
  readonly dataCutoff: string | null;
};

export type OnboardingState = {
  readonly schemaVersion: 1;
  readonly revision: number;
  readonly currentStep: OnboardingStep;
  readonly status: 'pending' | 'in_progress' | 'completed';
  readonly source: {
    readonly id: string;
    readonly label: string;
    readonly catalogManifestRecordId: string;
    readonly catalogDatasetVersion: string;
    readonly dataCutoff: string | null;
  } | null;
  readonly instrument: OnboardingInstrument | null;
  readonly sync: {
    readonly status: 'idle' | 'verified' | 'failed';
    readonly providerId: string;
    readonly manifestRecordId: string | null;
    readonly datasetVersion: string | null;
    readonly dataCutoff: string | null;
    readonly rowCount: number;
  } | null;
  readonly error: {
    readonly code: string;
    readonly actions: readonly OnboardingAction[];
  } | null;
  readonly demoMode: boolean;
};

export type OnboardingAction =
  'retry' | 'switch_provider' | 'advanced' | 'demo' | 'exit_demo';

export type OnboardingApi = {
  readonly getState: (signal?: AbortSignal) => Promise<OnboardingState>;
  readonly getSources: (
    signal?: AbortSignal,
  ) => Promise<readonly OnboardingSource[]>;
  readonly searchInstruments: (options: {
    readonly query: string;
    readonly limit?: number;
    readonly signal?: AbortSignal;
  }) => Promise<readonly OnboardingInstrument[]>;
  readonly saveProgress: (input: {
    readonly currentStep: OnboardingStep;
    readonly sourceId?: string;
    readonly symbol?: string;
  }) => Promise<OnboardingState>;
  readonly synchronize: (input: {
    readonly sourceId: string;
    readonly symbol: string;
  }) => Promise<OnboardingState>;
  readonly complete: (symbol: string) => Promise<OnboardingState>;
  readonly runAction: (action: OnboardingAction) => Promise<OnboardingState>;
};

export class OnboardingProtocolError extends Error {
  constructor() {
    super('首次设置服务返回了无法识别的响应');
    this.name = 'OnboardingProtocolError';
  }
}

function record(
  value: JsonValue | undefined,
): Readonly<Record<string, JsonValue>> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new OnboardingProtocolError();
  }
  return value as Readonly<Record<string, JsonValue>>;
}

function text(value: JsonValue | undefined): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > 512) {
    throw new OnboardingProtocolError();
  }
  return value;
}

function optionalText(value: JsonValue | undefined): string | null {
  return value === null ? null : text(value);
}

function oneOf<const T extends string>(
  value: JsonValue | undefined,
  allowed: readonly T[],
): T {
  if (typeof value !== 'string' || !allowed.includes(value as T)) {
    throw new OnboardingProtocolError();
  }
  return value as T;
}

const steps: readonly OnboardingStep[] = [
  'welcome',
  'data_preparation',
  'instrument_selection',
  'synchronization',
  'completed',
];
const actions: readonly OnboardingAction[] = [
  'retry',
  'switch_provider',
  'advanced',
  'demo',
  'exit_demo',
];
const instrumentKinds = ['index', 'stock', 'etf', 'fund', 'bond'] as const;

function decodeInstrument(value: JsonValue | undefined): OnboardingInstrument {
  const item = record(value);
  const symbol = text(item['symbol']);
  if (!/^[0-9]{6}\.(?:SS|SH|SZ|BJ)$/u.test(symbol)) {
    throw new OnboardingProtocolError();
  }
  const instrumentKind = oneOf(item['instrument_kind'], instrumentKinds);
  if (
    (instrumentKind === 'index' && !symbol.endsWith('.SS')) ||
    (instrumentKind !== 'index' && symbol.endsWith('.SS'))
  ) {
    throw new OnboardingProtocolError();
  }
  return {
    symbol,
    name: text(item['name']),
    exchange: oneOf(item['exchange'], ['SH', 'SZ', 'BJ']),
    instrumentKind,
  };
}

function decodeState(value: JsonValue | undefined): OnboardingState {
  const item = record(value);
  if (item['schema_version'] !== 1) throw new OnboardingProtocolError();
  const revision = item['revision'];
  if (!Number.isSafeInteger(revision) || Number(revision) < 0) {
    throw new OnboardingProtocolError();
  }
  const sourceValue = item['source'];
  const source = sourceValue === null ? null : record(sourceValue);
  const syncValue = item['sync'];
  const sync = syncValue === null ? null : record(syncValue);
  const errorValue = item['error'];
  const error = errorValue === null ? null : record(errorValue);
  const errorActions = error?.['actions'];
  if (error !== null && !Array.isArray(errorActions)) {
    throw new OnboardingProtocolError();
  }
  const demoMode = item['demo_mode'];
  if (typeof demoMode !== 'boolean') throw new OnboardingProtocolError();
  return {
    schemaVersion: 1,
    revision: Number(revision),
    currentStep: oneOf(item['current_step'], steps),
    status: oneOf(item['status'], ['pending', 'in_progress', 'completed']),
    source:
      source === null
        ? null
        : {
            id: text(source['id']),
            label: text(source['label']),
            catalogManifestRecordId: text(source['catalog_manifest_record_id']),
            catalogDatasetVersion: text(source['catalog_dataset_version']),
            dataCutoff: optionalText(source['data_cutoff']),
          },
    instrument:
      item['instrument'] === null ? null : decodeInstrument(item['instrument']),
    sync:
      sync === null
        ? null
        : {
            status: oneOf(sync['status'], ['idle', 'verified', 'failed']),
            providerId: text(sync['provider_id']),
            manifestRecordId: optionalText(sync['manifest_record_id']),
            datasetVersion: optionalText(sync['dataset_version']),
            dataCutoff: optionalText(sync['data_cutoff']),
            rowCount:
              Number.isSafeInteger(sync['row_count']) &&
              Number(sync['row_count']) >= 0
                ? Number(sync['row_count'])
                : (() => {
                    throw new OnboardingProtocolError();
                  })(),
          },
    error:
      error === null
        ? null
        : {
            code: text(error['code']),
            actions: (errorActions as readonly JsonValue[]).map((action) =>
              oneOf(action, actions),
            ),
          },
    demoMode,
  };
}

function decodeSource(value: JsonValue): OnboardingSource {
  const item = record(value);
  const recommended = item['recommended'];
  const requiresToken = item['requires_token'];
  if (typeof recommended !== 'boolean' || typeof requiresToken !== 'boolean') {
    throw new OnboardingProtocolError();
  }
  return {
    id: text(item['id']),
    label: text(item['label']),
    description: text(item['description']),
    recommended,
    requiresToken,
    status: oneOf(item['status'], ['unknown', 'ready', 'unavailable']),
    dataCutoff: optionalText(item['data_cutoff']),
  };
}

function decodeItems<T>(
  value: JsonValue | undefined,
  decode: (item: JsonValue) => T,
): readonly T[] {
  const items = record(value)['items'];
  if (!Array.isArray(items) || items.length > 100) {
    throw new OnboardingProtocolError();
  }
  return items.map(decode);
}

export function createOnboardingApi(
  client: ApiClient = createApiClient('/api/v1/onboarding'),
): OnboardingApi {
  return {
    async getState(signal) {
      return decodeState(await client.get('/state', { signal }));
    },
    async getSources(signal) {
      return decodeItems(
        await client.get('/sources', { signal }),
        decodeSource,
      );
    },
    async searchInstruments({ query, limit = 20, signal }) {
      const params = new URLSearchParams({ q: query, limit: String(limit) });
      return decodeItems(
        await client.get(`/instruments?${params.toString()}`, { signal }),
        decodeInstrument,
      );
    },
    async saveProgress(input) {
      return decodeState(
        await client.put('/progress', {
          body: {
            current_step: input.currentStep,
            ...(input.sourceId === undefined
              ? {}
              : { source_id: input.sourceId }),
            ...(input.symbol === undefined ? {} : { symbol: input.symbol }),
          },
        }),
      );
    },
    async synchronize(input) {
      return decodeState(
        await client.post('/sync', {
          body: { source_id: input.sourceId, symbol: input.symbol },
        }),
      );
    },
    async complete(symbol) {
      return decodeState(await client.post('/complete', { body: { symbol } }));
    },
    async runAction(action) {
      return decodeState(await client.post(`/actions/${action}`));
    },
  };
}

export const onboardingApi = createOnboardingApi();
