import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react';

import type {
  DesktopBridge,
  DesktopExitState,
  TauriDesktopBridge,
} from '../../app/desktopBridge';

type DesktopExitGuardProps = {
  readonly bridge: DesktopBridge;
  readonly children: ReactNode;
};

const idleExitState: DesktopExitState = Object.freeze({ state: 'idle' });

function isSameExitState(
  current: DesktopExitState,
  next: DesktopExitState,
): boolean {
  return (
    current.state === next.state &&
    ((current.state !== 'blocked' &&
      current.state !== 'checkpoint_timed_out') ||
      (next.state === current.state &&
        current.queued === next.queued &&
        current.running === next.running))
  );
}

function TauriDesktopExitGuard({
  bridge,
  children,
}: {
  readonly bridge: TauriDesktopBridge;
  readonly children: ReactNode;
}) {
  const [exitState, setExitState] = useState<DesktopExitState>(idleExitState);
  const [actionPending, setActionPending] = useState(false);
  const [checkpointing, setCheckpointing] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let active = true;
    let unsubscribe: (() => void) | undefined;

    void bridge
      .subscribeExit(
        (next) => {
          if (!active) return;
          setActionPending(false);
          if (next.state !== 'checking') setCheckpointing(false);
          setExitState((current) =>
            isSameExitState(current, next) ? current : next,
          );
        },
        () => {
          if (!active) return;
          setActionPending(false);
          setExitState(idleExitState);
        },
      )
      .then((stopListening) => {
        if (!active) {
          stopListening();
          return;
        }
        unsubscribe = stopListening;
      })
      .catch(() => {
        if (active) setExitState(idleExitState);
      });

    return () => {
      active = false;
      unsubscribe?.();
    };
  }, [bridge]);

  useEffect(() => {
    if (
      exitState.state === 'confirm' ||
      exitState.state === 'blocked' ||
      exitState.state === 'checkpoint_timed_out'
    ) {
      cancelRef.current?.focus();
    }
  }, [exitState]);

  const cancelExit = useCallback(async () => {
    if (actionPending) return;
    const previousState = exitState.state;
    setActionPending(true);
    try {
      await bridge.cancelExit();
    } catch {
      setExitState((current) =>
        current.state === previousState ? idleExitState : current,
      );
    } finally {
      setActionPending(false);
    }
  }, [actionPending, bridge, exitState.state]);

  const confirmExit = useCallback(async () => {
    if (
      actionPending ||
      (exitState.state !== 'confirm' &&
        exitState.state !== 'blocked' &&
        exitState.state !== 'checkpoint_timed_out')
    )
      return;
    setActionPending(true);
    setCheckpointing(exitState.state !== 'confirm');
    setExitState({ state: 'checking' });
    try {
      await bridge.confirmExit();
    } catch {
      setExitState((current) =>
        current.state === 'checking' ? idleExitState : current,
      );
      setActionPending(false);
    }
  }, [actionPending, bridge, exitState.state]);

  function containFocus(event: ReactKeyboardEvent<HTMLElement>) {
    if (event.key === 'Escape') {
      if (
        !actionPending &&
        (exitState.state === 'confirm' ||
          exitState.state === 'blocked' ||
          exitState.state === 'checkpoint_timed_out')
      ) {
        event.preventDefault();
        void cancelExit();
      }
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(
      dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
      ) ?? [],
    );
    const first = focusable[0];
    const last = focusable.at(-1);
    if (first === undefined || last === undefined) {
      event.preventDefault();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  const isBusy =
    exitState.state === 'checking' || exitState.state === 'shutting_down';

  return (
    <>
      {children}
      {exitState.state === 'idle' ? null : (
        <div className="desktop-exit-backdrop" role="presentation">
          <dialog
            ref={dialogRef}
            className="desktop-exit-dialog"
            open
            aria-modal="true"
            aria-labelledby="desktop-exit-title"
            aria-describedby="desktop-exit-description"
            tabIndex={-1}
            onKeyDown={containFocus}
          >
            <span className="panel-kicker">STOCK DESK / SAFE EXIT</span>
            <h2 id="desktop-exit-title">
              {exitState.state === 'checkpoint_timed_out'
                ? '尚未到达安全检查点'
                : exitState.state === 'blocked'
                  ? '后台任务仍在运行'
                  : exitState.state === 'confirm'
                    ? '确认退出 Stock Desk？'
                    : exitState.state === 'checking'
                      ? checkpointing
                        ? '正在保存安全检查点'
                        : '正在检查后台任务'
                      : '正在安全退出'}
            </h2>
            <div id="desktop-exit-description" className="desktop-exit-copy">
              {exitState.state === 'blocked' ||
              exitState.state === 'checkpoint_timed_out' ? (
                <>
                  <p>
                    {exitState.state === 'checkpoint_timed_out'
                      ? '当前任务在 10 秒内未到达安全位置，应用仍保持运行。可稍后重试或打开诊断。'
                      : '确认后将停止领取新任务，最多等待 10 秒保存安全检查点；下次启动可选择继续或取消。'}
                  </p>
                  <dl className="desktop-exit-counts">
                    <div>
                      <dt>排队任务</dt>
                      <dd>{exitState.queued}</dd>
                    </div>
                    <div>
                      <dt>运行任务</dt>
                      <dd>{exitState.running}</dd>
                    </div>
                  </dl>
                </>
              ) : exitState.state === 'confirm' ? (
                <p>退出前会先检查后台任务，避免工作意外中断。</p>
              ) : (
                <p aria-live="polite">
                  {exitState.state === 'checking'
                    ? checkpointing
                      ? '请稍候，正在等待当前任务到达可恢复的安全位置。'
                      : '请稍候，正在确认是否可以安全退出。'
                    : '正在关闭本地服务并保存应用状态。'}
                </p>
              )}
            </div>
            <div className="desktop-exit-actions">
              {exitState.state === 'blocked' ||
              exitState.state === 'checkpoint_timed_out' ? (
                <>
                  <button
                    ref={cancelRef}
                    type="button"
                    disabled={actionPending}
                    onClick={() => void cancelExit()}
                  >
                    返回应用
                  </button>
                  <button
                    type="button"
                    disabled={actionPending}
                    onClick={() => {
                      setActionPending(true);
                      void bridge
                        .openDiagnostics()
                        .catch(() => undefined)
                        .finally(() => setActionPending(false));
                    }}
                  >
                    打开诊断
                  </button>
                  <button
                    className="desktop-exit-primary"
                    type="button"
                    disabled={actionPending}
                    onClick={() => void confirmExit()}
                  >
                    {exitState.state === 'checkpoint_timed_out'
                      ? '重试保存检查点'
                      : '保存检查点并退出'}
                  </button>
                </>
              ) : (
                <>
                  <button
                    ref={cancelRef}
                    type="button"
                    disabled={actionPending || isBusy}
                    onClick={() => void cancelExit()}
                  >
                    取消
                  </button>
                  <button
                    className="desktop-exit-primary"
                    type="button"
                    disabled={actionPending || isBusy}
                    onClick={() => void confirmExit()}
                  >
                    退出应用
                  </button>
                </>
              )}
            </div>
          </dialog>
        </div>
      )}
    </>
  );
}

export function DesktopExitGuard({ bridge, children }: DesktopExitGuardProps) {
  if (!bridge.isDesktop) return children;
  return (
    <TauriDesktopExitGuard bridge={bridge}>{children}</TauriDesktopExitGuard>
  );
}
