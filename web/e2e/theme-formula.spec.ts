import { expect, test } from '@playwright/test';

test('Formula Studio keeps theme, focus and layout usable through 200% scaling', async ({
  page,
}) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 683, height: 384 });
  await page.goto('/formulas');

  const theme = page.getByRole('combobox', { name: '界面主题' });
  await expect(theme).toHaveValue('system');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

  await theme.selectOption('light');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await expect(page.locator('html')).toHaveAttribute(
    'data-theme-preference',
    'light',
  );

  await theme.selectOption('dark');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.reload();
  await expect(theme).toHaveValue('dark');

  await theme.selectOption('system');
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.emulateMedia({ colorScheme: 'light' });
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

  const demo = page.getByRole('button', { name: '先看只读演示' });
  if (await demo.isVisible()) await demo.click();

  const editor = page.getByRole('textbox', { name: '通达信公式代码' });
  await editor.focus();
  await expect(editor).toBeFocused();
  await expect(editor).toHaveCSS('outline-style', 'solid');

  for (const viewport of [
    { width: 1366, height: 768 },
    { width: 683, height: 384 },
  ]) {
    await page.setViewportSize(viewport);
    const bounds = await page.evaluate(() => ({
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
    expect(bounds.scroll).toBeLessThanOrEqual(bounds.client + 1);
  }
});
