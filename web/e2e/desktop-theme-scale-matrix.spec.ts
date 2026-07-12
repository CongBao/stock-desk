/// <reference lib="dom" />

import AxeBuilder from '@axe-core/playwright';
import type { Locator, Page } from '@playwright/test';

import { expect, test } from './fixtures';
import { mockCompletedGuidance } from './guidanceMocks';

const coreRoutes = [
  { label: '行情', path: '/market' },
  { label: '自定义公式', path: '/formulas' },
  { label: '策略回测', path: '/backtests' },
  { label: '智能分析', path: '/analysis' },
  { label: '任务中心', path: '/tasks' },
] as const;

// These CSS viewport sizes model a 1366x768 window at each effective scale.
// They are browser evidence only and must never be reported as Windows OS DPI.
const effectiveScaleMatrix = [
  { percent: 100, width: 1366, height: 768 },
  { percent: 125, width: 1093, height: 614 },
  { percent: 150, width: 911, height: 512 },
  { percent: 175, width: 781, height: 439 },
  { percent: 200, width: 683, height: 384 },
] as const;

type ResolvedTheme = 'dark' | 'light';
type ThemePreference = 'dark' | 'light' | 'system';

test.beforeEach(async ({ page }) => {
  await mockCompletedGuidance(page);
});

async function visibleBox(locator: Locator) {
  if (!(await locator.isVisible())) return null;
  return locator.boundingBox();
}

function intersects(
  first: NonNullable<Awaited<ReturnType<typeof visibleBox>>>,
  second: NonNullable<Awaited<ReturnType<typeof visibleBox>>>,
  tolerance = 0,
) {
  return !(
    first.x + first.width <= second.x + tolerance ||
    second.x + second.width <= first.x + tolerance ||
    first.y + first.height <= second.y + tolerance ||
    second.y + second.height <= first.y + tolerance
  );
}

async function expectNoShellOverlap(page: Page) {
  const rail = await visibleBox(page.locator('.navigation-rail'));
  const workspace = await visibleBox(page.locator('#main-content'));
  const context = await visibleBox(page.locator('#context-panel'));
  expect(rail).not.toBeNull();
  expect(workspace).not.toBeNull();
  if (rail !== null && workspace !== null) {
    expect(intersects(rail, workspace, 1)).toBe(false);
  }
  if (context !== null && workspace !== null) {
    const contextIsOverlay = await page
      .locator('#context-panel')
      .evaluate((element) => getComputedStyle(element).position === 'fixed');
    if (!contextIsOverlay)
      expect(intersects(context, workspace, 1)).toBe(false);
  }
}

async function expectNoInteractiveControlOverlap(page: Page, label: string) {
  const controls = page.locator(
    'a:visible, button:visible, input:visible, select:visible, textarea:visible, [role="tab"]:visible',
  );
  const snapshots = await controls.evaluateAll((elements) =>
    elements.flatMap((element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      if (
        style.position === 'fixed' ||
        rect.bottom <= 0 ||
        rect.right <= 0 ||
        rect.top >= document.documentElement.clientHeight ||
        rect.left >= document.documentElement.clientWidth
      ) {
        return [];
      }
      const clipsOverflow = (value: string) =>
        value === 'auto' ||
        value === 'clip' ||
        value === 'hidden' ||
        value === 'scroll';
      let left = rect.left;
      let right = rect.right;
      let top = rect.top;
      let bottom = rect.bottom;
      let nonScrollableClipPixels = 0;
      let ancestor = element.parentElement;
      while (ancestor !== null) {
        const ancestorStyle = getComputedStyle(ancestor);
        const ancestorRect = ancestor.getBoundingClientRect();
        const clipLeft = ancestorRect.left + ancestor.clientLeft;
        const clipTop = ancestorRect.top + ancestor.clientTop;
        if (clipsOverflow(ancestorStyle.overflowX)) {
          const nextLeft = Math.max(left, clipLeft);
          const nextRight = Math.min(right, clipLeft + ancestor.clientWidth);
          if (
            ancestorStyle.overflowX === 'clip' ||
            ancestorStyle.overflowX === 'hidden'
          ) {
            nonScrollableClipPixels = Math.max(
              nonScrollableClipPixels,
              nextLeft - left,
              right - nextRight,
            );
          }
          left = nextLeft;
          right = nextRight;
        }
        if (clipsOverflow(ancestorStyle.overflowY)) {
          const nextTop = Math.max(top, clipTop);
          const nextBottom = Math.min(bottom, clipTop + ancestor.clientHeight);
          if (
            ancestorStyle.overflowY === 'clip' ||
            ancestorStyle.overflowY === 'hidden'
          ) {
            nonScrollableClipPixels = Math.max(
              nonScrollableClipPixels,
              nextTop - top,
              bottom - nextBottom,
            );
          }
          top = nextTop;
          bottom = nextBottom;
        }
        ancestor = ancestor.parentElement;
      }
      const clippedByAncestor =
        left > rect.left + 1 ||
        right < rect.right - 1 ||
        top > rect.top + 1 ||
        bottom < rect.bottom - 1;
      return [
        {
          box: {
            x: Math.max(0, left),
            y: Math.max(0, top),
            width:
              Math.min(document.documentElement.clientWidth, right) -
              Math.max(0, left),
            height:
              Math.min(document.documentElement.clientHeight, bottom) -
              Math.max(0, top),
          },
          clippedByAncestor: clippedByAncestor && nonScrollableClipPixels > 2,
          clippingPixels: nonScrollableClipPixels,
          label:
            element.getAttribute('aria-label') ??
            element.textContent?.trim() ??
            '',
        },
      ];
    }),
  );

  const clipped = snapshots.filter((snapshot) => snapshot.clippedByAncestor);
  expect(
    clipped.map((snapshot) => ({
      label: snapshot.label,
      pixels: snapshot.clippingPixels,
    })),
    `${label}: interactive controls are clipped by a containing panel`,
  ).toEqual([]);

  for (let first = 0; first < snapshots.length; first += 1) {
    const firstControl = snapshots[first];
    if (firstControl === undefined) continue;
    for (let second = first + 1; second < snapshots.length; second += 1) {
      const secondControl = snapshots[second];
      if (secondControl === undefined) continue;
      expect(
        intersects(firstControl.box, secondControl.box),
        `${label}: interactive controls overlap: ${firstControl.label || String(first)} / ${secondControl.label || String(second)}`,
      ).toBe(false);
    }
  }
}

async function expectKeyboardFocusAndCriticalReachability(page: Page) {
  const theme = page.getByRole('combobox', { name: '界面主题' });
  await theme.scrollIntoViewIfNeeded();
  await theme.focus();
  await expect(theme).toBeFocused();
  const focusStyle = await theme.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      boxShadow: style.boxShadow,
      outlineStyle: style.outlineStyle,
      outlineWidth: style.outlineWidth,
    };
  });
  expect(
    focusStyle.outlineStyle !== 'none' && focusStyle.outlineWidth !== '0px',
  ).toBe(true);

  await page.keyboard.press('Tab');
  const afterTab = await page.evaluate(() => {
    const active = document.activeElement;
    if (!(active instanceof HTMLElement)) return null;
    return {
      name:
        active.getAttribute('aria-label') ?? active.textContent?.trim() ?? '',
      tag: active.tagName.toLowerCase(),
    };
  });
  expect(afterTab).not.toBeNull();
  expect(afterTab?.name.trim().length).toBeGreaterThan(0);

  const critical = page
    .locator(
      '#main-content button:not([disabled]):visible, #main-content a[href]:visible, #main-content input:not([disabled]):visible, #main-content select:not([disabled]):visible, #main-content textarea:not([disabled]):visible',
    )
    .first();
  if ((await critical.count()) === 0) return;
  await critical.scrollIntoViewIfNeeded();
  await critical.focus();
  await expect(critical).toBeFocused();
  const box = await critical.boundingBox();
  expect(box).not.toBeNull();
  if (box !== null) {
    const viewport = page.viewportSize();
    expect(viewport).not.toBeNull();
    if (viewport !== null) {
      expect(box.x).toBeGreaterThanOrEqual(-1);
      expect(box.y).toBeGreaterThanOrEqual(-1);
      expect(box.x + box.width).toBeLessThanOrEqual(viewport.width + 1);
      expect(box.y + box.height).toBeLessThanOrEqual(viewport.height + 1);
    }
  }
}

async function expectNonColorStatusCues(page: Page) {
  const topbar = page.locator('.topbar-state');
  await expect(topbar).toHaveAttribute(
    'data-state',
    /^(checking|healthy|degraded|unavailable)$/u,
  );
  await expect(topbar).toContainText(/\S/u);
  if (await topbar.isVisible()) {
    await expect(topbar.locator('.status-symbol')).toBeVisible();
  }

  const statuses = page.locator(
    '[role="status"]:visible, [role="alert"]:visible, [aria-live="polite"]:visible, [aria-live="assertive"]:visible',
  );
  for (let index = 0; index < (await statuses.count()); index += 1) {
    const status = statuses.nth(index);
    const cue = await status.evaluate((element) =>
      (
        element.getAttribute('aria-label') ??
        element.getAttribute('title') ??
        element.textContent ??
        ''
      ).trim(),
    );
    if (cue.length === 0) {
      await expect(status).not.toHaveAttribute('data-state');
      await expect(status).not.toHaveAttribute('data-status');
    }
  }

  const visibleStateCues = page.locator(
    '[data-state]:visible, [data-status]:visible',
  );
  for (let index = 0; index < (await visibleStateCues.count()); index += 1) {
    const cue = visibleStateCues.nth(index);
    const nonColorCue = await cue.evaluate((element) =>
      (
        element.getAttribute('aria-label') ??
        element.getAttribute('title') ??
        element.textContent ??
        ''
      ).trim(),
    );
    expect(nonColorCue.length).toBeGreaterThan(0);
  }
}

async function expectLayoutContract(page: Page, label: string) {
  const dimensions = await page.evaluate(() => ({
    clientHeight: document.documentElement.clientHeight,
    clientWidth: document.documentElement.clientWidth,
    scrollHeight: document.documentElement.scrollHeight,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(
    dimensions.clientWidth + 1,
  );
  expect(dimensions.scrollHeight).toBeGreaterThanOrEqual(
    dimensions.clientHeight,
  );
  await expectNoShellOverlap(page);
  await expectNoInteractiveControlOverlap(page, label);
  await expectKeyboardFocusAndCriticalReachability(page);
  await expectNonColorStatusCues(page);
}

async function expectTheme(
  page: Page,
  preference: ThemePreference,
  resolved: ResolvedTheme,
) {
  const theme = page.getByRole('combobox', { name: '界面主题' });
  await theme.selectOption(preference);
  await expect(theme).toHaveValue(preference);
  await expect(page.locator('html')).toHaveAttribute(
    'data-theme-preference',
    preference,
  );
  await expect(page.locator('html')).toHaveAttribute('data-theme', resolved);
}

async function expectNoSeriousAccessibilityViolation(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();
  expect(
    results.violations.filter((violation) =>
      ['critical', 'serious'].includes(violation.impact ?? ''),
    ),
  ).toEqual([]);
}

for (const route of coreRoutes) {
  for (const theme of ['light', 'dark'] as const) {
    test(`${route.label} ${theme} remains usable through the 100-200 percent effective viewport matrix`, async ({
      page,
    }) => {
      await page.goto(route.path);
      await expect(
        page.locator('#main-content h1, #main-content h2').first(),
      ).toBeVisible();
      await expectTheme(page, theme, theme);

      for (const scale of effectiveScaleMatrix) {
        await page.setViewportSize({
          width: scale.width,
          height: scale.height,
        });
        await expect(page.locator('.app-shell')).toHaveAttribute(
          'data-navigation-collapsed',
          String(scale.width <= 1200),
        );
        await expectLayoutContract(
          page,
          `${route.path} ${theme} ${String(scale.percent)}% equivalent viewport`,
        );
        if (scale.percent === 100 || scale.percent === 200) {
          await expectNoSeriousAccessibilityViolation(page);
        }
      }
    });
  }

  test(`${route.label} System follows both color schemes without restart at 100 and 200 percent equivalent viewports`, async ({
    page,
  }) => {
    await page.goto(route.path);
    await expect(
      page.locator('#main-content h1, #main-content h2').first(),
    ).toBeVisible();
    for (const scale of [effectiveScaleMatrix[0], effectiveScaleMatrix[4]]) {
      await page.setViewportSize({ width: scale.width, height: scale.height });
      await expect(page.locator('.app-shell')).toHaveAttribute(
        'data-navigation-collapsed',
        String(scale.width <= 1200),
      );
      for (const scheme of ['light', 'dark'] as const) {
        await page.emulateMedia({ colorScheme: scheme });
        await expectTheme(page, 'system', scheme);
        await expectLayoutContract(
          page,
          `${route.path} system/${scheme} ${String(scale.percent)}% equivalent viewport`,
        );
        await expectNoSeriousAccessibilityViolation(page);
      }
    }
  });
}
