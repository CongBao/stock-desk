import { expect, test } from '@playwright/test';

test('real source settings contract renders every priority category', async ({
  page,
}) => {
  await page.goto('/settings');

  await expect(
    page.getByRole('heading', { level: 2, name: '数据源设置' }),
  ).toBeVisible();
  await expect(page.getByText('数据源设置读取失败，请稍后重试。')).toHaveCount(
    0,
  );

  for (const category of [
    '日线行情',
    '周线行情',
    '60 分钟行情',
    '证券目录',
    '交易日历',
    '回测执行状态',
    '基本面',
    '公告',
    '新闻',
  ]) {
    await expect(
      page.getByRole('group', { name: `${category}优先级` }),
    ).toBeVisible();
  }
});
