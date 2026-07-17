import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';

import { ApiError } from '../shared/api/client';
import {
  resetMarketStore,
  useMarketStore,
} from '../features/market/marketStore';
import { WorkspacePersistenceGate } from './WorkspacePersistenceGate';
import type {
  WorkspaceApi,
  WorkspaceState,
  WorkspaceValue,
} from './workspaceApi';

const restoredWorkspace: WorkspaceValue = {
  currentPage: '/formulas',
  instrument: {
    symbol: '600000.SH',
    name: '浦发银行',
    exchange: 'SH',
    instrumentKind: 'stock',
  },
  period: '1w',
  adjustment: 'hfq',
  zoom: { start: 20, end: 80 },
  mainChart: 'candlestick',
  subchart: { kind: 'volume' },
};

function state(overrides: Partial<WorkspaceState> = {}): WorkspaceState {
  return {
    schemaVersion: 1,
    revision: 4,
    updatedAt: '2026-07-12T06:00:00Z',
    expiresAt: '2027-01-08T06:00:00Z',
    restored: true,
    notice: null,
    workspace: restoredWorkspace,
    ...overrides,
  };
}

function Probe() {
  const location = useLocation();
  const market = useMarketStore((value) => value);
  return (
    <div>
      <span>
        route:{location.pathname}
        {location.search}
      </span>
      <span>
        instrument:{market.selectedInstrument?.name}:
        {market.selectedInstrument?.symbol}
      </span>
      <span>
        market:{market.period}:{market.adjustment}:{market.zoom.start}:
        {market.zoom.end}:{market.mainChart}:{market.subchart.kind}
      </span>
      <button type="button" onClick={() => market.setPeriod('60m')}>
        切换周期
      </button>
    </div>
  );
}

function renderGate(api: WorkspaceApi, initialEntry = '/market') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <WorkspacePersistenceGate api={api}>
        <Probe />
      </WorkspacePersistenceGate>
    </MemoryRouter>,
  );
}

it.each([
  '/backtests/11111111-1111-1111-1111-111111111111',
  '/backtests?symbol=600000.SH&period=1d',
])(
  'preserves an explicit backtest entry while restoring market preferences',
  async (entry) => {
    const api: WorkspaceApi = {
      get: vi.fn(() => Promise.resolve(state())),
      put: vi.fn(),
    };
    renderGate(api, entry);

    expect(await screen.findByText(`route:${entry}`)).toBeVisible();
    expect(screen.getByText('instrument:浦发银行:600000.SH')).toBeVisible();
  },
);

beforeEach(() => resetMarketStore());

it('restores route and every allowlisted market preference before mounting children', async () => {
  const setItem = vi.spyOn(Storage.prototype, 'setItem');
  const api: WorkspaceApi = {
    get: vi.fn(() => Promise.resolve(state())),
    put: vi.fn(),
  };
  renderGate(api);

  expect(screen.getByRole('status')).toHaveTextContent('正在恢复工作区');
  await waitFor(() =>
    expect(screen.getByText('route:/formulas')).toBeVisible(),
  );
  expect(screen.getByText('instrument:浦发银行:600000.SH')).toBeVisible();
  expect(
    screen.getByText('market:1w:hfq:20:80:candlestick:volume'),
  ).toBeVisible();
  expect(setItem).not.toHaveBeenCalled();
});

it('shows backend recovery notice without blocking the safe default workspace', async () => {
  const api: WorkspaceApi = {
    get: vi.fn(() =>
      Promise.resolve(
        state({
          revision: 0,
          restored: false,
          updatedAt: null,
          expiresAt: null,
          notice: 'workspace_expired',
          workspace: {
            ...restoredWorkspace,
            currentPage: '/market',
            instrument: {
              symbol: '000001.SS',
              name: '上证指数',
              exchange: 'SH',
              instrumentKind: 'index',
            },
          },
        }),
      ),
    ),
    put: vi.fn(),
  };
  renderGate(api);

  expect(await screen.findByText('route:/market')).toBeVisible();
  expect(screen.getByText('instrument:上证指数:000001.SS')).toBeVisible();
  expect(screen.getByRole('status')).toHaveTextContent(
    '上次工作区已过期，已安全打开默认行情。',
  );
});

it('persists valid user changes with CAS and merges once after a 409', async () => {
  const user = userEvent.setup();
  const put = vi
    .fn<WorkspaceApi['put']>()
    .mockRejectedValueOnce(
      new ApiError('conflict', { kind: 'http', status: 409 }),
    )
    .mockImplementation((request) =>
      Promise.resolve(
        state({
          revision: request.expectedRevision + 1,
          workspace: request.workspace,
        }),
      ),
    );
  const get = vi
    .fn<WorkspaceApi['get']>()
    .mockResolvedValueOnce(state())
    .mockResolvedValueOnce(
      state({
        revision: 9,
        workspace: { ...restoredWorkspace, adjustment: 'none' },
      }),
    );
  renderGate({ get, put });
  await waitFor(() =>
    expect(screen.getByText('route:/formulas')).toBeVisible(),
  );

  await user.click(screen.getByRole('button', { name: '切换周期' }));

  await waitFor(() => expect(put).toHaveBeenCalledTimes(2));
  const mergedRequest = put.mock.calls[1]?.[0];
  expect(mergedRequest?.expectedRevision).toBe(9);
  expect(mergedRequest?.workspace).toMatchObject({
    period: '60m',
    adjustment: 'none',
  });
  expect(screen.getByRole('status')).toHaveTextContent(
    '工作区在其他窗口发生变化，已安全合并并保存。',
  );
});

it('does not navigate or leak a protocol failure from an illegal route', async () => {
  const api: WorkspaceApi = {
    get: vi.fn(() => Promise.reject(new Error('https://evil/?token=secret'))),
    put: vi.fn(),
  };
  renderGate(api);

  expect(await screen.findByText('route:/market')).toBeVisible();
  expect(screen.queryByText(/evil|token|secret/u)).toBeNull();
  expect(screen.getByRole('status')).toHaveTextContent(
    '工作区恢复暂不可用，已安全打开默认行情。',
  );
  expect(
    screen.getByText('market:1d:none:0:100:candlestick:volume'),
  ).toBeVisible();
});

it('does not persist the initial restored snapshot', async () => {
  const api: WorkspaceApi = {
    get: vi.fn(() => Promise.resolve(state())),
    put: vi.fn(),
  };
  renderGate(api);
  await waitFor(() =>
    expect(screen.getByText('route:/formulas')).toBeVisible(),
  );
  await act(() => new Promise((resolve) => window.setTimeout(resolve, 400)));
  expect(api.put).not.toHaveBeenCalled();
});
