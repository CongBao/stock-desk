import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { DataSourcesPage } from './DataSourcesPage';
import type {
  SourceDiagnostic,
  SourceSettings,
  SourceSettingsApi,
  TushareSourceStatus,
} from './sourceSettingsApi';
import { diagnosticResponse, settingsResponse } from './testFixtures';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => {
    resolve = accept;
  });
  return { promise, resolve };
}

function createApi(overrides: Partial<SourceSettingsApi> = {}) {
  return {
    getSettings: vi.fn(() => Promise.resolve(settingsResponse)),
    savePublic: vi.fn(() => Promise.resolve(settingsResponse)),
    saveTushare: vi.fn(() => Promise.resolve(settingsResponse.tushare)),
    testSource: vi.fn(() => Promise.resolve(diagnosticResponse)),
    ...overrides,
  } satisfies SourceSettingsApi;
}

it('renders source cards, safe token state, priorities, and TDX path', async () => {
  render(<DataSourcesPage api={createApi()} />);

  expect(
    await screen.findByRole('heading', { level: 2, name: '数据源设置' }),
  ).toBeInTheDocument();
  for (const source of [
    'Tushare',
    'AKShare',
    'BaoStock',
    '通达信本地',
    'Eastmoney',
  ]) {
    expect(screen.getByRole('heading', { name: source })).toBeInTheDocument();
  }
  const token = screen.getByLabelText('Tushare Token');
  expect(token).toHaveAttribute('type', 'password');
  expect(token).toHaveAttribute('autocomplete', 'new-password');
  expect(token).toHaveValue('');
  expect(screen.getByText('已配置：ts-p•••••••3456')).toBeInTheDocument();
  expect(screen.queryByDisplayValue('ts-p•••••••3456')).not.toBeInTheDocument();
  expect(screen.getByLabelText('通达信 vipdoc 目录')).toHaveValue(
    '/safe/vipdoc',
  );
  expect(
    screen.getByRole('group', { name: '60 分钟行情优先级' }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole('group', { name: '回测执行状态优先级' }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole('group', { name: '基本面优先级' }),
  ).toBeInTheDocument();
  expect(screen.getByRole('group', { name: '公告优先级' })).toBeInTheDocument();
  expect(screen.getByRole('group', { name: '新闻优先级' })).toBeInTheDocument();
  expect(
    screen.getAllByRole('button', { name: /^上移/u }).length,
  ).toBeGreaterThan(0);
});

it('reorders with accessible buttons and saves public settings plus a cleared token', async () => {
  const user = userEvent.setup();
  const api = createApi();
  render(<DataSourcesPage api={api} />);
  await screen.findByDisplayValue('/safe/vipdoc');

  await user.click(
    screen.getByRole('button', { name: '上移 BaoStock（60 分钟行情）' }),
  );
  await user.clear(screen.getByLabelText('通达信 vipdoc 目录'));
  await user.type(screen.getByLabelText('通达信 vipdoc 目录'), '/new/vipdoc');
  await user.type(screen.getByLabelText('Tushare Token'), 'never-render-token');
  await user.click(screen.getByRole('button', { name: '保存数据源设置' }));

  await waitFor(() => expect(api.savePublic).toHaveBeenCalledOnce());
  expect(api.savePublic).toHaveBeenCalledWith(
    expect.objectContaining({
      priorities: expect.objectContaining({
        minute_bars: ['baostock', 'tushare', 'eastmoney'],
      }) as unknown,
      tdxPath: '/new/vipdoc',
    }),
    expect.objectContaining({
      signal: expect.any(AbortSignal) as unknown,
    }) as unknown,
  );
  expect(api.saveTushare).toHaveBeenCalledWith(
    'never-render-token',
    expect.objectContaining({
      signal: expect.any(AbortSignal) as unknown,
    }) as unknown,
  );
  expect(screen.getByLabelText('Tushare Token')).toHaveValue('');
  expect(screen.queryByText('never-render-token')).not.toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('设置已安全保存');
});

it('shows diagnostic status, permissions, periods, cutoffs, gaps, and fallback', async () => {
  const user = userEvent.setup();
  const api = createApi();
  render(<DataSourcesPage api={api} />);
  await screen.findByDisplayValue('/safe/vipdoc');

  await user.click(screen.getByRole('button', { name: '测试 Tushare 连接' }));

  expect(await screen.findByText('权限不足')).toBeInTheDocument();
  expect(screen.getByText('日线、周线')).toBeInTheDocument();
  expect(screen.getByText(/60 分钟行情：权限不足/u)).toBeInTheDocument();
  expect(
    screen.getByText('provider permission was denied'),
  ).toBeInTheDocument();
  expect(screen.getByText(/检测于 2026年7月6日/u)).toBeInTheDocument();
  expect(screen.getByText('数据截至').parentElement).toHaveTextContent('2026');
  expect(screen.getByText('最近更新').parentElement).toHaveTextContent('2026');
});

it('shows recognized TDX markets with its validated period and cutoff', async () => {
  const user = userEvent.setup();
  const tdxDiagnostic: SourceDiagnostic = {
    ...diagnosticResponse,
    source: 'tdx_local',
    status: 'available',
    capabilities: ['bars'],
    permissions: diagnosticResponse.permissions.map((permission) => ({
      ...permission,
      state: permission.category === 'daily_bars' ? 'available' : 'unsupported',
    })),
    available_periods: ['1d'],
    markets: ['SH', 'SZ'],
    gaps: diagnosticResponse.permissions
      .filter((permission) => permission.category !== 'daily_bars')
      .map((permission) => ({
        category: permission.category,
        state: 'unsupported',
        reason: 'unsupported',
        detail: `unsupported ${permission.category}`,
      })),
    last_update: null,
    data_cutoff: '2024-07-02T07:00:00Z',
    fallback_reason: null,
  };
  const api = createApi({
    testSource: vi.fn(() => Promise.resolve(tdxDiagnostic)),
  });
  render(<DataSourcesPage api={api} />);
  await screen.findByDisplayValue('/safe/vipdoc');

  await user.click(
    screen.getByRole('button', { name: '测试 通达信本地 连接' }),
  );

  const result = await screen.findByLabelText('通达信本地 检测结果');
  expect(within(result).getByText('上交所、深交所')).toBeInTheDocument();
  expect(within(result).getByText('日线')).toBeInTheDocument();
  expect(within(result).getByText('数据截至').parentElement).toHaveTextContent(
    '2024',
  );
});

it('uses fixed safe UI errors without rendering rejected provider details', async () => {
  const user = userEvent.setup();
  const unsafe = 'token-and-private-path';
  const api = createApi({
    testSource: vi.fn(() => Promise.reject(new Error(unsafe))),
  });
  render(<DataSourcesPage api={api} />);
  await screen.findByDisplayValue('/safe/vipdoc');

  await user.click(screen.getByRole('button', { name: '测试 Tushare 连接' }));

  expect(await screen.findByRole('alert')).toHaveTextContent(
    '连接检测失败，请检查本地配置后重试。',
  );
  expect(screen.queryByText(unsafe)).not.toBeInTheDocument();
});

it('aborts initial and diagnostic requests on unmount without late updates', async () => {
  const load = deferred<typeof settingsResponse>();
  const diagnostic = deferred<typeof diagnosticResponse>();
  const signals: AbortSignal[] = [];
  const api = createApi({
    getSettings: vi.fn((options: { readonly signal?: AbortSignal } = {}) => {
      const { signal } = options;
      if (signal) signals.push(signal);
      return load.promise;
    }),
    testSource: vi.fn(
      (
        _source: Parameters<SourceSettingsApi['testSource']>[0],
        options: { readonly signal?: AbortSignal } = {},
      ) => {
        const { signal } = options;
        if (signal) signals.push(signal);
        return diagnostic.promise;
      },
    ),
  });
  const first = render(<DataSourcesPage api={api} />);
  first.unmount();
  expect(signals[0]?.aborted).toBe(true);

  const second = render(
    <DataSourcesPage api={createApi({ testSource: api.testSource })} />,
  );
  await screen.findByDisplayValue('/safe/vipdoc');
  await userEvent.click(
    screen.getByRole('button', { name: '测试 Tushare 连接' }),
  );
  second.unmount();
  expect(signals[1]?.aborted).toBe(true);

  await act(async () => {
    load.resolve(settingsResponse);
    diagnostic.resolve(diagnosticResponse);
    await Promise.resolve();
  });
});

it('keeps path priority and token edits made while an older save is pending', async () => {
  const user = userEvent.setup();
  const publicSave = deferred<SourceSettings>();
  const tokenSave = deferred<TushareSourceStatus>();
  const api = createApi({
    savePublic: vi.fn(() => publicSave.promise),
    saveTushare: vi.fn(() => tokenSave.promise),
  });
  render(<DataSourcesPage api={api} />);
  await screen.findByDisplayValue('/safe/vipdoc');

  await user.click(
    screen.getByRole('button', { name: '上移 BaoStock（60 分钟行情）' }),
  );
  const path = screen.getByLabelText('通达信 vipdoc 目录');
  await user.clear(path);
  await user.type(path, '/revision-a/vipdoc');
  await user.type(screen.getByLabelText('Tushare Token'), 'revision-a-token');
  await user.click(screen.getByRole('button', { name: '保存数据源设置' }));
  await waitFor(() => expect(api.savePublic).toHaveBeenCalledOnce());

  await user.click(
    screen.getByRole('button', { name: '下移 BaoStock（60 分钟行情）' }),
  );
  await user.clear(path);
  await user.type(path, '/revision-b/vipdoc');
  await user.type(screen.getByLabelText('Tushare Token'), 'revision-b-token');

  const revisionA: SourceSettings = {
    ...settingsResponse,
    priorities: {
      ...settingsResponse.priorities,
      minute_bars: ['baostock', 'tushare', 'eastmoney'],
    },
    tdx_path: '/revision-a/vipdoc',
  };
  await act(async () => {
    publicSave.resolve(revisionA);
    await Promise.resolve();
  });
  await waitFor(() => expect(api.saveTushare).toHaveBeenCalledOnce());
  await act(async () => {
    tokenSave.resolve({
      ...settingsResponse.tushare,
      masked_hint: 'revi•••••••safe',
    });
    await Promise.resolve();
  });

  expect(path).toHaveValue('/revision-b/vipdoc');
  expect(screen.getByLabelText('Tushare Token')).toHaveValue(
    'revision-b-token',
  );
  const minuteLane = screen.getByRole('group', {
    name: '60 分钟行情优先级',
  });
  const rows = within(minuteLane).getAllByRole('listitem');
  expect(rows[0]).toHaveTextContent('Tushare');
  expect(rows[1]).toHaveTextContent('BaoStock');
  expect(screen.queryByText('设置已安全保存')).not.toBeInTheDocument();
  expect(screen.getByText('存在未保存更改')).toBeInTheDocument();
  expect(screen.getByText('已配置：revi•••••••safe')).toBeInTheDocument();
});

it('clears completed diagnostics when configuration changes and remains stale after save', async () => {
  const user = userEvent.setup();
  const api = createApi({
    testSource: vi.fn(
      (source: Parameters<SourceSettingsApi['testSource']>[0]) =>
        Promise.resolve({
          ...diagnosticResponse,
          source,
        } satisfies SourceDiagnostic),
    ),
  });
  render(<DataSourcesPage api={api} />);
  await screen.findByDisplayValue('/safe/vipdoc');

  await user.click(screen.getByRole('button', { name: '测试 Tushare 连接' }));
  await user.click(
    screen.getByRole('button', { name: '测试 通达信本地 连接' }),
  );
  await waitFor(() => expect(screen.getAllByText('权限不足')).toHaveLength(2));

  await user.type(screen.getByLabelText('Tushare Token'), 'new-token');
  await user.click(screen.getByRole('button', { name: '保存数据源设置' }));
  await waitFor(() => expect(api.savePublic).toHaveBeenCalledOnce());

  expect(screen.queryByText('权限不足')).not.toBeInTheDocument();
  expect(screen.getAllByText('配置已变更，请重新检测')).toHaveLength(2);
});

it('aborts and ignores a diagnostic started under an older edit revision', async () => {
  const user = userEvent.setup();
  const diagnostic = deferred<SourceDiagnostic>();
  let signal: AbortSignal | undefined;
  const api = createApi({
    testSource: vi.fn(
      (
        _source: Parameters<SourceSettingsApi['testSource']>[0],
        options: { readonly signal?: AbortSignal } = {},
      ) => {
        signal = options.signal;
        return diagnostic.promise;
      },
    ),
  });
  render(<DataSourcesPage api={api} />);
  await screen.findByDisplayValue('/safe/vipdoc');

  await user.click(screen.getByRole('button', { name: '测试 Tushare 连接' }));
  expect(signal?.aborted).toBe(false);
  await user.type(screen.getByLabelText('Tushare Token'), 'replacement-token');
  expect(signal?.aborted).toBe(true);
  await user.click(screen.getByRole('button', { name: '保存数据源设置' }));

  await act(async () => {
    diagnostic.resolve(diagnosticResponse);
    await Promise.resolve();
  });

  expect(screen.queryByText('权限不足')).not.toBeInTheDocument();
  expect(screen.getByText('配置已变更，请重新检测')).toBeInTheDocument();
});

it('disables connection diagnostics while settings are dirty with accessible guidance', async () => {
  const user = userEvent.setup();
  render(<DataSourcesPage api={createApi()} />);
  await screen.findByDisplayValue('/safe/vipdoc');

  const buttons = screen.getAllByRole('button', { name: /^测试/u });
  expect(buttons.every((button) => !button.hasAttribute('disabled'))).toBe(
    true,
  );

  await user.type(screen.getByLabelText('Tushare Token'), 'dirty-token');

  expect(buttons.every((button) => button.hasAttribute('disabled'))).toBe(true);
  expect(buttons[0]).toHaveAccessibleDescription(
    '当前配置尚未成功保存，请先保存后再检测连接。',
  );
});

it('blocks diagnostics through save and ignores an abort-insensitive old response', async () => {
  const user = userEvent.setup();
  const firstDiagnostic = deferred<SourceDiagnostic>();
  const publicSave = deferred<SourceSettings>();
  const signals: AbortSignal[] = [];
  let diagnosticCalls = 0;
  const api = createApi({
    savePublic: vi.fn(() => publicSave.promise),
    testSource: vi.fn(
      (
        _source: Parameters<SourceSettingsApi['testSource']>[0],
        options: { readonly signal?: AbortSignal } = {},
      ) => {
        if (options.signal) signals.push(options.signal);
        diagnosticCalls += 1;
        return diagnosticCalls === 1
          ? firstDiagnostic.promise
          : Promise.resolve(diagnosticResponse);
      },
    ),
  });
  render(<DataSourcesPage api={api} />);
  await screen.findByDisplayValue('/safe/vipdoc');

  await user.click(screen.getByRole('button', { name: '测试 Tushare 连接' }));
  await user.click(screen.getByRole('button', { name: '保存数据源设置' }));
  expect(signals[0]?.aborted).toBe(true);
  expect(
    screen
      .getAllByRole('button', { name: /^测试/u })
      .every((button) => button.hasAttribute('disabled')),
  ).toBe(true);

  await act(async () => {
    firstDiagnostic.resolve(diagnosticResponse);
    await Promise.resolve();
  });
  expect(screen.queryByText('权限不足')).not.toBeInTheDocument();

  await act(async () => {
    publicSave.resolve(settingsResponse);
    await Promise.resolve();
  });
  await screen.findByText('设置已安全保存');
  expect(
    screen
      .getAllByRole('button', { name: /^测试/u })
      .every((button) => !button.hasAttribute('disabled')),
  ).toBe(true);
  expect(screen.getByText('配置已变更，请重新检测')).toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: '测试 Tushare 连接' }));
  expect(await screen.findByText('权限不足')).toBeInTheDocument();
});

it('keeps connection diagnostics disabled after a save error', async () => {
  const user = userEvent.setup();
  const api = createApi({
    savePublic: vi.fn(() => Promise.reject(new Error('unsafe failure'))),
  });
  render(<DataSourcesPage api={api} />);
  await screen.findByDisplayValue('/safe/vipdoc');

  await user.type(screen.getByLabelText('Tushare Token'), 'dirty-token');
  await user.click(screen.getByRole('button', { name: '保存数据源设置' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('保存失败');

  const buttons = screen.getAllByRole('button', { name: /^测试/u });
  expect(buttons.every((button) => button.hasAttribute('disabled'))).toBe(true);
  expect(buttons[0]).toHaveAccessibleDescription(
    '当前配置尚未成功保存，请先保存后再检测连接。',
  );
});
