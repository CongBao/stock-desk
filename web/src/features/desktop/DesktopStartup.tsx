import { useEffect, useState, type ReactNode } from 'react';

import type {
  DesktopBridge,
  DesktopRuntimeState,
  TauriDesktopBridge,
} from '../../app/desktopBridge';
import { ServiceRecovery } from './ServiceRecovery';

type DesktopStartupProps = {
  readonly bridge: DesktopBridge;
  readonly children: ReactNode;
};

function DesktopStarting() {
  return (
    <main className="desktop-startup">
      <section role="status" className="desktop-startup-card">
        <span className="panel-kicker">STOCK DESK / STARTING</span>
        <h1>正在启动桌面服务</h1>
        <p>正在准备本地行情、公式和回测工作区…</p>
      </section>
    </main>
  );
}

function TauriDesktopStartup({
  bridge,
  children,
}: {
  readonly bridge: TauriDesktopBridge;
  readonly children: ReactNode;
}) {
  const [runtimeState, setRuntimeState] = useState<DesktopRuntimeState>({
    state: 'starting',
  });

  useEffect(() => {
    let active = true;
    let initialized = false;
    let latestSubscribedState: DesktopRuntimeState | undefined;
    let eventChannelFailed = false;
    let unsubscribe: (() => void) | undefined;

    const recoveryState: DesktopRuntimeState = {
      state: 'recovery',
      reason: 'sidecar_unavailable',
      canRestart: true,
    };

    async function start() {
      try {
        const stopListening = await bridge.subscribe(
          (state) => {
            latestSubscribedState = state;
            if (active && initialized) setRuntimeState(state);
          },
          () => {
            eventChannelFailed = true;
            if (active && initialized) setRuntimeState(recoveryState);
          },
        );
        if (!active) {
          stopListening();
          return;
        }
        unsubscribe = stopListening;
        const state = await bridge.getRuntimeState();
        initialized = true;
        if (active) {
          setRuntimeState(
            eventChannelFailed
              ? recoveryState
              : (latestSubscribedState ?? state),
          );
        }
      } catch {
        if (active) setRuntimeState(recoveryState);
      }
    }

    void start();
    return () => {
      active = false;
      unsubscribe?.();
    };
  }, [bridge]);

  async function restartService() {
    const previousState = runtimeState;
    setRuntimeState({ state: 'starting' });
    try {
      await bridge.restartService();
    } catch (error) {
      setRuntimeState((currentState) =>
        currentState.state === 'starting' ? previousState : currentState,
      );
      throw error;
    }
  }

  if (runtimeState.state === 'ready') return children;
  if (runtimeState.state === 'starting') return <DesktopStarting />;
  return (
    <ServiceRecovery
      bridge={bridge}
      reason={runtimeState.reason}
      canRestart={runtimeState.canRestart}
      onRestarting={restartService}
    />
  );
}

export function DesktopStartup({ bridge, children }: DesktopStartupProps) {
  if (!bridge.isDesktop) return children;
  return <TauriDesktopStartup bridge={bridge}>{children}</TauriDesktopStartup>;
}
