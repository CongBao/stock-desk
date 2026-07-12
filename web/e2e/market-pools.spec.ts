import { expect, test } from './fixtures';

const poolName = 'E2E 跨会话观察池';
const renamedPool = 'E2E 跨会话编辑池';

test('all-A index industry and editable custom pools show composition timestamps across sessions', async ({
  page,
}) => {
  await page.goto('/market');
  await page.getByRole('button', { name: '打开股票池' }).click();

  const presets = [
    { name: /全部A股/u, category: '全 A' },
    { name: /Stock Desk Synthetic Demo Index/u, category: '指数' },
    { name: /Stock Desk Synthetic Demo Industry/u, category: '行业' },
  ] as const;
  for (const preset of presets) {
    const button = page.getByRole('button', { name: preset.name });
    await expect(button).toBeVisible();
    await button.click();
    const detail = page.getByRole('group', {
      name: `${await button.locator('strong').innerText()}成分信息`,
      exact: true,
    });
    await expect(detail).toContainText(preset.category);
    await expect(detail).toContainText('成分截至');
    await expect(detail).toContainText('更新于');
    await expect(detail.locator('time')).toHaveCount(2);
  }
  await page.getByRole('button', { name: '关闭股票池' }).click();

  const search = page.getByRole('combobox', { name: '搜索证券' });
  await search.fill('600000');
  await page
    .getByRole('option', {
      name: 'Stock Desk Synthetic Alpha (CC0 Demo) 600000.SH',
      exact: true,
    })
    .click();
  await page.getByRole('button', { name: '新建自定义池' }).click();
  await page.getByRole('textbox', { name: '股票池名称' }).fill(poolName);
  await page
    .getByRole('button', { name: /加入Stock Desk Synthetic Alpha/u })
    .click();
  await page.getByRole('button', { name: '创建股票池' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);

  await page.reload();
  await page.getByRole('button', { name: '打开股票池' }).click();
  await page.getByRole('button', { name: new RegExp(poolName, 'u') }).click();
  await page.getByRole('button', { name: '关闭股票池' }).click();
  await page.getByRole('button', { name: '编辑当前股票池' }).click();
  const editDialog = page.getByRole('dialog', { name: '编辑自定义池' });
  await editDialog.getByLabel('股票池名称').fill(renamedPool);
  await editDialog.getByLabel('编辑池搜索证券').fill('000001');
  await editDialog
    .getByRole('button', { name: /加入 Stock Desk Synthetic Suspended/u })
    .click();
  await editDialog.getByRole('button', { name: '保存股票池' }).click();
  await expect(editDialog).toHaveCount(0);

  await page.reload();
  await page.getByRole('button', { name: '打开股票池' }).click();
  await page
    .getByRole('button', { name: new RegExp(renamedPool, 'u') })
    .click();
  await expect(page.getByText('自定义成员版本 2')).toBeVisible();
  await expect(
    page.getByRole('list', { name: `${renamedPool}成员` }),
  ).toContainText('000001.SZ');
  await page.getByRole('button', { name: '关闭股票池' }).click();

  await page.getByRole('button', { name: '编辑当前股票池' }).click();
  await page.getByRole('button', { name: '删除股票池' }).click();
  await page.getByRole('button', { name: '确认删除' }).click();
  await page.getByRole('button', { name: '打开股票池' }).click();
  await expect(
    page.getByRole('button', { name: new RegExp(renamedPool, 'u') }),
  ).toHaveCount(0);
});
