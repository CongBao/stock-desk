import { expect, test, type Page, type Route } from '@playwright/test';

const navigationState = {
  schema_version: 1,
  revision: 0,
  watchlist: [],
  recent: [],
  notice: null,
};

const completedOnboarding = {
  schema_version: 1,
  revision: 1,
  current_step: 'completed',
  status: 'completed',
  source: {
    id: 'stock_desk_demo',
    label: 'Stock Desk 演示快照',
    catalog_manifest_record_id: `sha256:${'a'.repeat(64)}`,
    catalog_dataset_version: `sha256:${'a'.repeat(64)}`,
    data_cutoff: '2026-07-11T07:00:00Z',
  },
  instrument: {
    symbol: '000001.SS',
    name: '上证指数',
    exchange: 'SH',
    instrument_kind: 'index',
  },
  sync: {
    status: 'verified',
    provider_id: 'stock_desk_demo',
    manifest_record_id: `sha256:${'a'.repeat(64)}`,
    dataset_version: `sha256:${'a'.repeat(64)}`,
    data_cutoff: '2026-07-11T07:00:00Z',
    row_count: 240,
  },
  error: null,
  demo_mode: false,
};

async function mockNavigation(page: Page) {
  await page.route('**/api/v1/onboarding/state', async (route: Route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(completedOnboarding),
    });
  });
  await page.route('**/api/v1/market/navigation', async (route: Route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON() as {
        watchlist: unknown[];
        recent: unknown[];
      };
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          ...navigationState,
          revision: 1,
          watchlist: body.watchlist,
          recent: body.recent,
        }),
      });
      return;
    }
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(navigationState),
    });
  });
}

async function expectNoHorizontalOverflow(page: Page) {
  const dimensions = await page.evaluate(() => ({
    client: (
      globalThis as unknown as {
        document: { documentElement: { clientWidth: number } };
      }
    ).document.documentElement.clientWidth,
    scroll: (
      globalThis as unknown as {
        document: { documentElement: { scrollWidth: number } };
      }
    ).document.documentElement.scrollWidth,
  }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.client + 1);
}

for (const viewport of [
  { name: '1366x768', width: 1366, height: 768, narrow: false },
  {
    name: '200 percent effective viewport',
    width: 683,
    height: 384,
    narrow: true,
  },
  { name: '320px narrow window', width: 320, height: 720, narrow: true },
] as const) {
  test(`${viewport.name} keeps Market navigation operable without overlap`, async ({
    page,
  }) => {
    await page.setViewportSize({
      width: viewport.width,
      height: viewport.height,
    });
    await mockNavigation(page);
    await page.goto('/market');

    const search = page.getByRole('combobox', {
      name: '搜索证券',
      exact: true,
    });
    await expect(search).toBeVisible();
    await expect(search).toBeFocused();
    await expect(
      page.getByRole('button', { name: '打开股票池' }),
    ).toBeVisible();
    await expectNoHorizontalOverflow(page);

    const rail = page.getByRole('complementary', {
      name: '自选与最近访问',
    });
    const center = page.getByRole('region', { name: '行情图表工作区' });
    const toggle = page.getByRole('button', {
      name: viewport.narrow ? '展开自选与最近访问' : '收起自选与最近访问',
    });
    await expect(toggle.locator('svg')).toBeVisible();
    await expect(toggle).not.toContainText(/ZX|ZJ|WATCH/u);

    if (viewport.narrow) {
      await toggle.click();
      await expect(
        page.getByRole('button', { name: '收起自选与最近访问' }),
      ).toBeVisible();
      const railBox = await rail.boundingBox();
      const centerBox = await center.boundingBox();
      expect(railBox).not.toBeNull();
      expect(centerBox).not.toBeNull();
      if (railBox !== null && centerBox !== null) {
        expect(railBox.y + railBox.height).toBeLessThanOrEqual(centerBox.y + 1);
      }
      await page.keyboard.press('Escape');
      const collapsedToggle = page.getByRole('button', {
        name: '展开自选与最近访问',
      });
      await expect(collapsedToggle).toBeVisible();
      await expect(collapsedToggle).toBeFocused();
    }

    await expectNoHorizontalOverflow(page);
  });
}
