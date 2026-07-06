import type { JsonValue } from '../../shared/api/client';

import backendBarsResponse from './fixtures/backend-bars-response.json';
import backendPresetPoolResponse from './fixtures/backend-preset-pool-response.json';
import { MAX_SHA256_INPUT_BYTES, sha256FallbackHex, sha256Hex } from './sha256';

const encoder = new TextEncoder();

function sortJsonKeys(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map(sortJsonKeys);
  if (value === null || typeof value !== 'object') return value;
  const object = value as { readonly [key: string]: JsonValue };
  return Object.fromEntries(
    Object.keys(object)
      .sort()
      .map((key) => [key, sortJsonKeys(object[key])]),
  );
}

function canonicalBytes(value: JsonValue, ensureAscii: boolean): Uint8Array {
  const encoded = JSON.stringify(sortJsonKeys(value));
  if (!ensureAscii) return encoder.encode(encoded);
  let ascii = '';
  for (let index = 0; index < encoded.length; index += 1) {
    const codeUnit = encoded.charCodeAt(index);
    ascii +=
      codeUnit <= 0x7f
        ? encoded.charAt(index)
        : `\\u${codeUnit.toString(16).padStart(4, '0')}`;
  }
  return encoder.encode(ascii);
}

function hexadecimal(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer), (byte) =>
    byte.toString(16).padStart(2, '0'),
  ).join('');
}

it.each([
  [
    'empty',
    '',
    'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
  ],
  [
    'abc',
    'abc',
    'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
  ],
  [
    'NIST multi-block',
    'abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq',
    '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1',
  ],
] as const)('hashes the %s SHA-256 vector', (_name, source, expected) => {
  expect(sha256FallbackHex(encoder.encode(source))).toBe(expected);
});

it('bounds fallback input before allocating padded blocks', () => {
  expect(() =>
    sha256FallbackHex(new Uint8Array(MAX_SHA256_INPUT_BYTES + 1)),
  ).toThrow(RangeError);
});

it('bounds the WebCrypto path before hashing', async () => {
  await expect(
    sha256Hex(new Uint8Array(MAX_SHA256_INPUT_BYTES + 1)),
  ).rejects.toThrow(RangeError);
});

it('prefers WebCrypto when subtle is available and restores the global', async () => {
  const originalCrypto = globalThis.crypto;
  const digest = vi.fn(() => Promise.resolve(new Uint8Array(32).buffer));
  vi.stubGlobal('crypto', { subtle: { digest } });
  try {
    await expect(sha256Hex(encoder.encode('abc'))).resolves.toBe(
      '0'.repeat(64),
    );
    expect(digest).toHaveBeenCalledOnce();
  } finally {
    vi.unstubAllGlobals();
  }
  expect(globalThis.crypto).toBe(originalCrypto);
});

it('matches WebCrypto for real route, manifest, and UTF-8 preset payloads', async () => {
  const manifest = backendBarsResponse.routing_manifest;
  const routePayload: JsonValue = {
    schema_version: manifest.schema_version,
    category: manifest.category,
    request: manifest.request,
    priority: manifest.priority,
    attempts: manifest.attempts,
    selected_source: manifest.selected_source,
    upstream_dataset_version: manifest.upstream_dataset_version,
    upstream_data_cutoff: manifest.upstream_data_cutoff,
    upstream_adjustment: manifest.upstream_adjustment,
    transition: manifest.transition,
  };
  const preset = backendPresetPoolResponse.detail;
  const snapshotPayload: JsonValue = {
    composition: preset.provenance.composition,
    instrument_dataset_version: preset.provenance.instrument_dataset_version,
    instrument_manifest_record_id: preset.provenance.manifest_record_id,
    schema_version: 'stock-desk-preset-pool-v1',
  };
  const cases = [
    [
      canonicalBytes(routePayload, true),
      manifest.route_version,
      'route payload',
    ],
    [
      canonicalBytes(manifest, true),
      backendBarsResponse.manifest_record_id,
      'manifest payload',
    ],
    [
      canonicalBytes(snapshotPayload, false),
      preset.snapshot_id,
      'UTF-8 preset payload',
    ],
  ] as const;
  const subtle = globalThis.crypto.subtle;

  for (const [bytes, expected, name] of cases) {
    const fallback = sha256FallbackHex(bytes);
    const webCrypto = hexadecimal(
      await subtle.digest('SHA-256', new Uint8Array(bytes)),
    );
    expect(fallback, name).toBe(webCrypto);
    expect(`sha256:${fallback}`, name).toBe(expected);
  }
});
