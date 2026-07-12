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
