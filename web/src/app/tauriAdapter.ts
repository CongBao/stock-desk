import { invoke, isTauri } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';

import type { DesktopAdapter } from './desktopBridge';
import type { ApiTransport, ApiTransportRequest } from '../shared/api/client';

const commands = {
  cancelExit: 'desktop_cancel_exit',
  confirmExit: 'desktop_confirm_exit',
  getRuntimeState: 'desktop_runtime_state',
  openDiagnostics: 'desktop_open_diagnostics',
  requestExit: 'desktop_request_exit',
  restartService: 'desktop_restart_service',
} as const;

const runtimeStateEvent = 'desktop-runtime-state';
const exitStateEvent = 'desktop-exit-state';
export const MAX_DESKTOP_API_RESPONSE_BYTES = 192 * 1_048_576;

type DesktopApiResponse = {
  readonly body: string;
  readonly content_type: string;
  readonly status: number;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function decodeApiResponse(value: unknown): DesktopApiResponse {
  if (!isRecord(value)) throw new TypeError('Invalid desktop API response');
  const keys = Object.keys(value).sort();
  if (
    keys.length !== 3 ||
    keys[0] !== 'body' ||
    keys[1] !== 'content_type' ||
    keys[2] !== 'status' ||
    !Number.isInteger(value.status) ||
    (value.status as number) < 100 ||
    (value.status as number) > 599 ||
    typeof value.content_type !== 'string' ||
    value.content_type.length === 0 ||
    value.content_type.length > 256 ||
    /[\r\n]/u.test(value.content_type) ||
    typeof value.body !== 'string' ||
    !isDesktopApiResponseSizeAllowed(
      new TextEncoder().encode(value.body).byteLength,
    )
  ) {
    throw new TypeError('Invalid desktop API response');
  }
  return value as DesktopApiResponse;
}

export function isDesktopApiResponseSizeAllowed(byteLength: number): boolean {
  return (
    Number.isSafeInteger(byteLength) &&
    byteLength >= 0 &&
    byteLength <= MAX_DESKTOP_API_RESPONSE_BYTES
  );
}

function asError(value: unknown): Error {
  return value instanceof Error
    ? value
    : new Error('Desktop API request failed', { cause: value });
}

function invokeApi(
  request: ApiTransportRequest,
  signal?: AbortSignal,
): Promise<Response> {
  return new Promise((resolve, reject) => {
    let finished = false;
    const cleanup = () => signal?.removeEventListener('abort', onAbort);
    const settle = (operation: () => void) => {
      if (finished) return;
      finished = true;
      cleanup();
      operation();
    };
    const onAbort = () =>
      settle(() => reject(new DOMException('Request aborted', 'AbortError')));

    if (signal?.aborted) {
      onAbort();
      return;
    }
    signal?.addEventListener('abort', onAbort, { once: true });
    void invoke<unknown>('desktop_api_request', { request }).then(
      (value) => {
        settle(() => {
          try {
            const response = decodeApiResponse(value);
            resolve(
              new Response(response.status === 204 ? null : response.body, {
                headers: { 'Content-Type': response.content_type },
                status: response.status,
              }),
            );
          } catch (error) {
            reject(asError(error));
          }
        });
      },
      (error: unknown) => settle(() => reject(asError(error))),
    );
  });
}

export function createTauriApiTransport(): ApiTransport | undefined {
  return isTauri() ? invokeApi : undefined;
}

export function createTauriAdapter(): DesktopAdapter | undefined {
  if (!isTauri()) return undefined;

  return {
    getRuntimeState: () => invoke<unknown>(commands.getRuntimeState),
    restartService: () => invoke<void>(commands.restartService),
    requestExit: () => invoke<void>(commands.requestExit),
    cancelExit: () => invoke<void>(commands.cancelExit),
    confirmExit: () => invoke<void>(commands.confirmExit),
    openDiagnostics: () => invoke<void>(commands.openDiagnostics),
    subscribe: (listener) =>
      listen<unknown>(runtimeStateEvent, (event) => listener(event.payload)),
    subscribeExit: (listener) =>
      listen<unknown>(exitStateEvent, (event) => listener(event.payload)),
  };
}
