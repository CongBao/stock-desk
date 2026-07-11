import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import {
  createDesktopBridge,
  type DesktopAdapter,
} from '../../app/desktopBridge';
import { DesktopStartup } from './DesktopStartup';

function adapter(overrides: Partial<DesktopAdapter> = {}): DesktopAdapter {
  return {
    cancelExit: vi.fn(() => Promise.resolve()),
    confirmExit: vi.fn(() => Promise.resolve()),
    getRuntimeState: vi.fn(() => Promise.resolve({ state: 'starting' })),
    openDiagnostics: vi.fn(() => Promise.resolve()),
    requestExit: vi.fn(() => Promise.resolve()),
    restartService: vi.fn(() => Promise.resolve()),
    subscribe: vi.fn(() => Promise.resolve(() => undefined)),
    subscribeExit: vi.fn(() => Promise.resolve(() => undefined)),
    ...overrides,
  };
}

it('renders browser children synchronously without a startup flash', () => {
  render(
    <DesktopStartup bridge={createDesktopBridge()}>
      <p>workspace ready</p>
    </DesktopStartup>,
  );

  expect(screen.getByText('workspace ready')).toBeInTheDocument();
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

it('does not mount workspace children until desktop runtime is ready', async () => {
  let emit: ((payload: unknown) => void) | undefined;
  const desktopAdapter = adapter({
    subscribe: vi.fn((listener: (payload: unknown) => void) => {
      emit = listener;
      return Promise.resolve(() => undefined);
    }),
  });
  render(
    <DesktopStartup bridge={createDesktopBridge(desktopAdapter)}>
      <p>workspace ready</p>
    </DesktopStartup>,
  );

  expect(screen.queryByText('workspace ready')).not.toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('正在启动桌面服务');
  await waitFor(() => expect(desktopAdapter.subscribe).toHaveBeenCalledOnce());
  act(() => emit?.({ state: 'ready' }));
  expect(await screen.findByText('workspace ready')).toBeInTheDocument();
});

it('does not overwrite a newer subscribed state with a stale initial response', async () => {
  let resolveInitial: ((state: unknown) => void) | undefined;
  const desktopAdapter = adapter({
    getRuntimeState: vi.fn(
      () =>
        new Promise((resolve) => {
          resolveInitial = resolve;
        }),
    ),
    subscribe: vi.fn((listener: (payload: unknown) => void) => {
      listener({ state: 'ready' });
      return Promise.resolve(() => undefined);
    }),
  });
  render(
    <DesktopStartup bridge={createDesktopBridge(desktopAdapter)}>
      <p>workspace ready</p>
    </DesktopStartup>,
  );

  await waitFor(() => expect(resolveInitial).toBeDefined());
  act(() => resolveInitial?.({ state: 'starting' }));

  expect(await screen.findByText('workspace ready')).toBeInTheDocument();
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

it('fails closed to recovery when the desktop payload is invalid', async () => {
  render(
    <DesktopStartup
      bridge={createDesktopBridge(
        adapter({
          getRuntimeState: vi.fn(() =>
            Promise.resolve({ state: 'ready', token: 'private' }),
          ),
        }),
      )}
    >
      <p>workspace ready</p>
    </DesktopStartup>,
  );

  expect(
    await screen.findByRole('heading', { name: '桌面服务需要恢复' }),
  ).toBeInTheDocument();
  expect(screen.queryByText('workspace ready')).not.toBeInTheDocument();
  expect(document.body).not.toHaveTextContent('private');
});

it('leaves a mounted workspace when a subscribed state becomes invalid', async () => {
  let emit: ((payload: unknown) => void) | undefined;
  render(
    <DesktopStartup
      bridge={createDesktopBridge(
        adapter({
          getRuntimeState: vi.fn(() => Promise.resolve({ state: 'ready' })),
          subscribe: vi.fn((listener: (payload: unknown) => void) => {
            emit = listener;
            return Promise.resolve(() => undefined);
          }),
        }),
      )}
    >
      <p>workspace ready</p>
    </DesktopStartup>,
  );

  expect(await screen.findByText('workspace ready')).toBeInTheDocument();
  act(() => emit?.({ state: 'ready', port: 49152 }));
  expect(
    await screen.findByRole('heading', { name: '桌面服务需要恢复' }),
  ).toBeInTheDocument();
  expect(screen.queryByText('workspace ready')).not.toBeInTheDocument();
});

it('returns to starting after one explicit successful restart', async () => {
  const user = userEvent.setup();
  const restartService = vi.fn(() => Promise.resolve());
  render(
    <DesktopStartup
      bridge={createDesktopBridge(
        adapter({
          getRuntimeState: vi.fn(() =>
            Promise.resolve({
              state: 'recovery',
              reason: 'sidecar_unavailable',
              can_restart: true,
            }),
          ),
          restartService,
        }),
      )}
    >
      <p>workspace ready</p>
    </DesktopStartup>,
  );

  await user.click(
    await screen.findByRole('button', { name: 'Restart Service' }),
  );
  expect(restartService).toHaveBeenCalledOnce();
  expect(screen.getByRole('status')).toHaveTextContent('正在启动桌面服务');
  expect(screen.queryByText('workspace ready')).not.toBeInTheDocument();
});

it('does not overwrite a ready event that arrives before restart resolves', async () => {
  const user = userEvent.setup();
  let emit: ((payload: unknown) => void) | undefined;
  let resolveRestart: (() => void) | undefined;
  const restartService = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        resolveRestart = resolve;
      }),
  );
  render(
    <DesktopStartup
      bridge={createDesktopBridge(
        adapter({
          getRuntimeState: vi.fn(() =>
            Promise.resolve({
              state: 'recovery',
              reason: 'sidecar_unavailable',
              can_restart: true,
            }),
          ),
          restartService,
          subscribe: vi.fn((listener: (payload: unknown) => void) => {
            emit = listener;
            return Promise.resolve(() => undefined);
          }),
        }),
      )}
    >
      <p>workspace ready</p>
    </DesktopStartup>,
  );

  await user.click(
    await screen.findByRole('button', { name: 'Restart Service' }),
  );
  act(() => emit?.({ state: 'ready' }));
  expect(await screen.findByText('workspace ready')).toBeInTheDocument();
  act(() => resolveRestart?.());
  expect(screen.getByText('workspace ready')).toBeInTheDocument();
});
