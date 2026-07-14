import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import {
  createDesktopBridge,
  type DesktopAdapter,
} from '../../app/desktopBridge';
import { DesktopUpdateNotice } from './DesktopUpdateNotice';

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
      Promise.resolve({ state: 'idle', current_version: '1.1.0' }),
    ),
    confirmExit: vi.fn(() => Promise.resolve()),
    confirmUpdate: vi.fn(() => Promise.resolve()),
    dismissUpdate: vi.fn(() => Promise.resolve()),
    exportDiagnostics: vi.fn(() => Promise.resolve('saved' as const)),
    getRuntimeState: vi.fn(() => Promise.resolve({ state: 'ready' })),
    getUpdateState: vi.fn(() =>
      Promise.resolve({ state: 'idle', current_version: '1.1.0' }),
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

it('renders nothing in the browser and performs no update check', () => {
  render(<DesktopUpdateNotice bridge={createDesktopBridge()} />);

  expect(screen.queryByText(/更新/u)).not.toBeInTheDocument();
});

it('shows an ignorable non-blocking notice and requires explicit confirmation', async () => {
  const user = userEvent.setup();
  const confirmUpdate = vi.fn(() => Promise.resolve());
  const dismissUpdate = vi.fn(() => Promise.resolve());
  let emit: ((payload: unknown) => void) | undefined;
  const desktopAdapter = adapter({
    confirmUpdate,
    dismissUpdate,
    getUpdateState: vi.fn(() =>
      Promise.resolve({ state: 'idle', current_version: '1.1.0' }),
    ),
    checkForUpdates: vi.fn(() =>
      Promise.resolve({
        state: 'available',
        current_version: '1.1.0',
        version: '1.2.0',
        notes: '安全与稳定性更新',
      }),
    ),
    subscribeUpdate: vi.fn((listener: (payload: unknown) => void) => {
      emit = listener;
      return Promise.resolve(() => undefined);
    }),
  });
  render(<DesktopUpdateNotice bridge={createDesktopBridge(desktopAdapter)} />);

  const notice = await screen.findByRole('status');
  expect(notice).toHaveTextContent('发现可信更新 1.2.0');
  expect(notice).toHaveTextContent('安全与稳定性更新');
  expect(confirmUpdate).not.toHaveBeenCalled();

  const installTrigger = screen.getByRole('button', { name: '查看并安装' });
  await user.click(installTrigger);
  const dialog = screen.getByRole('dialog', { name: '确认安装更新' });
  expect(dialog.tagName).toBe('DIALOG');
  expect(dialog).toHaveTextContent('不会静默强制更新');
  const cancel = screen.getByRole('button', { name: '暂不安装' });
  const confirm = screen.getByRole('button', { name: '确认下载并安装' });
  expect(dialog).toHaveAttribute('aria-modal', 'true');
  expect(cancel).toHaveFocus();
  await user.tab({ shift: true });
  expect(confirm).toHaveFocus();
  await user.tab();
  expect(cancel).toHaveFocus();
  expect(confirmUpdate).not.toHaveBeenCalled();

  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('status')).toBeVisible();
  await waitFor(() => expect(installTrigger).toHaveFocus());

  await user.click(screen.getByRole('button', { name: '查看并安装' }));
  await user.click(screen.getByRole('button', { name: '确认下载并安装' }));
  expect(confirmUpdate).toHaveBeenCalledOnce();

  act(() =>
    emit?.({
      state: 'downloading',
      current_version: '1.1.0',
      version: '1.2.0',
    }),
  );
  expect(screen.getByRole('status')).toHaveTextContent('正在下载更新 1.2.0');

  act(() =>
    emit?.({
      state: 'available',
      current_version: '1.1.0',
      version: '1.2.0',
      notes: null,
    }),
  );
  await user.click(screen.getByRole('button', { name: '稍后提醒' }));
  await waitFor(() => expect(dismissUpdate).toHaveBeenCalledOnce());
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

it('retries a previously verified install without checking or downloading again', async () => {
  const user = userEvent.setup();
  const confirmUpdate = vi.fn(() => Promise.resolve());
  const checkForUpdates = vi.fn(() =>
    Promise.resolve({ state: 'idle', current_version: '1.1.0' }),
  );
  const desktopAdapter = adapter({
    checkForUpdates,
    confirmUpdate,
    getUpdateState: vi.fn(() =>
      Promise.resolve({
        state: 'ready_to_install',
        current_version: '1.1.0',
        version: '1.2.0',
      }),
    ),
  });
  render(<DesktopUpdateNotice bridge={createDesktopBridge(desktopAdapter)} />);

  expect(await screen.findByRole('status')).toHaveTextContent(
    '更新 1.2.0 已验证',
  );
  await user.click(screen.getByRole('button', { name: '重新尝试安装' }));

  await waitFor(() => expect(confirmUpdate).toHaveBeenCalledOnce());
  expect(checkForUpdates).not.toHaveBeenCalled();
});

it('keeps a host-retained verified installer retryable after a failed handoff', async () => {
  const user = userEvent.setup();
  const ready = {
    state: 'ready_to_install',
    current_version: '1.1.0',
    version: '1.2.0',
  } as const;
  const getUpdateState = vi
    .fn()
    .mockResolvedValueOnce(ready)
    .mockResolvedValueOnce(ready);
  const desktopAdapter = adapter({
    confirmUpdate: vi.fn(() => Promise.reject(new Error('handoff failed'))),
    getUpdateState,
  });
  render(<DesktopUpdateNotice bridge={createDesktopBridge(desktopAdapter)} />);

  await user.click(
    await screen.findByRole('button', { name: '重新尝试安装' }),
  );

  await waitFor(() => expect(getUpdateState).toHaveBeenCalledTimes(2));
  expect(
    screen.getByRole('button', { name: '重新尝试安装' }),
  ).toBeEnabled();
  expect(screen.getByRole('status')).toHaveTextContent('更新 1.2.0 已验证');
});

it('does not let a stale initial state replace a newer subscribed event', async () => {
  const initial = deferred<unknown>();
  const checkForUpdates = vi.fn(() =>
    Promise.resolve({ state: 'idle', current_version: '1.1.0' }),
  );
  let emit: ((payload: unknown) => void) | undefined;
  const desktopAdapter = adapter({
    checkForUpdates,
    getUpdateState: vi.fn(() => initial.promise),
    subscribeUpdate: vi.fn((listener: (payload: unknown) => void) => {
      emit = listener;
      return Promise.resolve(() => undefined);
    }),
  });
  render(<DesktopUpdateNotice bridge={createDesktopBridge(desktopAdapter)} />);
  await waitFor(() =>
    expect(desktopAdapter.getUpdateState).toHaveBeenCalledOnce(),
  );

  act(() =>
    emit?.({
      state: 'downloading',
      current_version: '1.1.0',
      version: '1.2.0',
    }),
  );
  await act(async () => {
    initial.resolve({ state: 'idle', current_version: '1.1.0' });
    await initial.promise;
  });

  expect(screen.getByRole('status')).toHaveTextContent('正在下载更新 1.2.0');
  expect(checkForUpdates).not.toHaveBeenCalled();
});

it('keeps an event delivered before the subscription promise resolves', async () => {
  const checkForUpdates = vi.fn(() =>
    Promise.resolve({ state: 'idle', current_version: '1.1.0' }),
  );
  const desktopAdapter = adapter({
    checkForUpdates,
    getUpdateState: vi.fn(() =>
      Promise.resolve({ state: 'idle', current_version: '1.1.0' }),
    ),
    subscribeUpdate: vi.fn((listener: (payload: unknown) => void) => {
      listener({
        state: 'downloading',
        current_version: '1.1.0',
        version: '1.2.0',
      });
      return Promise.resolve(() => undefined);
    }),
  });
  render(<DesktopUpdateNotice bridge={createDesktopBridge(desktopAdapter)} />);

  expect(await screen.findByRole('status')).toHaveTextContent(
    '正在下载更新 1.2.0',
  );
  expect(checkForUpdates).not.toHaveBeenCalled();
});

it('keeps a valid event when the subscription promise later rejects', async () => {
  const desktopAdapter = adapter({
    subscribeUpdate: vi.fn((listener: (payload: unknown) => void) => {
      listener({
        state: 'downloading',
        current_version: '1.1.0',
        version: '1.2.0',
      });
      return Promise.reject(new Error('subscription acknowledgement failed'));
    }),
  });
  render(<DesktopUpdateNotice bridge={createDesktopBridge(desktopAdapter)} />);

  expect(await screen.findByRole('status')).toHaveTextContent(
    '正在下载更新 1.2.0',
  );
  expect(desktopAdapter.getUpdateState).not.toHaveBeenCalled();
});

it('keeps a newer progress event when the older check request fails', async () => {
  const checked = deferred<unknown>();
  let emit: ((payload: unknown) => void) | undefined;
  const desktopAdapter = adapter({
    checkForUpdates: vi.fn(() => checked.promise),
    subscribeUpdate: vi.fn((listener: (payload: unknown) => void) => {
      emit = listener;
      return Promise.resolve(() => undefined);
    }),
  });
  render(<DesktopUpdateNotice bridge={createDesktopBridge(desktopAdapter)} />);
  await waitFor(() =>
    expect(desktopAdapter.checkForUpdates).toHaveBeenCalledOnce(),
  );

  act(() =>
    emit?.({
      state: 'verifying',
      current_version: '1.1.0',
      version: '1.2.0',
    }),
  );
  await act(async () => {
    checked.reject(new Error('older check failed'));
    await Promise.resolve();
  });

  expect(screen.getByRole('status')).toHaveTextContent('正在验证更新 1.2.0');
});

it('scopes Escape to the active update dialog when another modal is on top', async () => {
  const user = userEvent.setup();
  const confirmation = deferred<void>();
  const desktopAdapter = adapter({
    checkForUpdates: vi.fn(() =>
      Promise.resolve({
        state: 'available',
        current_version: '1.1.0',
        version: '1.2.0',
        notes: null,
      }),
    ),
    confirmUpdate: vi.fn(() => confirmation.promise),
  });
  render(
    <>
      <DesktopUpdateNotice bridge={createDesktopBridge(desktopAdapter)} />
      <dialog open aria-modal="true" aria-label="退出确认">
        <button type="button">取消退出</button>
      </dialog>
    </>,
  );

  await user.click(await screen.findByRole('button', { name: '查看并安装' }));
  screen.getByRole('button', { name: '取消退出' }).focus();
  await user.keyboard('{Escape}');

  expect(
    screen.getByRole('dialog', { name: '确认安装更新' }),
  ).toBeInTheDocument();
  const exitCancel = screen.getByRole('button', { name: '取消退出' });
  expect(exitCancel).toHaveFocus();

  await user.click(screen.getByRole('button', { name: '确认下载并安装' }));
  exitCancel.focus();
  await act(async () => {
    confirmation.resolve();
    await confirmation.promise;
  });
  await waitFor(() =>
    expect(
      screen.queryByRole('dialog', { name: '确认安装更新' }),
    ).not.toBeInTheDocument(),
  );
  expect(exitCancel).toHaveFocus();
});

it('moves focus to progress when an event removes the trigger during confirmation', async () => {
  const user = userEvent.setup();
  const confirmation = deferred<void>();
  let emit: ((payload: unknown) => void) | undefined;
  const desktopAdapter = adapter({
    checkForUpdates: vi.fn(() =>
      Promise.resolve({
        state: 'available',
        current_version: '1.1.0',
        version: '1.2.0',
        notes: null,
      }),
    ),
    confirmUpdate: vi.fn(() => confirmation.promise),
    subscribeUpdate: vi.fn((listener: (payload: unknown) => void) => {
      emit = listener;
      return Promise.resolve(() => undefined);
    }),
  });
  render(<DesktopUpdateNotice bridge={createDesktopBridge(desktopAdapter)} />);

  await user.click(await screen.findByRole('button', { name: '查看并安装' }));
  await user.click(screen.getByRole('button', { name: '确认下载并安装' }));
  act(() =>
    emit?.({
      state: 'downloading',
      current_version: '1.1.0',
      version: '1.2.0',
    }),
  );

  const progress = screen.getByRole('status');
  expect(progress).toHaveTextContent('正在下载更新 1.2.0');
  await waitFor(() => expect(progress).toHaveFocus());
  await act(async () => {
    confirmation.resolve();
    await confirmation.promise;
  });
});

it('keeps the current version visible after a verification failure', async () => {
  const desktopAdapter = adapter({
    checkForUpdates: vi.fn(() =>
      Promise.resolve({
        state: 'failed',
        current_version: '1.1.0',
        code: 'desktop_updater_signature_invalid',
        can_retry: true,
      }),
    ),
  });
  render(<DesktopUpdateNotice bridge={createDesktopBridge(desktopAdapter)} />);

  const notice = await screen.findByRole('status');
  expect(notice).toHaveTextContent('更新未安装');
  expect(notice).toHaveTextContent('当前版本 1.1.0 仍可继续使用');
  expect(notice).not.toHaveTextContent('desktop_updater_signature_invalid');
});

it('always dismisses failures and opens diagnostics for non-retryable errors', async () => {
  const user = userEvent.setup();
  const openDiagnostics = vi.fn(() => Promise.resolve());
  const desktopAdapter = adapter({
    checkForUpdates: vi.fn(() =>
      Promise.resolve({
        state: 'failed',
        current_version: '1.1.0',
        code: 'desktop_updater_trust_not_activated',
        can_retry: false,
      }),
    ),
    openDiagnostics,
  });
  render(<DesktopUpdateNotice bridge={createDesktopBridge(desktopAdapter)} />);

  await user.click(await screen.findByRole('button', { name: '打开诊断' }));
  expect(openDiagnostics).toHaveBeenCalledOnce();
  await user.click(screen.getByRole('button', { name: '关闭通知' }));
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

it('fails closed and hides expanded update protocol payloads', async () => {
  const desktopAdapter = adapter({
    checkForUpdates: vi.fn(() =>
      Promise.resolve({
        state: 'available',
        current_version: '1.1.0',
        version: '1.2.0',
        notes: null,
        device_id: 'must-not-cross',
      }),
    ),
  });
  render(<DesktopUpdateNotice bridge={createDesktopBridge(desktopAdapter)} />);

  await waitFor(() =>
    expect(desktopAdapter.checkForUpdates).toHaveBeenCalled(),
  );
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
  expect(document.body).not.toHaveTextContent('must-not-cross');
});

it('turns dismiss and retry promise failures into actionable safe states', async () => {
  const user = userEvent.setup();
  const dismissUpdate = vi.fn(() =>
    Promise.reject(new Error('host unavailable')),
  );
  const checkForUpdates = vi
    .fn()
    .mockResolvedValueOnce({
      state: 'available',
      current_version: '1.1.0-beta.2',
      version: '1.2.0',
      notes: null,
    })
    .mockRejectedValueOnce(new Error('network unavailable'));
  const desktopAdapter = adapter({ checkForUpdates, dismissUpdate });
  render(<DesktopUpdateNotice bridge={createDesktopBridge(desktopAdapter)} />);

  await user.click(await screen.findByRole('button', { name: '稍后提醒' }));
  const failed = await screen.findByRole('status');
  expect(failed).toHaveTextContent('更新未安装');
  expect(failed).toHaveTextContent('1.1.0-beta.2');

  await user.click(screen.getByRole('button', { name: '重新检查' }));
  expect(await screen.findByRole('status')).toHaveTextContent('更新未安装');
  expect(screen.getByRole('button', { name: '重新检查' })).toBeEnabled();
});
