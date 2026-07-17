import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';

import { resetMarketStore, useMarketStore } from '../market/marketStore';
import { OnboardingGate } from './OnboardingGate';
import type { OnboardingApi, OnboardingState } from './onboardingApi';
import theme from '../../app/theme.css?raw';

const digest = `sha256:${'a'.repeat(64)}`;

function onboardingState(
  currentStep: OnboardingState['currentStep'],
  overrides: Partial<OnboardingState> = {},
): OnboardingState {
  return {
    schemaVersion: 1,
    revision: 1,
    currentStep,
    status: currentStep === 'completed' ? 'completed' : 'in_progress',
    source:
      currentStep === 'welcome' || currentStep === 'data_preparation'
        ? null
        : {
            id: 'akshare',
            label: 'AKShare',
            catalogManifestRecordId: digest,
            catalogDatasetVersion: digest,
            dataCutoff: '2026-07-11T07:00:00Z',
          },
    instrument:
      currentStep === 'welcome' ||
      currentStep === 'data_preparation' ||
      currentStep === 'instrument_selection'
        ? null
        : {
            symbol: '000001.SS',
            name: '上证指数',
            exchange: 'SH',
            instrumentKind: 'index',
          },
    sync:
      currentStep === 'synchronization'
        ? {
            status: 'verified',
            providerId: 'akshare',
            manifestRecordId: digest,
            datasetVersion: digest,
            dataCutoff: '2026-07-11T07:00:00Z',
            rowCount: 240,
          }
        : null,
    error: null,
    demoMode: false,
    ...overrides,
  };
}

function api(
  initial: OnboardingState = onboardingState('welcome'),
): OnboardingApi {
  return {
    getState: vi.fn(() => Promise.resolve(initial)),
    getSources: vi.fn(() =>
      Promise.resolve([
        {
          id: 'akshare',
          label: 'AKShare',
          description: '无需密钥的 A 股日线来源',
          recommended: true,
          requiresToken: false,
          status: 'ready',
          dataCutoff: '2026-07-11T07:00:00Z',
        },
      ] as const),
    ),
    searchInstruments: vi.fn(() => Promise.resolve([])),
    saveProgress: vi.fn((input: Parameters<OnboardingApi['saveProgress']>[0]) =>
      Promise.resolve(
        onboardingState(input.currentStep, {
          revision: input.currentStep === 'data_preparation' ? 2 : 3,
        }),
      ),
    ),
    synchronize: vi.fn(() =>
      Promise.resolve(onboardingState('synchronization', { revision: 4 })),
    ),
    complete: vi.fn(() =>
      Promise.resolve(onboardingState('completed', { revision: 5 })),
    ),
    runAction: vi.fn((action) =>
      Promise.resolve(
        action === 'demo'
          ? onboardingState('welcome', {
              demoMode: true,
              instrument: {
                symbol: '600000.SH',
                name: 'Stock Desk 合成演示标的（非真实行情）',
                exchange: 'SH',
                instrumentKind: 'stock',
              },
            })
          : action === 'exit_demo'
            ? onboardingState('data_preparation', {
                revision: 3,
                demoMode: false,
                source: null,
              })
            : action === 'advanced'
              ? onboardingState('data_preparation', {
                  revision: 3,
                  error: {
                    code: 'advanced_configuration_required',
                    actions: ['retry', 'switch_provider', 'advanced', 'demo'],
                  },
                })
              : onboardingState('data_preparation'),
      ),
    ),
  };
}

function SelectedInstrument() {
  const selected = useMarketStore((state) => state.selectedInstrument);
  return (
    <p>
      workspace:{selected?.name}:{selected?.symbol}
    </p>
  );
}

function CurrentLocation() {
  const location = useLocation();
  return <p>location:{location.pathname + location.search}</p>;
}

function renderGate(client: OnboardingApi) {
  return render(
    <MemoryRouter initialEntries={['/market']}>
      <OnboardingGate api={client}>
        <SelectedInstrument />
      </OnboardingGate>
    </MemoryRouter>,
  );
}

beforeEach(() => resetMarketStore());

it('completes first run in four primary clicks and opens the default market', async () => {
  const client = api();
  const user = userEvent.setup();
  renderGate(client);

  await user.click(await screen.findByRole('button', { name: '开始' }));
  await user.click(await screen.findByRole('button', { name: '继续' }));
  expect(screen.getByText('上证指数')).toBeInTheDocument();
  expect(screen.getByText('000001.SS')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '加载行情' }));
  await user.click(await screen.findByRole('button', { name: '打开行情' }));

  expect(await screen.findByText('workspace:上证指数:000001.SS')).toBeVisible();
  expect(client.synchronize).toHaveBeenCalledWith({
    sourceId: 'akshare',
    symbol: '000001.SS',
  });
});

it('recovers persisted progress when a slow desktop request times out after commit', async () => {
  const initial = onboardingState('instrument_selection', {
    source: {
      id: 'akshare',
      label: 'AKShare',
      catalogManifestRecordId: digest,
      catalogDatasetVersion: digest,
      dataCutoff: null,
    },
  });
  const recovered = onboardingState('synchronization', {
    revision: 5,
    source: {
      id: 'baostock',
      label: 'BaoStock',
      catalogManifestRecordId: digest,
      catalogDatasetVersion: digest,
      dataCutoff: null,
    },
  });
  const synchronizing = onboardingState('synchronization', {
    revision: 4,
    source: recovered.source,
    sync: {
      status: 'idle',
      providerId: null,
      manifestRecordId: null,
      datasetVersion: null,
      dataCutoff: null,
      rowCount: 0,
    },
  });
  const client = api(initial);
  vi.mocked(client.getState)
    .mockResolvedValueOnce(initial)
    .mockResolvedValueOnce(synchronizing)
    .mockResolvedValueOnce(synchronizing)
    .mockResolvedValueOnce(recovered);
  vi.mocked(client.synchronize).mockRejectedValueOnce(
    new Error('desktop proxy timed out after the sidecar committed'),
  );
  const user = userEvent.setup();
  renderGate(client);

  await user.click(await screen.findByRole('button', { name: '加载行情' }));

  expect(
    await screen.findByRole(
      'heading',
      { name: '可以开始使用了' },
      { timeout: 4_000 },
    ),
  ).toBeVisible();
  expect(document.querySelector('.onboarding-success-icon')).toBeNull();
  expect(screen.getByRole('button', { name: '打开行情' })).toBeEnabled();
  expect(screen.queryByText('操作失败，请重试。')).toBeNull();
  expect(client.getState).toHaveBeenCalledTimes(4);
});

it('resumes from a persisted step without replaying welcome', async () => {
  renderGate(
    api(
      onboardingState('instrument_selection', {
        source: {
          id: 'baostock',
          label: 'BaoStock',
          catalogManifestRecordId: digest,
          catalogDatasetVersion: digest,
          dataCutoff: null,
        },
      }),
    ),
  );

  expect(
    await screen.findByRole('heading', { name: '选择一只股票' }),
  ).toBeVisible();
  expect(screen.queryByRole('button', { name: '开始' })).toBeNull();
});

it('restores the persisted selection when onboarding is already complete', async () => {
  renderGate(
    api(
      onboardingState('completed', {
        instrument: {
          symbol: '000001.SS',
          name: '上证指数',
          exchange: 'SH',
          instrumentKind: 'index',
        },
      }),
    ),
  );

  expect(await screen.findByText('workspace:上证指数:000001.SS')).toBeVisible();
});

it('keeps demo read-only and does not mark onboarding complete', async () => {
  const client = api();
  const user = userEvent.setup();
  renderGate(client);

  await user.click(await screen.findByRole('button', { name: '进入演示模式' }));

  expect(await screen.findByText('演示模式 · 当前显示示例数据')).toBeVisible();
  expect(
    screen.getByRole('button', { name: '设置真实行情' }).parentElement
      ?.parentElement,
  ).toHaveClass('onboarding-notice-frame');
  expect(
    screen.getByText(
      'workspace:Stock Desk 合成演示标的（非真实行情）:600000.SH',
    ),
  ).toBeVisible();
  expect(client.complete).not.toHaveBeenCalled();
});

it('restores persisted demo mode and can exit into a usable real-data setup', async () => {
  const client = api(
    onboardingState('instrument_selection', {
      demoMode: true,
      source: null,
      error: {
        code: 'demo_read_only',
        actions: ['retry', 'switch_provider', 'advanced'],
      },
    }),
  );
  const user = userEvent.setup();
  renderGate(client);

  expect(await screen.findByText('演示模式 · 当前显示示例数据')).toBeVisible();
  await user.click(screen.getByRole('button', { name: '设置真实行情' }));

  expect(
    await screen.findByRole('heading', { name: '选择数据源' }),
  ).toBeVisible();
  expect(screen.getByRole('button', { name: '继续' })).toBeEnabled();
  expect(client.runAction).toHaveBeenCalledWith('exit_demo');
});

it('opens the real Tushare and local TDX settings from advanced setup', async () => {
  const client = api(onboardingState('data_preparation'));
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/market']}>
      <OnboardingGate api={client}>
        <CurrentLocation />
      </OnboardingGate>
    </MemoryRouter>,
  );

  await user.click(await screen.findByRole('button', { name: '数据源设置' }));

  expect(
    await screen.findByText('location:/settings?focus=data-sources'),
  ).toBeVisible();
  expect(screen.getByRole('button', { name: '返回首次设置' })).toBeEnabled();
  expect(client.runAction).toHaveBeenCalledWith('advanced');
});

it('shows a recoverable safe error without rendering exception details', async () => {
  const client = api();
  vi.mocked(client.getState).mockRejectedValueOnce(
    new Error('http://127.0.0.1:43127 C:\\secret\\token traceback'),
  );
  renderGate(client);

  expect(
    await screen.findByRole('heading', { name: '暂时无法打开' }),
  ).toBeVisible();
  expect(screen.getByRole('button', { name: '重试' })).toBeEnabled();
  expect(screen.queryByText(/127\.0\.0\.1|secret|traceback/u)).toBeNull();
});

it('supports code, Chinese, or pinyin search with keyboard selection', async () => {
  const client = api(
    onboardingState('instrument_selection', {
      source: {
        id: 'akshare',
        label: 'AKShare',
        catalogManifestRecordId: digest,
        catalogDatasetVersion: digest,
        dataCutoff: null,
      },
    }),
  );
  vi.mocked(client.searchInstruments).mockResolvedValue([
    {
      symbol: '600519.SH',
      name: '贵州茅台',
      exchange: 'SH',
      instrumentKind: 'stock',
    },
  ]);
  const user = userEvent.setup();
  renderGate(client);

  const search = await screen.findByRole('combobox', {
    name: '按代码、中文或拼音搜索证券',
  });
  await user.type(search, 'gzmt');
  await waitFor(() => expect(client.searchInstruments).toHaveBeenCalled());
  await user.keyboard('{Enter}');
  expect(await screen.findByText('贵州茅台')).toBeVisible();
  expect(screen.getByText('600519.SH')).toBeVisible();
});

it('can prepare and open the searched non-default stock', async () => {
  const client = api(
    onboardingState('instrument_selection', {
      source: {
        id: 'akshare',
        label: 'AKShare',
        catalogManifestRecordId: digest,
        catalogDatasetVersion: digest,
        dataCutoff: null,
      },
    }),
  );
  vi.mocked(client.searchInstruments).mockResolvedValue([
    {
      symbol: '600000.SH',
      name: '浦发银行',
      exchange: 'SH',
      instrumentKind: 'stock',
    },
  ]);
  vi.mocked(client.synchronize).mockResolvedValue(
    onboardingState('synchronization', {
      source: {
        id: 'baostock',
        label: 'BaoStock',
        catalogManifestRecordId: digest,
        catalogDatasetVersion: digest,
        dataCutoff: null,
      },
      instrument: {
        symbol: '600000.SH',
        name: '浦发银行',
        exchange: 'SH',
        instrumentKind: 'stock',
      },
      sync: {
        status: 'verified',
        providerId: 'baostock',
        manifestRecordId: digest,
        datasetVersion: digest,
        dataCutoff: null,
        rowCount: 240,
      },
    }),
  );
  vi.mocked(client.complete).mockResolvedValue(
    onboardingState('completed', {
      source: {
        id: 'baostock',
        label: 'BaoStock',
        catalogManifestRecordId: digest,
        catalogDatasetVersion: digest,
        dataCutoff: null,
      },
      instrument: {
        symbol: '600000.SH',
        name: '浦发银行',
        exchange: 'SH',
        instrumentKind: 'stock',
      },
    }),
  );
  const user = userEvent.setup();
  renderGate(client);

  await user.type(
    await screen.findByRole('combobox', {
      name: '按代码、中文或拼音搜索证券',
    }),
    '600000',
  );
  await waitFor(() => expect(client.searchInstruments).toHaveBeenCalled());
  await user.keyboard('{Enter}');
  await user.click(screen.getByRole('button', { name: '加载行情' }));
  await user.click(await screen.findByRole('button', { name: '打开行情' }));

  expect(client.synchronize).toHaveBeenCalledWith({
    sourceId: 'akshare',
    symbol: '600000.SH',
  });
  expect(await screen.findByText('workspace:浦发银行:600000.SH')).toBeVisible();
});

it('uses concise user language and shared readable error colors', async () => {
  renderGate(
    api(
      onboardingState('synchronization', {
        sync: {
          status: 'failed',
          providerId: null,
          manifestRecordId: null,
          datasetVersion: null,
          dataCutoff: null,
          rowCount: 0,
        },
        error: {
          code: 'provider_invalid_response',
          actions: ['retry', 'switch_provider'],
        },
      }),
    ),
  );

  expect(await screen.findByRole('alert')).toHaveTextContent(
    '暂时无法加载行情',
  );
  expect(document.body.textContent).not.toMatch(
    /FIRST RUN|SETUP|目录|同步|验证|诊断|技术信息|invalid_response/u,
  );
  expect(theme).toContain('--status-error-text:');
  expect(theme).toContain('--status-error-surface:');
  expect(theme).toContain('color: var(--status-error-text);');
  expect(theme).not.toMatch(
    /\.onboarding-inline-error p,[\s\S]{0,100}#fde68a/u,
  );
});

it('keeps data-source failure actions outside the error and retries from the primary button', async () => {
  const client = api(
    onboardingState('data_preparation', {
      error: {
        code: 'provider_invalid_response',
        actions: ['retry', 'switch_provider', 'advanced', 'demo'],
      },
    }),
  );
  const user = userEvent.setup();
  renderGate(client);

  const alert = await screen.findByRole('alert');
  expect(within(alert).queryByRole('button')).toBeNull();
  await user.click(screen.getByRole('button', { name: '重试' }));
  expect(client.runAction).toHaveBeenCalledWith('retry');
});

it('turns an exhausted data-source request into a retry without changing its action', async () => {
  const initial = onboardingState('data_preparation');
  const client = api(initial);
  vi.mocked(client.saveProgress).mockRejectedValue(
    new Error('desktop request failed'),
  );
  vi.mocked(client.getState).mockResolvedValue(initial);
  const user = userEvent.setup();
  renderGate(client);
  const continueButton = await screen.findByRole('button', { name: '继续' });
  await waitFor(() => expect(continueButton).toBeEnabled());
  const timeout = vi
    .spyOn(window, 'setTimeout')
    .mockImplementation((handler) => {
      if (typeof handler === 'function') handler();
      return 1 as unknown as ReturnType<typeof window.setTimeout>;
    });
  try {
    await act(async () => {
      await user.click(continueButton);
      for (let turn = 0; turn < 100; turn += 1) await Promise.resolve();
    });
    expect(client.getState).toHaveBeenCalledTimes(31);
    const retryButton = screen.getByRole('button', { name: '重试' });
    expect(retryButton).toBeEnabled();
    expect(screen.getByRole('alert')).toHaveTextContent('操作失败，请重试。');
    expect(client.saveProgress).toHaveBeenCalledOnce();

    await act(async () => {
      await user.click(retryButton);
      for (let turn = 0; turn < 100; turn += 1) await Promise.resolve();
    });

    expect(client.saveProgress).toHaveBeenCalledTimes(2);
    expect(client.runAction).not.toHaveBeenCalledWith('retry');
  } finally {
    timeout.mockRestore();
  }
});

it('uses concise labels without a decorative hero glyph or redundant data hints', async () => {
  const client = api();
  const user = userEvent.setup();
  renderGate(client);

  expect(await screen.findByText('首次设置')).toBeVisible();
  expect(screen.queryByText('⌁')).toBeNull();
  await user.click(screen.getByRole('button', { name: '开始' }));

  expect(
    await screen.findByRole('heading', { name: '选择数据源' }),
  ).toBeVisible();
  expect(screen.queryByText('默认选项适合大多数用户。')).toBeNull();
  expect(screen.queryByText('继续时自动检查')).toBeNull();
  expect(screen.getByRole('button', { name: '数据源设置' })).toBeEnabled();
});

it('shows a spinner on the clicked action without replacing its label', async () => {
  const client = api();
  vi.mocked(client.saveProgress).mockImplementation(
    () => new Promise<OnboardingState>(() => undefined),
  );
  const user = userEvent.setup();
  renderGate(client);

  const start = await screen.findByRole('button', { name: '开始' });
  await user.click(start);

  expect(start).toHaveAttribute('aria-busy', 'true');
  expect(start).toHaveTextContent('开始');
  expect(within(start).getByTestId('async-action-spinner')).toBeVisible();
});
