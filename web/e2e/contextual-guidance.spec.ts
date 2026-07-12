import { expect, test } from '@playwright/test';

test('first-visit guidance persists, stays keyboard-safe, and can be reopened', async ({
  page,
}) => {
  await page.route('**/api/v1/onboarding/state', async (route) => {
    await route.fulfill({
      json: {
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
      },
    });
  });
  let preferences = {
    schema_version: 1,
    revision: 0,
    pages: {},
  } as {
    schema_version: 1;
    revision: number;
    pages: Record<
      string,
      { content_version: number; status: 'completed' | 'dismissed' }
    >;
  };
  let writes = 0;
  await page.route('**/api/v1/guidance/preferences', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ json: preferences });
      return;
    }
    const body = route.request().postDataJSON() as {
      expected_revision: number;
      page: string;
      content_version: number;
      status: 'completed' | 'dismissed';
    };
    expect(body.expected_revision).toBe(preferences.revision);
    writes += 1;
    preferences = {
      ...preferences,
      revision: preferences.revision + 1,
      pages: {
        ...preferences.pages,
        [body.page]: {
          content_version: body.content_version,
          status: body.status,
        },
      },
    };
    await route.fulfill({ json: preferences });
  });

  await page.setViewportSize({ width: 640, height: 450 });
  await page.goto('/market');
  const dialog = page.getByRole('dialog', { name: '行情快速引导' });
  await expect(dialog).toBeVisible();
  await expect(
    page.locator('[data-guidance-target="market-search"]'),
  ).toHaveAttribute('data-guidance-active', 'true');
  const dialogBox = await dialog.boundingBox();
  const targetBox = await page
    .locator('[data-guidance-target="market-search"]')
    .boundingBox();
  expect(dialogBox).not.toBeNull();
  expect(targetBox).not.toBeNull();
  if (dialogBox !== null && targetBox !== null) {
    const overlaps = !(
      dialogBox.y + dialogBox.height <= targetBox.y ||
      targetBox.y + targetBox.height <= dialogBox.y
    );
    expect(overlaps).toBe(false);
  }

  await expect(page.getByRole('button', { name: '下一步' })).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(page.getByRole('button', { name: '跳过引导' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  expect(writes).toBe(1);

  await page.reload();
  await expect(dialog).toBeHidden();
  await page.getByRole('button', { name: '帮助' }).click();
  await page.getByRole('menuitem', { name: '重新打开行情引导' }).click();
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  expect(writes).toBe(1);
});

test('Formula Studio guidance anchors have real geometry at 200 percent effective width', async ({
  page,
}) => {
  await page.route('**/api/v1/onboarding/state', async (route) => {
    await route.fulfill({
      json: {
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
      },
    });
  });
  await page.route('**/api/v1/workspace', async (route) => {
    await route.fulfill({
      json: {
        schema_version: 1,
        revision: 1,
        updated_at: '2026-07-12T06:00:00Z',
        expires_at: '2027-01-08T06:00:00Z',
        restored: true,
        notice: null,
        workspace: {
          current_page: '/formulas',
          instrument: {
            symbol: '000001.SS',
            name: '上证指数',
            exchange: 'SH',
            kind: 'index',
          },
          period: '1d',
          adjustment: 'qfq',
          zoom: { start: 0, end: 100 },
          main_chart: 'candlestick',
          subchart: { kind: 'volume' },
        },
      },
    });
  });
  let revision = 0;
  await page.route('**/api/v1/guidance/preferences', async (route) => {
    if (route.request().method() !== 'GET') revision += 1;
    await route.fulfill({
      json: { schema_version: 1, revision, pages: {} },
    });
  });

  await page.setViewportSize({ width: 683, height: 384 });
  await page.goto('/formulas');
  const dialog = page.getByRole('dialog', { name: '公式快速引导' });
  await expect(dialog).toBeVisible();

  for (const targetName of [
    'formula-editor',
    'formula-parameters',
    'formula-preview',
    'formula-save',
  ]) {
    const target = page.locator(`[data-guidance-target="${targetName}"]`);
    await expect(target).toHaveAttribute('data-guidance-active', 'true');
    const box = await target.boundingBox();
    expect(
      box,
      `${targetName} must expose a drawable guidance box`,
    ).not.toBeNull();
    expect(box?.width ?? 0).toBeGreaterThan(0);
    expect(box?.height ?? 0).toBeGreaterThan(0);
    if (targetName !== 'formula-save') {
      await page.getByRole('button', { name: '下一步' }).click();
    }
  }

  await expect(page.getByRole('button', { name: '完成引导' })).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(page.getByRole('button', { name: '跳过引导' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
});
