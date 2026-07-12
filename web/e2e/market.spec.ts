import type { Page } from '@playwright/test';

import { expect, test } from './fixtures';

function waitForBars(page: Page, period: string, adjustment: string) {
  return page.waitForResponse(
    (response: { url(): string; status(): number }) => {
      const url = new URL(response.url());
      return (
        url.pathname === '/api/market/bars' &&
        url.searchParams.get('period') === period &&
        url.searchParams.get('adjustment') === adjustment &&
        response.status() === 200
      );
    },
  );
}

test('real local market workflow stays cached, traceable, and interactive', async ({
  page,
}) => {
  await page.goto('/market');
  const search = page.getByRole('combobox', { name: '搜索证券' });
  await search.fill('600000');
  const chartStart = Date.now();
  await Promise.all([
    waitForBars(page, '1d', 'qfq'),
    page
      .getByRole('option', {
        name: 'Stock Desk Synthetic Alpha (CC0 Demo) 600000.SH',
        exact: true,
      })
      .click(),
  ]);
  const ohlcv = page.getByRole('status', { name: '当前 K 线 OHLCV' });
  await expect(ohlcv).toContainText('量');
  await expect(page.locator('.market-chart-canvas canvas')).toHaveCount(1);
  expect(Date.now() - chartStart).toBeLessThan(2_000);
  await expect(
    page.getByText(/数据来源：Stock Desk 合成演示 · CC0-1\.0/u),
  ).toBeVisible();

  const dailyReadout = await ohlcv.textContent();
  await Promise.all([
    waitForBars(page, '1w', 'qfq'),
    page.getByRole('radio', { name: '周线' }).click(),
  ]);
  await expect(ohlcv).not.toHaveText(dailyReadout ?? '');
  const weeklyReadout = await ohlcv.textContent();
  await Promise.all([
    waitForBars(page, '60m', 'qfq'),
    page.getByRole('radio', { name: '60 分钟' }).click(),
  ]);
  await expect(ohlcv).not.toHaveText(weeklyReadout ?? '');
  await Promise.all([
    waitForBars(page, '60m', 'hfq'),
    page.getByRole('combobox', { name: '复权方式' }).selectOption('hfq'),
  ]);

  const canvas = page.locator('.market-chart-canvas canvas');
  await expect(page.locator('.market-chart-canvas')).toHaveAttribute(
    'aria-busy',
    'false',
  );
  const zoomState = page.getByRole('status', { name: '图表缩放范围' });
  const initialZoomState = await zoomState.textContent();
  const previousReadout = await ohlcv.textContent();
  await canvas.scrollIntoViewIfNeeded();
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  if (box) {
    await page.mouse.move(box.x + box.width * 0.25, box.y + box.height * 0.3);
    await expect.poll(() => ohlcv.textContent()).not.toBe(previousReadout);
    await page.mouse.wheel(0, -600);
    await expect.poll(() => zoomState.textContent()).not.toBe(initialZoomState);
    await page.mouse.move(box.x + box.width * 0.7, box.y + 120);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.5, box.y + 120);
    await page.mouse.up();
  }
  await page.getByRole('button', { name: '重置图表缩放' }).click();
  await expect(zoomState).toContainText('0%–100%');

  await page.getByRole('button', { name: '打开股票池' }).click();
  await expect(
    page.getByRole('button', { name: /Stock Desk Synthetic Demo Index/u }),
  ).toBeVisible();
  await expect(
    page.getByRole('button', { name: /Stock Desk Synthetic Demo Industry/u }),
  ).toBeVisible();
  await page.getByRole('button', { name: '关闭股票池' }).click();

  await page.getByRole('button', { name: '新建自定义池' }).click();
  await page.getByRole('textbox', { name: '股票池名称' }).fill('E2E 观察池');
  await page
    .getByRole('button', { name: /加入Stock Desk Synthetic Alpha/u })
    .click();
  await page.getByLabel('搜索更多证券').fill('600036');
  await page
    .getByRole('button', {
      name: /加入 Stock Desk Synthetic Missing.*600036.SH/u,
    })
    .click();
  await page.getByRole('button', { name: '创建股票池' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: '打开股票池' }).click();
  await page.getByRole('button', { name: /E2E 观察池/u }).click();
  await expect(
    page.getByRole('heading', { name: /E2E 观察池.*成员/u }),
  ).toBeVisible();
  await page.getByRole('button', { name: '关闭股票池' }).click();
  await page.getByRole('button', { name: '编辑当前股票池' }).click();
  await page.getByRole('button', { name: '下移 600000.SH' }).click();
  await page.getByRole('button', { name: '保存股票池' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);

  await Promise.all([
    waitForBars(page, '1d', 'hfq'),
    page.getByRole('radio', { name: '日线' }).click(),
  ]);
  await Promise.all([
    waitForBars(page, '1d', 'none'),
    page.getByRole('combobox', { name: '复权方式' }).selectOption('none'),
  ]);
  await page.getByRole('radio', { name: '当前股票池' }).click();
  await page.getByRole('button', { name: '启动更新' }).click();
  await expect(page.getByRole('region', { name: '更新进度' })).toBeVisible();
  const cancel = page.getByRole('button', { name: '取消更新' });
  await expect(cancel).toBeVisible();
  const cancelResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      response.request().method() === 'POST' &&
      /^\/api\/tasks\/[^/]+\/cancel$/u.test(url.pathname)
    );
  });
  await cancel.click();
  const cancellation = await cancelResponse;
  expect([200, 409]).toContain(cancellation.status());
  if (cancellation.status() === 200) {
    await expect(page.getByText(/已取消|已请求取消/u).first()).toBeVisible({
      timeout: 15_000,
    });
  } else {
    // The worker may finish this two-symbol fixture between rendering the
    // cancel button and accepting the request. A 409 is the canonical race
    // outcome; the UI must refresh to the durable terminal state.
    await expect(page.getByText(/已完成|更新失败/u).first()).toBeVisible({
      timeout: 15_000,
    });
  }

  await page.getByRole('checkbox', { name: '启用每日更新' }).check();
  await page.getByLabel('每日更新时间').fill('18:30');
  await page.getByRole('button', { name: '保存每日计划' }).click();
  await expect(page.getByText(/范围快照已冻结/u)).toBeVisible();
  await page.reload();
  await expect(page.getByText(/范围快照已冻结/u)).toBeVisible();
  await page.getByRole('button', { name: '打开股票池' }).click();
  await page.getByRole('button', { name: /E2E 观察池/u }).click();
  await expect(
    page.getByRole('heading', { name: /E2E 观察池.*成员/u }),
  ).toBeVisible();
  await page.getByRole('button', { name: '关闭股票池' }).click();
  await page.getByRole('button', { name: '编辑当前股票池' }).click();
  await page.getByRole('button', { name: '删除股票池' }).click();
  await expect(page.getByRole('alert')).toContainText('删除后无法撤销');
  await page.getByRole('button', { name: '确认删除' }).click();
  await page.getByRole('button', { name: '打开股票池' }).click();
  await expect(page.getByRole('button', { name: /E2E 观察池/u })).toHaveCount(
    0,
  );
  await page.getByRole('button', { name: '关闭股票池' }).click();

  await expect(page.getByRole('button', { name: /实时行情/u })).toHaveCount(0);
  await expect(page.getByRole('link', { name: /动态选股/u })).toHaveCount(0);
});
