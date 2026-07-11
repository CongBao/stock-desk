import {
  createDesktopBridge,
  DesktopBridgeProtocolError,
  type DesktopAdapter,
  type DesktopRuntimeState,
} from './desktopBridge';

function createAdapter(
  overrides: Partial<DesktopAdapter> = {},
): DesktopAdapter {
  return {
    cancelExit: vi.fn(() => Promise.resolve()),
    confirmExit: vi.fn(() => Promise.resolve()),
    getRuntimeState: vi.fn(() => Promise.resolve({ state: 'ready' })),
    openDiagnostics: vi.fn(() => Promise.resolve()),
    requestExit: vi.fn(() => Promise.resolve()),
    restartService: vi.fn(() => Promise.resolve()),
    subscribe: vi.fn(() => Promise.resolve(() => undefined)),
    subscribeExit: vi.fn(() => Promise.resolve(() => undefined)),
    ...overrides,
  };
}

it('uses a synchronous ready and no-op fallback outside Tauri', () => {
  const bridge = createDesktopBridge();
  const listener = vi.fn();

  expect(bridge.isDesktop).toBe(false);
  expect(bridge.getRuntimeState()).toEqual({ state: 'ready' });
  expect(bridge.restartService()).toBeUndefined();
  expect(bridge.requestExit()).toBeUndefined();
  expect(bridge.cancelExit()).toBeUndefined();
  expect(bridge.confirmExit()).toBeUndefined();
  expect(bridge.openDiagnostics()).toBeUndefined();

  const unsubscribe = bridge.subscribe(listener);
  const unsubscribeExit = bridge.subscribeExit(listener);
  expect(unsubscribe).toBeTypeOf('function');
  expect(listener).not.toHaveBeenCalled();
  expect(() => unsubscribe()).not.toThrow();
  expect(() => unsubscribeExit()).not.toThrow();
});

it.each<readonly [unknown, DesktopRuntimeState]>([
  [{ state: 'starting' }, { state: 'starting' }],
  [{ state: 'ready' }, { state: 'ready' }],
  [
    { state: 'recovery', reason: 'sidecar_unavailable', can_restart: true },
    { state: 'recovery', reason: 'sidecar_unavailable', canRestart: true },
  ],
  [
    { state: 'recovery', reason: 'startup_timeout', can_restart: false },
    { state: 'recovery', reason: 'startup_timeout', canRestart: false },
  ],
])(
  'strictly decodes a supported desktop runtime state',
  async (wire, state) => {
    const bridge = createDesktopBridge(
      createAdapter({ getRuntimeState: vi.fn(() => Promise.resolve(wire)) }),
    );

    expect(bridge.isDesktop).toBe(true);
    await expect(bridge.getRuntimeState()).resolves.toEqual(state);
  },
);

it.each([
  { state: 'unknown' },
  { state: 'ready', token: 'must-not-cross-the-bridge' },
  { state: 'starting', port: 49152 },
  {
    state: 'recovery',
    reason: 'sidecar_unavailable',
    can_restart: true,
    url: 'http://127.0.0.1:49152',
  },
  { state: 'recovery', reason: 'traceback', can_restart: true },
  { state: 'recovery', reason: 'startup_timeout' },
  null,
])('fails closed for an unknown or expanded runtime payload', async (wire) => {
  const bridge = createDesktopBridge(
    createAdapter({ getRuntimeState: vi.fn(() => Promise.resolve(wire)) }),
  );

  await expect(bridge.getRuntimeState()).rejects.toBeInstanceOf(
    DesktopBridgeProtocolError,
  );
});

it('delegates desktop commands without accepting command payloads', async () => {
  const adapter = createAdapter();
  const bridge = createDesktopBridge(adapter);

  await expect(bridge.restartService()).resolves.toBeUndefined();
  await expect(bridge.requestExit()).resolves.toBeUndefined();
  await expect(bridge.cancelExit()).resolves.toBeUndefined();
  await expect(bridge.confirmExit()).resolves.toBeUndefined();
  await expect(bridge.openDiagnostics()).resolves.toBeUndefined();

  expect(adapter.restartService).toHaveBeenCalledWith();
  expect(adapter.requestExit).toHaveBeenCalledWith();
  expect(adapter.cancelExit).toHaveBeenCalledWith();
  expect(adapter.confirmExit).toHaveBeenCalledWith();
  expect(adapter.openDiagnostics).toHaveBeenCalledWith();
});

it('decodes subscribed events before exposing them and rejects unsafe events', async () => {
  const receiver: { current?: (payload: unknown) => void } = {};
  const unlisten = vi.fn();
  const adapter = createAdapter({
    subscribe: vi.fn((listener: (payload: unknown) => void) => {
      receiver.current = listener;
      return Promise.resolve(unlisten);
    }),
  });
  const bridge = createDesktopBridge(adapter);
  const listener = vi.fn();

  const unsubscribe = await bridge.subscribe(listener);
  receiver.current?.({ state: 'ready' });
  expect(listener).toHaveBeenCalledWith({ state: 'ready' });

  expect(() =>
    receiver.current?.({ state: 'ready', token: 'must-not-cross-the-bridge' }),
  ).toThrow(DesktopBridgeProtocolError);
  expect(listener).toHaveBeenCalledTimes(1);

  unsubscribe();
  expect(unlisten).toHaveBeenCalledOnce();
});

it('does not read from or write to browser persistence', async () => {
  const localGet = vi.spyOn(Storage.prototype, 'getItem');
  const localSet = vi.spyOn(Storage.prototype, 'setItem');
  const adapter = createAdapter();
  const bridge = createDesktopBridge(adapter);

  await bridge.getRuntimeState();
  await bridge.restartService();
  await bridge.requestExit();
  await bridge.cancelExit();
  await bridge.confirmExit();
  await bridge.openDiagnostics();
  await bridge.subscribe(() => undefined);
  await bridge.subscribeExit(() => undefined);

  expect(localGet).not.toHaveBeenCalled();
  expect(localSet).not.toHaveBeenCalled();
});
