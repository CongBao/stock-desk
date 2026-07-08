import { expect, test, type Page, type Route } from '@playwright/test';

const taskId = '11111111-1111-4111-8111-111111111111';
const secondTaskId = '22222222-2222-4222-8222-222222222222';
const runId = '33333333-3333-4333-8333-333333333333';
const eventId = '44444444-4444-4444-8444-444444444444';
const secret = '503-SENTINEL-DO-NOT-RENDER';

function task(
  status:
    'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' = 'running',
) {
  const terminal =
    status === 'succeeded' || status === 'failed' || status === 'cancelled';
  return {
    id: taskId,
    correlation_id: taskId,
    kind: 'backtest.run',
    status,
    progress: status === 'succeeded' ? 1 : 0.4,
    payload: { token: 'PAYLOAD-SECRET' },
    result: { private: 'RESULT-SECRET' },
    error: { raw_exception: 'ERROR-SECRET' },
    cancel_requested: false,
    worker_id: 'WORKER-SECRET',
    created_at: '2026-07-08T00:00:00Z',
    updated_at: terminal ? '2026-07-08T00:00:05Z' : '2026-07-08T00:00:02Z',
    started_at: status === 'queued' ? null : '2026-07-08T00:00:01Z',
    finished_at: terminal ? '2026-07-08T00:00:05Z' : null,
    duration_ms: terminal ? 4_000 : null,
    presentation: {
      label: '股票池回测',
      stage: status === 'succeeded' ? 'completed' : 'executing',
      processed: status === 'succeeded' ? 5 : 2,
      total: 5,
      failed: 1,
      target: { type: 'backtest_run', id: runId },
    },
  };
}

const analysisTask = {
  ...task('succeeded'),
  id: secondTaskId,
  correlation_id: secondTaskId,
  kind: 'analysis.run',
  presentation: {
    label: '智能分析',
    stage: null,
    processed: null,
    total: null,
    failed: null,
    target: null,
  },
};

const metrics = {
  total: 12,
  by_status: { queued: 1, running: 2, succeeded: 6, failed: 2, cancelled: 1 },
  failure_count: 2,
  completed_count: 9,
  average_duration_ms: 500,
  min_duration_ms: 100,
  max_duration_ms: 900,
};

const events = [
  {
    id: eventId,
    task_id: taskId,
    correlation_id: taskId,
    event_name: 'task.progressed',
    level: 'info',
    progress: 0.4,
    detail: { private: 'EVENT-SECRET' },
    occurred_at: '2026-07-08T00:00:02Z',
    presentation: {
      label: '已处理回测标的',
      stage: 'executing',
      processed: 2,
      total: 5,
      failed: 1,
    },
  },
];

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

async function installTaskStubs(
  page: Page,
  options: {
    readonly lifecycle?: boolean;
    readonly failSecondList?: boolean;
    readonly trackCancel?: { count: number };
  } = {},
) {
  let centerLists = 0;
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (!url.pathname.startsWith('/api/')) {
      await route.fallback();
      return;
    }
    if (url.pathname === '/api/health') {
      await json(route, {
        name: 'stock-desk',
        status: 'ok',
        api_version: 'v1',
      });
      return;
    }
    if (
      url.pathname === '/api/tasks' &&
      url.searchParams.get('limit') === '5'
    ) {
      await json(route, [task('running')]);
      return;
    }
    if (
      url.pathname === '/api/tasks' &&
      url.searchParams.get('limit') === '100'
    ) {
      centerLists += 1;
      if (options.failSecondList && centerLists >= 3) {
        await json(
          route,
          { code: 'storage_unavailable', diagnostic: secret },
          503,
        );
        return;
      }
      await json(route, [
        options.lifecycle && centerLists >= 3 ? task('succeeded') : task(),
        analysisTask,
      ]);
      return;
    }
    if (url.pathname === '/api/tasks/metrics') {
      await json(route, metrics);
      return;
    }
    if (url.pathname === `/api/tasks/${taskId}/events`) {
      await json(route, events);
      return;
    }
    if (url.pathname === `/api/tasks/${secondTaskId}/events`) {
      await json(route, []);
      return;
    }
    if (url.pathname === `/api/tasks/${taskId}/cancel`) {
      if (options.trackCancel) options.trackCancel.count += 1;
      await json(route, { ...task('running'), cancel_requested: true });
      return;
    }
    if (url.pathname === `/api/tasks/${taskId}`) {
      await json(
        route,
        options.lifecycle && centerLists >= 3 ? task('succeeded') : task(),
      );
      return;
    }
    if (url.pathname === `/api/tasks/${secondTaskId}`) {
      await json(route, analysisTask);
      return;
    }
    await json(route, []);
  });
}

async function noHorizontalOverflow(page: Page) {
  expect(
    await page.evaluate(() => {
      const browserGlobal = globalThis as unknown as {
        document: {
          documentElement: { scrollWidth: number; clientWidth: number };
        };
      };
      const root = browserGlobal.document.documentElement;
      return root.scrollWidth <= root.clientWidth;
    }),
  ).toBe(true);
}

test('shows a deterministic lifecycle while shell and center use distinct list bounds', async ({
  page,
}) => {
  const requests: string[] = [];
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  page.on('request', (request) => {
    if (request.url().includes('/api/tasks?')) requests.push(request.url());
  });
  await installTaskStubs(page, { lifecycle: true });
  await page.goto('/tasks');
  await page.waitForTimeout(250);
  expect(pageErrors).toEqual([]);

  await expect(page.getByRole('heading', { name: '任务中心' })).toBeVisible();
  await expect(
    page.getByRole('progressbar', { name: '任务总体进度' }),
  ).toHaveAttribute('aria-valuenow', '40');
  await expect(page.getByText('已处理回测标的')).toBeVisible();
  await expect(
    page.getByRole('link', { name: '打开回测报告' }),
  ).toHaveAttribute('href', `/backtests/${runId}`);
  await expect(page.getByText('已完成', { exact: true }).last()).toBeVisible({
    timeout: 6_000,
  });
  expect(requests.some((url) => url.endsWith('/api/tasks?limit=5'))).toBe(true);
  expect(requests.some((url) => url.endsWith('/api/tasks?limit=100'))).toBe(
    true,
  );
  await expect(
    page.getByText(
      /PAYLOAD-SECRET|RESULT-SECRET|ERROR-SECRET|EVENT-SECRET|WORKER-SECRET/u,
    ),
  ).toHaveCount(0);
});

test('keyboard selection and cancellation send one POST and announce reflection', async ({
  page,
}) => {
  const cancellation = { count: 0 };
  await installTaskStubs(page, { trackCancel: cancellation });
  await page.goto('/tasks');
  const analysis = page.getByRole('button', { name: /智能分析/u });
  await analysis.focus();
  await page.keyboard.press('Enter');
  await expect(analysis).toHaveAttribute('aria-current', 'true');
  const backtest = page.getByRole('button', { name: /股票池回测/u }).first();
  await backtest.focus();
  await page.keyboard.press('Enter');
  await page.getByRole('button', { name: '取消任务' }).click();
  await expect(page.getByRole('button', { name: '已请求取消' })).toBeDisabled();
  await page.waitForTimeout(2_500);
  expect(cancellation.count).toBe(1);
  await expect(page.getByTestId('task-live-status')).toHaveAttribute(
    'aria-live',
    'polite',
  );
});

test('a safe 503 keeps stale state and never renders diagnostic secrets', async ({
  page,
}) => {
  await installTaskStubs(page, { failSecondList: true });
  await page.goto('/tasks');
  await expect(
    page.getByRole('button', { name: /股票池回测/u }).first(),
  ).toBeVisible();
  await page.getByRole('button', { name: '刷新任务' }).click();
  await expect(page.getByRole('alert')).toContainText('任务列表刷新失败');
  await expect(
    page.getByRole('button', { name: /股票池回测/u }).first(),
  ).toBeVisible();
  await expect(page.getByText(secret)).toHaveCount(0);
});

for (const viewport of [
  { name: 'wide', width: 1600, height: 900 },
  { name: 'desktop', width: 1366, height: 768 },
  { name: 'tablet landscape', width: 1024, height: 768 },
  { name: 'tablet portrait', width: 768, height: 1024 },
  { name: 'mobile', width: 390, height: 844 },
  { name: '200 percent zoom equivalent', width: 800, height: 450 },
]) {
  test(`${viewport.name} keeps task controls reachable without overlap or document overflow`, async ({
    page,
  }) => {
    await page.setViewportSize({
      width: viewport.width,
      height: viewport.height,
    });
    await installTaskStubs(page);
    await page.goto('/tasks');
    await expect(page.getByRole('button', { name: '刷新任务' })).toBeVisible();
    await expect(page.getByLabel('状态筛选')).toBeVisible();
    await expect(page.getByLabel('类型筛选')).toBeVisible();
    await expect(page.getByRole('button', { name: '取消任务' })).toBeVisible();
    await expect(
      page.getByRole('complementary', { name: '上下文状态' }),
    ).toBeHidden();
    await noHorizontalOverflow(page);
    const [rail, workspace] = await Promise.all([
      page.locator('.navigation-rail').boundingBox(),
      page.locator('main.workspace').boundingBox(),
    ]);
    expect(rail).not.toBeNull();
    expect(workspace).not.toBeNull();
    if (rail && workspace && viewport.width > 760) {
      expect(rail.x + rail.width).toBeLessThanOrEqual(workspace.x + 1);
    }
    expect(
      await page.locator('.task-center-layout').evaluate((element) => {
        const layout = element as unknown as {
          scrollWidth: number;
          clientWidth: number;
        };
        return layout.scrollWidth <= layout.clientWidth;
      }),
    ).toBe(true);
  });
}
