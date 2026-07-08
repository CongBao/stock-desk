import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

const coreRoutes = [
  '/market',
  '/formulas',
  '/backtests',
  '/analysis',
  '/tasks',
  '/settings',
] as const;

async function waitForWorkspace(page: Page) {
  await expect(page.locator('#main-content')).toBeVisible();
  await expect(
    page.locator('#main-content h1, #main-content h2').first(),
  ).toBeVisible();
}

for (const route of coreRoutes) {
  test(`${route} has no serious or critical accessibility violations`, async ({
    page,
  }) => {
    await page.goto(route);
    await waitForWorkspace(page);

    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
      .analyze();

    expect(
      results.violations.filter((violation) =>
        ['critical', 'serious'].includes(violation.impact ?? ''),
      ),
    ).toEqual([]);
  });
}

test('skip link and collapsed icon navigation remain keyboard operable', async ({
  page,
}) => {
  await page.setViewportSize({ width: 900, height: 700 });
  await page.goto('/market');
  await waitForWorkspace(page);

  const skipLink = page.getByRole('link', { name: '跳到主要内容' });
  await skipLink.focus();
  await expect(skipLink).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.locator('#main-content')).toBeFocused();

  const shell = page.locator('.app-shell');
  await expect(shell).toHaveAttribute('data-navigation-collapsed', 'true');
  const navigation = page.getByRole('navigation', { name: '主导航' });
  const links = navigation.getByRole('link');
  await expect(links.first()).toHaveAccessibleName(/行情/u);
  await expect(links.first().locator('.nav-label')).toBeHidden();

  const toggle = page.getByRole('button', { name: '展开主导航' });
  await toggle.focus();
  await page.keyboard.press('Enter');
  await expect(
    page.getByRole('button', { name: '收起主导航' }),
  ).toHaveAttribute('aria-expanded', 'true');
  await expect(links.first().locator('.nav-label')).toBeVisible();
});

test('reduced motion preference disables nonessential transitions', async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/market');
  await waitForWorkspace(page);

  const transitionDurations = await page
    .locator('.navigation-rail, .context-panel, button, a')
    .evaluateAll((elements) => {
      const browserGlobal = globalThis as unknown as {
        getComputedStyle: (element: unknown) => { transitionDuration: string };
      };
      return elements.map(
        (element) => browserGlobal.getComputedStyle(element).transitionDuration,
      );
    });
  expect(
    transitionDurations.every(
      (duration) => Number.parseFloat(duration) <= 0.00001,
    ),
  ).toBe(true);
});
