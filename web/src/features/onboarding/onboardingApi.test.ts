import { createOnboardingApi } from './onboardingApi';
import type { ApiClient, JsonValue } from '../../shared/api/client';

const state = {
  schema_version: 1,
  revision: 3,
  current_step: 'synchronization',
  status: 'in_progress',
  source: {
    id: 'akshare',
    label: 'AKShare',
    catalog_manifest_record_id: `sha256:${'a'.repeat(64)}`,
    catalog_dataset_version: `sha256:${'b'.repeat(64)}`,
    data_cutoff: '2026-07-11T07:00:00Z',
  },
  instrument: {
    symbol: '000001.SS',
    name: '上证指数',
    exchange: 'SH',
    instrument_kind: 'index',
  },
  sync: {
    status: 'verified',
    provider_id: 'akshare',
    manifest_record_id: `sha256:${'c'.repeat(64)}`,
    dataset_version: `sha256:${'d'.repeat(64)}`,
    data_cutoff: '2026-07-11T07:00:00Z',
    row_count: 240,
  },
  error: null,
  demo_mode: false,
} as const;

function client(response: JsonValue = state): ApiClient & {
  get: ReturnType<typeof vi.fn>;
  post: ReturnType<typeof vi.fn>;
  put: ReturnType<typeof vi.fn>;
} {
  return {
    get: vi.fn(() => Promise.resolve(response)),
    post: vi.fn(() => Promise.resolve(response)),
    put: vi.fn(() => Promise.resolve(response)),
  };
}

it('decodes the canonical Shanghai Composite as an index', async () => {
  const transport = client();
  const api = createOnboardingApi(transport);

  await expect(api.getState()).resolves.toMatchObject({
    currentStep: 'synchronization',
    instrument: {
      symbol: '000001.SS',
      instrumentKind: 'index',
    },
    sync: { status: 'verified', rowCount: 240 },
  });
  expect(transport.get).toHaveBeenCalledWith('/state', { signal: undefined });
});

it('decodes a persisted failed synchronization without provider evidence', async () => {
  const transport = client({
    ...state,
    sync: {
      status: 'failed',
      provider_id: null,
      manifest_record_id: null,
      dataset_version: null,
      data_cutoff: null,
      row_count: 0,
    },
    error: {
      code: 'provider_unavailable',
      actions: ['retry', 'switch_provider'],
    },
  });

  await expect(
    createOnboardingApi(transport).getState(),
  ).resolves.toMatchObject({
    sync: {
      status: 'failed',
      providerId: null,
      rowCount: 0,
    },
    error: {
      code: 'provider_unavailable',
    },
  });
});

it('uses the versioned onboarding endpoints and explicit progress bodies', async () => {
  const transport = client();
  const api = createOnboardingApi(transport);

  await api.saveProgress({
    currentStep: 'instrument_selection',
    sourceId: 'akshare',
    symbol: '000001.SS',
  });
  await api.synchronize({ sourceId: 'akshare', symbol: '000001.SS' });
  await api.complete('000001.SS');
  await api.runAction('retry');

  expect(transport.put).toHaveBeenCalledWith('/progress', {
    body: {
      current_step: 'instrument_selection',
      source_id: 'akshare',
      symbol: '000001.SS',
    },
  });
  expect(transport.post).toHaveBeenNthCalledWith(1, '/sync', {
    body: { source_id: 'akshare', symbol: '000001.SS' },
  });
  expect(transport.post).toHaveBeenNthCalledWith(2, '/complete', {
    body: { symbol: '000001.SS' },
  });
  expect(transport.post).toHaveBeenNthCalledWith(3, '/actions/retry');
});

it('keeps an untested source distinct from a verified ready source', async () => {
  const transport = client({
    items: [
      {
        id: 'akshare',
        label: 'AKShare',
        description: 'A 股行情',
        recommended: true,
        requires_token: false,
        status: 'unknown',
        data_cutoff: null,
      },
    ],
  });

  await expect(createOnboardingApi(transport).getSources()).resolves.toEqual([
    expect.objectContaining({ id: 'akshare', status: 'unknown' }),
  ]);
});

it('rejects an equity alias mislabeled as the canonical index identity', async () => {
  const transport = client({
    ...state,
    instrument: {
      symbol: '000001.SZ',
      name: '平安银行',
      exchange: 'SZ',
      instrument_kind: 'index',
    },
  });

  await expect(createOnboardingApi(transport).getState()).rejects.toThrow(
    '首次设置服务返回了无法识别的响应',
  );
});
