import { expect, test, type Page } from '@playwright/test';

const runId = '11111111-1111-1111-1111-111111111111';
const digest = (character: string) => `sha256:${character.repeat(64)}`;
const now = '2026-07-08T08:00:00Z';
const navigationLabels = [
  '行情',
  '自定义公式',
  '策略回测',
  '智能分析',
  '任务中心',
  '设置',
] as const;

const model = {
  id: digest('a'),
  public_config_hash: digest('a'),
  display_name: '研究模型',
  provider: 'deepseek',
  base_url: 'https://api.deepseek.com',
  model: 'deepseek-chat',
  temperature: 0.1,
  timeout: 90.0,
  max_output: 4096,
  api_key_configured: true,
  masked_api_key: 'sk-a•••••••tail',
  status: 'verified',
  revision: 1,
  verified_at: now,
  last_tested_at: now,
  error_code: null,
  supersedes_id: null,
  created_at: now,
  updated_at: now,
};

const stages = [
  ['market', -4, 'data'],
  ['fundamentals', -3, 'data'],
  ['announcements', -2, 'data'],
  ['news', -1, 'data'],
  ['technical', 0, 'role'],
  ['fundamental_news', 1, 'role'],
  ['bull', 2, 'role'],
  ['bear', 3, 'role'],
  ['risk_decision', 4, 'role'],
].map(([stage, ordinal, kind]) => ({
  stage,
  ordinal,
  kind,
  status: 'succeeded',
  attempt_count: 1,
  source_run_id: null,
  failure_code: null,
  retryable: null,
  started_at: now,
  finished_at: now,
  duration_ms: 120,
  retry_allowed: false,
}));

const overview = {
  run_id: runId,
  task_id: 'task-1',
  symbol: '600000.SH',
  parent_run_id: null,
  requested_stage: null,
  status: 'succeeded',
  task_status: 'succeeded',
  progress: 1,
  cancel_requested: false,
  current_stage: null,
  snapshot_id: digest('b'),
  report_id: digest('c'),
  failure_code: null,
  model_config_id: digest('a'),
  model_provider: 'deepseek',
  model_name: 'deepseek-chat',
  created_at: now,
  updated_at: now,
  started_at: now,
  finished_at: now,
  duration_ms: 1080,
};

const evidence = {
  evidence_id: digest('d'),
  snapshot_id: digest('b'),
  section_id: digest('e'),
  section_kind: 'fundamentals',
  canonical_source: 'tushare',
  source_record: 'income:600000.SH:2025',
  source_url: 'https://example.com/source',
  published_at: null,
  data_cutoff: now,
  fetched_at: now,
  dataset_version: '2026-07-08',
  excerpt: '净利润同比增长，现金流改善。',
  quality_flags: [],
};

const claim = {
  text: '盈利质量持续改善',
  evidence_ids: [digest('d')],
  stance: 'support',
};

const report = {
  schema_version: 'analysis-report-v1',
  report_id: digest('c'),
  snapshot_id: digest('b'),
  status: 'complete',
  rating: 'bullish',
  confidence: 0.78,
  confidence_explanation: '证据覆盖关键财务与风险维度。',
  core_judgments: [claim],
  bull_claims: [{ ...claim, text: '收入结构优化' }],
  bear_claims: [{ ...claim, text: '息差仍承压', stance: 'oppose' }],
  risks: [{ ...claim, text: '资产质量波动', stance: 'uncertain' }],
  evidence_items: [evidence],
  role_outputs: [],
  model_metadata: [],
  quality_flags: [],
  quality_notes: [],
  missing_modules: [],
  missing_sections: [],
  recovery_actions: [],
  generated_at: now,
  disclaimer: '本报告仅为研究辅助信息，不构成投资建议、个性化建议或交易指令。',
  retry_actions: [],
  failed_modules: [],
  blocked_modules: [],
  stage_failures: [],
};

async function installStubs(page: Page) {
  await page.route('**/api/**', async (route) => {
    const { pathname } = new URL(route.request().url());
    if (!pathname.startsWith('/api/')) {
      await route.fallback();
      return;
    }
    let body: unknown = [];
    if (pathname.endsWith('/health'))
      body = { name: 'stock-desk', status: 'ok', api_version: 'v1' };
    else if (pathname.endsWith('/settings/models'))
      body = { items: [model], next_cursor: null };
    else if (pathname.endsWith(`/analysis/${runId}/report`)) body = report;
    else if (pathname.endsWith(`/analysis/${runId}`))
      body = { ...overview, stages };
    else if (pathname.endsWith('/analysis'))
      body = { items: [overview], next_cursor: null };
    else if (pathname.includes('/market/pools'))
      body = { items: [], next_cursor: null };
    else if (pathname.endsWith('/market/schedules/daily')) body = {};
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });
}

function intersects(
  left: { x: number; y: number; width: number; height: number },
  right: { x: number; y: number; width: number; height: number },
) {
  return !(
    left.x + left.width <= right.x ||
    right.x + right.width <= left.x ||
    left.y + left.height <= right.y ||
    right.y + right.height <= left.y
  );
}

async function expectNoPageOverflow(page: Page) {
  expect(
    await page.evaluate(() => {
      const browserGlobal = globalThis as unknown as {
        document: {
          documentElement: { clientWidth: number; scrollWidth: number };
        };
      };
      const root = browserGlobal.document.documentElement;
      return root.scrollWidth <= root.clientWidth;
    }),
  ).toBe(true);
}

async function openReport(page: Page) {
  await page.goto('/analysis');
  await page.getByRole('button', { name: /查看 600000.SH/u }).click();
  await expect(
    page.getByRole('heading', { name: '600000.SH 智能分析' }),
  ).toBeVisible();
}

test.beforeEach(async ({ page }) => installStubs(page));

for (const viewport of [
  { name: 'wide desktop', width: 1600, height: 900 },
  { name: '1366 desktop', width: 1366, height: 768 },
]) {
  test(`${viewport.name} keeps synchronized three-column analysis unclipped`, async ({
    page,
  }) => {
    await page.setViewportSize({
      width: viewport.width,
      height: viewport.height,
    });
    await openReport(page);
    const [rail, workspace, process, conclusion, evidencePanel] =
      await Promise.all([
        page.locator('.navigation-rail').boundingBox(),
        page.locator('main.workspace').boundingBox(),
        page.getByRole('complementary', { name: '分析流程' }).boundingBox(),
        page.getByRole('article', { name: '研究结论' }).boundingBox(),
        page.getByRole('complementary', { name: '证据详情' }).boundingBox(),
      ]);
    expect(
      rail && workspace && process && conclusion && evidencePanel,
    ).toBeTruthy();
    if (rail && workspace && process && conclusion && evidencePanel) {
      expect(intersects(rail, workspace)).toBe(false);
      expect(intersects(process, conclusion)).toBe(false);
      expect(intersects(process, evidencePanel)).toBe(false);
      expect(intersects(conclusion, evidencePanel)).toBe(false);
    }
    await expect(
      page.getByRole('complementary', { name: '上下文状态' }),
    ).toBeHidden();
    expect(
      await page
        .locator('main.workspace')
        .evaluate(
          (element) =>
            (element as unknown as { scrollWidth: number; clientWidth: number })
              .scrollWidth <=
            (element as unknown as { scrollWidth: number; clientWidth: number })
              .clientWidth,
        ),
    ).toBe(true);
    expect(
      await page
        .locator('.analysis-report-workspace')
        .evaluate(
          (element) =>
            (element as unknown as { scrollWidth: number; clientWidth: number })
              .scrollWidth <=
            (element as unknown as { scrollWidth: number; clientWidth: number })
              .clientWidth,
        ),
    ).toBe(true);
    await expectNoPageOverflow(page);
  });
}

for (const viewport of [
  { name: '1181 expanded landscape', width: 1181, height: 700 },
  { name: 'narrow landscape', width: 1100, height: 700 },
  { name: 'tablet', width: 768, height: 1024 },
  { name: 'mobile', width: 390, height: 844 },
  { name: '200 percent zoom equivalent', width: 800, height: 450 },
]) {
  test(`${viewport.name} uses bounded non-overlapping drawers`, async ({
    page,
  }) => {
    await page.setViewportSize({
      width: viewport.width,
      height: viewport.height,
    });
    await openReport(page);
    const shell = page.locator('.app-shell');
    await expect(shell).toHaveAttribute('data-navigation-collapsed', 'true');
    for (const label of navigationLabels) {
      const navigationLink = page.getByRole('link', {
        name: label,
        exact: true,
      });
      await expect(navigationLink.locator('svg')).toBeVisible();
      await expect(navigationLink.locator('.nav-label')).toBeHidden();
      await expect(navigationLink).toHaveAttribute('title', label);
    }
    if (viewport.width <= 420) {
      const boxes = await Promise.all(
        navigationLabels.map((label) =>
          page.getByRole('link', { name: label, exact: true }).boundingBox(),
        ),
      );
      expect(boxes.every((box) => box !== null)).toBe(true);
      const visibleBoxes = boxes.filter((box) => box !== null);
      const firstX = visibleBoxes[0]?.x;
      expect(firstX).toBeDefined();
      for (const box of visibleBoxes) {
        expect(box.x).toBe(firstX);
        expect(box.width).toBeGreaterThanOrEqual(44);
        expect(box.height).toBeGreaterThanOrEqual(44);
      }
    }
    if (viewport.width === 1181) {
      await page.getByRole('button', { name: '展开主导航' }).click();
      await expect(shell).toHaveAttribute('data-navigation-collapsed', 'false');
    }

    const toolbar = page.getByRole('toolbar', { name: '报告面板工具栏' });
    await toolbar.scrollIntoViewIfNeeded();
    await page.getByRole('button', { name: '查看分析流程' }).click();
    const drawer = page.locator('#analysis-process-drawer[data-open="true"]');
    await expect(drawer).toBeVisible();
    const conclusion = page.getByRole('article', { name: '研究结论' });
    const [rail, workspace, toolbarBox, drawerBox, conclusionBox] =
      await Promise.all([
        page.locator('.navigation-rail').boundingBox(),
        page.locator('main.workspace').boundingBox(),
        toolbar.boundingBox(),
        drawer.boundingBox(),
        conclusion.boundingBox(),
      ]);
    expect(
      rail && workspace && toolbarBox && drawerBox && conclusionBox,
    ).toBeTruthy();
    if (rail && workspace && toolbarBox && drawerBox && conclusionBox) {
      expect(intersects(rail, workspace)).toBe(false);
      expect(intersects(rail, drawerBox)).toBe(false);
      expect(intersects(toolbarBox, drawerBox)).toBe(false);
      expect(intersects(drawerBox, conclusionBox)).toBe(false);
    }
    await page.getByRole('button', { name: '关闭分析流程' }).click();
    await expect(
      page.getByRole('button', { name: '查看分析流程' }),
    ).toBeFocused();

    await page.getByRole('button', { name: '查看证据' }).click();
    const evidenceDrawer = page.locator(
      '#analysis-evidence-drawer[data-open="true"]',
    );
    await expect(evidenceDrawer).toBeVisible();
    const [
      evidenceRailBox,
      evidenceWorkspaceBox,
      evidenceBox,
      evidenceToolbarBox,
      evidenceConclusionBox,
    ] = await Promise.all([
      page.locator('.navigation-rail').boundingBox(),
      page.locator('main.workspace').boundingBox(),
      evidenceDrawer.boundingBox(),
      toolbar.boundingBox(),
      conclusion.boundingBox(),
    ]);
    expect(
      evidenceRailBox &&
        evidenceWorkspaceBox &&
        evidenceBox &&
        evidenceToolbarBox &&
        evidenceConclusionBox,
    ).toBeTruthy();
    if (
      evidenceRailBox &&
      evidenceWorkspaceBox &&
      evidenceBox &&
      evidenceToolbarBox &&
      evidenceConclusionBox
    ) {
      expect(intersects(evidenceRailBox, evidenceWorkspaceBox)).toBe(false);
      expect(intersects(evidenceRailBox, evidenceBox)).toBe(false);
      expect(intersects(evidenceToolbarBox, evidenceBox)).toBe(false);
      expect(intersects(evidenceBox, evidenceConclusionBox)).toBe(false);
    }
    await page.getByRole('button', { name: '关闭证据' }).click();
    await expect(page.getByRole('button', { name: '查看证据' })).toBeFocused();

    if ((await shell.getAttribute('data-navigation-collapsed')) === 'false') {
      await page.getByRole('button', { name: '收起主导航' }).click();
    }
    const expand = page.getByRole('button', { name: '展开主导航' });
    await expand.click();
    await expect(shell).toHaveAttribute('data-navigation-collapsed', 'false');
    await expect(
      page.getByRole('button', { name: '收起主导航' }),
    ).toBeVisible();
    await page.getByRole('button', { name: '收起主导航' }).click();
    await expect(shell).toHaveAttribute('data-navigation-collapsed', 'true');
    await expectNoPageOverflow(page);
  });
}
