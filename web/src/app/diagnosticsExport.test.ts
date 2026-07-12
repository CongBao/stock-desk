import type { JsonValue } from '../shared/api/client';
import {
  DiagnosticExportProtocolError,
  exportHealthyDiagnostics,
  validateDiagnosticSnapshot,
  type DiagnosticSavePicker,
} from './diagnosticsExport';

function snapshot(): Record<string, JsonValue> {
  return {
    schema_version: 'stock-desk-diagnostic-snapshot-v1',
    created_at: '2026-07-13T08:00:00Z',
    application: { version: '1.1.0', source_revision: 'a'.repeat(40) },
    platform: { system: 'windows', architecture: 'x86_64' },
    service_health: {
      sidecar: 'ready',
      storage: 'ready',
      market_worker: 'unavailable',
    },
    configuration: {
      available: true,
      daily_sources: ['akshare'],
      weekly_sources: [],
      minute_sources: [],
      instrument_sources: ['akshare'],
      tushare_token_configured: false,
      local_tdx_configured: false,
      model_providers: ['deepseek'],
    },
    events: [
      {
        timestamp: '2026-07-13T08:00:00+00:00',
        level: 'error',
        component: 'market_worker',
        event_code: 'market_worker.unavailable',
        failure_id: 'market_worker_unavailable',
      },
    ],
    failure_ids: ['market_worker_unavailable'],
    privacy: {
      telemetry_enabled: false,
      automatic_crash_upload: false,
      automatic_diagnostic_upload: false,
      stable_device_identifier: false,
    },
  };
}

it('requests a snapshot only after invocation and saves host-validated bytes locally', async () => {
  const value = snapshot();
  const post = vi.fn(() => Promise.resolve(value));
  const write = vi.fn<(data: string) => Promise<void>>(() => Promise.resolve());
  const close = vi.fn(() => Promise.resolve());
  const abort = vi.fn(() => Promise.resolve());
  const picker: DiagnosticSavePicker = vi.fn(() =>
    Promise.resolve({
      createWritable: () => Promise.resolve({ write, close, abort }),
    }),
  );
  const validated = validateDiagnosticSnapshot(value);
  const validateAtHost = vi.fn(() => Promise.resolve(validated));

  expect(post).not.toHaveBeenCalled();
  await expect(
    exportHealthyDiagnostics({ api: { post }, picker, validateAtHost }),
  ).resolves.toBe('saved');

  expect(post).toHaveBeenCalledWith('/v1/diagnostics/snapshot');
  expect(validateAtHost).toHaveBeenCalledWith(value);
  expect(write).toHaveBeenCalledWith(validated);
  expect(close).toHaveBeenCalledOnce();
  expect(abort).not.toHaveBeenCalled();
  expect(JSON.parse(String(write.mock.calls[0]?.[0]))).toEqual(value);
});

it('treats an explicit save cancellation as a safe no-op', async () => {
  const value = snapshot();
  const picker: DiagnosticSavePicker = vi.fn(() =>
    Promise.reject(new DOMException('cancelled', 'AbortError')),
  );

  await expect(
    exportHealthyDiagnostics({
      api: { post: vi.fn(() => Promise.resolve(value)) },
      picker,
      validateAtHost: () => Promise.resolve(validateDiagnosticSnapshot(value)),
    }),
  ).resolves.toBe('cancelled');
});

it.each([
  { ...snapshot(), extra: 'not allowed' },
  { ...snapshot(), schema_version: 'future-schema' },
  { ...snapshot(), privacy: { telemetry_enabled: true } },
  {
    ...snapshot(),
    events: [
      {
        timestamp: '2026-07-13T08:00:00Z',
        level: 'error',
        component: 'sidecar',
        event_code: 'sidecar.failed',
        failure_id: null,
        message: 'C:\\' + 'Users\\Bao\\secret.txt',
      },
    ],
  },
])(
  'fails closed before opening a save picker for malformed data',
  async (value) => {
    const picker = vi.fn();
    await expect(
      exportHealthyDiagnostics({
        api: { post: vi.fn(() => Promise.resolve(value as JsonValue)) },
        picker,
        validateAtHost: vi.fn(),
      }),
    ).rejects.toBeInstanceOf(DiagnosticExportProtocolError);
    expect(picker).not.toHaveBeenCalled();
  },
);

it('fails closed when the host does not return the exact validated bytes', async () => {
  const value = snapshot();
  const picker = vi.fn();
  await expect(
    exportHealthyDiagnostics({
      api: { post: vi.fn(() => Promise.resolve(value)) },
      picker,
      validateAtHost: () => Promise.resolve('{}\n'),
    }),
  ).rejects.toBeInstanceOf(DiagnosticExportProtocolError);
  expect(picker).not.toHaveBeenCalled();
});

it('never uses fetch or performs an upload', async () => {
  const value = snapshot();
  const browserFetch = vi.spyOn(globalThis, 'fetch');
  const picker: DiagnosticSavePicker = vi.fn(() =>
    Promise.resolve({
      createWritable: () =>
        Promise.resolve({
          write: () => Promise.resolve(),
          close: () => Promise.resolve(),
          abort: () => Promise.resolve(),
        }),
    }),
  );
  await exportHealthyDiagnostics({
    api: { post: vi.fn(() => Promise.resolve(value)) },
    picker,
    validateAtHost: () => Promise.resolve(validateDiagnosticSnapshot(value)),
  });
  expect(browserFetch).not.toHaveBeenCalled();
  browserFetch.mockRestore();
});

it('aborts a partial file when writing fails', async () => {
  const value = snapshot();
  const abort = vi.fn(() => Promise.resolve());
  const picker: DiagnosticSavePicker = vi.fn(() =>
    Promise.resolve({
      createWritable: () =>
        Promise.resolve({
          write: () => Promise.reject(new Error('disk full')),
          close: vi.fn(() => Promise.resolve()),
          abort,
        }),
    }),
  );
  await expect(
    exportHealthyDiagnostics({
      api: { post: vi.fn(() => Promise.resolve(value)) },
      picker,
      validateAtHost: () => Promise.resolve(validateDiagnosticSnapshot(value)),
    }),
  ).rejects.toThrow('disk full');
  expect(abort).toHaveBeenCalledOnce();
});
