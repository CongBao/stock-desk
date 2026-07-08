import { expect, test } from '@playwright/test';

test('market terminal preserves navy structure and rise-fall colors with three aligned regions', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto('/market');

  const left = page.getByRole('complementary', {
    name: '证券选择与股票池',
  });
  const center = page.getByRole('region', { name: '行情图表工作区' });
  const right = page.getByRole('complementary', {
    name: '数据证据与快捷操作',
  });
  await expect(left).toBeVisible();
  await expect(center).toBeVisible();
  await expect(right).toBeVisible();

  const [leftBox, centerBox, rightBox] = await Promise.all([
    left.boundingBox(),
    center.boundingBox(),
    right.boundingBox(),
  ]);
  expect(leftBox).not.toBeNull();
  expect(centerBox).not.toBeNull();
  expect(rightBox).not.toBeNull();
  expect(leftBox?.x ?? 0).toBeLessThan(centerBox?.x ?? 0);
  expect(centerBox?.x ?? 0).toBeLessThan(rightBox?.x ?? 0);
  expect(Math.abs((leftBox?.y ?? 0) - (centerBox?.y ?? 0))).toBeLessThan(2);
  expect(Math.abs((centerBox?.y ?? 0) - (rightBox?.y ?? 0))).toBeLessThan(2);

  const palette = await page.evaluate(() => {
    const browserGlobal = globalThis as unknown as {
      document: { readonly documentElement: unknown; readonly body: unknown };
      getComputedStyle(element: unknown): {
        readonly backgroundColor: string;
        getPropertyValue(name: string): string;
      };
    };
    const styles = browserGlobal.getComputedStyle(
      browserGlobal.document.documentElement,
    );
    const body = browserGlobal.getComputedStyle(browserGlobal.document.body);
    return {
      surface: styles.getPropertyValue('--surface-0').trim(),
      rise: styles.getPropertyValue('--rise').trim(),
      fall: styles.getPropertyValue('--fall').trim(),
      background: body.backgroundColor,
    };
  });
  expect(palette.surface).toBe('#07111f');
  expect(palette.rise).toBe('#ef4444');
  expect(palette.fall).toBe('#22c55e');
  expect(palette.background).toBe('rgb(7, 17, 31)');

  await expect(page.locator('[data-direction="rise"]').first()).toContainText(
    '上涨（红）',
  );
  await expect(page.locator('[data-direction="fall"]').first()).toContainText(
    '下跌（绿）',
  );
});
