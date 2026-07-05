import { expect, test } from '@playwright/test';

const navigationLabels = [
  '行情',
  '自定义公式',
  '策略回测',
  '智能分析',
  '任务中心',
  '设置',
] as const;

test('fresh user sees the live foundation shell and completed demo task', async ({
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
    await expect(page.getByRole('link', { name: label })).toBeVisible();
  }
  await expect(
    page.getByText('布局预览 / 非实时数据', { exact: true }),
  ).toBeVisible();
  await expect(page.getByText('系统正常', { exact: true })).toBeVisible();
  await expect(
    page.getByText('已检测：API / 任务存储', { exact: true }),
  ).toBeVisible();
  await expect(page.getByText('Worker 未检测', { exact: true })).toBeVisible();

  const panelToggle = page.getByRole('button', { name: '打开上下文面板' });
  await panelToggle.click();
  await expect(
    page.getByRole('button', { name: '关闭上下文面板' }),
  ).toBeFocused();
  await expect(page.getByRole('heading', { name: '近期任务' })).toBeVisible();
  await expect(page.getByText('API 服务可用', { exact: true })).toBeVisible();
  await expect(page.getByText('任务存储可用', { exact: true })).toBeVisible();
  await expect(
    page.getByText('任务 Worker：未检测', { exact: true }),
  ).toBeVisible();

  const demoTask = page
    .getByRole('listitem')
    .filter({ hasText: 'demo.double' })
    .first();
  await expect(demoTask).toContainText('demo.double');
  await expect(demoTask).toContainText('已成功', { timeout: 15_000 });
  await expect(demoTask).toContainText('结果：42');

  await page.keyboard.press('Escape');
  await expect(panelToggle).toBeFocused();

  await page.getByRole('link', { name: '自定义公式' }).click();
  await expect(page).toHaveTitle('自定义公式 · stock-desk');
  await expect(
    page.getByRole('heading', { level: 2, name: '自定义公式' }),
  ).toBeFocused();

  await page.goBack();
  await expect(page).toHaveTitle('行情工作区 · stock-desk');
  await expect(
    page.getByRole('heading', { level: 2, name: '行情工作区' }),
  ).toBeFocused();
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
