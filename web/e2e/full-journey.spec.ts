import type { Page } from '@playwright/test';

import { expect, test } from './fixtures';

const START = '2024-02-10';
const END = '2024-06-28';
const MACD_NAME = 'Stock Desk Demo MACD (CC0 synthetic)';
const CUSTOM_NAME = 'Stock Desk Demo custom wave (CC0 synthetic)';
const PARTIAL_POOL_NAME = 'Stock Desk Synthetic Demo Index (CC0)';

type PreviewBody = {
  readonly formula: {
    readonly signal_series_id: string;
    readonly formula_version_id: string;
    readonly formula_checksum: string;
    readonly symbol: string;
    readonly period: string;
    readonly adjustment: string;
    readonly manifest_record_id: string;
    readonly dataset_version: string;
    readonly route_version: string;
    readonly query_start: string;
    readonly query_end: string;
    readonly parameters: readonly unknown[];
    readonly timestamps: readonly string[];
    readonly signals: readonly {
      readonly name: string;
      readonly values: readonly (boolean | null)[];
    }[];
  };
};

async function noHorizontalOverflow(page: Page): Promise<boolean> {
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

async function finishCommonBacktestSteps(page: Page) {
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByRole('button', { name: '运行预检' }).click();
}

async function waitForBacktestReport(page: Page) {
  await expect(page.getByRole('heading', { name: '回测结论' })).toBeVisible({
    timeout: 45_000,
  });
}

async function previewSavedFormula(
  page: Page,
  name: string,
): Promise<PreviewBody> {
  await page
    .getByLabel('打开已保存公式')
    .selectOption({ label: `${name} · v1` });
  await expect(page.getByText(`已打开：${name}`)).toBeVisible();
  const response = page.waitForResponse((candidate) => {
    const url = new URL(candidate.url());
    return (
      url.pathname === '/api/market/bars' &&
      url.searchParams.has('formula_version_id') &&
      candidate.status() === 200
    );
  });
  await page.getByRole('button', { name: '运行预览' }).click();
  return (await (await response).json()) as PreviewBody;
}

test('complete public demo journey uses real API worker and frozen provenance', async ({
  page,
  request,
}) => {
  test.setTimeout(120_000);
  await expect
    .poll(async () => (await request.get('/api/health')).status())
    .toBe(200);

  await page.goto('/market');
  await expect(page.getByText('系统正常', { exact: true })).toBeVisible();
  const search = page.getByRole('combobox', { name: '搜索证券' });
  await search.fill('600000');
  await page
    .getByRole('option', {
      name: 'Stock Desk Synthetic Alpha (CC0 Demo) 600000.SH',
      exact: true,
    })
    .click();
  await expect(page.locator('.market-chart-canvas canvas')).toHaveCount(1);
  await expect(
    page.getByText(/数据来源：Stock Desk 合成演示 · CC0-1\.0/u),
  ).toBeVisible();

  await page.getByRole('link', { name: '自定义公式' }).click();
  const macdPreview = await previewSavedFormula(page, MACD_NAME);
  await expect(page.getByText(/[1-9]\d* 个买点/u)).toBeVisible();
  expect(
    macdPreview.formula.signals
      .find((signal) => signal.name === 'BUY')
      ?.values.some(Boolean),
  ).toBe(true);
  const previewBody = await previewSavedFormula(page, CUSTOM_NAME);
  await expect(page.getByText(/[1-9]\d* 个买点/u)).toBeVisible();
  await expect(
    page.getByRole('img', { name: /K 线主图.*公式输出.*买卖信号/u }),
  ).toBeVisible();
  const customFormula = (await (
    await request.get('/api/formulas?limit=100')
  ).json()) as {
    readonly items: readonly { readonly name: string }[];
  };
  expect(customFormula.items.some((item) => item.name === CUSTOM_NAME)).toBe(
    true,
  );

  await page.goto(
    `/backtests?symbol=600000.SH&period=1d&adjustment=qfq&start=${START}&end=${END}`,
  );
  await page.getByLabel('保存的交易公式').selectOption({ label: CUSTOM_NAME });
  await page.getByRole('button', { name: '下一步' }).click();
  await finishCommonBacktestSteps(page);
  await expect(page.getByLabel('服务端预检结果')).toContainText('可运行 1 / 1');
  await page.getByRole('button', { name: '提交回测' }).click();
  await expect(page).toHaveURL(/\/backtests\/[0-9a-f-]{36}$/u);
  const singleRunId = page.url().split('/').at(-1) ?? '';
  await waitForBacktestReport(page);
  await page.getByRole('tab', { name: '交易明细' }).click();
  await expect(
    page.getByRole('button', { name: '固定回放' }).first(),
  ).toBeVisible();
  await page.getByRole('button', { name: '固定回放' }).first().click();
  await expect(page.getByRole('heading', { name: /固定回放/u })).toBeVisible();

  const report = (await (
    await request.get(`/api/backtests/${singleRunId}/report`)
  ).json()) as {
    readonly formula_parameters: readonly unknown[];
    readonly formula_version_id: string;
    readonly formula_checksum: string;
    readonly overview: { readonly snapshot_id: string };
  };
  const symbols = (await (
    await request.get(`/api/backtests/${singleRunId}/symbols`)
  ).json()) as {
    readonly items: readonly {
      readonly symbol: string;
      readonly signal_series_id: string;
      readonly provenance: {
        readonly signal_manifest_record_id: string;
        readonly signal_dataset_version: string;
        readonly signal_route_version: string;
        readonly signal_query: {
          readonly symbol: string;
          readonly instrument_kind: string;
          readonly period: string;
          readonly adjustment: string;
          readonly start: string;
          readonly end: string;
        };
      };
    }[];
  };
  const replay = (await (
    await request.get(
      `/api/backtests/${singleRunId}/trades/600000.SH/0/replay?limit=500`,
    )
  ).json()) as {
    readonly snapshot_id: string;
    readonly formula: {
      readonly formula_version_id: string;
      readonly formula_checksum: string;
      readonly signal_series_id: string;
      readonly signals: readonly {
        readonly name: string;
        readonly values: readonly (boolean | null)[];
      }[];
    };
    readonly bars: readonly { readonly timestamp: string }[];
    readonly fill_markers: readonly { readonly signal_at: string }[];
  };
  expect(report.formula_version_id).toBe(
    previewBody.formula.formula_version_id,
  );
  expect(report.formula_checksum).toBe(previewBody.formula.formula_checksum);
  expect(replay.formula.formula_checksum).toBe(
    previewBody.formula.formula_checksum,
  );
  const symbolResult = symbols.items[0];
  expect(symbolResult?.signal_series_id).toBe(
    previewBody.formula.signal_series_id,
  );
  expect(replay.formula.signal_series_id).toBe(
    previewBody.formula.signal_series_id,
  );
  expect(symbolResult?.provenance.signal_query).toEqual({
    symbol: previewBody.formula.symbol,
    instrument_kind: 'stock',
    period: previewBody.formula.period,
    adjustment: previewBody.formula.adjustment,
    start: previewBody.formula.query_start,
    end: previewBody.formula.query_end,
  });
  expect(symbolResult?.provenance.signal_manifest_record_id).toBe(
    previewBody.formula.manifest_record_id,
  );
  expect(symbolResult?.provenance.signal_dataset_version).toBe(
    previewBody.formula.dataset_version,
  );
  expect(symbolResult?.provenance.signal_route_version).toBe(
    previewBody.formula.route_version,
  );
  expect(report.formula_parameters).toEqual(previewBody.formula.parameters);
  const previewOrdinals = new Map(
    previewBody.formula.timestamps.map((timestamp, index) => [
      timestamp,
      index,
    ]),
  );
  for (const replaySignal of replay.formula.signals) {
    const previewSignal = previewBody.formula.signals.find(
      (item) => item.name === replaySignal.name,
    );
    expect(previewSignal).toBeDefined();
    expect(replaySignal.values).toEqual(
      replay.bars.map((bar) => {
        const ordinal = previewOrdinals.get(bar.timestamp);
        expect(ordinal).toBeDefined();
        return previewSignal?.values[ordinal ?? -1];
      }),
    );
  }
  expect(replay.snapshot_id).toBe(report.overview.snapshot_id);
  const buy = previewBody.formula.signals.find((item) => item.name === 'BUY');
  const firstEligibleBuy = previewBody.formula.timestamps.find(
    (timestamp, index) =>
      buy?.values[index] === true &&
      Date.parse(timestamp) >= Date.parse(`${START}T00:00:00+08:00`),
  );
  expect(replay.fill_markers[0]?.signal_at).toBe(firstEligibleBuy);

  await page.goto('/backtests');
  await page.getByLabel('保存的交易公式').selectOption({ label: MACD_NAME });
  await page.getByRole('button', { name: '下一步' }).click();
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
  const poolPreflight = page.getByLabel('服务端预检结果');
  await expect(poolPreflight).toContainText('可运行 2 / 3');
  await poolPreflight.getByRole('checkbox').check();
  await page.getByRole('button', { name: '提交回测' }).click();
  await page.getByRole('button', { name: '收起主导航' }).click();
  await expect(page.getByRole('button', { name: '展开主导航' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '近期任务' })).toBeVisible();
  await waitForBacktestReport(page);
  await expect(page.getByText('数据不足', { exact: true })).toBeVisible();

  await page.getByRole('link', { name: '智能分析' }).click();
  await page.getByLabel('股票代码').fill('600000.SH');
  await page
    .getByLabel('已验证模型')
    .selectOption({ label: 'Deterministic demo model · stock-desk-demo-stub' });
  await page.getByRole('button', { name: '运行预检' }).click();
  const analysisPreflight = page.getByLabel('四类数据预检结果');
  await expect(analysisPreflight).toContainText('行情数据');
  await expect(analysisPreflight).toContainText('公告');
  await expect(analysisPreflight).toContainText('缺失');
  await expect(analysisPreflight).toContainText('数据覆盖满足评级门槛');
  await page.getByRole('button', { name: '启动智能分析' }).click();
  await page.getByRole('button', { name: '查看分析流程' }).click();
  await expect(
    page.locator('#analysis-process-drawer[data-open="true"]'),
  ).toBeVisible();
  expect(await noHorizontalOverflow(page)).toBe(true);
  await expect(
    page.getByRole('heading', { name: '600000.SH 智能分析' }),
  ).toBeVisible({ timeout: 45_000 });
  await expect(page.getByText('中性', { exact: true })).toBeVisible();
  await expect(page.getByText('不构成投资建议')).toBeVisible();
  await page.getByRole('button', { name: '查看证据' }).click();
  const evidence = page.getByRole('complementary', { name: '证据详情' });
  await expect(evidence).toContainText('synthetic');
  await page.getByRole('button', { name: '关闭证据' }).click();

  await page.getByRole('button', { name: '打开上下文面板' }).click();
  const recent = page.locator('#context-panel');
  await expect(recent.getByRole('heading', { name: '近期任务' })).toBeVisible();
  await expect(
    recent.getByRole('listitem', { name: /^backtest\.run /u }).first(),
  ).toBeVisible();
  await expect(
    recent.getByRole('listitem', { name: /^analysis\.run /u }).first(),
  ).toBeVisible();
  expect(await page.locator('body').innerText()).not.toMatch(
    /api[_-]?key|secret|token/iu,
  );
});
