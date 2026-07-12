import type { Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';

import { expect, test } from './fixtures';

const MACD_NAME = 'Stock Desk Demo MACD (CC0 synthetic)';
const CUSTOM_NAME = 'Stock Desk Demo custom wave (CC0 synthetic)';
const PARTIAL_POOL_NAME = 'Stock Desk Synthetic Demo Index (CC0)';
const START = '2024-02-10';
const END = '2024-06-28';

async function noHorizontalOverflow(page: Page) {
  return page.evaluate(() => {
    const browserGlobal = globalThis as unknown as {
      document: {
        documentElement: { clientWidth: number; scrollWidth: number };
      };
    };
    const root = browserGlobal.document.documentElement;
    return root.scrollWidth <= root.clientWidth;
  });
}

async function navigationDoesNotOverlapWorkspace(page: Page) {
  const [rail, workspace] = await Promise.all([
    page.locator('.navigation-rail').boundingBox(),
    page.locator('main.workspace').boundingBox(),
  ]);
  if (rail === null || workspace === null) return false;
  const separatedHorizontally = rail.x + rail.width <= workspace.x;
  const separatedVertically = rail.y + rail.height <= workspace.y;
  return separatedHorizontally || separatedVertically;
}

async function chooseFormula(page: Page, name: string) {
  await page.getByLabel('保存的交易公式').selectOption({ label: name });
  await page.getByRole('button', { name: '下一步' }).click();
}

async function finishCommonSteps(page: Page) {
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByRole('button', { name: '运行预检' }).click();
}

async function waitForReport(page: Page) {
  await expect(page.getByRole('heading', { name: '回测结论' })).toBeVisible({
    timeout: 30_000,
  });
}

test.describe.serial('Stage 3 real local backtesting', () => {
  let completedRunUrl = '';

  test('held pool run reaches persisted terminal cancellation through the worker', async ({
    page,
    request,
  }) => {
    test.setTimeout(60_000);
    const response = await request.get('/api/backtests?limit=100');
    const payload = (await response.json()) as {
      readonly items: readonly {
        readonly run_id: string;
        readonly status: string;
      }[];
    };
    const held =
      payload.items.find((item) => item.status === 'running') ??
      payload.items.find((item) => item.status === 'cancelled');
    expect(held).toBeDefined();
    await page.goto(`/backtests/${held?.run_id ?? ''}`);
    if (held?.status === 'running') {
      await page.getByRole('button', { name: '取消回测' }).click();
      await expect(page.getByText(/取消不会删除已持久化的数据/u)).toBeVisible();
    }
    await expect(
      page.locator('.run-progress .status-badge[data-status="cancelled"]'),
    ).toBeVisible({ timeout: 45_000 });
    await expect(page.getByRole('button', { name: '取消回测' })).toHaveCount(0);
    await expect(page.getByRole('heading', { name: '回测结论' })).toBeVisible();
    const metadata = page.locator('.report-metadata');
    await expect(metadata).toContainText('公式版本');
    await expect(metadata).toContainText('公式校验和');
    await expect(metadata).toContainText('回测引擎');
    await expect(metadata).toContainText('信号数据源');
    await expect
      .poll(async () => {
        const current = await request.get(
          `/api/backtests/${held?.run_id ?? ''}`,
        );
        const body = (await current.json()) as {
          readonly status: string;
          readonly finished_at: string | null;
        };
        return [body.status, body.finished_at !== null] as const;
      })
      .toEqual(['cancelled', true]);
  });

  test('desktop market prefill runs MACD through report, replay, and export', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1440, height: 960 });
    await page.goto('/market');
    await page.getByRole('combobox', { name: '搜索证券' }).fill('600000');
    await page
      .getByRole('option', {
        name: 'Stock Desk Synthetic Alpha (CC0 Demo) 600000.SH',
        exact: true,
      })
      .click();
    await expect(page.locator('.market-chart-canvas canvas')).toHaveCount(1);
    await page.getByLabel('开始日期').fill(START);
    await page.getByLabel('结束日期').fill(END);
    await page.getByRole('link', { name: '回测当前股票' }).click();

    await expect(page).toHaveURL(
      `/backtests?symbol=600000.SH&period=1d&adjustment=qfq&start=${START}&end=${END}`,
    );
    await expect(
      page.getByLabel('当前配置摘要').getByText('600000.SH', { exact: true }),
    ).toBeVisible();
    await expect(page.getByText(`${START} → ${END}`)).toBeVisible();
    await chooseFormula(page, MACD_NAME);
    await finishCommonSteps(page);
    await expect(page.getByLabel('服务端预检结果')).toContainText(
      '可运行 1 / 1',
    );
    await page.getByRole('button', { name: '提交回测' }).click();
    await expect(page).toHaveURL(/\/backtests\/[0-9a-f-]{36}$/u);
    completedRunUrl = page.url();
    await waitForReport(page);
    await expect(
      page
        .getByRole('region', { name: '回测结论' })
        .getByText('胜率', { exact: true }),
    ).toBeVisible();
    const winMetric = page
      .locator('.report-metric-grid article')
      .filter({ hasText: /^胜率/u });
    const displayedWinRate =
      Number((await winMetric.locator('strong').innerText()).replace('%', '')) /
      100;
    const displayedDenominator = Number(
      (await winMetric.locator('small').innerText()).match(/^\d+/u)?.[0] ??
        '-1',
    );
    await expect(
      page.getByRole('region', { name: '可横向滚动的分组表现表' }),
    ).toContainText('600000.SH');
    await page.getByRole('radio', { name: '按月' }).click();
    await expect(
      page.getByRole('region', { name: '可横向滚动的分组表现表' }),
    ).toContainText('2024-03');
    await page.getByRole('tab', { name: '交易明细' }).click();
    const realizedRows = page.locator(
      '[aria-label="可横向滚动的交易表"] tbody tr',
    );
    await expect(realizedRows).not.toHaveCount(0);
    const realizedCount = await realizedRows.count();
    expect(realizedCount).toBeGreaterThan(0);
    let positiveCount = 0;
    for (let index = 0; index < realizedCount; index += 1) {
      const value = Number(
        await realizedRows.nth(index).locator('td').nth(2).innerText(),
      );
      if (value > 0) positiveCount += 1;
    }
    expect(displayedDenominator).toBe(realizedCount);
    expect(displayedWinRate).toBeCloseTo(positiveCount / realizedCount, 4);
    await page.getByRole('tab', { name: '开放仓位' }).click();
    const openRows = page.locator('[aria-label="可横向滚动的交易表"] tbody tr');
    await expect(openRows).not.toHaveCount(0);
    const openCount = await openRows.count();
    expect(openCount).toBeGreaterThan(0);
    expect(displayedDenominator).toBe(realizedCount);
    await page.getByRole('tab', { name: '交易明细' }).click();
    await expect(
      page.getByRole('button', { name: '固定回放' }).first(),
    ).toBeVisible();
    await page.getByRole('button', { name: '固定回放' }).first().click();
    await expect(
      page.getByRole('heading', { name: /固定回放/u }),
    ).toBeVisible();
    await expect(
      page.getByRole('heading', { name: '订单生命周期' }),
    ).toBeVisible();
    await expect(
      page.locator('.trade-replay .market-chart-canvas canvas'),
    ).toHaveCount(1);

    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('link', { name: '导出交易 CSV' }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(
      /^stock-desk-backtest-[0-9a-f-]{36}-trades\.csv$/u,
    );
    const path = await download.path();
    expect(path).not.toBeNull();
    expect(await readFile(path ?? '', 'utf8')).toContain('snapshot_id');
  });

  test('custom formula discloses and completes a partial pool', async ({
    page,
    request,
  }) => {
    const formulasResponse = await request.get('/api/formulas?limit=100');
    const formulas = (await formulasResponse.json()) as {
      readonly items: readonly { readonly id: string; readonly name: string }[];
    };
    const customFormula = formulas.items.find(
      (item) => item.name === CUSTOM_NAME,
    );
    const versionsResponse = await request.get(
      `/api/formulas/${customFormula?.id ?? ''}/versions`,
    );
    const versions = (await versionsResponse.json()) as {
      readonly items: readonly { readonly id: string }[];
    };
    const customVersionId = versions.items[0]?.id ?? '';
    expect(customVersionId).toMatch(/^[0-9a-f-]{36}$/u);
    await page.setViewportSize({ width: 1440, height: 960 });
    await page.goto('/backtests');
    await chooseFormula(page, CUSTOM_NAME);
    await page.getByRole('radio', { name: '预设股票池' }).click();
    await page
      .locator('.backtest-step select')
      .selectOption({ label: `${PARTIAL_POOL_NAME} · 3 只` });
    await page.getByRole('button', { name: '下一步' }).click();
    await page.getByLabel('开始日期（上海时区，含）').fill(START);
    await page.getByLabel('结束日期（上海时区，不含）').fill(END);
    await page.getByRole('button', { name: '下一步' }).click();
    await page.getByRole('button', { name: '下一步' }).click();
    await page.getByRole('button', { name: '运行预检' }).click();
    const preflight = page.getByLabel('服务端预检结果');
    await expect(preflight).toContainText('可运行 2 / 3');
    await expect(preflight).toContainText('缺口 1');
    await preflight.getByRole('checkbox').check();
    await page.getByRole('button', { name: '提交回测' }).click();
    await waitForReport(page);
    const metadata = page.locator('.report-metadata');
    await expect(metadata).toContainText(customVersionId);
    await expect(metadata).toContainText('formula-engine-v1');
    await expect(metadata).toContainText('tdx-v1');
    await expect(metadata).toContainText('backtest-engine-v1');
    await expect(page.getByText('数据不足', { exact: true })).toBeVisible();
    await page.getByRole('tab', { name: '失败记录' }).click();
    await expect(page.getByText('600036.SH')).toBeVisible();
  });

  test('1024 keeps wizard and completed replay reachable without page overflow', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1024, height: 1366 });
    await page.goto(
      `/backtests?symbol=600000.SH&period=1d&adjustment=qfq&start=${START}&end=${END}`,
    );
    await chooseFormula(page, MACD_NAME);
    for (const step of ['3. 周期', '4. 成本', '5. 复核']) {
      await page.getByRole('button', { name: '下一步' }).click();
      await expect(page.getByRole('heading', { name: step })).toBeVisible();
    }
    expect(await noHorizontalOverflow(page)).toBe(true);

    expect(completedRunUrl).not.toBe('');
    await page.goto(completedRunUrl);
    await waitForReport(page);
    await page.getByRole('tab', { name: '交易明细' }).click();
    await page.getByRole('button', { name: '固定回放' }).first().click();
    await expect(
      page.getByRole('heading', { name: /固定回放/u }),
    ).toBeVisible();
    expect(await noHorizontalOverflow(page)).toBe(true);
  });

  test('global shell stays operable across desktop, tablet, portrait, and 200% effective viewport', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/market');
    const collapse = page.getByRole('button', { name: '收起主导航' });
    await expect(collapse).toHaveAttribute('aria-expanded', 'true');
    await collapse.click();
    await expect(
      page.getByRole('button', { name: '展开主导航' }),
    ).toHaveAttribute('aria-expanded', 'false');
    await expect(page.getByRole('link', { name: '策略回测' })).toHaveAttribute(
      'title',
      '策略回测',
    );
    expect(await noHorizontalOverflow(page)).toBe(true);

    for (const viewport of [
      { width: 1180, height: 820 },
      { width: 1024, height: 768 },
      { width: 768, height: 1024 },
      { width: 720, height: 450 },
    ]) {
      await page.setViewportSize(viewport);
      await page.goto('/market');
      await expect(
        page.getByRole('button', { name: '展开主导航' }),
      ).toHaveAttribute('aria-expanded', 'false');
      await expect(
        page.getByRole('heading', { name: '行情工作区' }),
      ).toBeVisible();
      await expect(
        page.getByRole('region', { name: '行情图表工作区' }),
      ).toBeVisible();
      const rail = await page.locator('.navigation-rail').boundingBox();
      expect(rail?.width ?? 999).toBeLessThanOrEqual(80);
      expect(await navigationDoesNotOverlapWorkspace(page)).toBe(true);
      expect(await noHorizontalOverflow(page)).toBe(true);
    }

    await page.setViewportSize({ width: 1024, height: 768 });
    await page.goto('/formulas');
    await expect(
      page.getByRole('heading', { name: '公式工作台' }),
    ).toBeVisible();
    await page.getByRole('button', { name: '打开上下文面板' }).click();
    await expect(
      page.getByRole('complementary', { name: '上下文状态' }),
    ).toBeVisible();
    await page.getByRole('button', { name: '关闭上下文面板' }).click();
    expect(await noHorizontalOverflow(page)).toBe(true);

    await page.setViewportSize({ width: 720, height: 450 });
    await page.goto('/backtests');
    await expect(page.getByLabel('保存的交易公式')).toBeVisible();
    await expect(page.getByRole('button', { name: '下一步' })).toBeVisible();
    expect(await navigationDoesNotOverlapWorkspace(page)).toBe(true);
    await page.getByRole('button', { name: '展开主导航' }).click();
    await expect(
      page.getByRole('button', { name: '收起主导航' }),
    ).toHaveAttribute('aria-expanded', 'true');
    expect(await navigationDoesNotOverlapWorkspace(page)).toBe(true);
    expect(await noHorizontalOverflow(page)).toBe(true);
  });
});
