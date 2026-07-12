import type { Page } from '@playwright/test';
import { readFileSync } from 'node:fs';

import { expect, test } from './fixtures';
import { mockCompletedGuidance } from './guidanceMocks';

const backendBarsResponseBody = readFileSync(
  new URL(
    '../src/features/market/fixtures/backend-bars-response.json',
    import.meta.url,
  ),
  'utf8',
);
const backendInstrumentsResponseBody = readFileSync(
  new URL(
    '../src/features/market/fixtures/backend-instruments-response.json',
    import.meta.url,
  ),
  'utf8',
);
const backendPresetPoolResponse = JSON.parse(
  readFileSync(
    new URL(
      '../src/features/market/fixtures/backend-preset-pool-response.json',
      import.meta.url,
    ),
    'utf8',
  ),
) as { readonly page: unknown; readonly detail: unknown };

const navigationLabels = [
  '行情',
  '自定义公式',
  '策略回测',
  '智能分析',
  '任务中心',
  '设置',
] as const;

test.beforeEach(async ({ page }) => {
  await mockCompletedGuidance(page);
});

async function pageHasHorizontalOverflow(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const browserGlobal = globalThis as unknown as {
      document: {
        documentElement: { clientWidth: number; scrollWidth: number };
      };
    };
    const root = browserGlobal.document.documentElement;
    return root.scrollWidth > root.clientWidth;
  });
}

test('returning user sees the live foundation shell and completed demo task', async ({
  page,
  request,
}) => {
  await expect
    .poll(async () => (await request.get('/api/health')).status(), {
      message: 'API health should become ready before task creation',
      timeout: 15_000,
      intervals: [100, 250, 500],
    })
    .toBe(200);
  const created = await request.post('/api/tasks', {
    data: { kind: 'demo.double', payload: { value: 21 } },
  });
  expect(created.status()).toBe(201);

  await page.setViewportSize({ width: 1024, height: 900 });
  await page.goto('/market');

  await expect(
    page.getByRole('heading', { level: 1, name: 'stock-desk' }),
  ).toBeVisible();
  for (const label of navigationLabels) {
    await expect(
      page.getByRole('link', { name: label, exact: true }),
    ).toBeVisible();
  }
  await expect(
    page.getByRole('complementary', { name: '自选与最近访问' }),
  ).toBeVisible();
  await expect(page.getByRole('button', { name: '打开股票池' })).toBeVisible();
  await expect(
    page.getByRole('region', { name: '行情图表工作区' }),
  ).toBeVisible();
  await expect(
    page.getByRole('complementary', { name: '数据证据与快捷操作' }),
  ).toBeVisible();
  await expect(
    page.getByText('上证指数 · 000001.SS', { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole('region', { name: '公式结果副图' }),
  ).toContainText('当前不生成指标线或交易信号');
  await expect(page.getByText('系统正常', { exact: true })).toBeVisible();
  await expect(
    page.getByText('已检测：API / 任务存储', { exact: true }),
  ).toBeVisible();
  await expect(page.getByText('Worker 运行中', { exact: true })).toBeVisible();

  const panelToggle = page.getByRole('button', { name: '打开上下文面板' });
  await panelToggle.click();
  await expect(
    page.getByRole('button', { name: '关闭上下文面板' }),
  ).toBeFocused();
  await expect(page.getByRole('heading', { name: '近期任务' })).toBeVisible();
  await expect(page.getByText('API 服务可用', { exact: true })).toBeVisible();
  await expect(page.getByText('任务存储可用', { exact: true })).toBeVisible();
  await expect(
    page.getByText('任务 Worker：运行中', { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/最近心跳：/u)).toBeVisible();

  const demoTask = page
    .getByRole('listitem', { name: /demo\.double/u })
    .first();
  await expect(demoTask).toHaveAccessibleName(/demo\.double.*已成功/u, {
    timeout: 15_000,
  });
  await expect(demoTask).toContainText('后台任务');
  await expect(demoTask).toContainText('进度 100%');
  await expect(demoTask).not.toContainText('结果：');

  await page.keyboard.press('Escape');
  await expect(panelToggle).toBeFocused();

  await page.getByRole('link', { name: '自定义公式' }).click();
  await expect(page).toHaveTitle('自定义公式 · stock-desk');
  await expect(
    page.getByRole('heading', { level: 2, name: '公式工作台' }),
  ).toBeFocused();

  await page.goBack();
  await expect(page).toHaveTitle('行情工作区 · stock-desk');
  await expect(
    page.getByRole('heading', { level: 2, name: '行情工作区' }),
  ).toBeFocused();
});

test('desktop market terminal keeps all three work areas aligned without overflow', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/market');

  const left = page.getByRole('complementary', {
    name: '自选与最近访问',
  });
  const center = page.getByRole('region', { name: '行情图表工作区' });
  const right = page.getByRole('complementary', {
    name: '数据证据与快捷操作',
  });
  await expect(left).toBeVisible();
  await expect(center).toBeVisible();
  await expect(right).toBeVisible();
  await expect(
    center.getByRole('button', { name: '打开股票池' }),
  ).toBeVisible();

  for (const width of [1440, 1366, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    const [leftBox, centerBox, rightBox] = await Promise.all([
      left.boundingBox(),
      center.boundingBox(),
      right.boundingBox(),
    ]);
    expect(leftBox).not.toBeNull();
    expect(centerBox).not.toBeNull();
    expect(rightBox).not.toBeNull();
    if (!leftBox || !centerBox || !rightBox) {
      throw new Error(
        `Expected every ${width}px work area to have a bounding box`,
      );
    }
    expect(leftBox.x).toBeLessThan(centerBox.x);
    expect(centerBox.x).toBeLessThan(rightBox.x);
    expect(Math.abs(leftBox.y - centerBox.y)).toBeLessThanOrEqual(1);
    expect(Math.abs(centerBox.y - rightBox.y)).toBeLessThanOrEqual(1);
    expect(await pageHasHorizontalOverflow(page)).toBe(false);
  }
});

test('cached market canvas survives a failed background refresh and recovers once', async ({
  page,
  context,
}) => {
  let barsShouldFail = false;
  await page.route('**/api/market/instruments?**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: backendInstrumentsResponseBody,
    });
  });
  await page.route('**/api/market/pools?**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], next_cursor: null }),
    });
  });
  await page.route('**/api/market/bars?**', async (route) => {
    if (barsShouldFail) {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'test-only refresh failure' }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: backendBarsResponseBody,
    });
  });

  await page.goto('/market');
  const search = page.getByRole('combobox', { name: '搜索证券' });
  await search.fill('浦发');
  await page
    .getByRole('option', { name: '浦发银行 600000.SH', exact: true })
    .click();
  const chartCanvas = page.locator('.market-chart-canvas canvas');
  await expect(chartCanvas).toHaveCount(1);

  barsShouldFail = true;
  await context.setOffline(true);
  await context.setOffline(false);
  await expect(
    page.getByRole('alert').filter({ hasText: '行情数据读取失败' }),
  ).toBeVisible();
  await expect(chartCanvas).toHaveCount(1);

  barsShouldFail = false;
  await context.setOffline(true);
  await context.setOffline(false);
  await expect(
    page.getByRole('alert').filter({ hasText: '行情数据读取失败' }),
  ).toHaveCount(0);
  await expect(chartCanvas).toHaveCount(1);
});

test('market hashes render through the bounded fallback without crypto.subtle', async ({
  page,
}) => {
  await page.addInitScript(() => {
    const cryptoPrototype = Object.getPrototypeOf(globalThis.crypto) as object;
    Object.defineProperty(cryptoPrototype, 'subtle', {
      configurable: true,
      get: () => undefined,
    });
  });
  await page.route('**/api/market/instruments?**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: backendInstrumentsResponseBody,
    });
  });
  await page.route('**/api/market/pools?**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(backendPresetPoolResponse.page),
    });
  });
  await page.route('**/api/market/pools/*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(backendPresetPoolResponse.detail),
    });
  });
  await page.route('**/api/market/bars?**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: backendBarsResponseBody,
    });
  });

  await page.goto('/market');
  expect(await page.evaluate(() => globalThis.crypto.subtle)).toBeUndefined();
  await page.getByRole('button', { name: '打开股票池' }).click();
  await page.getByRole('button', { name: /全量 A 股/u }).click();
  await expect(
    page.getByRole('button', { name: /浦发银行.*600000\.SH/u }),
  ).toBeVisible();
  await page.getByRole('button', { name: '关闭股票池' }).click();

  const search = page.getByRole('combobox', { name: '搜索证券' });
  await search.fill('浦发');
  await page
    .getByRole('option', { name: '浦发银行 600000.SH', exact: true })
    .click();
  await expect(page.locator('.market-chart-canvas canvas')).toHaveCount(1);
  await expect(page.getByText('股票池暂不可用', { exact: true })).toHaveCount(
    0,
  );
  await expect(
    page.getByText('股票池详情暂不可用', { exact: true }),
  ).toHaveCount(0);
});

test('degraded health can recover through the accessible manual retry', async ({
  page,
}) => {
  let healthAvailable = false;
  await page.route('**/api/health', async (route) => {
    if (!healthAvailable) {
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'test-only outage' }),
      });
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
    await route.continue();
  });

  await page.setViewportSize({ width: 1024, height: 900 });
  await page.goto('/market');
  await expect(page.getByText('服务降级', { exact: true })).toBeVisible();

  await page.getByRole('button', { name: '打开上下文面板' }).click();
  await expect(
    page.getByText('API 服务暂不可用', { exact: true }),
  ).toBeVisible();
  await expect(page.getByText('任务存储可用', { exact: true })).toBeVisible();

  const retry = page.getByRole('button', { name: '重新检测' });
  healthAvailable = true;
  await retry.click();
  await expect(retry).toBeDisabled();
  await expect(page.getByText('系统正常', { exact: true })).toBeVisible();
});
