import { StrictMode } from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import {
  createDesktopBridge,
  type DesktopAdapter,
} from '../../app/desktopBridge';
import { DesktopExitGuard } from './DesktopExitGuard';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function adapter(overrides: Partial<DesktopAdapter> = {}): DesktopAdapter {
  return {
    cancelExit: vi.fn(() => Promise.resolve()),
    checkForUpdates: vi.fn(() =>
      Promise.resolve({ state: 'disabled', current_version: '1.1.0' }),
    ),
    confirmExit: vi.fn(() => Promise.resolve()),
    confirmUpdate: vi.fn(() => Promise.resolve()),
    dismissUpdate: vi.fn(() => Promise.resolve()),
    exportDiagnostics: vi.fn(() => Promise.resolve('saved' as const)),
    getRuntimeState: vi.fn(() => Promise.resolve({ state: 'ready' })),
    getUpdateState: vi.fn(() =>
      Promise.resolve({ state: 'disabled', current_version: '1.1.0' }),
    ),
    openDiagnostics: vi.fn(() => Promise.resolve()),
    requestExit: vi.fn(() => Promise.resolve()),
    restartService: vi.fn(() => Promise.resolve()),
    subscribe: vi.fn(() => Promise.resolve(() => undefined)),
    subscribeExit: vi.fn(() => Promise.resolve(() => undefined)),
    subscribeUpdate: vi.fn(() => Promise.resolve(() => undefined)),
    ...overrides,
  };
}

function installExitEmitter(overrides: Partial<DesktopAdapter> = {}) {
  let emit: ((payload: unknown) => void) | undefined;
  const unsubscribe = vi.fn();
  const desktopAdapter = adapter({
    subscribeExit: vi.fn((listener: (payload: unknown) => void) => {
      emit = listener;
      return Promise.resolve(unsubscribe);
    }),
    ...overrides,
  });
  return {
    desktopAdapter,
    emit: (payload: unknown) => emit?.(payload),
    unsubscribe,
  };
}

it('keeps the browser workspace synchronous and never opens an exit dialog', () => {
  render(
    <DesktopExitGuard bridge={createDesktopBridge()}>
      <p>workspace</p>
    </DesktopExitGuard>,
  );

  expect(screen.getByText('workspace')).toBeInTheDocument();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('focuses Cancel by default, traps Tab, and Escape cancels without arguments', async () => {
  const user = userEvent.setup();
  const cancelExit = vi.fn(() => Promise.resolve());
  const fixture = installExitEmitter({ cancelExit });
  render(
    <DesktopExitGuard bridge={createDesktopBridge(fixture.desktopAdapter)}>
      <button type="button">workspace action</button>
    </DesktopExitGuard>,
  );
  await waitFor(() =>
    expect(fixture.desktopAdapter.subscribeExit).toHaveBeenCalledOnce(),
  );

  act(() => fixture.emit({ state: 'confirm' }));
  expect(screen.getByRole('dialog').tagName).toBe('DIALOG');
  const cancel = screen.getByRole('button', { name: '取消' });
  const confirm = screen.getByRole('button', { name: '退出应用' });
  expect(cancel).toHaveFocus();
  await user.tab({ shift: true });
  expect(confirm).toHaveFocus();
  await user.keyboard('{Escape}');

  await waitFor(() => expect(cancelExit).toHaveBeenCalledOnce());
  expect(cancelExit).toHaveBeenCalledWith();
  act(() => fixture.emit({ state: 'idle' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('treats duplicate events idempotently and disables repeated confirmation', async () => {
  const user = userEvent.setup();
  let resolveConfirm: (() => void) | undefined;
  const confirmExit = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        resolveConfirm = resolve;
      }),
  );
  const fixture = installExitEmitter({ confirmExit });
  render(
    <DesktopExitGuard bridge={createDesktopBridge(fixture.desktopAdapter)}>
      <p>workspace</p>
    </DesktopExitGuard>,
  );
  await waitFor(() =>
    expect(fixture.desktopAdapter.subscribeExit).toHaveBeenCalled(),
  );

  act(() => {
    fixture.emit({ state: 'confirm' });
    fixture.emit({ state: 'confirm' });
  });
  const confirm = screen.getByRole('button', { name: '退出应用' });
  await user.dblClick(confirm);

  expect(confirmExit).toHaveBeenCalledOnce();
  expect(screen.getByRole('button', { name: '取消' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '退出应用' })).toBeDisabled();
  expect(screen.getByRole('heading')).toHaveTextContent('正在检查后台任务');
  act(() => fixture.emit({ state: 'shutting_down' }));
  expect(screen.getByRole('button', { name: '退出应用' })).toBeDisabled();
  act(() => resolveConfirm?.());
});

it('keeps a pending cancel locked across duplicate blocked and count events', async () => {
  const user = userEvent.setup();
  const cancellation = deferred<void>();
  const cancelExit = vi.fn(() => cancellation.promise);
  const confirmExit = vi.fn(() => Promise.resolve());
  const fixture = installExitEmitter({ cancelExit, confirmExit });
  render(
    <DesktopExitGuard bridge={createDesktopBridge(fixture.desktopAdapter)}>
      <p>workspace</p>
    </DesktopExitGuard>,
  );
  await waitFor(() =>
    expect(fixture.desktopAdapter.subscribeExit).toHaveBeenCalled(),
  );

  act(() => fixture.emit({ state: 'blocked', queued: 2, running: 1 }));
  await user.click(screen.getByRole('button', { name: '返回应用' }));
  expect(cancelExit).toHaveBeenCalledOnce();

  act(() => fixture.emit({ state: 'blocked', queued: 3, running: 1 }));
  expect(screen.getByText('排队任务').nextSibling).toHaveTextContent('3');
  expect(screen.getByRole('button', { name: '返回应用' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '打开诊断' })).toBeDisabled();
  const confirm = screen.getByRole('button', {
    name: '保存检查点并退出',
  });
  expect(confirm).toBeDisabled();
  await user.click(confirm);
  expect(confirmExit).not.toHaveBeenCalled();

  await act(async () => {
    cancellation.resolve();
    await cancellation.promise;
  });
  await waitFor(() => expect(confirm).toBeEnabled());
});

it('keeps diagnostics owned until it settles despite duplicate exit events', async () => {
  const user = userEvent.setup();
  const diagnostics = deferred<void>();
  const openDiagnostics = vi.fn(() => diagnostics.promise);
  const fixture = installExitEmitter({ openDiagnostics });
  render(
    <DesktopExitGuard bridge={createDesktopBridge(fixture.desktopAdapter)}>
      <p>workspace</p>
    </DesktopExitGuard>,
  );
  await waitFor(() =>
    expect(fixture.desktopAdapter.subscribeExit).toHaveBeenCalled(),
  );

  act(() => fixture.emit({ state: 'blocked', queued: 1, running: 1 }));
  await user.click(screen.getByRole('button', { name: '打开诊断' }));
  act(() => fixture.emit({ state: 'blocked', queued: 1, running: 2 }));

  expect(openDiagnostics).toHaveBeenCalledOnce();
  expect(screen.getByRole('button', { name: '返回应用' })).toBeDisabled();
  expect(
    screen.getByRole('button', { name: '保存检查点并退出' }),
  ).toBeDisabled();

  await act(async () => {
    diagnostics.resolve();
    await diagnostics.promise;
  });
  await waitFor(() =>
    expect(screen.getByRole('button', { name: '返回应用' })).toBeEnabled(),
  );
});

it.each([
  {
    authority: { state: 'blocked' as const, queued: 2, running: 1 },
    heading: '后台任务仍在运行',
  },
  {
    authority: {
      state: 'checkpoint_timed_out' as const,
      queued: 2,
      running: 1,
    },
    heading: '尚未到达安全检查点',
  },
])(
  'lets $authority.state complete confirm without letting its late promise release a newer cancel',
  async ({ authority, heading }) => {
    const user = userEvent.setup();
    const confirmation = deferred<void>();
    const cancellation = deferred<void>();
    const fixture = installExitEmitter({
      cancelExit: vi.fn(() => cancellation.promise),
      confirmExit: vi.fn(() => confirmation.promise),
    });
    render(
      <DesktopExitGuard bridge={createDesktopBridge(fixture.desktopAdapter)}>
        <p>workspace</p>
      </DesktopExitGuard>,
    );
    await waitFor(() =>
      expect(fixture.desktopAdapter.subscribeExit).toHaveBeenCalled(),
    );

    act(() => fixture.emit({ state: 'confirm' }));
    await user.click(screen.getByRole('button', { name: '退出应用' }));
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled();

    act(() => fixture.emit(authority));
    expect(screen.getByRole('heading')).toHaveTextContent(heading);
    const returnToApp = screen.getByRole('button', { name: '返回应用' });
    expect(returnToApp).toBeEnabled();

    await user.click(returnToApp);
    act(() => fixture.emit(authority));
    expect(returnToApp).toBeDisabled();

    await act(async () => {
      confirmation.resolve();
      await confirmation.promise;
    });
    expect(returnToApp).toBeDisabled();

    await act(async () => {
      cancellation.resolve();
      await cancellation.promise;
    });
    await waitFor(() => expect(returnToApp).toBeEnabled());
  },
);

it('shows blocked counts and requires a second explicit checkpoint confirmation', async () => {
  const user = userEvent.setup();
  const confirmExit = vi.fn(() => Promise.resolve());
  const fixture = installExitEmitter({ confirmExit });
  render(
    <DesktopExitGuard bridge={createDesktopBridge(fixture.desktopAdapter)}>
      <p>workspace</p>
    </DesktopExitGuard>,
  );
  await waitFor(() =>
    expect(fixture.desktopAdapter.subscribeExit).toHaveBeenCalled(),
  );
  act(() => fixture.emit({ state: 'blocked', queued: 2, running: 1 }));

  expect(screen.getByText(/最多等待 10 秒/u)).toBeInTheDocument();
  expect(screen.getByText('排队任务').nextSibling).toHaveTextContent('2');
  expect(screen.getByText('运行任务').nextSibling).toHaveTextContent('1');
  expect(screen.getByRole('button', { name: '返回应用' })).toHaveFocus();
  expect(screen.getByRole('button', { name: '打开诊断' })).toBeEnabled();
  await user.click(screen.getByRole('button', { name: '保存检查点并退出' }));
  expect(confirmExit).toHaveBeenCalledOnce();
  expect(screen.getByRole('heading')).toHaveTextContent('正在保存安全检查点');
});

it('keeps the app open after checkpoint timeout and offers an explicit retry', async () => {
  const user = userEvent.setup();
  const confirmExit = vi.fn(() => Promise.resolve());
  const fixture = installExitEmitter({ confirmExit });
  render(
    <DesktopExitGuard bridge={createDesktopBridge(fixture.desktopAdapter)}>
      <p>workspace</p>
    </DesktopExitGuard>,
  );
  await waitFor(() =>
    expect(fixture.desktopAdapter.subscribeExit).toHaveBeenCalled(),
  );
  act(() =>
    fixture.emit({ state: 'checkpoint_timed_out', queued: 1, running: 1 }),
  );

  expect(screen.getByRole('heading')).toHaveTextContent('尚未到达安全检查点');
  expect(screen.getByText(/应用仍保持运行/u)).toBeVisible();
  expect(screen.getByRole('button', { name: '返回应用' })).toHaveFocus();
  await user.click(screen.getByRole('button', { name: '重试保存检查点' }));
  expect(confirmExit).toHaveBeenCalledOnce();
  expect(screen.getByRole('heading')).toHaveTextContent('正在保存安全检查点');
});

it('fails closed to no exit for expanded payloads without leaking values', async () => {
  const fixture = installExitEmitter();
  render(
    <DesktopExitGuard bridge={createDesktopBridge(fixture.desktopAdapter)}>
      <p>workspace</p>
    </DesktopExitGuard>,
  );
  await waitFor(() =>
    expect(fixture.desktopAdapter.subscribeExit).toHaveBeenCalled(),
  );
  act(() =>
    fixture.emit({
      state: 'confirm',
      token: 'must-not-cross-the-bridge',
      url: 'http://127.0.0.1:49152',
    }),
  );

  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(document.body).not.toHaveTextContent(/must-not-cross|127\.0\.0\.1/u);
  expect(fixture.desktopAdapter.confirmExit).not.toHaveBeenCalled();
});

it('cleans a late subscription in StrictMode without handling stale events', async () => {
  const fixture = installExitEmitter();
  const view = render(
    <StrictMode>
      <DesktopExitGuard bridge={createDesktopBridge(fixture.desktopAdapter)}>
        <p>workspace</p>
      </DesktopExitGuard>
    </StrictMode>,
  );
  await waitFor(() =>
    expect(fixture.desktopAdapter.subscribeExit).toHaveBeenCalledTimes(2),
  );
  view.unmount();
  await waitFor(() => expect(fixture.unsubscribe).toHaveBeenCalledTimes(2));
  act(() => fixture.emit({ state: 'confirm' }));
  expect(screen.queryByRole('dialog')).toBeNull();
});
