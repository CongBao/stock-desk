import type { ApiClient, JsonValue } from '../shared/api/client';

export const MAX_DIAGNOSTIC_SNAPSHOT_BYTES = 256 * 1024;

export type DiagnosticExportResult = 'cancelled' | 'saved';

type FileSystemWritableFileStream = {
  readonly abort: () => Promise<void>;
  readonly close: () => Promise<void>;
  readonly write: (data: string) => Promise<void>;
};

type FileSystemFileHandle = {
  readonly createWritable: () => Promise<FileSystemWritableFileStream>;
};

export type DiagnosticSavePicker = (options: {
  readonly excludeAcceptAllOption: true;
  readonly suggestedName: string;
  readonly types: readonly [
    {
      readonly accept: { readonly 'application/json': readonly ['.json'] };
      readonly description: string;
    },
  ];
}) => Promise<FileSystemFileHandle>;

const SAFE_ID = /^[a-z][a-z0-9_.-]{0,95}$/u;
const VERSION = /^[0-9A-Za-z.+-]{1,64}$/u;
const REVISION = /^[0-9a-f]{40}$/u;
const RFC3339_UTC =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/u;
const PRIVATE_PATH =
  /(?:[a-z]:)?[\\/]users[\\/][^\\/\s"']+|\/(?:home|Users)\/[^/\s"']+/iu;
const DIRECT_CREDENTIAL =
  /\b(?:gh[pousr]_[A-Za-z0-9]{36,}|AKIA[0-9A-Z]{16}|sk-(?:proj-)?[A-Za-z0-9._-]{24,}|sk-ant-[A-Za-z0-9_-]{24,})\b/u;

export class DiagnosticExportProtocolError extends Error {
  constructor() {
    super('Diagnostic snapshot did not match the public protocol');
    this.name = 'DiagnosticExportProtocolError';
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return (
    actual.length === expected.length &&
    actual.every((key, index) => key === expected[index])
  );
}

function isSafeId(value: unknown): value is string {
  return typeof value === 'string' && SAFE_ID.test(value);
}

function isSafeIdList(value: unknown, maximum: number): value is string[] {
  return (
    Array.isArray(value) && value.length <= maximum && value.every(isSafeId)
  );
}

function isSourceConfiguration(value: unknown): boolean {
  if (
    !isRecord(value) ||
    !exactKeys(value, [
      'available',
      'daily_sources',
      'weekly_sources',
      'minute_sources',
      'instrument_sources',
      'tushare_token_configured',
      'local_tdx_configured',
      'model_providers',
    ]) ||
    typeof value.available !== 'boolean' ||
    typeof value.tushare_token_configured !== 'boolean' ||
    typeof value.local_tdx_configured !== 'boolean'
  ) {
    return false;
  }
  for (const key of [
    'daily_sources',
    'weekly_sources',
    'minute_sources',
    'instrument_sources',
  ] as const) {
    if (!isSafeIdList(value[key], 8)) return false;
  }
  return (
    Array.isArray(value.model_providers) &&
    value.model_providers.length <= 8 &&
    value.model_providers.every((provider) =>
      ['deepseek', 'openai_compatible', 'ollama'].includes(String(provider)),
    )
  );
}

function isEvent(value: unknown): boolean {
  return (
    isRecord(value) &&
    exactKeys(value, [
      'timestamp',
      'level',
      'component',
      'event_code',
      'failure_id',
    ]) &&
    typeof value.timestamp === 'string' &&
    RFC3339_UTC.test(value.timestamp) &&
    ['info', 'warning', 'error'].includes(String(value.level)) &&
    isSafeId(value.component) &&
    isSafeId(value.event_code) &&
    (value.failure_id === null || isSafeId(value.failure_id))
  );
}

export function validateDiagnosticSnapshot(value: unknown): string {
  if (
    !isRecord(value) ||
    !exactKeys(value, [
      'schema_version',
      'created_at',
      'application',
      'platform',
      'service_health',
      'configuration',
      'events',
      'failure_ids',
      'privacy',
    ]) ||
    value.schema_version !== 'stock-desk-diagnostic-snapshot-v1' ||
    typeof value.created_at !== 'string' ||
    !RFC3339_UTC.test(value.created_at) ||
    !isRecord(value.application) ||
    !exactKeys(value.application, ['version', 'source_revision']) ||
    typeof value.application.version !== 'string' ||
    !VERSION.test(value.application.version) ||
    !(
      value.application.source_revision === null ||
      (typeof value.application.source_revision === 'string' &&
        REVISION.test(value.application.source_revision))
    ) ||
    !isRecord(value.platform) ||
    !exactKeys(value.platform, ['system', 'architecture']) ||
    !['windows', 'other'].includes(String(value.platform.system)) ||
    !['x86_64', 'other'].includes(String(value.platform.architecture)) ||
    !isRecord(value.service_health) ||
    !exactKeys(value.service_health, ['sidecar', 'storage', 'market_worker']) ||
    value.service_health.sidecar !== 'ready' ||
    !['ready', 'unavailable'].includes(String(value.service_health.storage)) ||
    !['ready', 'unavailable'].includes(
      String(value.service_health.market_worker),
    ) ||
    !isSourceConfiguration(value.configuration) ||
    !Array.isArray(value.events) ||
    value.events.length > 200 ||
    !value.events.every(isEvent) ||
    !isSafeIdList(value.failure_ids, 32) ||
    new Set(value.failure_ids).size !== value.failure_ids.length ||
    !isRecord(value.privacy) ||
    !exactKeys(value.privacy, [
      'telemetry_enabled',
      'automatic_crash_upload',
      'automatic_diagnostic_upload',
      'stable_device_identifier',
    ]) ||
    value.privacy.telemetry_enabled !== false ||
    value.privacy.automatic_crash_upload !== false ||
    value.privacy.automatic_diagnostic_upload !== false ||
    value.privacy.stable_device_identifier !== false
  ) {
    throw new DiagnosticExportProtocolError();
  }

  const rendered = `${JSON.stringify(value, null, 2)}\n`;
  const bytes = new TextEncoder().encode(rendered).byteLength;
  if (
    bytes > MAX_DIAGNOSTIC_SNAPSHOT_BYTES ||
    PRIVATE_PATH.test(rendered) ||
    DIRECT_CREDENTIAL.test(rendered) ||
    /authorization\s*[:=]/iu.test(rendered)
  ) {
    throw new DiagnosticExportProtocolError();
  }
  return rendered;
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    'name' in error &&
    error.name === 'AbortError'
  );
}

export async function exportHealthyDiagnostics({
  api,
  picker,
  validateAtHost,
}: {
  readonly api: Pick<ApiClient, 'post'>;
  readonly picker: DiagnosticSavePicker | undefined;
  readonly validateAtHost: (snapshot: JsonValue) => Promise<string>;
}): Promise<DiagnosticExportResult> {
  if (picker === undefined) throw new DiagnosticExportProtocolError();
  const snapshot = await api.post('/v1/diagnostics/snapshot');
  const browserValidated = validateDiagnosticSnapshot(snapshot);
  const hostValidated = await validateAtHost(snapshot ?? null);
  if (hostValidated !== browserValidated)
    throw new DiagnosticExportProtocolError();

  let handle: FileSystemFileHandle;
  try {
    handle = await picker({
      excludeAcceptAllOption: true,
      suggestedName: 'stock-desk-diagnostics.json',
      types: [
        {
          accept: { 'application/json': ['.json'] },
          description: 'Stock Desk 安全诊断包',
        },
      ],
    });
  } catch (error) {
    if (isAbortError(error)) return 'cancelled';
    throw error;
  }
  const writable = await handle.createWritable();
  try {
    await writable.write(hostValidated);
    await writable.close();
  } catch (error) {
    try {
      await writable.abort();
    } catch {
      // The original write failure remains authoritative.
    }
    throw error;
  }
  return 'saved';
}
