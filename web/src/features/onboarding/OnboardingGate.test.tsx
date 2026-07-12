import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';

import { resetMarketStore, useMarketStore } from '../market/marketStore';
import { OnboardingGate } from './OnboardingGate';
import type { OnboardingApi, OnboardingState } from './onboardingApi';

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

  await user.click(await screen.findByRole('button', { name: '开始设置' }));
  await user.click(
    await screen.findByRole('button', { name: '使用此来源并继续' }),
  );
  expect(screen.getByText('上证指数')).toBeInTheDocument();
  expect(screen.getByText('000001.SS')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '同步并继续' }));
  await user.click(
    await screen.findByRole('button', { name: '进入行情工作区' }),
  );

  expect(await screen.findByText('workspace:上证指数:000001.SS')).toBeVisible();
  expect(client.synchronize).toHaveBeenCalledWith({
    sourceId: 'akshare',
    symbol: '000001.SS',
  });
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
    await screen.findByRole('heading', { name: '选择打开后的第一只证券' }),
  ).toBeVisible();
  expect(screen.queryByRole('button', { name: '开始设置' })).toBeNull();
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

  await user.click(await screen.findByRole('button', { name: '先看只读演示' }));

  expect(await screen.findByText(/只读演示 · 设置尚未完成/u)).toBeVisible();
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

  expect(await screen.findByText(/只读演示 · 设置尚未完成/u)).toBeVisible();
  await user.click(
    screen.getByRole('button', { name: '退出演示并配置真实数据' }),
  );

  expect(
    await screen.findByRole('heading', { name: '准备行情数据' }),
  ).toBeVisible();
  expect(
    screen.getByRole('button', { name: '使用此来源并继续' }),
  ).toBeEnabled();
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

  await user.click(await screen.findByRole('button', { name: '高级数据设置' }));

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
    await screen.findByRole('heading', { name: '首次设置暂时无法读取' }),
  ).toBeVisible();
  expect(screen.getByRole('button', { name: '重试读取' })).toBeEnabled();
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
