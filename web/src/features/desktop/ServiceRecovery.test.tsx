import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { TauriDesktopBridge } from '../../app/desktopBridge';
import { ServiceRecovery } from './ServiceRecovery';

function bridge(
  overrides: Partial<TauriDesktopBridge> = {},
): TauriDesktopBridge {
  return {
    cancelExit: vi.fn(() => Promise.resolve()),
    confirmExit: vi.fn(() => Promise.resolve()),
    exportDiagnostics: vi.fn(() => Promise.resolve('saved' as const)),
    isDesktop: true,
    getRuntimeState: vi.fn(() => Promise.resolve({ state: 'ready' } as const)),
    openDiagnostics: vi.fn(() => Promise.resolve()),
    requestExit: vi.fn(() => Promise.resolve()),
    restartService: vi.fn(() => Promise.resolve()),
    subscribe: vi.fn(() => Promise.resolve(() => undefined)),
    subscribeExit: vi.fn(() => Promise.resolve(() => undefined)),
    ...overrides,
  };
}

it('offers the three safe recovery actions without technical details', () => {
  render(
    <ServiceRecovery
      bridge={bridge()}
      reason="sidecar_unavailable"
      canRestart
      onRestarting={() => Promise.resolve()}
    />,
  );

  expect(screen.getByRole('button', { name: '重启服务' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '打开诊断' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '安全退出' })).toBeEnabled();
  expect(document.body).not.toHaveTextContent(
    /traceback|https?:|127\.0\.0\.1/u,
  );
  expect(screen.getByText(/仅保存到本机，不会自动上传/u)).toBeVisible();
});

it('keeps diagnostic export keyboard reachable', async () => {
  const user = userEvent.setup();
  const openDiagnostics = vi.fn(() => Promise.resolve());
  render(
    <ServiceRecovery
      bridge={bridge({ openDiagnostics })}
      reason="sidecar_unavailable"
      canRestart
      onRestarting={() => Promise.resolve()}
    />,
  );

  await user.tab();
  expect(screen.getByRole('button', { name: '重启服务' })).toHaveFocus();
  await user.tab();
  expect(screen.getByRole('button', { name: '打开诊断' })).toHaveFocus();
  await user.keyboard('{Enter}');
  expect(openDiagnostics).toHaveBeenCalledOnce();
});

it('does not automatically loop after restart fails or expose the exception', async () => {
  const user = userEvent.setup();
  const onRestarting = vi.fn(() =>
    Promise.reject(
      new Error('Traceback at C:\\' + 'Users\\private\\token.txt'),
    ),
  );
  render(
    <ServiceRecovery
      bridge={bridge()}
      reason="startup_timeout"
      canRestart
      onRestarting={onRestarting}
    />,
  );

  await user.click(screen.getByRole('button', { name: '重启服务' }));

  expect(onRestarting).toHaveBeenCalledOnce();
  expect(await screen.findByRole('status')).toHaveTextContent(
    '服务暂时无法重启，请稍后重试或选择其他操作。',
  );
  expect(document.body).not.toHaveTextContent(/Traceback|Users|token\.txt/u);
});

it('delegates diagnostics and safe exit without arguments', async () => {
  const user = userEvent.setup();
  const openDiagnostics = vi.fn(() => Promise.resolve());
  const requestExit = vi.fn(() => Promise.resolve());
  render(
    <ServiceRecovery
      bridge={bridge({ openDiagnostics, requestExit })}
      reason="version_mismatch"
      canRestart={false}
      onRestarting={() => Promise.resolve()}
    />,
  );

  expect(
    screen.queryByRole('button', { name: '重启服务' }),
  ).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '打开诊断' }));
  await user.click(screen.getByRole('button', { name: '安全退出' }));
  expect(openDiagnostics).toHaveBeenCalledWith();
  expect(requestExit).toHaveBeenCalledWith();
});

it('explains the bounded restart limit and keeps only diagnostics and safe exit', () => {
  render(
    <ServiceRecovery
      bridge={bridge()}
      reason="restart_limit_reached"
      canRestart={false}
      onRestarting={() => Promise.resolve()}
    />,
  );

  expect(screen.getByText(/已达到安全重启上限/u)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '重启服务' })).toBeNull();
  expect(screen.getByRole('button', { name: '打开诊断' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '安全退出' })).toBeEnabled();
});
