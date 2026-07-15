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
  { label: '设置', path: '/settings' },
] as const;

type CoreRoute = (typeof coreRoutes)[number];

// These CSS viewport sizes model a 1366x768 window at each effective scale.
// They are browser evidence only and must never be reported as Windows OS DPI.
const effectiveScaleMatrix = [
  { percent: 100, width: 1366, height: 768 },
  { percent: 125, width: 1093, height: 614 },
  { percent: 150, width: 911, height: 512 },
  { percent: 175, width: 781, height: 439 },
  { percent: 200, width: 683, height: 384 },
] as const;

// This is the native host's minimum logical window, not another scale claim.
// It deliberately runs the same fully-populated page contract as the scale
// matrix instead of being represented only by a shell/configuration assertion.
const minimumLogicalViewport = { width: 640, height: 360 } as const;

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
        style.pointerEvents === 'none' ||
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
      let effectivePosition = style.position;
      let ancestor = element.parentElement;
      while (ancestor !== null) {
        const ancestorStyle = getComputedStyle(ancestor);
        const ancestorRect = ancestor.getBoundingClientRect();
        if (ancestorStyle.position === 'fixed')
          effectivePosition = 'fixed-ancestor';
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
          position: effectivePosition,
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
        `${label}: interactive controls overlap (${firstControl.position}/${secondControl.position}): ${firstControl.label || String(first)} / ${secondControl.label || String(second)}`,
      ).toBe(false);
    }
  }
}

const denseRegions: Readonly<Record<CoreRoute['path'], readonly string[]>> = {
  '/market': [
    '.market-terminal-center',
    '.market-chart-card',
    '.market-chart-canvas',
    '.formula-subchart',
  ],
  '/formulas': [
    '.formula-library',
    '.formula-editor-panel',
    '.formula-monaco-shell',
    '.formula-preview-panel',
  ],
  '/backtests': [
    '.backtest-report',
    '.report-tabs',
    '[aria-label="可横向滚动的交易表"]',
  ],
  '/analysis': ['.analysis-report-workspace', '.analysis-conclusion'],
  '/tasks': [
    '.task-metrics',
    '.task-filters',
    '.task-recent-panel',
    '.task-detail-panel',
  ],
  '/settings': [
    '.source-overview',
    '.source-card-grid',
    '.priority-settings',
    '.settings-save-bar',
  ],
};

async function expectDenseRegionsReachable(page: Page, route: CoreRoute) {
  for (const selector of denseRegions[route.path]) {
    const region = page.locator(selector).first();
    await expect(
      region,
      `${route.path}: missing dense region ${selector}`,
    ).toBeVisible();
    await region.scrollIntoViewIfNeeded();
    const bounds = await region.boundingBox();
    const viewport = page.viewportSize();
    expect(
      bounds,
      `${route.path}: ${selector} has no layout box`,
    ).not.toBeNull();
    expect(viewport).not.toBeNull();
    if (bounds !== null && viewport !== null) {
      expect(
        bounds.width,
        `${route.path}: ${selector} is wider than the viewport`,
      ).toBeLessThanOrEqual(viewport.width + 1);
      expect(
        bounds.x,
        `${route.path}: ${selector} escapes the left edge`,
      ).toBeGreaterThanOrEqual(-1);
      expect(
        bounds.x + bounds.width,
        `${route.path}: ${selector} escapes the right edge`,
      ).toBeLessThanOrEqual(viewport.width + 1);
    }
  }

  const fixedControls = page.locator(
    'a:visible, button:visible, input:visible, select:visible, textarea:visible',
  );
  const fixedBoxes = await fixedControls.evaluateAll((elements) =>
    elements.flatMap((element) => {
      const style = getComputedStyle(element);
      if (style.position !== 'fixed') return [];
      const rect = element.getBoundingClientRect();
      if (
        rect.bottom <= 0 ||
        rect.right <= 0 ||
        rect.top >= document.documentElement.clientHeight ||
        rect.left >= document.documentElement.clientWidth
      )
        return [];
      return [
        {
          bottom: rect.bottom,
          label:
            element.getAttribute('aria-label') ??
            element.textContent?.trim() ??
            element.tagName,
          left: rect.left,
          right: rect.right,
          top: rect.top,
        },
      ];
    }),
  );
  const viewport = page.viewportSize();
  expect(viewport).not.toBeNull();
  if (viewport !== null) {
    for (const item of fixedBoxes) {
      expect(
        item.left,
        `${item.label}: fixed control escapes left`,
      ).toBeGreaterThanOrEqual(-1);
      expect(
        item.top,
        `${item.label}: fixed control escapes top`,
      ).toBeGreaterThanOrEqual(-1);
      expect(
        item.right,
        `${item.label}: fixed control escapes right`,
      ).toBeLessThanOrEqual(viewport.width + 1);
      expect(
        item.bottom,
        `${item.label}: fixed control escapes bottom`,
      ).toBeLessThanOrEqual(viewport.height + 1);
    }
  }
}

async function activeElementLabel(page: Page) {
  return page.evaluate(() => {
    const active = document.activeElement;
    if (!(active instanceof HTMLElement)) return null;
    return {
      label:
        active.getAttribute('aria-label') ?? active.textContent?.trim() ?? '',
      tag: active.tagName.toLowerCase(),
    };
  });
}

async function tabToControl(
  page: Page,
  target: Locator,
  description: string,
  maximumSteps = 320,
) {
  await expect(target, `${description}: target is not visible`).toBeVisible();
  const trail: string[] = [];
  for (let step = 0; step < maximumSteps; step += 1) {
    await page.keyboard.press('Tab');
    if (
      await target.evaluate(
        (element) => element === element.ownerDocument.activeElement,
      )
    )
      return;
    const active = await activeElementLabel(page);
    trail.push(active === null ? '<none>' : `${active.tag}:${active.label}`);
  }
  throw new Error(
    `${description}: Tab did not reach the target after ${String(maximumSteps)} steps; trail=${trail.join(' -> ')}`,
  );
}

function criticalAction(route: CoreRoute, page: Page) {
  if (route.path === '/market') {
    const target = page.getByRole('button', { name: '打开股票池' });
    return {
      activate: async () => {
        await page.keyboard.press('Enter');
        const dialog = page.getByRole('dialog', {
          name: '选择或管理股票池',
        });
        await expect(dialog).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(dialog).toHaveCount(0);
        await expect(target).toBeFocused();
      },
      target,
    };
  }
  if (route.path === '/formulas') {
    const target = page.locator('button.formula-preview-run');
    return {
      activate: async () => {
        await expect(target).toHaveText('运行预览');
        const previewRequest = page.waitForRequest((request) => {
          const url = new URL(request.url());
          return (
            request.method() === 'GET' &&
            url.pathname.endsWith('/api/market/bars') &&
            url.searchParams.has('formula_version_id')
          );
        });
        await page.keyboard.press('Enter');
        await previewRequest;
        await expect(target).toHaveText('运行预览');
        await expect(target).toBeEnabled();
        await expect(
          page.getByRole('img', { name: /K 线主图.*公式输出.*买卖信号/u }),
        ).toBeVisible();
      },
      target,
    };
  }
  if (route.path === '/backtests') {
    const target = page.getByRole('tab', { name: '交易明细' });
    return {
      activate: async () => {
        await page.keyboard.press('Enter');
        await expect(target).toHaveAttribute('aria-selected', 'true');
        await expect(
          page.locator('[aria-label="可横向滚动的交易表"] tbody tr').first(),
        ).toBeVisible();
      },
      target,
    };
  }
  if (route.path === '/analysis') {
    const target = page.getByRole('button', { name: '模型设置' });
    return {
      activate: async () => {
        await page.keyboard.press('Enter');
        const dialog = page.getByRole('dialog', { name: '模型设置' });
        await expect(dialog).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(dialog).toHaveCount(0);
        await expect(target).toBeFocused();
      },
      target,
    };
  }
  if (route.path === '/tasks') {
    const target = page.locator('button[data-guidance-target="tasks-refresh"]');
    return {
      activate: async () => {
        await expect(target).toHaveText('刷新任务');
        let releaseTaskList = () => {};
        const taskListGate = new Promise<void>((resolve) => {
          releaseTaskList = resolve;
        });
        await page.route(
          '**/api/tasks?**',
          async (taskListRoute) => {
            await taskListGate;
            await taskListRoute.fallback();
          },
          { times: 1 },
        );
        const refreshRequest = page.waitForRequest((request) => {
          const url = new URL(request.url());
          return (
            request.method() === 'GET' &&
            url.pathname === '/api/tasks' &&
            url.searchParams.get('limit') === '100'
          );
        });
        await page.keyboard.press('Enter');
        await refreshRequest;
        try {
          await expect(target).toHaveText('刷新中…');
          await expect(target).toBeDisabled();
        } finally {
          releaseTaskList();
        }
        await expect(target).toHaveText('刷新任务');
        await expect(target).toBeEnabled();
        await expect(page.locator('.task-detail-panel')).toBeVisible();
      },
      target,
    };
  }
  const target = page.getByRole('button', { name: '保存数据源设置' });
  return {
    activate: async () => {
      await page.keyboard.press('Enter');
      await expect(
        page.locator('.settings-save-bar [role="status"]'),
      ).toContainText('设置已安全保存');
    },
    target,
  };
}

async function expectKeyboardFocusAndCriticalReachability(
  page: Page,
  route: CoreRoute,
) {
  const theme = page.getByRole('combobox', { name: '界面主题' });
  // Dense-state preparation intentionally leaves focus in different places on
  // every route. Reach the stable theme control only through the real Tab
  // sequence; direct locator.focus()/DOM focus injection would hide broken
  // page order or a keyboard trap.
  await tabToControl(page, theme, `${route.path}: stable theme control`);
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

  await page.keyboard.press('Shift+Tab');
  const beforeTheme = await activeElementLabel(page);
  expect(beforeTheme).not.toBeNull();
  expect(beforeTheme?.label.trim().length).toBeGreaterThan(0);
  await page.keyboard.press('Tab');
  await expect(theme).toBeFocused();

  const action = criticalAction(route, page);
  await tabToControl(page, action.target, `${route.path}: critical action`);
  await expect(action.target).toBeFocused();
  const box = await action.target.boundingBox();
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
  await action.activate();
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
  const statusSnapshots = await statuses.evaluateAll((elements) =>
    elements.map((element) => ({
      cue: (
        element.getAttribute('aria-label') ??
        element.getAttribute('title') ??
        element.textContent ??
        ''
      ).trim(),
      hasState: element.hasAttribute('data-state'),
      hasStatus: element.hasAttribute('data-status'),
    })),
  );
  for (const status of statusSnapshots) {
    if (status.cue.length === 0) {
      expect(status.hasState).toBe(false);
      expect(status.hasStatus).toBe(false);
    }
  }

  const visibleStateCues = page.locator(
    '[data-state]:visible, [data-status]:visible',
  );
  const nonColorCues = await visibleStateCues.evaluateAll((elements) =>
    elements.map((element) =>
      (
        element.getAttribute('aria-label') ??
        element.getAttribute('title') ??
        element.textContent ??
        ''
      ).trim(),
    ),
  );
  for (const nonColorCue of nonColorCues) {
    expect(nonColorCue.length).toBeGreaterThan(0);
  }
}

async function expectLayoutContract(
  page: Page,
  label: string,
  route: CoreRoute,
) {
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
  await expectKeyboardFocusAndCriticalReachability(page, route);
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

const demoInstrumentName = 'Stock Desk Synthetic Alpha (CC0 Demo) 600000.SH';
const demoFormulaName = 'Stock Desk Demo MACD (CC0 synthetic)';
const denseStart = '2024-02-10';
const denseEnd = '2024-06-28';

async function selectDemoMarketInstrument(page: Page) {
  const selected = page.locator('.selected-instrument', {
    hasText: '600000.SH',
  });
  if (await selected.isVisible()) return;
  const search = page.getByRole('combobox', { name: '搜索证券' });
  await search.fill('600000');
  await page
    .getByRole('option', { name: demoInstrumentName, exact: true })
    .click();
  await expect(selected).toBeVisible();
}

async function prepareMarketDenseState(page: Page) {
  await selectDemoMarketInstrument(page);
  await expect(page.locator('.market-chart-canvas')).toHaveAttribute(
    'data-chart-ready',
    'true',
  );
  await expect(page.locator('.market-chart-canvas canvas')).toHaveCount(1);
  await expect(
    page.getByRole('status', { name: '当前 K 线 OHLCV' }),
  ).toContainText('量');
  await expect(
    page.getByRole('region', { name: '公式结果副图' }),
  ).toBeVisible();
}

async function prepareFormulaDenseState(page: Page) {
  const selector = page.getByRole('combobox', { name: '打开已保存公式' });
  await selector.selectOption({ label: `${demoFormulaName} · v1` });
  const preview = page.getByRole('button', { name: '运行预览' });
  await expect(preview).toBeEnabled();
  await preview.click();
  await expect(
    page.getByRole('img', { name: /K 线主图.*公式输出.*买卖信号/u }),
  ).toBeVisible();
  await expect(page.getByText(/[1-9]\d* 个买点/u)).toBeVisible();
  await expect(page.getByText(/[1-9]\d* 个卖点/u)).toBeVisible();
}

async function prepareBacktestDenseState(page: Page) {
  await page.goto(
    `/backtests?symbol=600000.SH&period=1d&adjustment=qfq&start=${denseStart}&end=${denseEnd}`,
  );
  await page.getByLabel('保存的交易公式').selectOption({
    label: demoFormulaName,
  });
  for (let step = 0; step < 4; step += 1) {
    await page.getByRole('button', { name: '下一步' }).click();
  }
  await page.getByRole('button', { name: '运行预检' }).click();
  await expect(page.getByLabel('服务端预检结果')).toContainText('可运行 1 / 1');
  await page.getByRole('button', { name: '提交回测' }).click();
  await expect(page).toHaveURL(/\/backtests\/[0-9a-f-]{36}$/u);
  await expect(page.getByRole('heading', { name: '回测结论' })).toBeVisible({
    timeout: 30_000,
  });
  await page.getByRole('tab', { name: '交易明细' }).click();
  await expect(
    page.locator('[aria-label="可横向滚动的交易表"] tbody tr').first(),
  ).toBeVisible();
}

async function prepareAnalysisDenseState(page: Page) {
  await page.getByLabel('股票代码').fill('600000.SH');
  await page.getByLabel('已验证模型').selectOption({ index: 1 });
  await page.getByRole('button', { name: '运行预检' }).click();
  await expect(page.getByLabel('四类数据预检结果')).toContainText(
    '数据覆盖满足评级门槛',
  );
  await page.getByRole('button', { name: '启动智能分析' }).click();
  await expect(
    page.getByRole('heading', { name: '600000.SH 智能分析' }),
  ).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('不构成投资建议')).toBeVisible();
}

async function prepareTaskDenseState(page: Page) {
  const task = page.locator('.task-center-list button').first();
  await expect(task).toBeVisible();
  await task.click();
  await expect(
    page.locator('.task-detail-panel .task-status-badge'),
  ).toBeVisible();
  await expect(page.locator('.task-timeline')).toBeVisible();
}

async function prepareSettingsDenseState(page: Page) {
  await expect(page.locator('.source-card')).toHaveCount(5);
  await expect(
    page.getByRole('group', { name: '日线行情优先级' }),
  ).toBeVisible();
  await expect(
    page.getByRole('button', { name: '保存数据源设置' }),
  ).toBeVisible();
}

async function prepareDenseState(page: Page, route: CoreRoute) {
  if (route.path === '/market') await prepareMarketDenseState(page);
  else if (route.path === '/formulas') await prepareFormulaDenseState(page);
  else if (route.path === '/backtests') await prepareBacktestDenseState(page);
  else if (route.path === '/analysis') await prepareAnalysisDenseState(page);
  else if (route.path === '/tasks') await prepareTaskDenseState(page);
  else await prepareSettingsDenseState(page);
}

async function expectAnalysisDrawersAtCurrentScale(page: Page) {
  const viewport = page.viewportSize();
  expect(viewport).not.toBeNull();
  if ((viewport?.width ?? 0) > 1280) {
    await expect(page.locator('#analysis-process-drawer')).toBeVisible();
    await expect(page.locator('#analysis-evidence-drawer')).toBeVisible();
    return;
  }

  await expect(page.locator('.analysis-report-toolbar')).toBeVisible();
  const processTrigger = page.getByRole('button', { name: '查看分析流程' });
  await processTrigger.click();
  const process = page.locator('#analysis-process-drawer[data-open="true"]');
  await expect(process).toBeVisible();
  await process.scrollIntoViewIfNeeded();
  await expect(
    process.getByRole('button', { name: '关闭分析流程' }),
  ).toBeVisible();
  await process.getByRole('button', { name: '关闭分析流程' }).click();
  await expect(processTrigger).toBeFocused();

  const evidenceTrigger = page.getByRole('button', { name: '查看证据' });
  await evidenceTrigger.click();
  const evidence = page.locator('#analysis-evidence-drawer[data-open="true"]');
  await expect(evidence).toBeVisible();
  await evidence.scrollIntoViewIfNeeded();
  await expect(
    evidence.getByRole('button', { name: '关闭证据' }),
  ).toBeVisible();
  await evidence.getByRole('button', { name: '关闭证据' }).click();
  await expect(evidenceTrigger).toBeFocused();
}

for (const route of coreRoutes) {
  for (const theme of ['light', 'dark'] as const) {
    test(`${route.label} ${theme} remains usable through the 100-200 percent effective viewport matrix`, async ({
      page,
    }) => {
      test.setTimeout(90_000);
      await page.goto(route.path);
      await expect(
        page.locator('#main-content h1, #main-content h2').first(),
      ).toBeVisible();
      await prepareDenseState(page, route);
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
          route,
        );
        await expectDenseRegionsReachable(page, route);
        if (route.path === '/analysis')
          await expectAnalysisDrawersAtCurrentScale(page);
        if (scale.percent === 100 || scale.percent === 200) {
          await expectNoSeriousAccessibilityViolation(page);
        }
      }

      await page.setViewportSize(minimumLogicalViewport);
      await expect(page.locator('.app-shell')).toHaveAttribute(
        'data-navigation-collapsed',
        'true',
      );
      await expectLayoutContract(
        page,
        `${route.path} ${theme} 640x360 minimum logical window`,
        route,
      );
      await expectDenseRegionsReachable(page, route);
      if (route.path === '/analysis')
        await expectAnalysisDrawersAtCurrentScale(page);
      await expectNoSeriousAccessibilityViolation(page);
    });
  }

  test(`${route.label} System follows both color schemes without restart through the 100-200 percent effective viewport matrix`, async ({
    page,
  }) => {
    test.setTimeout(120_000);
    await page.goto(route.path);
    await expect(
      page.locator('#main-content h1, #main-content h2').first(),
    ).toBeVisible();
    await prepareDenseState(page, route);
    for (const scale of effectiveScaleMatrix) {
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
          route,
        );
        await expectDenseRegionsReachable(page, route);
        if (route.path === '/analysis')
          await expectAnalysisDrawersAtCurrentScale(page);
        if (scale.percent === 100 || scale.percent === 200)
          await expectNoSeriousAccessibilityViolation(page);
      }
    }
    await page.setViewportSize(minimumLogicalViewport);
    await expect(page.locator('.app-shell')).toHaveAttribute(
      'data-navigation-collapsed',
      'true',
    );
    for (const scheme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      await expectTheme(page, 'system', scheme);
      await expectLayoutContract(
        page,
        `${route.path} system/${scheme} 640x360 minimum logical window`,
        route,
      );
      await expectDenseRegionsReachable(page, route);
      if (route.path === '/analysis')
        await expectAnalysisDrawersAtCurrentScale(page);
      await expectNoSeriousAccessibilityViolation(page);
    }
  });
}
