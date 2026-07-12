import type { Locator, Page } from '@playwright/test';

import { expect, test } from './fixtures';

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
    elements.flatMap((element) => {
      const browserElement = element as unknown as {
        getAttribute: (name: string) => string | null;
        getBoundingClientRect: () => {
          bottom: number;
          height: number;
          left: number;
          right: number;
          top: number;
          width: number;
          x: number;
          y: number;
        };
        parentElement: unknown;
        textContent: string | null;
      };
      const browserGlobal = globalThis as unknown as {
        getComputedStyle: (target: unknown) => {
          overflowX: string;
          overflowY: string;
        };
      };
      const clipsOverflow = (value: string) =>
        value === 'auto' ||
        value === 'clip' ||
        value === 'hidden' ||
        value === 'scroll';
      const rect = browserElement.getBoundingClientRect();
      let left = rect.left;
      let right = rect.right;
      let top = rect.top;
      let bottom = rect.bottom;
      let ancestor = browserElement.parentElement as null | {
        clientHeight: number;
        clientLeft: number;
        clientTop: number;
        clientWidth: number;
        getBoundingClientRect: () => {
          left: number;
          top: number;
        };
        parentElement: unknown;
      };

      while (ancestor !== null) {
        const style = browserGlobal.getComputedStyle(ancestor);
        const ancestorRect = ancestor.getBoundingClientRect();
        const clipLeft = ancestorRect.left + ancestor.clientLeft;
        const clipTop = ancestorRect.top + ancestor.clientTop;
        if (clipsOverflow(style.overflowX)) {
          left = Math.max(left, clipLeft);
          right = Math.min(right, clipLeft + ancestor.clientWidth);
        }
        if (clipsOverflow(style.overflowY)) {
          top = Math.max(top, clipTop);
          bottom = Math.min(bottom, clipTop + ancestor.clientHeight);
        }
        ancestor = ancestor.parentElement as typeof ancestor;
      }

      if (right <= left || bottom <= top) return [];
      return [
        {
          box: {
            x: left,
            y: top,
            width: right - left,
            height: bottom - top,
          },
          label:
            browserElement.getAttribute('aria-label') ??
            browserElement.textContent?.trim() ??
            '',
        },
      ];
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

async function emulateClassicScrollbar(page: Page) {
  await page.addStyleTag({
    content: '.primary-navigation::-webkit-scrollbar { width: 15px; }',
  });
}

for (const viewport of viewports) {
  test.describe(viewport.name, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    for (const route of routes) {
      test(`${route} has bounded non-overlapping layout`, async ({ page }) => {
        await page.goto(route);
        await emulateClassicScrollbar(page);
        await expect(page.locator('#main-content')).toBeVisible();
        if (route === '/formulas') {
          await expect(
            page.getByRole('button', { name: 'MAX · 两值中的较大值。' }),
          ).toHaveCount(1);
        }
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
