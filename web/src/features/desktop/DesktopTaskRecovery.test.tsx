import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import {
  createDesktopBridge,
  type DesktopAdapter,
} from '../../app/desktopBridge';
import { DesktopTaskRecovery } from './DesktopTaskRecovery';

function adapter(): DesktopAdapter {
  return {
    cancelExit: vi.fn(() => Promise.resolve()),
    confirmExit: vi.fn(() => Promise.resolve()),
    exportDiagnostics: vi.fn(() => Promise.resolve('saved' as const)),
    getRuntimeState: vi.fn(() => Promise.resolve({ state: 'ready' })),
    openDiagnostics: vi.fn(() => Promise.resolve()),
    requestExit: vi.fn(() => Promise.resolve()),
    restartService: vi.fn(() => Promise.resolve()),
    subscribe: vi.fn(() => Promise.resolve(() => undefined)),
    subscribeExit: vi.fn(() => Promise.resolve(() => undefined)),
  };
}

const recovery = (
  overrides: Partial<
    Record<
      'queued' | 'running' | 'analysis' | 'backtest' | 'market' | 'other',
      number
    >
  > = {},
) => ({
  required: true,
  queued: 1,
  running: 1,
  analysis: 0,
  backtest: 1,
  market: 1,
  other: 0,
  ...overrides,
});

it('defaults to cancel and offers explicit resume for incomplete work', async () => {
  const user = userEvent.setup();
  const post = vi.fn(() => Promise.resolve({ status: 'resumed', queued: 2 }));
  render(
    <DesktopTaskRecovery
      bridge={createDesktopBridge(adapter())}
      api={{ get: vi.fn(() => Promise.resolve(recovery())), post }}
    >
      <p>workspace</p>
    </DesktopTaskRecovery>,
  );

  expect(await screen.findByRole('dialog')).toBeVisible();
  expect(screen.getByRole('button', { name: '取消未完成任务' })).toHaveFocus();
  expect(screen.getByText('排队任务').nextSibling).toHaveTextContent('1');
  expect(screen.getByText('运行任务').nextSibling).toHaveTextContent('1');
  expect(screen.queryByText('workspace')).toBeNull();
  await user.click(screen.getByRole('button', { name: '继续未完成任务' }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith('/desktop/recovery/resume', {
      body: { confirm_analysis_cost: false },
    }),
  );
  expect(await screen.findByText('workspace')).toBeVisible();
});

it('can explicitly cancel incomplete work', async () => {
  const user = userEvent.setup();
  const post = vi.fn(() =>
    Promise.resolve({ status: 'cancelled', cancelled: 2 }),
  );
  render(
    <DesktopTaskRecovery
      bridge={createDesktopBridge(adapter())}
      api={{
        get: vi.fn(() => Promise.resolve(recovery({ queued: 2, running: 0 }))),
        post,
      }}
    >
      <p>workspace</p>
    </DesktopTaskRecovery>,
  );
  await user.click(
    await screen.findByRole('button', { name: '取消未完成任务' }),
  );
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith('/desktop/recovery/cancel'),
  );
  expect(await screen.findByText('workspace')).toBeVisible();
});

it('requires an extra model-cost confirmation before resuming analysis', async () => {
  const user = userEvent.setup();
  const post = vi.fn(() => Promise.resolve({ status: 'resumed', queued: 1 }));
  render(
    <DesktopTaskRecovery
      bridge={createDesktopBridge(adapter())}
      api={{
        get: vi.fn(() =>
          Promise.resolve(
            recovery({ analysis: 1, backtest: 0, market: 1, other: 0 }),
          ),
        ),
        post,
      }}
    >
      <p>workspace</p>
    </DesktopTaskRecovery>,
  );

  await user.click(
    await screen.findByRole('button', { name: '继续未完成任务' }),
  );
  expect(post).not.toHaveBeenCalled();
  expect(screen.getByText(/模型 API 并产生费用/u)).toBeVisible();
  await user.click(screen.getByRole('button', { name: '确认继续并产生费用' }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith('/desktop/recovery/resume', {
      body: { confirm_analysis_cost: true },
    }),
  );
});

it('keeps recovery failures actionable without discarding local work', async () => {
  const user = userEvent.setup();
  const desktopAdapter = adapter();
  const get = vi
    .fn()
    .mockRejectedValueOnce(new Error('private storage detail'))
    .mockResolvedValueOnce({
      required: false,
      queued: 0,
      running: 0,
      analysis: 0,
      backtest: 0,
      market: 0,
      other: 0,
    });
  render(
    <DesktopTaskRecovery
      bridge={createDesktopBridge(desktopAdapter)}
      api={{ get, post: vi.fn() }}
    >
      <p>workspace</p>
    </DesktopTaskRecovery>,
  );

  expect(await screen.findByText('无法检查未完成任务')).toBeVisible();
  expect(screen.queryByText('private storage detail')).toBeNull();
  await user.click(screen.getByRole('button', { name: '打开诊断' }));
  expect(desktopAdapter.openDiagnostics).toHaveBeenCalledOnce();
  await user.click(screen.getByRole('button', { name: '安全退出' }));
  expect(desktopAdapter.requestExit).toHaveBeenCalledOnce();
  await user.click(screen.getByRole('button', { name: '重启服务并重试' }));
  expect(desktopAdapter.restartService).toHaveBeenCalledOnce();
  expect(await screen.findByText('workspace')).toBeVisible();
  expect(get).toHaveBeenCalledTimes(2);
});
