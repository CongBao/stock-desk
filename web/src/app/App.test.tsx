import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  MemoryRouter,
  useNavigate,
  type MemoryRouterProps,
} from 'react-router-dom';

import { App } from './App';
import {
  createDesktopBridge,
  type DesktopAdapter,
  type DesktopBridge,
} from './desktopBridge';
import { settingsResponse } from '../features/settings/testFixtures';

vi.mock('../features/formulas/FormulaStudioPage', () => ({
  FormulaStudioPage: () => (
    <article>
      <h2
        ref={(node) => {
          node?.focus();
        }}
        data-page-heading
        tabIndex={-1}
      >
        公式工作台
      </h2>
      <span>v0.3.0 · Formula Studio</span>
    </article>
  ),
}));

vi.mock('../features/analysis/AnalysisPage', () => ({
  AnalysisPage: () => (
    <article>
      <h2 data-page-heading tabIndex={-1}>
        智能分析
      </h2>
      <span>真实分析工作台</span>
    </article>
  ),
}));

const healthyResponse = {
  name: 'stock-desk',
  status: 'ok',
  api_version: 'v1',
};

const runningWorkerResponse = {
  state: 'running',
  last_seen_at: '2026-07-09T02:00:00Z',
};

const completedTask = {
  id: '11111111-1111-4111-8111-111111111111',
  kind: 'demo.double',
  status: 'succeeded',
  progress: 1,
  cancel_requested: false,
  created_at: '2026-07-05T08:00:00Z',
  updated_at: '2026-07-05T08:00:01Z',
  finished_at: '2026-07-05T08:00:01Z',
  started_at: '2026-07-05T08:00:00Z',
  duration_ms: 1000,
  presentation: {
    label: '后台任务',
    stage: null,
    processed: null,
    total: null,
    failed: null,
    target: null,
  },
};

const disabledDailySchedule = {
  id: '00000000-0000-0000-0000-000000000001',
  enabled: false,
  timezone: 'Asia/Shanghai',
  local_time: '18:00',
  payload: {
    symbols: ['600000.SH'],
    period: '1d',
    adjustment: 'qfq',
    start: '2024-01-01T00:00:00Z',
    end: '2024-01-03T00:00:00Z',
  },
  symbols_frozen: true,
  last_enqueued_local_date: null,
  next_due_at: null,
  created_at: '2026-07-06T08:00:00Z',
  updated_at: '2026-07-06T08:00:00Z',
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === 'string') {
    return input;
  }
  return input instanceof URL ? input.href : input.url;
}

function isSystemStatusRequest(url: string): boolean {
  return (
    url === '/api/health' ||
    url === '/api/tasks?view=safe&limit=5' ||
    url === '/api/tasks/worker-status'
  );
}

function installHealthyFetch(
  tasks: readonly unknown[] = [],
  worker: unknown = runningWorkerResponse,
) {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = requestUrl(input);
    return Promise.resolve(
      url.includes('/market/pools')
        ? jsonResponse({ items: [], next_cursor: null })
        : url.endsWith('/market/schedules/daily')
          ? jsonResponse(disabledDailySchedule)
          : url.endsWith('/tasks/worker-status')
            ? jsonResponse(worker)
            : url.endsWith('/health')
              ? jsonResponse(healthyResponse)
              : jsonResponse(tasks),
    );
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function installPendingFetch() {
  const signals: AbortSignal[] = [];
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
    requestUrl(input).includes('/market/pools')
      ? Promise.resolve(jsonResponse({ items: [], next_cursor: null }))
      : requestUrl(input).endsWith('/market/schedules/daily')
        ? Promise.resolve(jsonResponse(disabledDailySchedule))
        : new Promise<Response>((_resolve, reject) => {
            if (init?.signal) {
              signals.push(init.signal);
              init.signal.addEventListener(
                'abort',
                () => reject(new DOMException('Aborted', 'AbortError')),
                { once: true },
              );
            }
          }),
  );
  vi.stubGlobal('fetch', fetchMock);
  return { fetchMock, signals };
}

function HistoryBackControl() {
  const navigate = useNavigate();

  return (
    <button type="button" onClick={() => void navigate(-1)}>
      测试返回
    </button>
  );
}

function renderApp(
  initialEntries: MemoryRouterProps['initialEntries'] = ['/market'],
  withBackControl = false,
  desktopBridge?: DesktopBridge,
) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        gcTime: 0,
        refetchOnWindowFocus: false,
        retry: false,
      },
    },
  });
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={initialEntries}>
          {withBackControl ? <HistoryBackControl /> : null}
          <App desktopBridge={desktopBridge} />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

beforeEach(() => {
  installPendingFetch();
  vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it('shows the product identity and all primary navigation items', () => {
  renderApp();

  expect(screen.getByText('stock-desk')).toBeInTheDocument();
  expect(screen.getByText('v1.0.0 · Task Center')).toBeInTheDocument();
  for (const label of [
    '行情',
    '自定义公式',
    '策略回测',
    '智能分析',
    '任务中心',
    '设置',
  ]) {
    expect(screen.getByRole('link', { name: label })).toBeInTheDocument();
  }
});

it('does not mount the workspace or request business APIs while desktop startup is pending', async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  const adapter: DesktopAdapter = {
    cancelExit: vi.fn(() => Promise.resolve()),
    confirmExit: vi.fn(() => Promise.resolve()),
    getRuntimeState: vi.fn(() => Promise.resolve({ state: 'starting' })),
    openDiagnostics: vi.fn(() => Promise.resolve()),
    requestExit: vi.fn(() => Promise.resolve()),
    restartService: vi.fn(() => Promise.resolve()),
    subscribe: vi.fn(() => Promise.resolve(() => undefined)),
    subscribeExit: vi.fn(() => Promise.resolve(() => undefined)),
  };

  renderApp(['/market'], false, createDesktopBridge(adapter));

  expect(screen.getByRole('status')).toHaveTextContent('正在启动桌面服务');
  expect(screen.queryByRole('main', { name: '行情图表工作区' })).toBeNull();
  await waitFor(() => expect(adapter.getRuntimeState).toHaveBeenCalledOnce());
  await waitFor(() => expect(adapter.subscribeExit).toHaveBeenCalledOnce());
  expect(fetchMock).not.toHaveBeenCalled();
});

it('collapses and expands the primary navigation without abbreviating link names', async () => {
  const user = userEvent.setup();
  renderApp();

  const collapse = screen.getByRole('button', { name: '收起主导航' });
  expect(collapse).toHaveAttribute('aria-expanded', 'true');
  await user.click(collapse);
  const expand = screen.getByRole('button', { name: '展开主导航' });
  expect(expand).toHaveAttribute('aria-expanded', 'false');
  expect(document.querySelector('.app-shell')).toHaveAttribute(
    'data-navigation-collapsed',
    'true',
  );
  for (const label of [
    '行情',
    '自定义公式',
    '策略回测',
    '智能分析',
    '任务中心',
    '设置',
  ]) {
    const link = screen.getByRole('link', { name: label });
    expect(link).toHaveAttribute('title', label);
    expect(link.querySelector('.nav-icon svg')).toHaveAttribute(
      'stroke',
      'currentColor',
    );
  }
  await user.click(expand);
  expect(screen.getByRole('button', { name: '收起主导航' })).toHaveAttribute(
    'aria-expanded',
    'true',
  );
});

it('opens on the cache-only three-column market workspace', async () => {
  renderApp(['/']);

  expect(
    await screen.findByRole('complementary', { name: '证券选择与股票池' }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole('region', { name: '行情图表工作区' }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole('complementary', { name: '数据证据与快捷操作' }),
  ).toBeInTheDocument();
  expect(screen.getByText('本地缓存')).toBeInTheDocument();
  expect(screen.queryByText(/布局预览/u)).not.toBeInTheDocument();
  expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content');
});

it('routes settings to the real data-source workspace', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = requestUrl(input);
      return Promise.resolve(
        url.endsWith('/settings/sources')
          ? jsonResponse(settingsResponse)
          : url.endsWith('/health')
            ? jsonResponse(healthyResponse)
            : jsonResponse([]),
      );
    }),
  );

  renderApp(['/settings']);

  expect(
    await screen.findByRole('heading', { level: 2, name: '数据源设置' }),
  ).toBeInTheDocument();
  expect(screen.queryByText(/能力按阶段交付/u)).not.toBeInTheDocument();
  await waitFor(() => expect(document.title).toBe('数据源设置 · stock-desk'));
});

it('routes analysis to the real intelligent-analysis workspace', async () => {
  renderApp(['/analysis']);

  expect(
    await screen.findByRole('heading', { level: 2, name: '智能分析' }),
  ).toBeInTheDocument();
  expect(screen.getByText('真实分析工作台')).toBeInTheDocument();
  expect(screen.queryByText(/能力按阶段交付/u)).not.toBeInTheDocument();
  expect(document.querySelector('.app-shell')).toHaveAttribute(
    'data-workspace',
    'analysis',
  );
});

it('routes tasks to the real v1 task workspace without planned copy', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.endsWith('/tasks?view=safe&limit=100'))
        return Promise.resolve(jsonResponse([]));
      if (url.endsWith('/tasks/metrics')) {
        return Promise.resolve(
          jsonResponse({
            total: 0,
            by_status: {
              queued: 0,
              running: 0,
              succeeded: 0,
              failed: 0,
              cancelled: 0,
            },
            failure_count: 0,
            completed_count: 0,
            average_duration_ms: null,
            min_duration_ms: null,
            max_duration_ms: null,
          }),
        );
      }
      if (url.endsWith('/health'))
        return Promise.resolve(jsonResponse(healthyResponse));
      if (url.endsWith('/tasks?view=safe&limit=5'))
        return Promise.resolve(jsonResponse([]));
      return Promise.resolve(jsonResponse([]));
    }),
  );

  renderApp(['/tasks']);

  expect(
    await screen.findByRole('heading', { level: 2, name: '任务中心' }),
  ).toBeVisible();
  expect(screen.queryByText('PLANNED WORKSPACE')).not.toBeInTheDocument();
  expect(screen.queryByText(/能力按阶段交付/u)).not.toBeInTheDocument();
  expect(document.querySelector('.app-shell')).toHaveAttribute(
    'data-workspace',
    'tasks',
  );
  expect(screen.getAllByText('v1.0.0 · Task Center')).toHaveLength(2);
});

it('supports direct refresh of a dynamic backtest run route', async () => {
  renderApp(['/backtests/11111111-1111-1111-1111-111111111111']);

  const heading = screen.getByRole('heading', { level: 2, name: '回测运行' });
  await waitFor(() => expect(heading).toHaveFocus());
  expect(document.title).toBe('策略回测 · stock-desk');
  expect(document.querySelector('.app-shell')).toHaveAttribute(
    'data-workspace',
    'backtests',
  );
  expect(screen.getByRole('link', { name: '策略回测' })).toHaveAttribute(
    'aria-current',
    'page',
  );
});

it.each(['/market/', '/MARKET'])(
  'keeps route effects aligned with market content for %s',
  async (pathname) => {
    renderApp([pathname]);

    const heading = screen.getByRole('heading', {
      level: 2,
      name: '行情工作区',
    });
    await waitFor(() => expect(heading).toHaveFocus());

    expect(
      screen.getByRole('region', { name: '行情图表工作区' }),
    ).toBeInTheDocument();
    expect(document.title).toBe('行情工作区 · stock-desk');
    expect(
      screen
        .getAllByRole('status')
        .find((status) => status.textContent === '已进入：行情工作区'),
    ).toBeDefined();
  },
);

it('updates route title, focus, announcement, scroll, and browser history', async () => {
  const user = userEvent.setup();
  renderApp(['/market'], true);

  const marketHeading = await screen.findByRole('heading', {
    level: 2,
    name: '行情工作区',
  });
  await waitFor(() => expect(marketHeading).toHaveFocus());
  expect(document.title).toBe('行情工作区 · stock-desk');

  const formulaLink = screen.getByRole('link', { name: '自定义公式' });
  await user.click(formulaLink);

  const formulaHeading = await screen.findByRole('heading', {
    level: 2,
    name: '公式工作台',
  });
  await waitFor(() => expect(formulaHeading).toHaveFocus());
  expect(formulaLink).toHaveAttribute('aria-current', 'page');
  expect(document.querySelector('.app-shell')).toHaveAttribute(
    'data-workspace',
    'formulas',
  );
  expect(document.title).toBe('自定义公式 · stock-desk');
  expect(screen.getByRole('status')).toHaveTextContent('已进入：自定义公式');
  expect(screen.getAllByText('v0.3.0 · Formula Studio')).not.toHaveLength(0);
  expect(screen.getByText('公式引擎 tdx-v1 已就绪')).toBeVisible();

  await user.click(screen.getByRole('button', { name: '测试返回' }));

  await waitFor(() =>
    expect(
      screen.getByRole('heading', { level: 2, name: '行情工作区' }),
    ).toHaveFocus(),
  );
  expect(screen.getByRole('link', { name: '行情' })).toHaveAttribute(
    'aria-current',
    'page',
  );
  expect(document.querySelector('.app-shell')).toHaveAttribute(
    'data-workspace',
    'default',
  );
  expect(document.title).toBe('行情工作区 · stock-desk');
  expect(window.scrollTo).toHaveBeenCalledWith({
    behavior: 'auto',
    left: 0,
    top: 0,
  });
});

it('renders an explicit focus-managed not-found page', async () => {
  renderApp(['/does-not-exist']);

  const heading = screen.getByRole('heading', {
    level: 2,
    name: '页面未找到',
  });
  await waitFor(() => expect(heading).toHaveFocus());
  expect(document.title).toBe('页面未找到 · stock-desk');
  expect(screen.getByRole('status')).toHaveTextContent('已进入：页面未找到');
  expect(screen.getByRole('link', { name: '返回行情工作区' })).toHaveAttribute(
    'href',
    '/market',
  );
});

it('opens the drawer after the click settles and restores focus on Escape', async () => {
  const user = userEvent.setup();
  renderApp();

  const toggle = screen.getByRole('button', { name: '打开上下文面板' });
  const panel = screen.getByRole('complementary', { name: '上下文状态' });

  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  expect(toggle).toHaveAttribute('aria-controls', 'context-panel');
  expect(panel).toHaveAttribute('data-open', 'false');
  expect(panel).toHaveAttribute('tabindex', '0');

  await user.click(toggle);

  const closeButton = screen.getByRole('button', { name: '关闭上下文面板' });
  await waitFor(() => expect(closeButton).toHaveFocus());
  expect(toggle).toHaveAttribute('aria-expanded', 'true');
  expect(toggle).toHaveAccessibleName('隐藏上下文面板');
  expect(panel).toHaveAttribute('data-open', 'true');

  await user.keyboard('{Escape}');

  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  expect(toggle).toHaveAccessibleName('打开上下文面板');
  expect(panel).toHaveAttribute('data-open', 'false');
  expect(toggle).toHaveFocus();
});

it('supports both the drawer trigger and close button without trapping focus', async () => {
  const user = userEvent.setup();
  renderApp();

  const toggle = screen.getByRole('button', { name: '打开上下文面板' });
  await user.click(toggle);
  const closeButton = screen.getByRole('button', { name: '关闭上下文面板' });
  await waitFor(() => expect(closeButton).toHaveFocus());

  await user.tab();
  expect(closeButton).not.toHaveFocus();

  await user.click(toggle);
  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  expect(toggle).toHaveFocus();

  await user.click(toggle);
  await waitFor(() => expect(closeButton).toHaveFocus());
  await user.click(closeButton);
  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  expect(toggle).toHaveFocus();
});

it('creates isolated drawer state for every App mount', async () => {
  const user = userEvent.setup();
  const firstMount = renderApp();

  await user.click(screen.getByRole('button', { name: '打开上下文面板' }));
  expect(
    screen.getByRole('complementary', { name: '上下文状态' }),
  ).toHaveAttribute('data-open', 'true');
  firstMount.unmount();

  renderApp();

  expect(
    screen.getByRole('button', { name: '打开上下文面板' }),
  ).toHaveAttribute('aria-expanded', 'false');
  expect(
    screen.getByRole('complementary', { name: '上下文状态' }),
  ).toHaveAttribute('data-open', 'false');
});

it('uses one navigation/main landmark and named complementary work areas', () => {
  renderApp();

  expect(screen.getAllByRole('navigation')).toHaveLength(1);
  expect(screen.getAllByRole('main')).toHaveLength(1);
  expect(screen.getAllByRole('complementary')).toHaveLength(3);
  expect(
    screen.getByRole('complementary', { name: '上下文状态' }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole('complementary', { name: '证券选择与股票池' }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole('complementary', { name: '数据证据与快捷操作' }),
  ).toBeInTheDocument();
  expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  expect(screen.getByRole('link', { name: '跳到主要内容' })).toHaveAttribute(
    'href',
    '#main-content',
  );
});

it('reports a fresh persisted Worker heartbeat in both status surfaces', async () => {
  installHealthyFetch();

  renderApp();

  expect(screen.getByText('系统检查中', { exact: true })).toBeInTheDocument();
  expect(
    await screen.findByText('系统正常', { exact: true }),
  ).toBeInTheDocument();
  expect(screen.getByText('API 服务可用', { exact: true })).toBeInTheDocument();
  expect(screen.getByText('任务存储可用', { exact: true })).toBeInTheDocument();
  expect(
    screen.getByText('已检测：API / 任务存储', { exact: true }),
  ).toBeInTheDocument();
  expect(
    screen.getByText('Worker 运行中', { exact: true }),
  ).toBeInTheDocument();
  expect(
    screen.getByText('任务 Worker：运行中', { exact: true }),
  ).toBeInTheDocument();
});

it('reports a stale Worker heartbeat as not detected with its last-seen time', async () => {
  installHealthyFetch([], {
    state: 'not_detected',
    last_seen_at: '2026-07-09T01:59:00Z',
  });

  renderApp();

  expect(
    await screen.findByText('Worker 未检测', { exact: true }),
  ).toBeInTheDocument();
  expect(
    screen.getByText('任务 Worker：未检测', { exact: true }),
  ).toBeInTheDocument();
  expect(screen.getByText(/最近心跳：/u)).toBeInTheDocument();
});

it('prioritizes API offline over a cached running Worker heartbeat', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.endsWith('/health')) {
        return Promise.reject(new TypeError('offline'));
      }
      if (url.endsWith('/tasks/worker-status')) {
        return Promise.resolve(jsonResponse(runningWorkerResponse));
      }
      return Promise.resolve(jsonResponse([]));
    }),
  );

  renderApp();

  expect(
    await screen.findByText('Worker：API 离线', { exact: true }),
  ).toBeInTheDocument();
  expect(
    screen.getByText('任务 Worker：API 离线', { exact: true }),
  ).toBeInTheDocument();
  expect(
    screen.queryByText('Worker 运行中', { exact: true }),
  ).not.toBeInTheDocument();
});

it('treats a Worker transport failure as API offline before health refresh', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.endsWith('/tasks/worker-status')) {
        return Promise.reject(new TypeError('offline'));
      }
      return Promise.resolve(
        url.endsWith('/health')
          ? jsonResponse(healthyResponse)
          : jsonResponse([]),
      );
    }),
  );

  renderApp();

  expect(
    await screen.findByText('Worker：API 离线', { exact: true }),
  ).toBeInTheDocument();
  expect(
    screen.queryByText('Worker 状态不可用', { exact: true }),
  ).not.toBeInTheDocument();
});

it('rejects Worker status protocol extensions without exposing identity', async () => {
  installHealthyFetch([], {
    ...runningWorkerResponse,
    worker_id: 'private-hostname-4242',
  });

  renderApp();

  expect(
    await screen.findByText('Worker 状态不可用', { exact: true }),
  ).toBeInTheDocument();
  expect(screen.queryByText(/private-hostname/u)).not.toBeInTheDocument();
});

it('rejects a running Worker status without a heartbeat timestamp', async () => {
  installHealthyFetch([], { state: 'running', last_seen_at: null });

  renderApp();

  expect(
    await screen.findByText('Worker 状态不可用', { exact: true }),
  ).toBeInTheDocument();
});

it('polls Worker freshness and cancels polling after unmount', async () => {
  vi.useFakeTimers();
  const fetchMock = installHealthyFetch();
  const mounted = renderApp();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(100);
  });
  const workerCallCount = () =>
    fetchMock.mock.calls.filter(([input]) =>
      requestUrl(input).endsWith('/tasks/worker-status'),
    ).length;
  expect(workerCallCount()).toBe(1);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000);
  });
  expect(workerCallCount()).toBe(2);

  mounted.unmount();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  expect(workerCallCount()).toBe(2);
});

it('shows strictly decoded recent task state without raw result context', async () => {
  installHealthyFetch([completedTask]);

  renderApp();

  const task = await screen.findByRole('listitem', {
    name: /demo\.double.*已成功/,
  });
  expect(task).toHaveTextContent('后台任务');
  expect(task).toHaveTextContent('已成功');
  expect(task).toHaveTextContent('进度 100%');
  expect(task).not.toHaveTextContent('结果：42');
  expect(task).toHaveAccessibleName(/demo\.double.*已成功/);
});

it('rejects an explicit raw result at the safe browser boundary', async () => {
  installHealthyFetch([{ ...completedTask, result: { value: null } }]);

  renderApp();

  expect(await screen.findByText('任务列表暂不可用')).toBeInTheDocument();
  expect(screen.queryByText('结果：')).not.toBeInTheDocument();
});

it('labels a running task as unfinished without inventing a result', async () => {
  installHealthyFetch([
    {
      ...completedTask,
      status: 'running',
      progress: 0.5,
      finished_at: null,
      duration_ms: null,
    },
  ]);

  renderApp();

  const task = await screen.findByRole('listitem', {
    name: /demo\.double.*运行中/,
  });
  expect(task).toHaveTextContent('完成 未结束');
  expect(task).not.toHaveTextContent('结果：');
});

it.each([{ nested: true }, ['nested']])(
  'rejects a task with raw complex result.value at the safe boundary: %j',
  async (complexValue) => {
    installHealthyFetch([
      { ...completedTask, result: { value: complexValue } },
    ]);

    renderApp();

    expect(await screen.findByText('任务列表暂不可用')).toBeInTheDocument();
    expect(screen.queryByText('结果：')).not.toBeInTheDocument();
  },
);

it('stays checking when one valid endpoint remains pending', async () => {
  let resolveTasks: ((response: Response) => void) | undefined;
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.endsWith('/health')) {
        return Promise.resolve(jsonResponse(healthyResponse));
      }
      if (url.endsWith('/tasks/worker-status')) {
        return Promise.resolve(jsonResponse(runningWorkerResponse));
      }
      return new Promise<Response>((resolve) => {
        resolveTasks = resolve;
      });
    }),
  );

  renderApp();

  await screen.findByText('API 服务可用', { exact: true });
  const checking = screen.getByText('系统检查中', { exact: true });
  expect(checking.closest('[aria-live="polite"]')).toHaveTextContent(
    '已检测：API / 任务存储',
  );
  expect(
    screen.queryByText('服务降级', { exact: true }),
  ).not.toBeInTheDocument();

  act(() => {
    resolveTasks?.(jsonResponse([]));
  });

  expect(
    await screen.findByText('系统正常', { exact: true }),
  ).toBeInTheDocument();
});

it('times out hung requests, retries once, and enables manual retry', async () => {
  vi.useFakeTimers();
  const { fetchMock } = installPendingFetch();

  renderApp();

  expect(screen.getByText('系统检查中', { exact: true })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '重新检测' })).toBeDisabled();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_100);
  });

  const systemCalls = fetchMock.mock.calls.filter(([input]) =>
    isSystemStatusRequest(requestUrl(input)),
  );
  expect(systemCalls).toHaveLength(6);
  expect(screen.getByText('服务不可用', { exact: true })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '重新检测' })).toBeEnabled();
});

it('keeps manual retry enabled during a background refresh', async () => {
  installHealthyFetch();
  const mounted = renderApp();
  await screen.findByText('系统正常', { exact: true });
  const pending = installPendingFetch();

  act(() => {
    void mounted.queryClient.refetchQueries({
      queryKey: ['system-status'],
    });
  });
  await waitFor(() => expect(pending.fetchMock).toHaveBeenCalledTimes(3));

  expect(screen.getByRole('button', { name: '重新检测' })).toBeEnabled();
});

it('uses a textual degraded state when one endpoint violates the protocol', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) =>
      Promise.resolve(
        requestUrl(input).endsWith('/health')
          ? jsonResponse({ ...healthyResponse, api_version: 'v2' })
          : jsonResponse([]),
      ),
    ),
  );

  renderApp();

  expect(
    await screen.findByText('服务降级', { exact: true }),
  ).toBeInTheDocument();
  expect(
    await screen.findByText('API 服务协议异常', { exact: true }),
  ).toBeInTheDocument();
  expect(screen.getByText('任务存储可用', { exact: true })).toBeInTheDocument();
});

it('reports unavailable without exposing raw network errors', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.reject(new Error('secret upstream token must never be rendered')),
    ),
  );

  renderApp();

  expect(
    await screen.findByText('服务不可用', { exact: true }),
  ).toBeInTheDocument();
  expect(screen.queryByText(/secret upstream token/)).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '重新检测' })).toBeEnabled();
});

it('recovers both endpoint states after a bounded retry and manual recheck', async () => {
  let available = false;
  const releaseResponses: Array<() => void> = [];
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    if (!available) {
      return Promise.reject(new TypeError('offline'));
    }
    return new Promise<Response>((resolve) => {
      const url = requestUrl(input);
      const response = url.endsWith('/health')
        ? jsonResponse(healthyResponse)
        : url.endsWith('/tasks/worker-status')
          ? jsonResponse(runningWorkerResponse)
          : jsonResponse([]);
      releaseResponses.push(() => resolve(response));
    });
  });
  vi.stubGlobal('fetch', fetchMock);
  const user = userEvent.setup();

  renderApp();
  expect(
    await screen.findByText('服务不可用', { exact: true }),
  ).toBeInTheDocument();
  const systemCalls = fetchMock.mock.calls.filter(([input]) =>
    isSystemStatusRequest(requestUrl(input)),
  );
  expect(systemCalls).toHaveLength(6);

  available = true;
  const retry = screen.getByRole('button', { name: '重新检测' });
  await user.click(retry);
  expect(retry).toBeDisabled();
  expect(releaseResponses).toHaveLength(3);
  act(() => {
    for (const release of releaseResponses) {
      release();
    }
  });
  expect(
    await screen.findByText('系统正常', { exact: true }),
  ).toBeInTheDocument();
});

it('shares endpoint queries between topbar and context panel consumers', async () => {
  const fetchMock = installHealthyFetch();

  renderApp();

  await screen.findByText('系统正常', { exact: true });
  const systemCalls = fetchMock.mock.calls
    .map(([input]) => requestUrl(input))
    .filter(isSystemStatusRequest);
  expect(systemCalls).toHaveLength(3);
  expect(systemCalls.sort()).toEqual([
    '/api/health',
    '/api/tasks/worker-status',
    '/api/tasks?view=safe&limit=5',
  ]);
});

it('aborts both in-flight endpoint requests after the final consumer unmounts', async () => {
  const { fetchMock, signals } = installPendingFetch();

  const mounted = renderApp();
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(5));
  expect(signals).toHaveLength(3);

  mounted.unmount();

  await waitFor(() =>
    expect(signals.every((signal) => signal.aborted)).toBe(true),
  );
  await new Promise((resolve) => window.setTimeout(resolve, 20));
  expect(fetchMock).toHaveBeenCalledTimes(5);
});
