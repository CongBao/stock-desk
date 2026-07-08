import type { ApiClient, JsonValue } from '../../shared/api/client';

import {
  createSourceSettingsApi,
  SourceSettingsProtocolError,
} from './sourceSettingsApi';
import { diagnosticResponse, settingsResponse } from './testFixtures';

function clientReturning(method: keyof ApiClient, value: JsonValue): ApiClient {
  return {
    get: vi.fn(() =>
      Promise.resolve(method === 'get' ? value : settingsResponse),
    ),
    put: vi.fn((path: string) =>
      Promise.resolve(
        method === 'put'
          ? value
          : path.endsWith('/tushare')
            ? settingsResponse.tushare
            : settingsResponse,
      ),
    ),
    post: vi.fn(() =>
      Promise.resolve(method === 'post' ? value : diagnosticResponse),
    ),
  };
}

it('decodes settings and forwards abortable public/token writes', async () => {
  const client = clientReturning('get', settingsResponse);
  const controller = new AbortController();
  const api = createSourceSettingsApi(client);

  await expect(api.getSettings({ signal: controller.signal })).resolves.toEqual(
    settingsResponse,
  );
  await api.savePublic(
    {
      priorities: settingsResponse.priorities,
      tdxPath: '/new/vipdoc',
    },
    { signal: controller.signal },
  );
  await api.saveTushare('write-only-token', { signal: controller.signal });

  expect(client.get).toHaveBeenCalledWith('/settings/sources', {
    signal: controller.signal,
  });
  expect(client.put).toHaveBeenNthCalledWith(1, '/settings/sources', {
    body: {
      priorities: settingsResponse.priorities,
      tdx_path: '/new/vipdoc',
    },
    signal: controller.signal,
  });
  expect(client.put).toHaveBeenNthCalledWith(2, '/settings/sources/tushare', {
    body: { token: 'write-only-token' },
    signal: controller.signal,
  });
});

it('decodes every category in the backend source-priorities contract', async () => {
  const priorities = await createSourceSettingsApi(
    clientReturning('get', settingsResponse),
  ).getSettings();

  expect(Object.keys(priorities.priorities)).toEqual([
    'daily_bars',
    'weekly_bars',
    'minute_bars',
    'instruments',
    'trading_calendar',
    'execution_status',
    'fundamentals',
    'announcements',
    'news',
  ]);
});

it('posts source diagnostics and binds the response source', async () => {
  const client = clientReturning('post', diagnosticResponse);
  const controller = new AbortController();

  await expect(
    createSourceSettingsApi(client).testSource('tushare', {
      signal: controller.signal,
    }),
  ).resolves.toEqual(diagnosticResponse);
  expect(client.post).toHaveBeenCalledWith('/settings/sources/tushare/test', {
    signal: controller.signal,
  });

  const mismatched = structuredClone(diagnosticResponse) as Record<
    string,
    unknown
  >;
  mismatched['source'] = 'baostock';
  await expect(
    createSourceSettingsApi(
      clientReturning('post', mismatched as JsonValue),
    ).testSource('tushare'),
  ).rejects.toBeInstanceOf(SourceSettingsProtocolError);
});

it.each([
  [
    'duplicate priority',
    (payload: Record<string, unknown>) => {
      const priorities = payload['priorities'] as Record<string, unknown>;
      priorities['minute_bars'] = ['tushare', 'tushare'];
    },
  ],
  [
    'unknown provider',
    (payload: Record<string, unknown>) => {
      const priorities = payload['priorities'] as Record<string, unknown>;
      priorities['daily_bars'] = ['unknown'];
    },
  ],
  [
    'priority without an implemented source',
    (payload: Record<string, unknown>) => {
      const priorities = payload['priorities'] as Record<string, unknown>;
      priorities['minute_bars'] = ['eastmoney'];
    },
  ],
  [
    'relative TDX path',
    (payload: Record<string, unknown>) => {
      payload['tdx_path'] = 'relative/vipdoc';
    },
  ],
  [
    'implausibly short absolute TDX path',
    (payload: Record<string, unknown>) => {
      payload['tdx_path'] = '/x';
    },
  ],
  [
    'secret-shaped extra field',
    (payload: Record<string, unknown>) => {
      payload['token'] = 'must-not-be-accepted';
    },
  ],
  [
    'unknown priority category',
    (payload: Record<string, unknown>) => {
      const priorities = payload['priorities'] as Record<string, unknown>;
      priorities['private_feed'] = ['akshare'];
    },
  ],
] as const)('rejects malformed settings: %s', async (_name, mutate) => {
  const payload = structuredClone(settingsResponse) as Record<string, unknown>;
  mutate(payload);

  await expect(
    createSourceSettingsApi(
      clientReturning('get', payload as JsonValue),
    ).getSettings(),
  ).rejects.toBeInstanceOf(SourceSettingsProtocolError);
});

it('rejects malformed diagnostic timestamps and unsafe extras', async () => {
  const payload = structuredClone(diagnosticResponse) as Record<
    string,
    unknown
  >;
  payload['last_checked'] = 'not-a-time';
  payload['unsafe'] = 'private provider text';

  await expect(
    createSourceSettingsApi(
      clientReturning('post', payload as JsonValue),
    ).testSource('tushare'),
  ).rejects.toBeInstanceOf(SourceSettingsProtocolError);
});

it('accepts an available TDX source with explicit unsupported categories', async () => {
  const tdxDiagnostic = {
    source: 'tdx_local',
    status: 'available',
    capabilities: ['bars'],
    permissions: [
      { category: 'minute_bars', state: 'unsupported' },
      { category: 'daily_bars', state: 'available' },
      { category: 'weekly_bars', state: 'unsupported' },
      { category: 'instruments', state: 'unsupported' },
      { category: 'trading_calendar', state: 'unsupported' },
      { category: 'execution_status', state: 'unsupported' },
    ],
    available_periods: ['1d'],
    markets: ['SH', 'SZ'],
    gaps: [
      {
        category: 'minute_bars',
        state: 'unsupported',
        reason: 'unsupported',
        detail: 'provider does not support 60-minute bars',
      },
      {
        category: 'weekly_bars',
        state: 'unsupported',
        reason: 'unsupported',
        detail: 'provider does not support weekly bars',
      },
      {
        category: 'instruments',
        state: 'unsupported',
        reason: 'unsupported',
        detail: 'provider does not support instruments',
      },
      {
        category: 'trading_calendar',
        state: 'unsupported',
        reason: 'unsupported',
        detail: 'provider does not support trading calendar',
      },
      {
        category: 'execution_status',
        state: 'unsupported',
        reason: 'unsupported',
        detail: 'provider does not support execution status',
      },
    ],
    last_checked: '2026-07-06T09:30:00Z',
    last_update: null,
    data_cutoff: '2024-07-02T07:00:00Z',
    fallback_reason: null,
  } as const;

  await expect(
    createSourceSettingsApi(clientReturning('post', tdxDiagnostic)).testSource(
      'tdx_local',
    ),
  ).resolves.toEqual(tdxDiagnostic);
});

it.each([
  [
    'missing permission',
    (payload: Record<string, unknown>) => {
      (payload['permissions'] as unknown[]).pop();
    },
  ],
  [
    'reordered permissions',
    (payload: Record<string, unknown>) => {
      (payload['permissions'] as unknown[]).reverse();
    },
  ],
  [
    'gap reason-state contradiction',
    (payload: Record<string, unknown>) => {
      const gap = (payload['gaps'] as Record<string, unknown>[])[0];
      if (gap) gap['reason'] = 'timeout';
    },
  ],
  [
    'permission without matching gap',
    (payload: Record<string, unknown>) => {
      const permissions = payload['permissions'] as Record<string, unknown>[];
      const daily = permissions.find(
        (item) => item['category'] === 'daily_bars',
      );
      if (daily) daily['state'] = 'unavailable';
    },
  ],
  [
    'capability without category evidence',
    (payload: Record<string, unknown>) => {
      payload['available_periods'] = ['1d', '1w', '60m'];
    },
  ],
  [
    'status-fallback contradiction',
    (payload: Record<string, unknown>) => {
      const fallback = payload['fallback_reason'] as Record<string, unknown>;
      fallback['reason'] = 'timeout';
    },
  ],
  [
    'future last update',
    (payload: Record<string, unknown>) => {
      payload['last_update'] = '2026-07-06T10:30:00Z';
    },
  ],
] as const)('rejects contradictory diagnostic: %s', async (_name, mutate) => {
  const payload = structuredClone(diagnosticResponse) as Record<
    string,
    unknown
  >;
  mutate(payload);

  await expect(
    createSourceSettingsApi(
      clientReturning('post', payload as JsonValue),
    ).testSource('tushare'),
  ).rejects.toBeInstanceOf(SourceSettingsProtocolError);
});
