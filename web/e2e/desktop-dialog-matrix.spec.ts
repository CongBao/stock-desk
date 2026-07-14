import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Locator, type Page } from '@playwright/test';

import { installReturningUserState } from './fixtures';

// These CSS viewport sizes model a 1366x768 window at each effective scale.
// They are browser evidence only and must never be reported as Windows OS DPI.
const viewports = [
  { percent: 100, width: 1366, height: 768 },
  { percent: 125, width: 1093, height: 614 },
  { percent: 150, width: 911, height: 512 },
  { percent: 175, width: 781, height: 439 },
  { percent: 200, width: 683, height: 384 },
  // Native minimum logical window contract, kept separate from scale claims.
  { percent: 'minimum', width: 640, height: 360 },
] as const;

const themeCases = [
  { colorScheme: 'light', preference: 'light', resolved: 'light' },
  { colorScheme: 'dark', preference: 'dark', resolved: 'dark' },
  { colorScheme: 'light', preference: 'system', resolved: 'light' },
  { colorScheme: 'dark', preference: 'system', resolved: 'dark' },
] as const;

const themeStorageKey = 'stock-desk.preferences.v1.1.theme';

type ThemeCase = (typeof themeCases)[number];

const dialogControlSelector = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

type DesktopHarnessWindow = Window & {
  readonly __stockDeskDesktopHarness: {
    readonly emit: (event: string, payload: unknown) => void;
    readonly hasListener: (event: string) => boolean;
    readonly resolveExit: () => void;
    readonly resolveUpdate: () => void;
  };
};

async function installThemePreference(page: Page, theme: ThemeCase) {
  await page.emulateMedia({ colorScheme: theme.colorScheme });
  await page.addInitScript(
    ({ key, preference }) => localStorage.setItem(key, preference),
    { key: themeStorageKey, preference: theme.preference },
  );
}

async function assertResolvedTheme(page: Page, theme: ThemeCase) {
  await expect(page.locator('html')).toHaveAttribute(
    'data-theme-preference',
    theme.preference,
  );
  await expect(page.locator('html')).toHaveAttribute(
    'data-theme',
    theme.resolved,
  );
}

async function assertNativeModal(
  dialog: Locator,
  viewport: (typeof viewports)[number],
) {
  await expect(dialog).toBeVisible();
  await expect
    .poll(() =>
      dialog.evaluate(
        (element) =>
          element instanceof HTMLDialogElement &&
          element.open &&
          element.matches(':modal'),
      ),
    )
    .toBe(true);

  const bounds = await dialog.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds?.x ?? -1).toBeGreaterThanOrEqual(0);
  expect(bounds?.y ?? -1).toBeGreaterThanOrEqual(0);
  expect((bounds?.x ?? 0) + (bounds?.width ?? 0)).toBeLessThanOrEqual(
    viewport.width + 1,
  );
  expect((bounds?.y ?? 0) + (bounds?.height ?? 0)).toBeLessThanOrEqual(
    viewport.height + 1,
  );
  await assertEveryDialogControlReachable(dialog, viewport);
}

async function assertEveryDialogControlReachable(
  dialog: Locator,
  viewport: (typeof viewports)[number],
) {
  const controlIndexes = await dialog
    .locator(dialogControlSelector)
    .evaluateAll(
      (controls, selector) => {
        const dialog = document.querySelector<HTMLDialogElement>(selector);
        return controls.flatMap((control, index) => {
          if (!(control instanceof HTMLElement) || dialog === null) return [];
          let current: HTMLElement | null = control;
          while (current !== null && dialog.contains(current)) {
            const style = getComputedStyle(current);
            if (
              current.hidden ||
              current.hasAttribute('inert') ||
              current.getAttribute('aria-hidden') === 'true' ||
              style.display === 'none' ||
              style.visibility === 'hidden'
            )
              return [];
            current = current.parentElement;
          }
          return control.tabIndex >= 0 && control.getClientRects().length > 0
            ? [index]
            : [];
        });
      },
      await dialog.evaluate((element) => {
        if (element.id.length === 0)
          element.id = `dialog-audit-${crypto.randomUUID()}`;
        return `#${CSS.escape(element.id)}`;
      }),
    );

  expect(controlIndexes.length).toBeGreaterThanOrEqual(1);
  const controls = dialog.locator(dialogControlSelector);
  for (const index of controlIndexes) {
    const control = controls.nth(index);
    await control.scrollIntoViewIfNeeded();
    const result = await control.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const dialog = element.closest('dialog');
      if (dialog === null) throw new Error('audited control left its dialog');
      let visibleLeft = Math.max(0, rect.left);
      let visibleTop = Math.max(0, rect.top);
      let visibleRight = Math.min(window.innerWidth, rect.right);
      let visibleBottom = Math.min(window.innerHeight, rect.bottom);
      let hasScrollableRoute = false;
      let ancestor: HTMLElement | null = element.parentElement;
      while (ancestor !== null && dialog.contains(ancestor)) {
        const style = getComputedStyle(ancestor);
        const clips = (value: string) =>
          value === 'auto' ||
          value === 'clip' ||
          value === 'hidden' ||
          value === 'scroll';
        const scrolls = (value: string) =>
          value === 'auto' || value === 'scroll';
        const ancestorRect = ancestor.getBoundingClientRect();
        const left = ancestorRect.left + ancestor.clientLeft;
        const top = ancestorRect.top + ancestor.clientTop;
        if (clips(style.overflowX)) {
          visibleLeft = Math.max(visibleLeft, left);
          visibleRight = Math.min(visibleRight, left + ancestor.clientWidth);
        }
        if (clips(style.overflowY)) {
          visibleTop = Math.max(visibleTop, top);
          visibleBottom = Math.min(visibleBottom, top + ancestor.clientHeight);
        }
        if (
          (scrolls(style.overflowX) &&
            ancestor.scrollWidth > ancestor.clientWidth + 1) ||
          (scrolls(style.overflowY) &&
            ancestor.scrollHeight > ancestor.clientHeight + 1)
        )
          hasScrollableRoute = true;
        if (ancestor === dialog) break;
        ancestor = ancestor.parentElement;
      }
      const centerX = Math.max(
        visibleLeft,
        Math.min(
          visibleRight - 1,
          visibleLeft + (visibleRight - visibleLeft) / 2,
        ),
      );
      const centerY = Math.max(
        visibleTop,
        Math.min(
          visibleBottom - 1,
          visibleTop + (visibleBottom - visibleTop) / 2,
        ),
      );
      const topmost = document.elementFromPoint(centerX, centerY);
      return {
        fullyVisible:
          visibleRight - visibleLeft >= rect.width - 1 &&
          visibleBottom - visibleTop >= rect.height - 1,
        hasScrollableRoute,
        hitTestPassed:
          topmost !== null &&
          (element === topmost || element.contains(topmost)),
        label:
          element.getAttribute('aria-label') ??
          element.textContent?.trim() ??
          element.tagName,
        rect: {
          bottom: rect.bottom,
          left: rect.left,
          right: rect.right,
          top: rect.top,
        },
      };
    });
    expect(
      result.fullyVisible,
      `${result.label}: control remains clipped after ${result.hasScrollableRoute ? 'using its explicit scroll container' : 'layout'}`,
    ).toBe(true);
    expect(
      result.rect.left,
      `${result.label}: control escapes dialog viewport left`,
    ).toBeGreaterThanOrEqual(-1);
    expect(
      result.rect.top,
      `${result.label}: control escapes dialog viewport top`,
    ).toBeGreaterThanOrEqual(-1);
    expect(
      result.rect.right,
      `${result.label}: control escapes dialog viewport right`,
    ).toBeLessThanOrEqual(viewport.width + 1);
    expect(
      result.rect.bottom,
      `${result.label}: control escapes dialog viewport bottom`,
    ).toBeLessThanOrEqual(viewport.height + 1);
    expect(
      result.hitTestPassed,
      `${result.label}: control is covered at its visible center`,
    ).toBe(true);

    const overlap = await dialog.evaluate(
      (element, { controlSelector, targetIndex }) => {
        const controls = Array.from(
          element.querySelectorAll<HTMLElement>(controlSelector),
        );
        const target = controls[targetIndex];
        if (target === undefined) return null;
        const targetRect = target.getBoundingClientRect();
        const available = (control: HTMLElement) => {
          let current: HTMLElement | null = control;
          while (current !== null && element.contains(current)) {
            const style = getComputedStyle(current);
            if (
              current.hidden ||
              current.hasAttribute('inert') ||
              current.getAttribute('aria-hidden') === 'true' ||
              style.display === 'none' ||
              style.visibility === 'hidden'
            )
              return false;
            current = current.parentElement;
          }
          const rect = control.getBoundingClientRect();
          return (
            rect.width > 0 &&
            rect.height > 0 &&
            rect.bottom > 0 &&
            rect.right > 0 &&
            rect.top < window.innerHeight &&
            rect.left < window.innerWidth
          );
        };
        for (const [otherIndex, other] of controls.entries()) {
          if (
            otherIndex === targetIndex ||
            !available(other) ||
            target.contains(other) ||
            other.contains(target)
          )
            continue;
          const otherRect = other.getBoundingClientRect();
          const overlapWidth =
            Math.min(targetRect.right, otherRect.right) -
            Math.max(targetRect.left, otherRect.left);
          const overlapHeight =
            Math.min(targetRect.bottom, otherRect.bottom) -
            Math.max(targetRect.top, otherRect.top);
          if (overlapWidth > 1 && overlapHeight > 1)
            return (
              other.getAttribute('aria-label') ??
              other.textContent?.trim() ??
              other.tagName
            );
        }
        return null;
      },
      { controlSelector: dialogControlSelector, targetIndex: index },
    );
    expect(
      overlap,
      `${result.label}: visible interactive control overlaps ${overlap ?? ''}`,
    ).toBeNull();
  }
}

async function assertActionReachable(action: Locator, viewportHeight: number) {
  await action.scrollIntoViewIfNeeded();
  await expect(action).toBeVisible();
  const bounds = await action.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds?.y ?? -1).toBeGreaterThanOrEqual(0);
  expect((bounds?.y ?? 0) + (bounds?.height ?? 0)).toBeLessThanOrEqual(
    viewportHeight + 1,
  );
}

async function assertBidirectionalTabTrap(page: Page, dialog: Locator) {
  const initial = await dialog.evaluate((element, controlSelector) => {
    const controls = Array.from(
      element.querySelectorAll<HTMLElement>(controlSelector),
    ).filter((control) => {
      const style = window.getComputedStyle(control);
      return (
        control.tabIndex >= 0 &&
        !control.matches(':disabled') &&
        control.closest('[hidden], [inert], [aria-hidden="true"]') === null &&
        style.display !== 'none' &&
        style.visibility !== 'hidden'
      );
    });
    const activeIndex = controls.findIndex(
      (control) => control === document.activeElement,
    );
    const nearestScrollContainer = (control: HTMLElement) => {
      let ancestor: HTMLElement | null = control.parentElement;
      while (ancestor !== null && element.contains(ancestor)) {
        const style = getComputedStyle(ancestor);
        const scrolls = (value: string) =>
          value === 'auto' || value === 'scroll';
        if (
          (scrolls(style.overflowY) &&
            ancestor.scrollHeight > ancestor.clientHeight + 1) ||
          (scrolls(style.overflowX) &&
            ancestor.scrollWidth > ancestor.clientWidth + 1)
        )
          return ancestor;
        if (ancestor === element) break;
        ancestor = ancestor.parentElement;
      }
      return element;
    };
    const visualOrderIssues = controls.flatMap((control, index) => {
      const next = controls[index + 1];
      if (next === undefined) return [];
      if (nearestScrollContainer(control) !== nearestScrollContainer(next))
        return [];
      const currentBox = control.getBoundingClientRect();
      const nextBox = next.getBoundingClientRect();
      const sameRow = Math.abs(currentBox.top - nextBox.top) <= 4;
      const label = (item: HTMLElement) =>
        item.getAttribute('aria-label') ??
        item.textContent?.trim() ??
        item.tagName;
      if (nextBox.top + 4 < currentBox.top)
        return [
          `${index}: upward jump ${label(control)} (${String(currentBox.top)}) -> ${label(next)} (${String(nextBox.top)})`,
        ];
      if (sameRow && nextBox.left + 4 < currentBox.left)
        return [
          `${index}: right-to-left jump ${label(control)} -> ${label(next)}`,
        ];
      return [];
    });
    return { activeIndex, controlCount: controls.length, visualOrderIssues };
  }, dialogControlSelector);
  expect(initial.controlCount).toBeGreaterThanOrEqual(2);
  expect(initial.activeIndex).toBeGreaterThanOrEqual(0);
  expect(initial.visualOrderIssues).toEqual([]);

  const activeIndex = async () =>
    dialog.evaluate((element, controlSelector) => {
      const controls = Array.from(
        element.querySelectorAll<HTMLElement>(controlSelector),
      ).filter((control) => {
        const style = window.getComputedStyle(control);
        return (
          control.tabIndex >= 0 &&
          !control.matches(':disabled') &&
          control.closest('[hidden], [inert], [aria-hidden="true"]') === null &&
          style.display !== 'none' &&
          style.visibility !== 'hidden'
        );
      });
      return controls.findIndex(
        (control) => control === document.activeElement,
      );
    }, dialogControlSelector);

  let expected = initial.activeIndex;
  for (let step = 0; step < initial.controlCount; step += 1) {
    await page.keyboard.press('Tab');
    expected = (expected + 1) % initial.controlCount;
    expect(await activeIndex()).toBe(expected);
  }
  for (let step = 0; step < initial.controlCount; step += 1) {
    await page.keyboard.press('Shift+Tab');
    expected = (expected - 1 + initial.controlCount) % initial.controlCount;
    expect(await activeIndex()).toBe(expected);
  }
}

async function assertDialogAccessibility(page: Page) {
  const results = await new AxeBuilder({ page })
    .include('dialog[open]')
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();
  expect(
    results.violations.filter((violation) =>
      ['critical', 'serious'].includes(violation.impact ?? ''),
    ),
  ).toEqual([]);
}

async function installAnalysisRoutes(page: Page) {
  await page.route('**/api/**', async (route) => {
    const { pathname } = new URL(route.request().url());
    if (pathname.endsWith('/settings/models')) {
      await route.fulfill({
        json: {
          items: [
            {
              api_key_configured: true,
              base_url: 'https://api.deepseek.com',
              created_at: '2026-07-14T00:00:00Z',
              display_name: '矩阵模型',
              error_code: null,
              id: `sha256:${'a'.repeat(64)}`,
              last_tested_at: '2026-07-14T00:00:00Z',
              masked_api_key: 'sk-a•••••••tail',
              max_output: 4096,
              model: 'deepseek-chat',
              provider: 'deepseek',
              public_config_hash: `sha256:${'b'.repeat(64)}`,
              revision: 1,
              status: 'verified',
              supersedes_id: null,
              temperature: 0.1,
              timeout: 90,
              updated_at: '2026-07-14T00:00:00Z',
              verified_at: '2026-07-14T00:00:00Z',
            },
          ],
          next_cursor: null,
        },
      });
      return;
    }
    if (
      pathname.includes('/settings/models/') &&
      pathname.endsWith('/disable')
    ) {
      await route.fulfill({ status: 204 });
      return;
    }
    if (pathname.endsWith('/analysis') && route.request().method() === 'GET') {
      await route.fulfill({ json: { items: [], next_cursor: null } });
      return;
    }
    await route.fallback();
  });
}

async function installDesktopHarness(page: Page) {
  await page.addInitScript(() => {
    let nextCallbackId = 1;
    let nextEventId = 1;
    const callbacks = new Map<number, (value: unknown) => void>();
    const listeners = new Map<
      string,
      Map<number, { callbackId: number; eventId: number }>
    >();
    let recoveryRequired = true;
    let resolveExitRequest: (() => void) | null = null;
    let resolveUpdateRequest: (() => void) | null = null;
    const update = {
      state: 'available',
      current_version: '1.1.0',
      version: '1.2.0',
      notes: '窄屏对话框验证更新',
    };

    function emit(event: string, payload: unknown) {
      for (const listener of listeners.get(event)?.values() ?? []) {
        callbacks.get(listener.callbackId)?.({
          event,
          id: listener.eventId,
          payload,
        });
      }
    }

    Object.assign(globalThis, {
      isTauri: true,
      __stockDeskDesktopHarness: {
        emit,
        hasListener: (event: string) => (listeners.get(event)?.size ?? 0) > 0,
        resolveExit: () => {
          resolveExitRequest?.();
          resolveExitRequest = null;
        },
        resolveUpdate: () => {
          resolveUpdateRequest?.();
          resolveUpdateRequest = null;
        },
      },
      __TAURI_EVENT_PLUGIN_INTERNALS__: {
        unregisterListener(event: string, eventId: number) {
          const eventListeners = listeners.get(event);
          const listener = eventListeners?.get(eventId);
          if (listener !== undefined) callbacks.delete(listener.callbackId);
          eventListeners?.delete(eventId);
        },
      },
      __TAURI_INTERNALS__: {
        transformCallback(callback: (value: unknown) => void) {
          const id = nextCallbackId++;
          callbacks.set(id, callback);
          return id;
        },
        unregisterCallback(id: number) {
          callbacks.delete(id);
        },
        async invoke(
          command: string,
          args: Record<string, unknown> = {},
        ): Promise<unknown> {
          if (command === 'plugin:event|listen') {
            const event = args['event'] as string;
            const callbackId = args['handler'] as number;
            const eventId = nextEventId++;
            const eventListeners =
              listeners.get(event) ??
              new Map<number, { callbackId: number; eventId: number }>();
            eventListeners.set(eventId, { callbackId, eventId });
            listeners.set(event, eventListeners);
            return eventId;
          }
          if (command === 'plugin:event|unlisten') return undefined;
          if (command === 'desktop_runtime_state') return { state: 'ready' };
          if (command === 'desktop_update_state') return update;
          if (command === 'desktop_check_for_updates') return update;
          if (command === 'desktop_cancel_exit') {
            emit('desktop-exit-state', { state: 'idle' });
            return undefined;
          }
          if (command === 'desktop_confirm_exit') {
            return new Promise<void>((resolve) => {
              resolveExitRequest = resolve;
            });
          }
          if (command === 'desktop_confirm_update') {
            return new Promise<void>((resolve) => {
              resolveUpdateRequest = resolve;
            });
          }
          if (
            command === 'desktop_dismiss_update' ||
            command === 'desktop_open_diagnostics' ||
            command === 'desktop_restart_service'
          )
            return undefined;
          if (command === 'desktop_request_exit') {
            emit('desktop-exit-state', { state: 'confirm' });
            return undefined;
          }
          if (command === 'desktop_api_request') {
            const request = args['request'] as {
              readonly body?: string;
              readonly method: string;
              readonly path: string;
            };
            if (
              request.method === 'POST' &&
              request.path.endsWith('/desktop/recovery/cancel')
            ) {
              recoveryRequired = false;
              return {
                body: JSON.stringify({ status: 'cancelled' }),
                content_type: 'application/json',
                status: 200,
              };
            }
            if (request.path.endsWith('/desktop/recovery')) {
              return {
                body: JSON.stringify({
                  required: recoveryRequired,
                  queued: recoveryRequired ? 1 : 0,
                  running: recoveryRequired ? 1 : 0,
                  analysis: recoveryRequired ? 1 : 0,
                  backtest: recoveryRequired ? 1 : 0,
                  market: 0,
                  other: 0,
                }),
                content_type: 'application/json',
                status: 200,
              };
            }
            const response = await fetch(request.path, {
              body: request.body,
              headers: {
                Accept: 'application/json',
                ...(request.body === undefined
                  ? {}
                  : { 'Content-Type': 'application/json' }),
              },
              method: request.method,
            });
            return {
              body: response.status === 204 ? '' : await response.text(),
              content_type:
                response.headers.get('Content-Type') ?? 'application/json',
              status: response.status,
            };
          }
          throw new Error(`Unexpected desktop command: ${command}`);
        },
      },
    });
  });
}

async function emitExitState(page: Page, payload: unknown) {
  await page.evaluate((next) => {
    (window as unknown as DesktopHarnessWindow).__stockDeskDesktopHarness.emit(
      'desktop-exit-state',
      next,
    );
  }, payload);
}

async function resolveDesktopRequest(page: Page, kind: 'exit' | 'update') {
  await page.evaluate((requestKind) => {
    const harness = (window as unknown as DesktopHarnessWindow)
      .__stockDeskDesktopHarness;
    if (requestKind === 'exit') harness.resolveExit();
    else harness.resolveUpdate();
  }, kind);
}

for (const theme of themeCases) {
  for (const viewport of viewports) {
    test.describe(`${theme.preference}/${theme.resolved} ${viewport.percent}% ${viewport.width}x${viewport.height}`, () => {
      test.use({ viewport });

      test('web workspace dialogs remain bounded, modal, keyboard-safe, and restore focus', async ({
        page,
      }) => {
        test.setTimeout(120_000);
        await installThemePreference(page, theme);
        await installReturningUserState(page);
        await installAnalysisRoutes(page);
        await page.goto('/market');
        await assertResolvedTheme(page, theme);

        const helpTrigger = page.getByRole('button', { name: '帮助' });
        await helpTrigger.click();
        await page.getByRole('menuitem', { name: '重新打开行情引导' }).click();
        const guidance = page.getByRole('dialog', { name: '行情快速引导' });
        await assertNativeModal(guidance, viewport);
        await expect(
          guidance.getByRole('button', { name: '下一步' }),
        ).toBeFocused();
        await assertActionReachable(
          guidance.getByRole('button', { name: '关闭引导' }),
          viewport.height,
        );
        await assertBidirectionalTabTrap(page, guidance);
        await assertDialogAccessibility(page);
        await page.keyboard.press('Escape');
        await expect(guidance).toHaveCount(0);
        await expect(helpTrigger).toBeFocused();

        const aboutTrigger = page.getByRole('button', {
          name: '关于 stock-desk',
        });
        await aboutTrigger.click();
        const about = page.getByRole('dialog', { name: '关于 stock-desk' });
        await assertNativeModal(about, viewport);
        await expect(
          about.getByRole('button', { name: '关闭关于信息' }),
        ).toBeFocused();
        await assertActionReachable(
          about.getByRole('button', { name: '导出诊断包' }),
          viewport.height,
        );
        await assertBidirectionalTabTrap(page, about);
        await assertDialogAccessibility(page);
        await page.keyboard.press('Escape');
        await expect(about).toHaveCount(0);
        await expect(aboutTrigger).toBeFocused();

        await page.goto('/analysis');
        await assertResolvedTheme(page, theme);
        const modelTrigger = page.getByRole('button', { name: '模型设置' });
        await modelTrigger.click();
        let modelDialog = page.getByRole('dialog', { name: '模型设置' });
        await assertNativeModal(modelDialog, viewport);
        await expect(
          modelDialog.getByRole('button', { name: '关闭模型设置' }),
        ).toBeFocused();
        await assertActionReachable(
          modelDialog.getByRole('button', { name: '保存模型配置' }),
          viewport.height,
        );
        await assertBidirectionalTabTrap(page, modelDialog);

        const disableTrigger = modelDialog.getByRole('button', {
          name: '禁用 矩阵模型',
        });
        await disableTrigger.click();
        const disableConfirmation = page.getByRole('alertdialog', {
          name: '确认禁用模型配置？',
        });
        await expect(disableConfirmation).toBeVisible();
        const nativeModelDialog = page.locator(
          'dialog:has([role="alertdialog"])',
        );
        await assertNativeModal(nativeModelDialog, viewport);
        await expect(
          disableConfirmation.getByRole('button', { name: '取消禁用' }),
        ).toBeFocused();
        await assertBidirectionalTabTrap(page, nativeModelDialog);
        await assertDialogAccessibility(page);
        await page.keyboard.press('Escape');
        modelDialog = page.getByRole('dialog', { name: '模型设置' });
        await expect(modelDialog).toBeVisible();
        await expect(disableTrigger).toBeFocused();

        const displayName = modelDialog.getByLabel('显示名称');
        await displayName.fill('未保存的窄屏模型');
        await displayName.focus();
        await page.keyboard.press('Escape');
        const discardConfirmation = page.getByRole('alertdialog', {
          name: '放弃未保存的模型设置？',
        });
        await expect(discardConfirmation).toBeVisible();
        await assertNativeModal(nativeModelDialog, viewport);
        await expect(
          discardConfirmation.getByRole('button', { name: '继续编辑' }),
        ).toBeFocused();
        await assertBidirectionalTabTrap(page, nativeModelDialog);
        await page.keyboard.press('Escape');
        modelDialog = page.getByRole('dialog', { name: '模型设置' });
        await expect(modelDialog).toBeVisible();
        await expect(displayName).toBeFocused();
        await expect(displayName).toHaveValue('未保存的窄屏模型');

        await modelDialog.getByRole('button', { name: '关闭模型设置' }).click();
        await page
          .getByRole('alertdialog', {
            name: '放弃未保存的模型设置？',
          })
          .getByRole('button', { name: '放弃更改' })
          .click();
        await expect(
          page.getByRole('dialog', { name: '模型设置' }),
        ).toHaveCount(0);
        await expect(modelTrigger).toBeFocused();

        await page.goto('/market');
        const search = page.getByRole('combobox', { name: '搜索证券' });
        const searchResult = page.getByRole('option', {
          name: 'Stock Desk Synthetic Alpha (CC0 Demo) 600000.SH',
          exact: true,
        });
        await search.fill('600000');
        await expect(searchResult).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(searchResult).toHaveCount(0);
        await expect(page.getByRole('dialog')).toHaveCount(0);
        await search.press('ArrowDown');
        await expect(searchResult).toBeVisible();
        await searchResult.click();

        const createTrigger = page.getByRole('button', {
          name: '新建自定义池',
        });
        await createTrigger.click();
        let poolDialog = page.getByRole('dialog', { name: '新建自定义池' });
        await assertNativeModal(poolDialog, viewport);
        const poolName = poolDialog.getByRole('textbox', {
          name: '股票池名称',
        });
        await expect(poolName).toBeFocused();
        await assertActionReachable(
          poolDialog.getByRole('button', { name: '取消' }),
          viewport.height,
        );
        await assertBidirectionalTabTrap(page, poolDialog);
        await assertDialogAccessibility(page);

        const uniquePoolName = `矩阵-${theme.preference}-${theme.resolved}-${viewport.width}`;
        await poolName.fill(uniquePoolName);
        await poolName.focus();
        await page.keyboard.press('Escape');
        poolDialog = page.getByRole('dialog', {
          name: '放弃新股票池草稿？',
        });
        await assertNativeModal(poolDialog, viewport);
        await expect(
          poolDialog.getByRole('button', { name: '继续编辑' }),
        ).toBeFocused();
        await assertBidirectionalTabTrap(page, poolDialog);
        await page.keyboard.press('Escape');
        poolDialog = page.getByRole('dialog', { name: '新建自定义池' });
        await expect(poolName).toBeFocused();
        await expect(poolName).toHaveValue(uniquePoolName);

        await poolDialog
          .getByRole('button', { name: /加入Stock Desk Synthetic Alpha/u })
          .click();
        await poolDialog.getByRole('button', { name: '创建股票池' }).click();
        await expect(poolDialog).toHaveCount(0);
        await expect(createTrigger).toBeFocused();

        const workflowTrigger = page.getByRole('button', {
          name: '打开股票池',
        });
        await workflowTrigger.click();
        const workflow = page.getByRole('dialog', {
          name: '选择或管理股票池',
        });
        await assertNativeModal(workflow, viewport);
        await expect(
          workflow.getByRole('button', { name: '关闭股票池' }),
        ).toBeFocused();
        await workflow
          .getByRole('button', { name: new RegExp(uniquePoolName, 'u') })
          .click();
        await expect(workflow.getByText('自定义成员版本 1')).toBeVisible();
        await assertBidirectionalTabTrap(page, workflow);
        await assertDialogAccessibility(page);
        await page.keyboard.press('Escape');
        await expect(workflow).toHaveCount(0);
        await expect(workflowTrigger).toBeFocused();

        const editTrigger = page.getByRole('button', {
          name: '编辑当前股票池',
        });
        await editTrigger.click();
        let editDialog = page.getByRole('dialog', { name: '编辑自定义池' });
        await assertNativeModal(editDialog, viewport);
        await expect(editDialog.getByLabel('股票池名称')).toBeFocused();
        await editDialog.getByRole('button', { name: '删除股票池' }).click();
        editDialog = page.getByRole('dialog', {
          name: '确认删除股票池？',
        });
        await expect(
          editDialog.getByRole('button', { name: '保留股票池' }),
        ).toBeFocused();
        await assertBidirectionalTabTrap(page, editDialog);
        await page.keyboard.press('Escape');
        editDialog = page.getByRole('dialog', { name: '编辑自定义池' });
        await expect(
          editDialog.getByRole('button', { name: '删除股票池' }),
        ).toBeFocused();

        const editName = editDialog.getByLabel('股票池名称');
        await editName.fill(`${uniquePoolName}-待放弃`);
        await editDialog.getByRole('button', { name: '取消' }).click();
        editDialog = page.getByRole('dialog', { name: '放弃股票池更改？' });
        await expect(
          editDialog.getByRole('button', { name: '继续编辑' }),
        ).toBeFocused();
        await editDialog.getByRole('button', { name: '放弃更改' }).click();
        await expect(editDialog).toHaveCount(0);
        await expect(editTrigger).toBeFocused();

        // This matrix shares one real API snapshot. Remove the pool created by
        // this cell so later theme/scale cells cannot cross the first catalog
        // page and accidentally turn the dialog check into a pagination test.
        await editTrigger.click();
        editDialog = page.getByRole('dialog', { name: '编辑自定义池' });
        await editDialog.getByRole('button', { name: '删除股票池' }).click();
        editDialog = page.getByRole('dialog', {
          name: '确认删除股票池？',
        });
        await editDialog.getByRole('button', { name: '确认删除' }).click();
        await expect(editDialog).toHaveCount(0);

        await workflowTrigger.click();
        const cleanupWorkflow = page.getByRole('dialog', {
          name: '选择或管理股票池',
        });
        await expect(
          cleanupWorkflow.getByRole('button', {
            name: new RegExp(uniquePoolName, 'u'),
          }),
        ).toHaveCount(0);
        await cleanupWorkflow
          .getByRole('button', { name: '关闭股票池' })
          .click();
      });

      test('desktop recovery, update, and exit dialogs use the same native safety contract', async ({
        page,
      }) => {
        test.setTimeout(90_000);
        await installThemePreference(page, theme);
        await installReturningUserState(page);
        await installDesktopHarness(page);
        await page.goto('/market');
        await assertResolvedTheme(page, theme);

        let dialog = page.getByRole('dialog', {
          name: '发现上次未完成的任务',
        });
        await assertNativeModal(dialog, viewport);
        await expect(
          dialog.getByRole('button', { name: '取消未完成任务' }),
        ).toBeFocused();
        await assertActionReachable(
          dialog.getByRole('button', { name: '继续未完成任务' }),
          viewport.height,
        );
        await assertBidirectionalTabTrap(page, dialog);
        await assertDialogAccessibility(page);
        await page.keyboard.press('Escape');
        await expect(dialog).toBeVisible();

        await dialog.getByRole('button', { name: '继续未完成任务' }).click();
        await expect(dialog).toContainText('模型 API 并产生费用');
        await expect(
          dialog.getByRole('button', { name: '返回' }),
        ).toBeFocused();
        await assertBidirectionalTabTrap(page, dialog);
        await page.keyboard.press('Escape');
        await expect(dialog).toContainText('模型 API 并产生费用');
        await dialog.getByRole('button', { name: '返回' }).click();
        await expect(
          dialog.getByRole('button', { name: '取消未完成任务' }),
        ).toBeFocused();
        await dialog.getByRole('button', { name: '取消未完成任务' }).click();
        await expect(dialog).toHaveCount(0);

        const installTrigger = page.getByRole('button', {
          name: '查看并安装',
        });
        await expect(installTrigger).toBeVisible();
        await installTrigger.click();
        dialog = page.getByRole('dialog', { name: '确认安装更新' });
        await assertNativeModal(dialog, viewport);
        await expect(
          dialog.getByRole('button', { name: '暂不安装' }),
        ).toBeFocused();
        await assertActionReachable(
          dialog.getByRole('button', { name: '确认下载并安装' }),
          viewport.height,
        );
        await assertBidirectionalTabTrap(page, dialog);
        await assertDialogAccessibility(page);
        await page.keyboard.press('Escape');
        await expect(dialog).toHaveCount(0);
        await expect(installTrigger).toBeFocused();

        await installTrigger.click();
        dialog = page.getByRole('dialog', { name: '确认安装更新' });
        await dialog.getByRole('button', { name: '确认下载并安装' }).click();
        await expect(
          dialog.getByRole('button', { name: '正在请求…' }),
        ).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(dialog).toBeVisible();
        await resolveDesktopRequest(page, 'update');
        await expect(dialog).toHaveCount(0);

        const exitReturnTarget = page.getByRole('button', {
          name: '关于 stock-desk',
        });
        await exitReturnTarget.focus();
        await expect
          .poll(() =>
            page.evaluate(() =>
              (
                window as unknown as DesktopHarnessWindow
              ).__stockDeskDesktopHarness.hasListener('desktop-exit-state'),
            ),
          )
          .toBe(true);

        await emitExitState(page, { state: 'confirm' });
        dialog = page.getByRole('dialog', { name: '确认退出 Stock Desk？' });
        await assertNativeModal(dialog, viewport);
        await expect(
          dialog.getByRole('button', { name: '取消' }),
        ).toBeFocused();
        await assertActionReachable(
          dialog.getByRole('button', { name: '退出应用' }),
          viewport.height,
        );
        await assertBidirectionalTabTrap(page, dialog);
        await page.keyboard.press('Escape');
        await expect(dialog).toHaveCount(0);
        await expect(exitReturnTarget).toBeFocused();

        await emitExitState(page, {
          queued: 2,
          running: 1,
          state: 'blocked',
        });
        dialog = page.getByRole('dialog', { name: '后台任务仍在运行' });
        await assertNativeModal(dialog, viewport);
        await expect(
          dialog.getByRole('button', { name: '返回应用' }),
        ).toBeFocused();
        await expect(dialog).toContainText('排队任务2');
        await expect(dialog).toContainText('运行任务1');
        await assertBidirectionalTabTrap(page, dialog);
        await assertDialogAccessibility(page);
        await page.keyboard.press('Escape');
        await expect(dialog).toHaveCount(0);

        await emitExitState(page, {
          queued: 2,
          running: 1,
          state: 'blocked',
        });
        dialog = page.getByRole('dialog', { name: '后台任务仍在运行' });
        await dialog.getByRole('button', { name: '保存检查点并退出' }).click();
        dialog = page.getByRole('dialog', { name: '正在保存安全检查点' });
        await expect(dialog).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(dialog).toBeVisible();

        await emitExitState(page, {
          queued: 2,
          running: 1,
          state: 'checkpoint_timed_out',
        });
        dialog = page.getByRole('dialog', { name: '尚未到达安全检查点' });
        await assertNativeModal(dialog, viewport);
        await expect(
          dialog.getByRole('button', { name: '返回应用' }),
        ).toBeFocused();
        await assertBidirectionalTabTrap(page, dialog);
        await assertDialogAccessibility(page);
        await dialog.getByRole('button', { name: '重试保存检查点' }).click();
        dialog = page.getByRole('dialog', { name: '正在保存安全检查点' });
        await expect(dialog).toBeVisible();
        await page.keyboard.press('Escape');
        await expect(dialog).toBeVisible();
        await resolveDesktopRequest(page, 'exit');
      });
    });
  }
}
