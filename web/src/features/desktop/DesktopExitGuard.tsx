import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';

import type {
  DesktopBridge,
  DesktopExitState,
  TauriDesktopBridge,
} from '../../app/desktopBridge';
import { ModalDialog } from '../../shared/ModalDialog';

type DesktopExitGuardProps = {
  readonly bridge: DesktopBridge;
  readonly children: ReactNode;
};

type ExitAction = {
  readonly id: number;
  readonly kind: 'cancel' | 'confirm' | 'diagnostics';
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
  const cancelRef = useRef<HTMLButtonElement>(null);
  const mountedRef = useRef(true);
  const nextActionIdRef = useRef(0);
  const activeActionRef = useRef<ExitAction | null>(null);

  const ownsAction = useCallback(
    (operation: ExitAction): boolean =>
      mountedRef.current && activeActionRef.current === operation,
    [],
  );

  const beginAction = useCallback(function beginAction(
    kind: ExitAction['kind'],
  ): ExitAction | null {
    if (activeActionRef.current !== null) return null;
    const operation = { id: nextActionIdRef.current + 1, kind };
    nextActionIdRef.current = operation.id;
    activeActionRef.current = operation;
    setActionPending(true);
    return operation;
  }, []);

  const finishAction = useCallback(function finishAction(
    operation: ExitAction,
  ): void {
    if (activeActionRef.current !== operation) return;
    activeActionRef.current = null;
    if (mountedRef.current) setActionPending(false);
  }, []);

  const reconcileActionForState = useCallback(
    function reconcileActionForState(next: DesktopExitState): void {
      const operation = activeActionRef.current;
      if (
        operation !== null &&
        (next.state === 'idle' ||
          next.state === 'shutting_down' ||
          (operation.kind === 'confirm' &&
            (next.state === 'blocked' ||
              next.state === 'checkpoint_timed_out')))
      )
        finishAction(operation);
    },
    [finishAction],
  );

  const finishActiveAction = useCallback(
    function finishActiveAction(): void {
      const operation = activeActionRef.current;
      if (operation !== null) finishAction(operation);
    },
    [finishAction],
  );

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      activeActionRef.current = null;
    };
  }, []);

  useEffect(() => {
    let active = true;
    let unsubscribe: (() => void) | undefined;

    void bridge
      .subscribeExit(
        (next) => {
          if (!active) return;
          reconcileActionForState(next);
          if (next.state !== 'checking') setCheckpointing(false);
          setExitState((current) =>
            isSameExitState(current, next) ? current : next,
          );
        },
        () => {
          if (!active) return;
          finishActiveAction();
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
        if (active) {
          finishActiveAction();
          setExitState(idleExitState);
        }
      });

    return () => {
      active = false;
      unsubscribe?.();
    };
  }, [bridge, finishActiveAction, reconcileActionForState]);

  useEffect(() => {
    if (
      exitState.state === 'confirm' ||
      exitState.state === 'blocked' ||
      exitState.state === 'checkpoint_timed_out'
    ) {
      cancelRef.current?.focus();
    }
  }, [exitState.state]);

  const cancelExit = useCallback(async () => {
    const operation = beginAction('cancel');
    if (operation === null) return;
    const previousState = exitState.state;
    try {
      await bridge.cancelExit();
    } catch {
      if (!ownsAction(operation)) return;
      setExitState((current) =>
        current.state === previousState ? idleExitState : current,
      );
    } finally {
      finishAction(operation);
    }
  }, [beginAction, bridge, exitState.state, finishAction, ownsAction]);

  const confirmExit = useCallback(async () => {
    if (
      exitState.state !== 'confirm' &&
      exitState.state !== 'blocked' &&
      exitState.state !== 'checkpoint_timed_out'
    )
      return;
    const operation = beginAction('confirm');
    if (operation === null) return;
    setCheckpointing(exitState.state !== 'confirm');
    setExitState({ state: 'checking' });
    try {
      await bridge.confirmExit();
    } catch {
      if (!ownsAction(operation)) return;
      setExitState((current) =>
        current.state === 'checking' ? idleExitState : current,
      );
    } finally {
      finishAction(operation);
    }
  }, [beginAction, bridge, exitState.state, finishAction, ownsAction]);

  const openDiagnostics = useCallback(async () => {
    const operation = beginAction('diagnostics');
    if (operation === null) return;
    try {
      await bridge.openDiagnostics();
    } catch {
      // The exit decision stays visible and remains safe to retry.
    } finally {
      finishAction(operation);
    }
  }, [beginAction, bridge, finishAction]);

  const isBusy =
    exitState.state === 'checking' || exitState.state === 'shutting_down';
  const canCancel =
    !actionPending &&
    (exitState.state === 'confirm' ||
      exitState.state === 'blocked' ||
      exitState.state === 'checkpoint_timed_out');

  return (
    <>
      {children}
      {exitState.state === 'idle' ? null : (
        <ModalDialog
          backdropClassName="desktop-exit-backdrop"
          className="desktop-exit-dialog"
          aria-labelledby="desktop-exit-title"
          aria-describedby="desktop-exit-description"
          tabIndex={-1}
          initialFocusRef={cancelRef}
          onEscape={canCancel ? () => void cancelExit() : undefined}
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
                  onClick={() => void openDiagnostics()}
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
        </ModalDialog>
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
