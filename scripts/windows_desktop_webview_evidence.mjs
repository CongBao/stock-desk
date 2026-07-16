import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

import { chromium } from "@playwright/test";

import {
  captureHandshake,
  runPackagedBacktestEvidence,
} from "./windows_packaged_backtest_evidence.mjs";

const sourceSha = process.env.SOURCE_SHA ?? "";
const sourceTree = process.env.SOURCE_TREE ?? "";
const outputDir = process.env.STOCK_DESK_DESKTOP_EVIDENCE_DIR ?? "";
const endpoint = process.env.STOCK_DESK_DESKTOP_CDP ?? "http://127.0.0.1:9222";
const gitObject = /^[0-9a-f]{40}$/u;

if (
  !gitObject.test(sourceSha) ||
  !gitObject.test(sourceTree) ||
  outputDir === ""
) {
  throw new Error(
    "desktop evidence requires exact source SHA/tree and an output directory",
  );
}

await mkdir(outputDir, { recursive: true });

async function connect() {
  const deadline = Date.now() + 90_000;
  let lastError;
  while (Date.now() < deadline) {
    try {
      return await chromium.connectOverCDP(endpoint);
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 1_000));
    }
  }
  throw new Error(
    `WebView2 CDP endpoint did not become ready: ${String(lastError)}`,
  );
}

function sha256(buffer) {
  return createHash("sha256").update(buffer).digest("hex");
}

async function activeElement(page) {
  return page.evaluate(() => {
    const element = document.activeElement;
    if (!(element instanceof HTMLElement)) return null;
    return {
      ariaLabel: element.getAttribute("aria-label"),
      role: element.getAttribute("role"),
      tag: element.tagName.toLowerCase(),
      text: element.textContent?.trim().slice(0, 120) ?? "",
    };
  });
}

async function visibleDomState(page) {
  return page.evaluate(() => {
    const root = document.documentElement;
    const visibleBox = (selector) => {
      const element = document.querySelector(selector);
      if (!(element instanceof HTMLElement)) return null;
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      if (
        style.display === "none" ||
        style.visibility === "hidden" ||
        box.width <= 0 ||
        box.height <= 0
      ) {
        return null;
      }
      return {
        bottom: box.bottom,
        height: box.height,
        left: box.left,
        right: box.right,
        top: box.top,
        width: box.width,
      };
    };
    const intersects = (first, second, tolerance = 0) =>
      !(
        first.right <= second.left + tolerance ||
        second.right <= first.left + tolerance ||
        first.bottom <= second.top + tolerance ||
        second.bottom <= first.top + tolerance
      );
    const controls = Array.from(
      document.querySelectorAll(
        'a, button, input, select, textarea, [role="tab"]',
      ),
    ).flatMap((element) => {
      if (!(element instanceof HTMLElement)) return [];
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      if (
        style.display === "none" ||
        style.visibility === "hidden" ||
        box.width <= 0 ||
        box.height <= 0
      ) {
        return [];
      }
      const clipsOverflow = (value) =>
        value === "auto" ||
        value === "clip" ||
        value === "hidden" ||
        value === "scroll";
      let left = box.left;
      let right = box.right;
      let top = box.top;
      let bottom = box.bottom;
      let nonScrollableClipPixels = 0;
      let ancestor = element.parentElement;
      while (ancestor !== null) {
        const ancestorStyle = getComputedStyle(ancestor);
        const ancestorBox = ancestor.getBoundingClientRect();
        const clipLeft = ancestorBox.left + ancestor.clientLeft;
        const clipTop = ancestorBox.top + ancestor.clientTop;
        if (clipsOverflow(ancestorStyle.overflowX)) {
          const nextLeft = Math.max(left, clipLeft);
          const nextRight = Math.min(right, clipLeft + ancestor.clientWidth);
          if (
            ancestorStyle.overflowX === "clip" ||
            ancestorStyle.overflowX === "hidden"
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
            ancestorStyle.overflowY === "clip" ||
            ancestorStyle.overflowY === "hidden"
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
      return [
        {
          bottom,
          clippedByAncestor:
            nonScrollableClipPixels > 2 &&
            (left > box.left + 1 ||
              right < box.right - 1 ||
              top > box.top + 1 ||
              bottom < box.bottom - 1),
          clippingPixels: nonScrollableClipPixels,
          height: bottom - top,
          left,
          right,
          top,
          width: right - left,
          name:
            element.getAttribute("aria-label") ??
            element.textContent?.trim().slice(0, 120) ??
            "",
        },
      ];
    });
    const viewportControls = controls.filter(
      (control) =>
        control.bottom > 0 &&
        control.right > 0 &&
        control.top < root.clientHeight &&
        control.left < root.clientWidth,
    );
    const controlClipping = viewportControls
      .filter((control) => control.clippedByAncestor)
      .map((control) => ({
        name: control.name,
        pixels: control.clippingPixels,
      }));
    const controlOverlap = [];
    for (let first = 0; first < viewportControls.length; first += 1) {
      const left = viewportControls[first];
      if (left === undefined) continue;
      for (
        let second = first + 1;
        second < viewportControls.length;
        second += 1
      ) {
        const right = viewportControls[second];
        if (right !== undefined && intersects(left, right)) {
          controlOverlap.push([left.name, right.name]);
        }
      }
    }
    const rail = visibleBox(".navigation-rail");
    const workspace = visibleBox("#main-content");
    const context = visibleBox("#context-panel");
    const contextElement = document.querySelector("#context-panel");
    const shellOverlap =
      (rail !== null && workspace !== null && intersects(rail, workspace, 1)) ||
      (context !== null &&
        workspace !== null &&
        contextElement instanceof HTMLElement &&
        getComputedStyle(contextElement).position !== "fixed" &&
        intersects(context, workspace, 1));
    const critical = Array.from(
      document.querySelectorAll(
        "#main-content button:not([disabled]), #main-content a[href], #main-content input:not([disabled]), #main-content select:not([disabled]), #main-content textarea:not([disabled])",
      ),
    ).find((element) => {
      if (!(element instanceof HTMLElement)) return false;
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      return (
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        box.width > 0 &&
        box.height > 0
      );
    });
    const criticalBox =
      critical instanceof HTMLElement ? critical.getBoundingClientRect() : null;
    const criticalControlClipped =
      criticalBox !== null &&
      (criticalBox.left < -1 ||
        criticalBox.top < -1 ||
        criticalBox.right > root.clientWidth + 1 ||
        criticalBox.bottom > root.clientHeight + 1);
    const themeSelector = document.querySelector('[aria-label="界面主题"]');
    const focusStyle =
      themeSelector instanceof HTMLElement
        ? getComputedStyle(themeSelector)
        : null;
    const focusVisible =
      themeSelector === document.activeElement &&
      focusStyle !== null &&
      focusStyle.outlineStyle !== "none" &&
      focusStyle.outlineWidth !== "0px";
    const topbar = document.querySelector(".topbar-state");
    const topbarBox =
      topbar instanceof HTMLElement ? topbar.getBoundingClientRect() : null;
    const topbarVisible =
      topbar instanceof HTMLElement &&
      getComputedStyle(topbar).display !== "none" &&
      getComputedStyle(topbar).visibility !== "hidden" &&
      topbarBox !== null &&
      topbarBox.width > 0 &&
      topbarBox.height > 0;
    const statusSymbol = document.querySelector(".topbar-state .status-symbol");
    const statusSymbolBox =
      statusSymbol instanceof HTMLElement
        ? statusSymbol.getBoundingClientRect()
        : null;
    const statusCues = Array.from(
      document.querySelectorAll(
        '[role="status"], [role="alert"], [aria-live="polite"], [aria-live="assertive"]',
      ),
    ).flatMap((element) => {
      if (!(element instanceof HTMLElement)) return [];
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      if (
        style.display === "none" ||
        style.visibility === "hidden" ||
        box.width <= 0 ||
        box.height <= 0
      ) {
        return [];
      }
      return [
        {
          cue: (
            element.getAttribute("aria-label") ??
            element.getAttribute("title") ??
            element.textContent ??
            ""
          ).trim(),
          encodedState:
            element.hasAttribute("data-state") ||
            element.hasAttribute("data-status"),
        },
      ];
    });
    const visibleStateCues = Array.from(
      document.querySelectorAll("[data-state], [data-status]"),
    ).flatMap((element) => {
      if (!(element instanceof HTMLElement)) return [];
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      if (
        style.display === "none" ||
        style.visibility === "hidden" ||
        box.width <= 0 ||
        box.height <= 0
      ) {
        return [];
      }
      return [
        (
          element.getAttribute("aria-label") ??
          element.getAttribute("title") ??
          element.textContent ??
          ""
        ).trim(),
      ];
    });
    return {
      clientHeight: root.clientHeight,
      clientWidth: root.clientWidth,
      controlClipping,
      controlOverlap,
      controls,
      criticalControlClipped,
      devicePixelRatio: globalThis.devicePixelRatio,
      focusVisible,
      horizontalOverflow: root.scrollWidth > root.clientWidth + 1,
      nonColorStatus: {
        allStatusCuesHaveText: statusCues.every(
          (item) => item.cue.length > 0 || !item.encodedState,
        ),
        allVisibleStateCuesHaveText: visibleStateCues.every(
          (cue) => cue.length > 0,
        ),
        state: topbar?.getAttribute("data-state") ?? null,
        symbolVisible:
          statusSymbolBox !== null &&
          statusSymbolBox.width > 0 &&
          statusSymbolBox.height > 0,
        text: topbar?.textContent?.trim().slice(0, 240) ?? "",
        topbarVisible,
      },
      scrollHeight: root.scrollHeight,
      scrollWidth: root.scrollWidth,
      shellOverlap,
      theme: root.dataset.theme ?? null,
      themePreference: root.dataset.themePreference ?? null,
    };
  });
}

function assertUsableState(state, label) {
  if (state.horizontalOverflow) {
    throw new Error(`horizontal overflow at ${label}`);
  }
  if (state.shellOverlap) {
    throw new Error(`shell overlap at ${label}`);
  }
  if (state.controlOverlap.length > 0) {
    throw new Error(
      `control overlap at ${label}: ${JSON.stringify(state.controlOverlap)}`,
    );
  }
  if (state.controlClipping.length > 0) {
    throw new Error(
      `control clipping at ${label}: ${JSON.stringify(state.controlClipping)}`,
    );
  }
  if (state.criticalControlClipped) {
    throw new Error(`critical control clipped at ${label}`);
  }
  if (!state.focusVisible) {
    throw new Error(`focus is not visibly rendered at ${label}`);
  }
  if (
    state.nonColorStatus.text === "" ||
    state.nonColorStatus.state === null ||
    (state.nonColorStatus.topbarVisible &&
      !state.nonColorStatus.symbolVisible) ||
    !state.nonColorStatus.allStatusCuesHaveText ||
    !state.nonColorStatus.allVisibleStateCuesHaveText
  ) {
    throw new Error(`non-color status cue is incomplete at ${label}`);
  }
}

async function dismissAutomaticGuidance(page, route) {
  const dialog = page.locator(".guidance-dialog");
  if (!route.guidanceExpected) {
    if ((await dialog.count()) !== 0) {
      throw new Error(`unexpected automatic guidance on ${route.path}`);
    }
    return "not-applicable-no-tour";
  }
  // Every guidance-enabled route below is a first visit in a fresh packaged
  // data root. Its tour must appear before the route can be measured. A missing
  // or late preference response fails closed instead of allowing a delayed
  // dialog to race the layout matrix.
  await dialog.waitFor({ state: "visible", timeout: 15_000 });

  const skip = dialog.getByRole("button", { name: "跳过引导" });
  await skip.waitFor({ state: "visible", timeout: 5_000 });
  await skip.click();
  await dialog.waitFor({ state: "hidden", timeout: 15_000 });
  return "fresh-page-dismissed";
}

async function focusEvidence(page) {
  const themeSelector = page.getByRole("combobox", { name: "界面主题" });
  await themeSelector.scrollIntoViewIfNeeded();
  await themeSelector.focus();
  const start = await activeElement(page);
  const critical = page
    .locator(
      "#main-content button:not([disabled]):visible, #main-content a[href]:visible, #main-content input:not([disabled]):visible, #main-content select:not([disabled]):visible, #main-content textarea:not([disabled]):visible",
    )
    .first();
  if ((await critical.count()) > 0) await critical.scrollIntoViewIfNeeded();
  await themeSelector.focus();
  await page.keyboard.press("Tab");
  const afterTab = await activeElement(page);
  await themeSelector.focus();
  if (
    start?.ariaLabel !== "界面主题" ||
    afterTab === null ||
    (afterTab.ariaLabel ?? afterTab.text) === ""
  ) {
    throw new Error(
      "packaged WebView keyboard focus did not advance from theme control",
    );
  }
  return { afterTab, start };
}

const effectiveMatrix = [
  { percent: 100, width: 1366, height: 768 },
  { percent: 125, width: 1093, height: 614 },
  { percent: 150, width: 911, height: 512 },
  { percent: 175, width: 781, height: 439 },
  { percent: 200, width: 683, height: 384 },
];

const coreRoutes = [
  { label: "行情", path: "/market", guidanceExpected: true },
  { label: "自定义公式", path: "/formulas", guidanceExpected: true },
  { label: "策略回测", path: "/backtests", guidanceExpected: true },
  { label: "智能分析", path: "/analysis", guidanceExpected: true },
  { label: "任务中心", path: "/tasks", guidanceExpected: true },
  { label: "设置", path: "/settings", guidanceExpected: false },
];

const themeCases = [
  { preference: "light", resolved: "light", systemScheme: "light" },
  { preference: "dark", resolved: "dark", systemScheme: "dark" },
  {
    preference: "system",
    resolved: "light",
    systemScheme: "light",
    evidenceKind: "tauri-webview-cdp-system-media-not-windows-theme",
  },
  {
    preference: "system",
    resolved: "dark",
    systemScheme: "dark",
    evidenceKind: "tauri-webview-cdp-system-media-not-windows-theme",
  },
];

async function ensureWorkspaceReady(page) {
  const navigation = page.locator("#primary-navigation");
  await navigation.waitFor({ state: "visible" });
  if ((await page.getByText("只读演示", { exact: true }).count()) > 0) {
    throw new Error("packaged backtest evidence refuses read-only demo mode");
  }
  const onboarding = await page.evaluate(async () => {
    const response = await globalThis.__TAURI_INTERNALS__.invoke(
      "desktop_api_request",
      {
        request: {
          method: "GET",
          path: "/api/v1/onboarding/state",
          body: null,
        },
      },
    );
    return { status: response.status, body: JSON.parse(response.body) };
  });
  if (
    onboarding.status !== 200 ||
    onboarding.body.status !== "completed" ||
    onboarding.body.demo_mode !== false
  ) {
    throw new Error(
      "packaged backtest evidence requires completed real-mode setup",
    );
  }
  return "hash-bound-public-fixture-real-mode";
}

async function waitForDesktopReady(page) {
  const deadline = Date.now() + 65_000;
  let latestState = { state: "starting" };
  while (Date.now() < deadline) {
    latestState = await page.evaluate(async () => {
      const internals = globalThis.__TAURI_INTERNALS__;
      if (internals === undefined || typeof internals.invoke !== "function") {
        throw new Error("Tauri invoke bridge is unavailable");
      }
      return internals.invoke("desktop_runtime_state");
    });
    if (latestState.state === "ready") return latestState;
    if (latestState.state === "recovery") {
      throw new Error(
        `packaged desktop entered recovery before onboarding: ${JSON.stringify(latestState)}`,
      );
    }
    await page.waitForTimeout(500);
  }
  throw new Error(
    `packaged desktop did not become ready before onboarding: ${JSON.stringify(latestState)}`,
  );
}

async function navigateToCoreRoute(page, route) {
  const currentPath = await page.evaluate(() => globalThis.location.pathname);
  if (currentPath === route.path) return "already-active";

  const link = page.locator(`#primary-navigation a[href="${route.path}"]`);
  let transition;
  if ((await link.count()) > 0) {
    await link.click();
    transition = "navigation-link";
  } else {
    throw new Error(`packaged navigation link is missing: ${route.path}`);
  }
  await page.waitForFunction(
    (expectedPath) => globalThis.location.pathname === expectedPath,
    route.path,
  );
  return transition;
}

async function reloadWorkspaceAfterPackagedBacktests(page) {
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector("#root", { state: "visible", timeout: 60_000 });
  const runtime = await waitForDesktopReady(page);
  const entryMode = await ensureWorkspaceReady(page);
  await page
    .getByRole("combobox", { name: "界面主题" })
    .waitFor({ state: "visible" });
  return {
    entry_mode: entryMode,
    runtime_state: runtime.state,
    surface: "reloaded-packaged-tauri-webview",
  };
}

const browser = await connect();
try {
  const pages = browser.contexts().flatMap((context) => context.pages());
  let page;
  for (const candidate of pages) {
    if (
      await candidate
        .evaluate(() => "__TAURI_INTERNALS__" in globalThis)
        .catch(() => false)
    ) {
      page = candidate;
      break;
    }
  }
  if (page === undefined)
    throw new Error("CDP did not expose the packaged Tauri WebView");

  await page.waitForSelector("#root", { state: "visible", timeout: 60_000 });
  await page
    .getByRole("combobox", { name: "界面主题" })
    .waitFor({ state: "visible" });
  const actualViewport = await visibleDomState(page);
  const actualUrl = page.url();
  const desktopRuntime = await waitForDesktopReady(page);
  const workspaceEntryMode = await ensureWorkspaceReady(page);
  const cdp = await page.context().newCDPSession(page);
  const screenshotRecords = [];
  const matrixRecords = [];
  const effectiveScreenshotRecords = [];
  for (const route of coreRoutes) {
    const routeTransition = await navigateToCoreRoute(page, route);
    await page.locator("#main-content h1, #main-content h2").first().waitFor({
      state: "visible",
    });
    const automaticGuidanceDisposition = await dismissAutomaticGuidance(
      page,
      route,
    );

    for (const themeCase of themeCases) {
      await cdp.send("Emulation.setEmulatedMedia", {
        features: [
          { name: "prefers-color-scheme", value: themeCase.systemScheme },
        ],
      });
      await page
        .getByRole("combobox", { name: "界面主题" })
        .selectOption(themeCase.preference);
      await page.waitForFunction(
        ({ preference, resolved }) =>
          document.documentElement.dataset.themePreference === preference &&
          document.documentElement.dataset.theme === resolved,
        { preference: themeCase.preference, resolved: themeCase.resolved },
      );

      for (const item of effectiveMatrix) {
        await cdp.send("Emulation.setDeviceMetricsOverride", {
          deviceScaleFactor: 1,
          height: item.height,
          mobile: false,
          width: item.width,
        });
        await page.waitForFunction(
          (collapsed) =>
            document
              .querySelector(".app-shell")
              ?.getAttribute("data-navigation-collapsed") === String(collapsed),
          item.width <= 1200,
        );
        const keyboard = await focusEvidence(page);
        const state = await visibleDomState(page);
        const label = `${route.path} ${themeCase.preference}/${themeCase.systemScheme} ${String(item.percent)}% equivalent viewport`;
        assertUsableState(state, label);
        const evidence = {
          ...item,
          route: route.path,
          routeLabel: route.label,
          routeTransition,
          automaticGuidanceDisposition,
          preference: themeCase.preference,
          resolvedTheme: themeCase.resolved,
          systemScheme: themeCase.systemScheme,
          evidenceKind:
            themeCase.evidenceKind ??
            "tauri-webview-cdp-effective-viewport-not-os-dpi",
          keyboard,
          state,
        };
        matrixRecords.push(evidence);

        if (
          route.path === "/market" &&
          item.percent === 100 &&
          themeCase.preference === themeCase.resolved
        ) {
          const fileName = `tauri-webview-${themeCase.resolved}.png`;
          const bytes = await page.screenshot({
            animations: "disabled",
            path: path.join(outputDir, fileName),
          });
          screenshotRecords.push({
            file: fileName,
            route: route.path,
            sha256: sha256(bytes),
            theme: themeCase.resolved,
          });
        }
        if (
          route.path === "/tasks" &&
          themeCase.preference === "system" &&
          themeCase.systemScheme === "dark"
        ) {
          const fileName = `tauri-webview-effective-${String(item.percent)}.png`;
          const bytes = await page.screenshot({
            animations: "disabled",
            path: path.join(outputDir, fileName),
          });
          effectiveScreenshotRecords.push({
            file: fileName,
            percent: item.percent,
            route: route.path,
            sha256: sha256(bytes),
            themePreference: "system",
            resolvedTheme: "dark",
          });
        }
      }
    }
  }
  await cdp.send("Emulation.clearDeviceMetricsOverride");
  await cdp.send("Emulation.setEmulatedMedia", { features: [] });

  const packagedBacktests = await runPackagedBacktestEvidence(page, outputDir);
  const postBacktestWorkspaceRestore =
    await reloadWorkspaceAfterPackagedBacktests(page);

  await navigateToCoreRoute(page, coreRoutes[0]);
  await page.getByRole("combobox", { name: "界面主题" }).selectOption("system");
  const finalKeyboard = await focusEvidence(page);
  const keyboardStart = finalKeyboard.start;
  const keyboardAfterTab = finalKeyboard.afterTab;

  await page.evaluate(async () => {
    const internals = globalThis.__TAURI_INTERNALS__;
    if (internals === undefined || typeof internals.invoke !== "function") {
      throw new Error("Tauri invoke bridge is unavailable");
    }
    await internals.invoke("desktop_request_exit");
  });
  const exitDialog = page.getByRole("dialog", {
    name: "确认退出 Stock Desk？",
  });
  await exitDialog.waitFor({ state: "visible" });
  const cancel = page.getByRole("button", { name: "取消", exact: true });
  if (
    !(await cancel.evaluate((element) => element === document.activeElement))
  ) {
    throw new Error(
      "packaged exit dialog did not focus the safe cancel action",
    );
  }
  await page.keyboard.press("Escape");
  await exitDialog.waitFor({ state: "hidden" });

  const manifest = {
    schema_version: "stock-desk-packaged-webview-evidence-v1",
    source_sha: sourceSha,
    source_tree: sourceTree,
    actual_tauri_webview: true,
    cdp_test_mode: true,
    url: actualUrl,
    actual_viewport: actualViewport,
    desktop_runtime: desktopRuntime,
    workspace_entry_mode: workspaceEntryMode,
    keyboard: { after_tab: keyboardAfterTab, start: keyboardStart },
    themes: screenshotRecords,
    effective_scale_matrix: effectiveScreenshotRecords,
    core_route_theme_scale_matrix: matrixRecords,
    packaged_backtests: {
      manifest: "packaged-backtest-evidence.json",
      schema_version: packagedBacktests.schema_version,
      cell_count: packagedBacktests.cells.length,
      checkpoint_run_id: packagedBacktests.checkpoint.run_id,
    },
    post_backtest_workspace_restore: postBacktestWorkspaceRestore,
    limitations: [
      "Every 100-200 percent matrix row uses CDP CSS viewport equivalence inside the packaged Tauri WebView; it is not Windows OS DPI and does not prove native Windows DPI behavior.",
      "System Light/Dark rows emulate prefers-color-scheme through CDP; they are not a Windows theme setting or native theme-change event.",
      "Native Windows DPI and shell chrome are recorded separately by the PowerShell host harness for the runner current scale only.",
    ],
  };
  await writeFile(
    path.join(outputDir, "tauri-webview-evidence.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
    "utf8",
  );

  const exitActivity = await page.evaluate(async () => {
    const internals = globalThis.__TAURI_INTERNALS__;
    const runtime = await internals.invoke("desktop_runtime_state");
    if (runtime.state !== "ready") {
      return {
        runtime,
        activity: { status: "unavailable_while_not_ready" },
      };
    }
    const response = await internals.invoke("desktop_api_request", {
      request: {
        method: "GET",
        path: "/api/desktop/activity",
        body: null,
      },
    });
    return {
      runtime,
      activity: { status: response.status, body: JSON.parse(response.body) },
    };
  });
  console.log(`STOCK_DESK_EXIT_ACTIVITY ${JSON.stringify(exitActivity)}`);

  await captureHandshake("os-real-click-exit", {
    candidate_sha256: process.env.STOCK_DESK_CANDIDATE_SHA256,
    phase: "ready-for-native-titlebar-and-dialog-clicks",
  });
  console.log(
    "STOCK_DESK_EXIT_OBSERVATION " +
      JSON.stringify({
        input_method: "win32-sendinput-physical-mouse",
        page_closed: page.isClosed(),
      }),
  );
} finally {
  await browser.close().catch(() => undefined);
}
