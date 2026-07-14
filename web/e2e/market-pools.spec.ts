import AxeBuilder from '@axe-core/playwright';

import { expect, test } from './fixtures';

const poolName = 'E2E 跨会话观察池';
const renamedPool = 'E2E 跨会话编辑池';

const poolDialogThemes = [
  {
    label: 'Light',
    preference: 'light',
    resolved: 'light',
    scheme: 'dark',
    surface1: 'rgb(255, 255, 255)',
    surface2: 'rgb(237, 242, 248)',
    text: 'rgb(23, 32, 51)',
  },
  {
    label: 'Dark',
    preference: 'dark',
    resolved: 'dark',
    scheme: 'light',
    surface1: 'rgb(12, 26, 43)',
    surface2: 'rgb(16, 34, 56)',
    text: 'rgb(230, 237, 247)',
  },
  {
    label: 'System Light',
    preference: 'system',
    resolved: 'light',
    scheme: 'light',
    surface1: 'rgb(255, 255, 255)',
    surface2: 'rgb(237, 242, 248)',
    text: 'rgb(23, 32, 51)',
  },
  {
    label: 'System Dark',
    preference: 'system',
    resolved: 'dark',
    scheme: 'dark',
    surface1: 'rgb(12, 26, 43)',
    surface2: 'rgb(16, 34, 56)',
    text: 'rgb(230, 237, 247)',
  },
] as const;

for (const theme of poolDialogThemes) {
  test(`custom pool dialog remains readable and accessible in ${theme.label}`, async ({
    page,
  }) => {
    await page.emulateMedia({ colorScheme: theme.scheme });
    await page.goto('/market');
    const themeSelector = page.getByRole('combobox', { name: '界面主题' });
    await themeSelector.selectOption(theme.preference);
    await expect(page.locator('html')).toHaveAttribute(
      'data-theme-preference',
      theme.preference,
    );
    await expect(page.locator('html')).toHaveAttribute(
      'data-theme',
      theme.resolved,
    );

    await page.getByRole('button', { name: '新建自定义池' }).click();
    const dialog = page.getByRole('dialog', { name: '新建自定义池' });
    const input = dialog.getByRole('textbox', { name: '股票池名称' });
    const cancel = dialog.getByRole('button', { name: '取消' });
    await expect(dialog).toBeVisible();
    await expect(input).toBeFocused();

    const styles = await dialog.evaluate((element) => {
      const browser = globalThis as unknown as {
        getComputedStyle: (target: Element) => CSSStyleDeclaration;
      };
      const inputElement = element.querySelector('input');
      const cancelButton = Array.from(element.querySelectorAll('button')).find(
        (button) => button.textContent?.trim() === '取消',
      );
      if (inputElement === null || cancelButton === undefined)
        throw new Error('Pool dialog controls are missing');
      const dialogStyle = browser.getComputedStyle(element);
      const inputStyle = browser.getComputedStyle(inputElement);
      const buttonStyle = browser.getComputedStyle(cancelButton);
      return {
        buttonBackground: buttonStyle.backgroundColor,
        buttonColor: buttonStyle.color,
        dialogBackground: dialogStyle.backgroundColor,
        dialogColor: dialogStyle.color,
        inputBackground: inputStyle.backgroundColor,
        inputColor: inputStyle.color,
      };
    });
    expect(styles).toEqual({
      buttonBackground: theme.surface2,
      buttonColor: theme.text,
      dialogBackground: theme.surface1,
      dialogColor: theme.text,
      inputBackground: theme.surface1,
      inputColor: theme.text,
    });

    const results = await new AxeBuilder({ page })
      .include('.market-pool-backdrop dialog')
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
      .analyze();
    expect(
      results.violations.filter((violation) =>
        ['critical', 'serious'].includes(violation.impact ?? ''),
      ),
    ).toEqual([]);

    await cancel.click();
    await expect(dialog).toHaveCount(0);
  });
}

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
  const editCurrentPool = page.getByRole('button', { name: '编辑当前股票池' });
  await expect(editCurrentPool).toBeVisible();
  await page.getByRole('button', { name: '关闭股票池' }).click();
  await editCurrentPool.click();
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
