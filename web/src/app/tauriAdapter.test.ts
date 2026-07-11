import { invoke, isTauri } from '@tauri-apps/api/core';
import { listen, type EventCallback } from '@tauri-apps/api/event';

import { createTauriAdapter, createTauriApiTransport } from './tauriAdapter';

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
