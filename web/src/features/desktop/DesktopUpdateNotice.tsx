import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from 'react';

import type {
  DesktopBridge,
  DesktopUpdateState,
  TauriDesktopBridge,
} from '../../app/desktopBridge';

function progressLabel(state: DesktopUpdateState): string | null {
  if (state.state === 'downloading') return `正在下载更新 ${state.version}`;
  if (state.state === 'verifying') return `正在验证更新 ${state.version}`;
  if (state.state === 'ready_to_install') return `更新 ${state.version} 已验证`;
  if (state.state === 'installing') return `正在安装更新 ${state.version}`;
  return null;
}

function UpdateConfirmation({
  version,
  pending,
  onCancel,
  onConfirm,
  fallbackFocusRef,
  returnFocusRef,
}: {
  readonly version: string;
  readonly pending: boolean;
  readonly onCancel: () => void;
  readonly onConfirm: () => void;
  readonly fallbackFocusRef: RefObject<HTMLElement | null>;
  readonly returnFocusRef: RefObject<HTMLButtonElement | null>;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useLayoutEffect(() => {
    cancelRef.current?.focus();
    return () => {
      const dialog = dialogRef.current;
      const ownedFocus =
        dialog !== null && dialog.contains(document.activeElement);
      if (!ownedFocus) return;
      window.setTimeout(() => {
        if (document.activeElement === document.body)
          (returnFocusRef.current ?? fallbackFocusRef.current)?.focus();
      }, 0);
    };
  }, [fallbackFocusRef, returnFocusRef]);

  function containFocus(event: ReactKeyboardEvent<HTMLDialogElement>) {
    if (event.key === 'Escape' && !pending) {
      event.preventDefault();
      event.stopPropagation();
      onCancel();
      return;
    }
    if (event.key !== 'Tab') return;
    const controls = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>(
        'button:not([disabled])',
      ),
    );
    const first = controls[0];
    const last = controls.at(-1);
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

  return (
    <div className="desktop-update-dialog-backdrop" role="presentation">
      <dialog
        ref={dialogRef}
        className="desktop-update-dialog"
        open
        aria-modal="true"
        aria-labelledby="desktop-update-confirm-title"
        aria-describedby="desktop-update-confirm-description"
        tabIndex={-1}
        onKeyDown={containFocus}
      >
        <h2 id="desktop-update-confirm-title">确认安装更新</h2>
        <p id="desktop-update-confirm-description">
          Stock Desk 将下载版本 {version}，依次验证 Tauri 更新签名、SHA-256 和
          Windows 可信签名。验证通过后才会保存工作区并安全退出安装。
        </p>
        <p>不会静默强制更新；任何验证失败都会保留当前版本和本地数据。</p>
        <div className="desktop-update-dialog-actions">
          <button
            ref={cancelRef}
            type="button"
            disabled={pending}
            onClick={onCancel}
          >
            暂不安装
          </button>
          <button type="button" disabled={pending} onClick={onConfirm}>
            {pending ? '正在请求…' : '确认下载并安装'}
          </button>
        </div>
      </dialog>
    </div>
  );
}

function TauriDesktopUpdateNotice({
  bridge,
}: {
  readonly bridge: TauriDesktopBridge;
}) {
  const [state, setState] = useState<DesktopUpdateState | null>(null);
  const [confirmationOpen, setConfirmationOpen] = useState(false);
  const [actionPending, setActionPending] = useState(false);
  const installTriggerRef = useRef<HTMLButtonElement>(null);
  const noticeRef = useRef<HTMLElement>(null);

  useEffect(() => {
    let active = true;
    let eventRevision = 0;
    let eventChannelFailed = false;
    let latestSubscribedState: DesktopUpdateState | undefined;
    let unsubscribe: (() => void) | undefined;

    async function initialize() {
      let stop: (() => void) | undefined;
      try {
        stop = await bridge.subscribeUpdate(
          (next) => {
            eventRevision += 1;
            latestSubscribedState = next;
            if (active) setState(next);
          },
          () => {
            eventRevision += 1;
            eventChannelFailed = true;
            if (active) setState(null);
          },
        );
      } catch {
        if (active && eventRevision === 0) setState(null);
        return;
      }
      if (!active) {
        stop();
        return;
      }
      unsubscribe = stop;

      const revisionBeforeInitial = eventRevision;
      let initial: DesktopUpdateState;
      try {
        initial = await bridge.getUpdateState();
      } catch {
        if (
          active &&
          eventRevision === revisionBeforeInitial &&
          latestSubscribedState === undefined &&
          !eventChannelFailed
        )
          setState(null);
        return;
      }
      if (!active || eventRevision !== revisionBeforeInitial) return;
      if (eventChannelFailed) return;
      const resolvedInitial = latestSubscribedState ?? initial;
      setState(resolvedInitial);
      if (resolvedInitial.state !== 'idle') return;

      const revisionBeforeCheck = eventRevision;
      try {
        const checked = await bridge.checkForUpdates();
        if (active && eventRevision === revisionBeforeCheck) setState(checked);
      } catch {
        if (active && eventRevision === revisionBeforeCheck) setState(null);
      }
    }

    void initialize();
    return () => {
      active = false;
      unsubscribe?.();
    };
  }, [bridge]);

  if (
    state === null ||
    state.state === 'disabled' ||
    state.state === 'idle' ||
    state.state === 'checking'
  ) {
    return null;
  }

  const progress = progressLabel(state);

  async function dismiss() {
    if (actionPending) return;
    const currentVersion = state?.currentVersion ?? 'unavailable';
    setActionPending(true);
    try {
      await bridge.dismissUpdate();
      setState({ state: 'idle', currentVersion });
    } catch {
      setState({
        state: 'failed',
        currentVersion,
        code: 'desktop_updater_dismiss_failed',
        canRetry: true,
      });
    } finally {
      setActionPending(false);
    }
  }

  async function confirm() {
    if (actionPending || state?.state !== 'available') return;
    setActionPending(true);
    try {
      await bridge.confirmUpdate();
      setConfirmationOpen(false);
    } catch {
      setConfirmationOpen(false);
      setState({
        state: 'failed',
        currentVersion: state.currentVersion,
        code: 'desktop_updater_request_failed',
        canRetry: true,
      });
    } finally {
      setActionPending(false);
    }
  }

  async function retry() {
    if (actionPending) return;
    const currentVersion = state?.currentVersion ?? 'unavailable';
    setActionPending(true);
    try {
      setState(await bridge.checkForUpdates());
    } catch {
      setState({
        state: 'failed',
        currentVersion,
        code: 'desktop_updater_check_failed',
        canRetry: true,
      });
    } finally {
      setActionPending(false);
    }
  }

  async function openDiagnostics() {
    if (actionPending) return;
    setActionPending(true);
    try {
      await bridge.openDiagnostics();
    } catch {
      // The safe updater failure remains visible and can still be dismissed.
    } finally {
      setActionPending(false);
    }
  }

  return (
    <>
      <aside
        ref={noticeRef}
        className="desktop-update-notice"
        role="status"
        aria-live="polite"
        tabIndex={-1}
      >
        {state.state === 'available' ? (
          <>
            <div>
              <strong>发现可信更新 {state.version}</strong>
              <p>{state.notes ?? '包含安全性与稳定性改进。'}</p>
            </div>
            <div className="desktop-update-actions">
              <button
                type="button"
                disabled={actionPending}
                onClick={() => void dismiss()}
              >
                稍后提醒
              </button>
              <button
                ref={installTriggerRef}
                type="button"
                disabled={actionPending}
                onClick={() => setConfirmationOpen(true)}
              >
                查看并安装
              </button>
            </div>
          </>
        ) : state.state === 'failed' ? (
          <>
            <div>
              <strong>更新未安装</strong>
              <p>
                当前版本 {state.currentVersion} 仍可继续使用，本地数据未改变。
              </p>
            </div>
            <div className="desktop-update-actions">
              <button
                type="button"
                disabled={actionPending}
                onClick={() =>
                  setState({
                    state: 'idle',
                    currentVersion: state.currentVersion,
                  })
                }
              >
                关闭通知
              </button>
              {state.canRetry ? (
                <button
                  type="button"
                  disabled={actionPending}
                  onClick={() => void retry()}
                >
                  重新检查
                </button>
              ) : (
                <button
                  type="button"
                  disabled={actionPending}
                  onClick={() => void openDiagnostics()}
                >
                  打开诊断
                </button>
              )}
            </div>
          </>
        ) : (
          <div>
            <strong>{progress}</strong>
            <p>请继续使用当前窗口；需要退出安装前会再次遵循安全退出流程。</p>
          </div>
        )}
      </aside>
      {confirmationOpen && state.state === 'available' ? (
        <UpdateConfirmation
          version={state.version}
          pending={actionPending}
          onCancel={() => setConfirmationOpen(false)}
          onConfirm={() => void confirm()}
          fallbackFocusRef={noticeRef}
          returnFocusRef={installTriggerRef}
        />
      ) : null}
    </>
  );
}

export function DesktopUpdateNotice({
  bridge,
}: {
  readonly bridge: DesktopBridge;
}) {
  if (!bridge.isDesktop) return null;
  return <TauriDesktopUpdateNotice bridge={bridge} />;
}
