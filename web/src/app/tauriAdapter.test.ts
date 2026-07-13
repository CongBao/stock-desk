import { invoke, isTauri } from '@tauri-apps/api/core';
import { listen, type EventCallback } from '@tauri-apps/api/event';

import { createApiClient } from '../shared/api/client';
import {
  createTauriAdapter,
  createTauriApiTransport,
  isDesktopApiResponseSizeAllowed,
  MAX_DESKTOP_API_RESPONSE_BYTES,
} from './tauriAdapter';
import { validateDiagnosticSnapshot } from './diagnosticsExport';

vi.mock('@tauri-apps/api/core', () => ({
  invoke: vi.fn(),
  isTauri: vi.fn(),
}));

vi.mock('@tauri-apps/api/event', () => ({
  listen: vi.fn(),
}));

it('does not construct a desktop adapter in a browser', () => {
  vi.mocked(isTauri).mockReturnValue(false);

  expect(createTauriAdapter()).toBeUndefined();
  expect(invoke).not.toHaveBeenCalled();
  expect(listen).not.toHaveBeenCalled();
});

it('uses only closed payload-free desktop commands', async () => {
  vi.mocked(isTauri).mockReturnValue(true);
  vi.mocked(invoke).mockResolvedValue(undefined);
  vi.mocked(listen).mockResolvedValue(vi.fn());
  const adapter = createTauriAdapter();
  expect(adapter).toBeDefined();
  if (adapter === undefined) throw new Error('adapter was not created');

  await adapter.getRuntimeState();
  await adapter.restartService();
  await adapter.requestExit();
  await adapter.cancelExit();
  await adapter.confirmExit();
  await adapter.openDiagnostics();

  expect(vi.mocked(invoke).mock.calls).toEqual([
    ['desktop_runtime_state'],
    ['desktop_restart_service'],
    ['desktop_request_exit'],
    ['desktop_cancel_exit'],
    ['desktop_confirm_exit'],
    ['desktop_open_diagnostics'],
  ]);
});

it('keeps the WebView2 save picker bound to window and uses only host IPC', async () => {
  vi.mocked(isTauri).mockReturnValue(true);
  const snapshot = {
    schema_version: 'stock-desk-diagnostic-snapshot-v1',
    created_at: '2026-07-13T08:00:00Z',
    application: { version: '1.1.0', source_revision: null },
    platform: { system: 'windows', architecture: 'x86_64' },
    service_health: {
      sidecar: 'ready',
      storage: 'ready',
      market_worker: 'ready',
    },
    configuration: {
      available: true,
      daily_sources: [],
      weekly_sources: [],
      minute_sources: [],
      instrument_sources: [],
      tushare_token_configured: false,
      local_tdx_configured: false,
      model_providers: [],
    },
    events: [],
    failure_ids: [],
    privacy: {
      telemetry_enabled: false,
      automatic_crash_upload: false,
      automatic_diagnostic_upload: false,
      stable_device_identifier: false,
    },
  };
  const rendered = validateDiagnosticSnapshot(snapshot);
  const write = vi.fn(() => Promise.resolve());
  const picker = vi.fn(function (this: unknown) {
    expect(this).toBe(window);
    return Promise.resolve({
      createWritable: () =>
        Promise.resolve({
          write,
          close: () => Promise.resolve(),
          abort: () => Promise.resolve(),
        }),
    });
  });
  Object.defineProperty(window, 'showSaveFilePicker', {
    configurable: true,
    value: picker,
  });
  vi.mocked(invoke).mockImplementation((command) => {
    if (command === 'desktop_api_request') {
      return Promise.resolve({
        body: JSON.stringify(snapshot),
        content_type: 'application/json',
        status: 200,
      });
    }
    if (command === 'desktop_validate_diagnostics') {
      return Promise.resolve(rendered);
    }
    return Promise.resolve(undefined);
  });
  const browserFetch = vi.spyOn(globalThis, 'fetch');
  const adapter = createTauriAdapter();
  if (adapter === undefined) throw new Error('adapter was not created');

  await expect(adapter.exportDiagnostics()).resolves.toBe('saved');
  expect(picker).toHaveBeenCalledOnce();
  expect(write).toHaveBeenCalledWith(rendered);
  expect(browserFetch).not.toHaveBeenCalled();
  expect(invoke).toHaveBeenCalledWith('desktop_api_request', {
    request: { method: 'POST', path: '/api/v1/diagnostics/snapshot' },
  });
  expect(invoke).toHaveBeenCalledWith('desktop_validate_diagnostics', {
    snapshot,
  });
  browserFetch.mockRestore();
  Reflect.deleteProperty(window, 'showSaveFilePicker');
});

it('subscribes only to the closed exit-state event payload', async () => {
  vi.mocked(isTauri).mockReturnValue(true);
  const unlisten = vi.fn();
  let emit: EventCallback<unknown> | undefined;
  vi.mocked(listen).mockImplementation((event, handler) => {
    expect(event).toBe('desktop-exit-state');
    emit = handler;
    return Promise.resolve(unlisten);
  });
  const adapter = createTauriAdapter();
  if (adapter === undefined) throw new Error('adapter was not created');
  const listener = vi.fn();

  const unsubscribe = await adapter.subscribeExit(listener);
  emit?.({
    event: 'desktop-exit-state',
    id: 1,
    payload: { state: 'confirm' },
  });

  expect(listener).toHaveBeenCalledWith({ state: 'confirm' });
  unsubscribe();
  expect(unlisten).toHaveBeenCalledOnce();
});

it('subscribes only to the closed runtime-state event payload', async () => {
  vi.mocked(isTauri).mockReturnValue(true);
  const unlisten = vi.fn();
  let emit: EventCallback<unknown> | undefined;
  vi.mocked(listen).mockImplementation((event, handler) => {
    expect(event).toBe('desktop-runtime-state');
    emit = handler;
    return Promise.resolve(unlisten);
  });
  const adapter = createTauriAdapter();
  if (adapter === undefined) throw new Error('adapter was not created');
  const listener = vi.fn();

  const unsubscribe = await adapter.subscribe(listener);
  emit?.({
    event: 'desktop-runtime-state',
    id: 1,
    payload: { state: 'ready' },
  });

  expect(listener).toHaveBeenCalledWith({ state: 'ready' });
  unsubscribe();
  expect(unlisten).toHaveBeenCalledOnce();
});

it('proxies only the closed API request and validates the host response', async () => {
  vi.mocked(isTauri).mockReturnValue(true);
  vi.mocked(invoke).mockResolvedValue({
    body: '{"status":"ok"}',
    content_type: 'application/json',
    status: 200,
  });
  const transport = createTauriApiTransport();
  if (transport === undefined) throw new Error('transport was not created');

  const response = await transport({ method: 'GET', path: '/api/health' });

  expect(invoke).toHaveBeenCalledWith('desktop_api_request', {
    request: { method: 'GET', path: '/api/health' },
  });
  await expect(response.json()).resolves.toEqual({ status: 'ok' });

  vi.mocked(invoke).mockResolvedValueOnce({
    body: '{"token":"private"}',
    content_type: 'application/json',
    port: 49152,
    status: 200,
  });
  await expect(
    transport({ method: 'GET', path: '/api/health' }),
  ).rejects.toThrow('Invalid desktop API response');
});

it('keeps the desktop response budget aligned above the public formula payload', () => {
  expect(MAX_DESKTOP_API_RESPONSE_BYTES).toBe(192 * 1_048_576);
  expect(isDesktopApiResponseSizeAllowed(128 * 1_048_576)).toBe(true);
  expect(isDesktopApiResponseSizeAllowed(MAX_DESKTOP_API_RESPONSE_BYTES)).toBe(
    true,
  );
  expect(
    isDesktopApiResponseSizeAllowed(MAX_DESKTOP_API_RESPONSE_BYTES + 1),
  ).toBe(false);
  expect(isDesktopApiResponseSizeAllowed(Number.MAX_SAFE_INTEGER + 1)).toBe(
    false,
  );
});

it('routes every Formula Studio operation through the host without exposing session authority', async () => {
  vi.mocked(isTauri).mockReturnValue(true);
  vi.mocked(invoke).mockClear();
  vi.mocked(invoke).mockResolvedValue({
    body: '{"ok":true}',
    content_type: 'application/json',
    status: 200,
  });
  const transport = createTauriApiTransport();
  if (transport === undefined) throw new Error('transport was not created');
  const client = createApiClient('/api', transport);
  const browserFetch = vi.spyOn(globalThis, 'fetch');

  await client.get('/formulas/templates');
  await client.post('/formulas/validate', {
    body: { source: 'X:C;', parameter_schema: {}, formula_type: 'indicator' },
  });
  await client.post('/formulas', {
    body: {
      name: 'Desktop formula',
      source: 'X:C;',
      parameter_schema: {},
      formula_type: 'indicator',
      placement: 'subchart',
    },
  });
  await client.post('/formulas/version-1/preview', {
    body: {
      symbol: '000001.SS',
      period: '1d',
      adjustment: 'qfq',
      start: '2026-01-01T00:00:00Z',
      end: '2026-07-01T00:00:00Z',
      parameters: {},
    },
  });

  expect(browserFetch).not.toHaveBeenCalled();
  const requests = vi.mocked(invoke).mock.calls.map(([, payload]) => payload);
  expect(requests).toHaveLength(4);
  expect(requests[0]).toEqual({
    request: { method: 'GET', path: '/api/formulas/templates' },
  });
  const serializedRequests = JSON.stringify(requests);
  for (const path of [
    '/api/formulas/validate',
    '/api/formulas',
    '/api/formulas/version-1/preview',
  ]) {
    expect(serializedRequests).toContain(`"path":"${path}"`);
  }
  expect(serializedRequests).not.toMatch(
    /authorization|bearer|127\.0\.0\.1|localhost|port/iu,
  );
  browserFetch.mockRestore();
});

it('routes Backtest, Analysis, and Task Center operations only through host IPC', async () => {
  vi.mocked(isTauri).mockReturnValue(true);
  vi.mocked(invoke).mockClear();
  vi.mocked(invoke).mockResolvedValue({
    body: '{"ok":true}',
    content_type: 'application/json',
    status: 200,
  });
  const transport = createTauriApiTransport();
  if (transport === undefined) throw new Error('transport was not created');
  const client = createApiClient('/api', transport);
  const browserFetch = vi.spyOn(globalThis, 'fetch');

  await client.post('/backtests/preflight', {
    body: { formula_version_id: 'formula-version', period: '1d' },
  });
  await client.post('/backtests', {
    body: { formula_version_id: 'formula-version', period: '1d' },
  });
  await client.post('/analysis', {
    body: { symbol: '600000.SH', model_config_id: 'model-config' },
  });
  await client.get('/tasks?view=safe&limit=100');
  await client.get('/tasks/task-id/events?view=safe&limit=100');

  expect(browserFetch).not.toHaveBeenCalled();
  expect(vi.mocked(invoke).mock.calls).toEqual([
    [
      'desktop_api_request',
      {
        request: {
          method: 'POST',
          path: '/api/backtests/preflight',
          body: JSON.stringify({
            formula_version_id: 'formula-version',
            period: '1d',
          }),
        },
      },
    ],
    [
      'desktop_api_request',
      {
        request: {
          method: 'POST',
          path: '/api/backtests',
          body: JSON.stringify({
            formula_version_id: 'formula-version',
            period: '1d',
          }),
        },
      },
    ],
    [
      'desktop_api_request',
      {
        request: {
          method: 'POST',
          path: '/api/analysis',
          body: JSON.stringify({
            symbol: '600000.SH',
            model_config_id: 'model-config',
          }),
        },
      },
    ],
    [
      'desktop_api_request',
      {
        request: {
          method: 'GET',
          path: '/api/tasks?view=safe&limit=100',
        },
      },
    ],
    [
      'desktop_api_request',
      {
        request: {
          method: 'GET',
          path: '/api/tasks/task-id/events?view=safe&limit=100',
        },
      },
    ],
  ]);
  const serializedRequests = JSON.stringify(vi.mocked(invoke).mock.calls);
  expect(serializedRequests).not.toMatch(
    /authorization|bearer|127\.0\.0\.1|localhost|session.secret|port/iu,
  );
  browserFetch.mockRestore();
});

it('rejects an aborted desktop API request without exposing its late result', async () => {
  vi.mocked(isTauri).mockReturnValue(true);
  let resolveInvoke: ((value: unknown) => void) | undefined;
  vi.mocked(invoke).mockReturnValue(
    new Promise((resolve) => {
      resolveInvoke = resolve;
    }),
  );
  const transport = createTauriApiTransport();
  if (transport === undefined) throw new Error('transport was not created');
  const controller = new AbortController();
  const pending = transport(
    { method: 'GET', path: '/api/health' },
    controller.signal,
  );

  controller.abort();
  await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  resolveInvoke?.({
    body: '{"status":"late"}',
    content_type: 'application/json',
    status: 200,
  });
});
