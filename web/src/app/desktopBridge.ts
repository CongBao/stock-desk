import {
  DESKTOP_BUILD_VERSION,
  isDesktopVersion,
  isStableDesktopVersion,
} from './buildIdentity';

export type DesktopRecoveryReason =
  | 'permission_denied'
  | 'restart_limit_reached'
  | 'sidecar_unavailable'
  | 'startup_timeout'
  | 'version_mismatch';

export type DesktopRuntimeState =
  | { readonly state: 'starting' }
  | { readonly state: 'ready' }
  | {
      readonly state: 'recovery';
      readonly reason: DesktopRecoveryReason;
      readonly canRestart: boolean;
    };

export type DesktopRuntimeListener = (state: DesktopRuntimeState) => void;
export type DesktopExitState =
  | { readonly state: 'idle' | 'confirm' | 'checking' | 'shutting_down' }
  | {
      readonly state: 'blocked' | 'checkpoint_timed_out';
      readonly queued: number;
      readonly running: number;
    };
export type DesktopExitListener = (state: DesktopExitState) => void;
export type DesktopUpdateState =
  | {
      readonly state: 'disabled' | 'idle' | 'checking';
      readonly currentVersion: string;
    }
  | {
      readonly state: 'available';
      readonly currentVersion: string;
      readonly version: string;
      readonly notes: string | null;
    }
  | {
      readonly state:
        'downloading' | 'verifying' | 'ready_to_install' | 'installing';
      readonly currentVersion: string;
      readonly version: string;
    }
  | {
      readonly state: 'failed';
      readonly currentVersion: string;
      readonly code: string;
      readonly canRetry: boolean;
    };
export type DesktopUpdateListener = (state: DesktopUpdateState) => void;
export type DesktopProtocolErrorListener = () => void;
export type DesktopUnsubscribe = () => void;
export type DesktopDiagnosticExportResult = 'cancelled' | 'saved';

export type DesktopAdapter = {
  readonly getRuntimeState: () => Promise<unknown>;
  readonly restartService: () => Promise<void>;
  readonly requestExit: () => Promise<void>;
  readonly cancelExit: () => Promise<void>;
  readonly confirmExit: () => Promise<void>;
  readonly openDiagnostics: () => Promise<void>;
  readonly exportDiagnostics: () => Promise<DesktopDiagnosticExportResult>;
  readonly getUpdateState: () => Promise<unknown>;
  readonly checkForUpdates: () => Promise<unknown>;
  readonly dismissUpdate: () => Promise<void>;
  readonly confirmUpdate: () => Promise<void>;
  readonly subscribe: (
    listener: (payload: unknown) => void,
  ) => Promise<DesktopUnsubscribe>;
  readonly subscribeExit: (
    listener: (payload: unknown) => void,
  ) => Promise<DesktopUnsubscribe>;
  readonly subscribeUpdate: (
    listener: (payload: unknown) => void,
  ) => Promise<DesktopUnsubscribe>;
};

export type BrowserDesktopBridge = {
  readonly isDesktop: false;
  readonly getRuntimeState: () => DesktopRuntimeState;
  readonly restartService: () => void;
  readonly requestExit: () => void;
  readonly cancelExit: () => void;
  readonly confirmExit: () => void;
  readonly openDiagnostics: () => void;
  readonly exportDiagnostics: () => void;
  readonly getUpdateState: () => DesktopUpdateState;
  readonly checkForUpdates: () => DesktopUpdateState;
  readonly dismissUpdate: () => void;
  readonly confirmUpdate: () => void;
  readonly subscribe: (
    listener: DesktopRuntimeListener,
    onProtocolError?: DesktopProtocolErrorListener,
  ) => DesktopUnsubscribe;
  readonly subscribeExit: (
    listener: DesktopExitListener,
    onProtocolError?: DesktopProtocolErrorListener,
  ) => DesktopUnsubscribe;
  readonly subscribeUpdate: (
    listener: DesktopUpdateListener,
    onProtocolError?: DesktopProtocolErrorListener,
  ) => DesktopUnsubscribe;
};

export type TauriDesktopBridge = {
  readonly isDesktop: true;
  readonly getRuntimeState: () => Promise<DesktopRuntimeState>;
  readonly restartService: () => Promise<void>;
  readonly requestExit: () => Promise<void>;
  readonly cancelExit: () => Promise<void>;
  readonly confirmExit: () => Promise<void>;
  readonly openDiagnostics: () => Promise<void>;
  readonly exportDiagnostics: () => Promise<DesktopDiagnosticExportResult>;
  readonly getUpdateState: () => Promise<DesktopUpdateState>;
  readonly checkForUpdates: () => Promise<DesktopUpdateState>;
  readonly dismissUpdate: () => Promise<void>;
  readonly confirmUpdate: () => Promise<void>;
  readonly subscribe: (
    listener: DesktopRuntimeListener,
    onProtocolError?: DesktopProtocolErrorListener,
  ) => Promise<DesktopUnsubscribe>;
  readonly subscribeExit: (
    listener: DesktopExitListener,
    onProtocolError?: DesktopProtocolErrorListener,
  ) => Promise<DesktopUnsubscribe>;
  readonly subscribeUpdate: (
    listener: DesktopUpdateListener,
    onProtocolError?: DesktopProtocolErrorListener,
  ) => Promise<DesktopUnsubscribe>;
};

export type DesktopBridge = BrowserDesktopBridge | TauriDesktopBridge;

const browserReadyState: DesktopRuntimeState = Object.freeze({
  state: 'ready',
});
const browserUpdateState: DesktopUpdateState = Object.freeze({
  state: 'disabled',
  currentVersion: DESKTOP_BUILD_VERSION ?? 'unavailable',
});

const recoveryReasons = new Set<DesktopRecoveryReason>([
  'permission_denied',
  'restart_limit_reached',
  'sidecar_unavailable',
  'startup_timeout',
  'version_mismatch',
]);

export class DesktopBridgeProtocolError extends Error {
  constructor() {
    super('Desktop runtime response did not match the public protocol');
    this.name = 'DesktopBridgeProtocolError';
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function hasExactKeys(
  value: Readonly<Record<string, unknown>>,
  expected: readonly string[],
): boolean {
  const actual = Object.keys(value).sort();
  const canonicalExpected = [...expected].sort();
  return (
    actual.length === canonicalExpected.length &&
    actual.every((key, index) => key === canonicalExpected[index])
  );
}

function decodeRuntimeState(value: unknown): DesktopRuntimeState {
  if (!isRecord(value) || typeof value.state !== 'string') {
    throw new DesktopBridgeProtocolError();
  }

  if (value.state === 'starting' || value.state === 'ready') {
    if (!hasExactKeys(value, ['state'])) {
      throw new DesktopBridgeProtocolError();
    }
    return { state: value.state };
  }

  if (
    value.state !== 'recovery' ||
    !hasExactKeys(value, ['state', 'reason', 'can_restart']) ||
    typeof value.reason !== 'string' ||
    !recoveryReasons.has(value.reason as DesktopRecoveryReason) ||
    typeof value.can_restart !== 'boolean'
  ) {
    throw new DesktopBridgeProtocolError();
  }

  return {
    state: 'recovery',
    reason: value.reason as DesktopRecoveryReason,
    canRestart: value.can_restart,
  };
}

function decodeExitState(value: unknown): DesktopExitState {
  if (!isRecord(value) || typeof value.state !== 'string') {
    throw new DesktopBridgeProtocolError();
  }
  if (
    value.state === 'idle' ||
    value.state === 'confirm' ||
    value.state === 'checking' ||
    value.state === 'shutting_down'
  ) {
    if (!hasExactKeys(value, ['state'])) throw new DesktopBridgeProtocolError();
    return { state: value.state };
  }
  if (
    (value.state !== 'blocked' && value.state !== 'checkpoint_timed_out') ||
    !hasExactKeys(value, ['state', 'queued', 'running']) ||
    !Number.isSafeInteger(value.queued) ||
    (value.queued as number) < 0 ||
    !Number.isSafeInteger(value.running) ||
    (value.running as number) < 0
  ) {
    throw new DesktopBridgeProtocolError();
  }
  return {
    state: value.state,
    queued: value.queued as number,
    running: value.running as number,
  };
}

function decodeUpdateState(value: unknown): DesktopUpdateState {
  if (
    !isRecord(value) ||
    typeof value.state !== 'string' ||
    !isDesktopVersion(value.current_version)
  ) {
    throw new DesktopBridgeProtocolError();
  }
  if (
    value.state === 'disabled' ||
    value.state === 'idle' ||
    value.state === 'checking'
  ) {
    if (!hasExactKeys(value, ['state', 'current_version'])) {
      throw new DesktopBridgeProtocolError();
    }
    return { state: value.state, currentVersion: value.current_version };
  }
  if (value.state === 'available') {
    if (
      !hasExactKeys(value, ['state', 'current_version', 'version', 'notes']) ||
      !isStableDesktopVersion(value.version) ||
      (value.notes !== null &&
        (typeof value.notes !== 'string' || value.notes.length > 2_000))
    ) {
      throw new DesktopBridgeProtocolError();
    }
    return {
      state: 'available',
      currentVersion: value.current_version,
      version: value.version,
      notes: value.notes,
    };
  }
  if (
    value.state === 'downloading' ||
    value.state === 'verifying' ||
    value.state === 'ready_to_install' ||
    value.state === 'installing'
  ) {
    if (
      !hasExactKeys(value, ['state', 'current_version', 'version']) ||
      !isStableDesktopVersion(value.version)
    ) {
      throw new DesktopBridgeProtocolError();
    }
    return {
      state: value.state,
      currentVersion: value.current_version,
      version: value.version,
    };
  }
  if (
    value.state !== 'failed' ||
    !hasExactKeys(value, ['state', 'current_version', 'code', 'can_retry']) ||
    typeof value.code !== 'string' ||
    !/^desktop_updater_[a-z0-9_]+$/u.test(value.code) ||
    typeof value.can_retry !== 'boolean'
  ) {
    throw new DesktopBridgeProtocolError();
  }
  return {
    state: 'failed',
    currentVersion: value.current_version,
    code: value.code,
    canRetry: value.can_retry,
  };
}

function handleDecodedPayload<T>(
  decode: (payload: unknown) => T,
  listener: (value: T) => void,
  onProtocolError: DesktopProtocolErrorListener | undefined,
): (payload: unknown) => void {
  return (payload) => {
    try {
      listener(decode(payload));
    } catch (error) {
      if (
        onProtocolError !== undefined &&
        error instanceof DesktopBridgeProtocolError
      ) {
        onProtocolError();
        return;
      }
      throw error;
    }
  };
}

function createBrowserBridge(): BrowserDesktopBridge {
  return {
    isDesktop: false,
    getRuntimeState: () => browserReadyState,
    restartService: () => undefined,
    requestExit: () => undefined,
    cancelExit: () => undefined,
    confirmExit: () => undefined,
    openDiagnostics: () => undefined,
    exportDiagnostics: () => undefined,
    getUpdateState: () => browserUpdateState,
    checkForUpdates: () => browserUpdateState,
    dismissUpdate: () => undefined,
    confirmUpdate: () => undefined,
    subscribe: () => () => undefined,
    subscribeExit: () => () => undefined,
    subscribeUpdate: () => () => undefined,
  };
}

function createTauriBridge(adapter: DesktopAdapter): TauriDesktopBridge {
  return {
    isDesktop: true,
    getRuntimeState: async () =>
      decodeRuntimeState(await adapter.getRuntimeState()),
    restartService: () => adapter.restartService(),
    requestExit: () => adapter.requestExit(),
    cancelExit: () => adapter.cancelExit(),
    confirmExit: () => adapter.confirmExit(),
    openDiagnostics: () => adapter.openDiagnostics(),
    exportDiagnostics: () => adapter.exportDiagnostics(),
    getUpdateState: async () =>
      decodeUpdateState(await adapter.getUpdateState()),
    checkForUpdates: async () =>
      decodeUpdateState(await adapter.checkForUpdates()),
    dismissUpdate: () => adapter.dismissUpdate(),
    confirmUpdate: () => adapter.confirmUpdate(),
    subscribe: (listener, onProtocolError) =>
      adapter.subscribe(
        handleDecodedPayload(decodeRuntimeState, listener, onProtocolError),
      ),
    subscribeExit: (listener, onProtocolError) =>
      adapter.subscribeExit(
        handleDecodedPayload(decodeExitState, listener, onProtocolError),
      ),
    subscribeUpdate: (listener, onProtocolError) =>
      adapter.subscribeUpdate(
        handleDecodedPayload(decodeUpdateState, listener, onProtocolError),
      ),
  };
}

export function createDesktopBridge(): BrowserDesktopBridge;
export function createDesktopBridge(
  adapter: DesktopAdapter,
): TauriDesktopBridge;
export function createDesktopBridge(adapter?: DesktopAdapter): DesktopBridge {
  return adapter === undefined
    ? createBrowserBridge()
    : createTauriBridge(adapter);
}
