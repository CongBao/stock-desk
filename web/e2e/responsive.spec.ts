import { expect, test, type Locator, type Page } from '@playwright/test';

const routes = [
  '/market',
  '/formulas',
  '/backtests',
  '/analysis',
  '/tasks',
  '/settings',
] as const;

const viewports = [
  { name: 'wide desktop', width: 1600, height: 900, collapsed: false },
  { name: 'narrow desktop', width: 1100, height: 700, collapsed: true },
  { name: 'tablet landscape', width: 1024, height: 768, collapsed: true },
  { name: 'tablet portrait', width: 768, height: 1024, collapsed: true },
  { name: 'mobile portrait', width: 390, height: 844, collapsed: true },
  {
    name: '200 percent effective viewport',
    width: 640,
    height: 450,
    collapsed: true,
  },
  {
    name: 'short landscape effective viewport',
    width: 640,
    height: 360,
    collapsed: true,
  },
] as const;

async function visibleBox(locator: Locator) {
  if (!(await locator.isVisible())) return null;
  return locator.boundingBox();
}

function intersects(
  first: NonNullable<Awaited<ReturnType<typeof visibleBox>>>,
  second: NonNullable<Awaited<ReturnType<typeof visibleBox>>>,
) {
  return !(
    first.x + first.width <= second.x ||
    second.x + second.width <= first.x ||
    first.y + first.height <= second.y ||
    second.y + second.height <= first.y
  );
}

async function expectNoShellOverlap(page: Page) {
  const rail = await visibleBox(page.locator('.navigation-rail'));
  const workspace = await visibleBox(page.locator('#main-content'));
  const context = await visibleBox(page.locator('#context-panel'));
  expect(rail).not.toBeNull();
  expect(workspace).not.toBeNull();
  if (rail !== null && workspace !== null) {
    expect(intersects(rail, workspace)).toBe(false);
  }
  if (context !== null && workspace !== null) {
    const contextIsOverlay = await page
      .locator('#context-panel')
      .evaluate((element) => {
        const browserGlobal = globalThis as unknown as {
          getComputedStyle: (target: unknown) => { position: string };
        };
        return browserGlobal.getComputedStyle(element).position === 'fixed';
      });
    if (!contextIsOverlay) expect(intersects(context, workspace)).toBe(false);
  }
}

async function expectNoInteractiveControlOverlap(page: Page) {
  const controls = page.locator(
    'a:visible, button:visible, input:visible, select:visible, textarea:visible, [role="tab"]:visible',
  );
  const snapshots = await controls.evaluateAll((elements) =>
    elements.map((element) => {
      const browserElement = element as unknown as {
        getAttribute: (name: string) => string | null;
        getBoundingClientRect: () => {
          height: number;
          width: number;
          x: number;
          y: number;
        };
        textContent: string | null;
      };
      const rect = browserElement.getBoundingClientRect();
      return {
        box: {
          x: rect.x,
          y: rect.y,
          width: rect.width,
          height: rect.height,
        },
        label:
          browserElement.getAttribute('aria-label') ??
          browserElement.textContent?.trim() ??
          '',
      };
    }),
  );

  for (let first = 0; first < snapshots.length; first += 1) {
    const firstControl = snapshots[first];
    if (firstControl === undefined) continue;
    for (let second = first + 1; second < snapshots.length; second += 1) {
      const secondControl = snapshots[second];
      if (secondControl === undefined) continue;
      expect(
        intersects(firstControl.box, secondControl.box),
        `interactive controls overlap: ${firstControl.label || first} / ${secondControl.label || second}`,
      ).toBe(false);
    }
  }
}

async function expectNavigationIsOperable(page: Page) {
  const navigation = page.getByRole('navigation', { name: '主导航' });
  await expect(navigation).toHaveCSS('overflow-y', 'auto');

  const targets = page.locator(
    '.navigation-toggle:visible, .primary-navigation .nav-link:visible',
  );
  const targetCount = await targets.count();
  expect(targetCount).toBeGreaterThan(1);
  for (let index = 0; index < targetCount; index += 1) {
    const target = targets.nth(index);
    const box = await target.boundingBox();
    expect(box, `navigation target ${String(index)} has no box`).not.toBeNull();
    if (box !== null) {
      expect(box.width).toBeGreaterThanOrEqual(44);
      expect(box.height).toBeGreaterThanOrEqual(44);
    }
  }
}

for (const viewport of viewports) {
  test.describe(viewport.name, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    for (const route of routes) {
      test(`${route} has bounded non-overlapping layout`, async ({ page }) => {
        await page.goto(route);
        await expect(page.locator('#main-content')).toBeVisible();
        await expect(page.locator('.app-shell')).toHaveAttribute(
          'data-navigation-collapsed',
          String(viewport.collapsed),
        );

        const overflow = await page.evaluate(() => {
          const browserGlobal = globalThis as unknown as {
            document: {
              documentElement: { clientWidth: number; scrollWidth: number };
            };
          };
          return {
            clientWidth: browserGlobal.document.documentElement.clientWidth,
            scrollWidth: browserGlobal.document.documentElement.scrollWidth,
          };
        });
        expect(overflow.scrollWidth).toBeLessThanOrEqual(
          overflow.clientWidth + 1,
        );
        await expectNoShellOverlap(page);
        await expectNoInteractiveControlOverlap(page);
        await expectNavigationIsOperable(page);
      });
    }
  });
}

test('navigation auto-collapses only when crossing the narrow breakpoint', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1100, height: 700 });
  await page.goto('/market');
  const shell = page.locator('.app-shell');
  const toggle = page.getByRole('button', { name: '展开主导航' });
  await expect(shell).toHaveAttribute('data-navigation-collapsed', 'true');

  await toggle.click();
  await expect(shell).toHaveAttribute('data-navigation-collapsed', 'false');
  await page.setViewportSize({ width: 1000, height: 700 });
  await expect(shell).toHaveAttribute('data-navigation-collapsed', 'false');

  await page.setViewportSize({ width: 1300, height: 700 });
  await expect(shell).toHaveAttribute('data-navigation-collapsed', 'false');
  await page.setViewportSize({ width: 1100, height: 700 });
  await expect(shell).toHaveAttribute('data-navigation-collapsed', 'true');
});

test('collapsed navigation renders icons without textual abbreviations', async ({
  page,
}) => {
  await page.setViewportSize({ width: 900, height: 700 });
  await page.goto('/market');
  const links = page
    .getByRole('navigation', { name: '主导航' })
    .getByRole('link');
  const count = await links.count();
  expect(count).toBeGreaterThan(0);
  for (let index = 0; index < count; index += 1) {
    const link = links.nth(index);
    await expect(link.locator('.nav-icon svg')).toBeVisible();
    await expect(link.locator('.nav-label')).toBeHidden();
    await expect(link).toHaveAccessibleName(/\S/u);
  }
});
