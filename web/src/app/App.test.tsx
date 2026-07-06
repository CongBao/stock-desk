import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  MemoryRouter,
  useNavigate,
  type MemoryRouterProps,
} from 'react-router-dom';

import { App } from './App';

const healthyResponse = {
  name: 'stock-desk',
  status: 'ok',
  api_version: 'v1',
};

const completedTask = {
  id: 'task-1',
  kind: 'demo.double',
  status: 'succeeded',
  progress: 1,
  created_at: '2026-07-05T08:00:00Z',
  updated_at: '2026-07-05T08:00:01Z',
  finished_at: '2026-07-05T08:00:01Z',
  result: { value: 42 },
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

function installHealthyFetch(tasks: readonly unknown[] = []) {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = requestUrl(input);
    return Promise.resolve(
      url.includes('/market/pools')
        ? jsonResponse({ items: [], next_cursor: null })
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
          <App />
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

  const formulaHeading = screen.getByRole('heading', {
    level: 2,
    name: '自定义公式',
  });
  await waitFor(() => expect(formulaHeading).toHaveFocus());
  expect(formulaLink).toHaveAttribute('aria-current', 'page');
  expect(document.title).toBe('自定义公式 · stock-desk');
  expect(screen.getByRole('status')).toHaveTextContent('已进入：自定义公式');
  expect(screen.getByText('计划版本 v0.3.0')).toBeInTheDocument();

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

it('reports healthy only after both live endpoints pass strict decoding', async () => {
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
  expect(screen.getAllByText(/Worker 未检测/)).not.toHaveLength(0);
  expect(
    screen.getByText('任务 Worker：未检测', { exact: true }),
  ).toBeInTheDocument();
});

it('shows strictly decoded recent task state and result context', async () => {
  installHealthyFetch([completedTask]);

  renderApp();

  const task = await screen.findByRole('listitem', {
    name: /demo\.double.*已成功/,
  });
  expect(task).toHaveTextContent('demo.double');
  expect(task).toHaveTextContent('已成功');
  expect(task).toHaveTextContent('进度 100%');
  expect(task).toHaveTextContent('结果：42');
  expect(task).toHaveAccessibleName(/demo\.double.*已成功/);
});

it.each([{ nested: true }, ['nested']])(
  'keeps a task with complex result.value without rendering it: %j',
  async (complexValue) => {
    installHealthyFetch([
      { ...completedTask, result: { value: complexValue } },
    ]);

    renderApp();

    const task = await screen.findByRole('listitem', {
      name: /demo\.double.*已成功/,
    });
    expect(task).toHaveTextContent('demo.double');
    expect(task).not.toHaveTextContent('结果：');
    expect(
      await screen.findByText('系统正常', { exact: true }),
    ).toBeInTheDocument();
  },
);

it('stays checking when one valid endpoint remains pending', async () => {
  let resolveTasks: ((response: Response) => void) | undefined;
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      if (requestUrl(input).endsWith('/health')) {
        return Promise.resolve(jsonResponse(healthyResponse));
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

  expect(fetchMock).toHaveBeenCalledTimes(5);
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
  await waitFor(() => expect(pending.fetchMock).toHaveBeenCalledTimes(2));

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
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (!available) {
      throw new TypeError('offline');
    }
    await new Promise((resolve) => window.setTimeout(resolve, 25));
    return requestUrl(input).endsWith('/health')
      ? jsonResponse(healthyResponse)
      : jsonResponse([]);
  });
  vi.stubGlobal('fetch', fetchMock);
  const user = userEvent.setup();

  renderApp();
  expect(
    await screen.findByText('服务不可用', { exact: true }),
  ).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledTimes(5);

  available = true;
  const retry = screen.getByRole('button', { name: '重新检测' });
  await user.click(retry);
  expect(retry).toBeDisabled();
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
    .filter((url) => !url.includes('/market/pools'));
  expect(systemCalls).toHaveLength(2);
  expect(systemCalls.sort()).toEqual(['/api/health', '/api/tasks?limit=5']);
});

it('aborts both in-flight endpoint requests after the final consumer unmounts', async () => {
  const { fetchMock, signals } = installPendingFetch();

  const mounted = renderApp();
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  expect(signals).toHaveLength(2);

  mounted.unmount();

  await waitFor(() =>
    expect(signals.every((signal) => signal.aborted)).toBe(true),
  );
  await new Promise((resolve) => window.setTimeout(resolve, 20));
  expect(fetchMock).toHaveBeenCalledTimes(3);
});
