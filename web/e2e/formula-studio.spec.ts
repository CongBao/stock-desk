import type { Page } from '@playwright/test';

import { expect, test } from './fixtures';

const macdSource =
  'DIF:EMA(C,12)-EMA(C,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);';
const parameterizedMacdSource = macdSource.replace('EMA(C,12)', 'EMA(C,SHORT)');
const isolatedApiOrigin = process.env['STOCK_DESK_E2E_API_ORIGIN'];

test.beforeEach(async ({ page }) => {
  if (isolatedApiOrigin === undefined) return;
  await page.route('**/*', async (route) => {
    const source = new URL(route.request().url());
    if (!source.pathname.startsWith('/api/')) {
      await route.fallback();
      return;
    }
    if (source.pathname === '/api/v1/onboarding/state') {
      await route.fallback();
      return;
    }
    const target = new URL(
      `${source.pathname}${source.search}`,
      isolatedApiOrigin,
    );
    const response = await route.fetch({ url: target.toString() });
    await route.fulfill({ response });
  });
});

async function replaceFormulaSource(page: Page, source: string) {
  const editor = page.locator('.monaco-editor').first();
  await editor.click({ position: { x: 120, y: 24 } });
  await page.keyboard.press('Control+Home');
  await page.keyboard.down('Shift');
  await page.keyboard.press('Control+End');
  await page.keyboard.up('Shift');
  await page.keyboard.insertText(source);
}

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

test('desktop MACD flow is atomic, versioned, parameterized, and safe', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto('/formulas');

  const library = page.getByRole('complementary', { name: '函数与模板库' });
  const editorPanel = page.getByRole('region', { name: '公式代码与参数' });
  const previewPanel = page.getByRole('region', { name: '公式图表预览' });
  await expect(library).toBeVisible();
  await expect(editorPanel).toBeVisible();
  await expect(previewPanel).toBeVisible();
  const [libraryBox, editorBox, previewBox] = await Promise.all([
    library.boundingBox(),
    editorPanel.boundingBox(),
    previewPanel.boundingBox(),
  ]);
  expect(libraryBox?.x).toBeLessThan(editorBox?.x ?? 0);
  expect(editorBox?.x).toBeLessThan(previewBox?.x ?? 0);
  expect(await noHorizontalOverflow(page)).toBe(true);

  await replaceFormulaSource(page, 'X:EM');
  await page.keyboard.type('(');
  const completionWidget = page.locator('.suggest-widget.visible');
  await expect(completionWidget).toBeVisible();
  const emaCompletion = completionWidget
    .locator('.monaco-list-row')
    .filter({ hasText: /^EMA/u })
    .first();
  await expect(emaCompletion).toBeVisible();
  await emaCompletion.click();
  await expect(page.locator('.monaco-editor .view-lines')).toContainText(
    /EMA\(X,\s*N\)/u,
  );

  await page.getByRole('button', { name: /MACD 金叉\/死叉/u }).click();
  await page.getByRole('button', { name: '立即校验' }).click();
  await expect(
    page.getByText('语法、函数支持度和未来函数检查已通过。'),
  ).toBeVisible();
  await page.getByRole('button', { name: '保存为新版本' }).click();
  await expect(page.getByText('已保存版本 v1')).toBeVisible();

  const atomicPreview = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === '/api/market/bars' &&
      url.searchParams.has('formula_version_id') &&
      response.status() === 200
    );
  });
  await page.getByRole('button', { name: '运行预览' }).click();
  const previewResponse = await atomicPreview;
  const previewBody = (await previewResponse.json()) as {
    readonly formula: {
      readonly numeric_outputs: readonly { readonly name: string }[];
      readonly signals: readonly {
        readonly name: string;
        readonly values: readonly (boolean | null)[];
      }[];
    };
  };
  expect(previewBody.formula.numeric_outputs.map((item) => item.name)).toEqual([
    'DIF',
    'DEA',
    'MACD',
  ]);
  for (const signalName of ['BUY', 'SELL']) {
    expect(
      previewBody.formula.signals
        .find((signal) => signal.name === signalName)
        ?.values.some(Boolean),
    ).toBe(true);
  }
  await expect(
    page.getByRole('img', { name: /K 线主图.*公式输出.*买卖信号/u }),
  ).toBeVisible();
  await expect(
    page.getByRole('heading', { name: 'K 线主图与公式副图' }),
  ).toBeVisible();
  await expect(page.getByText('3 条输出')).toBeVisible();
  await expect(page.getByText(/[1-9]\d* 个买点/u)).toBeVisible();
  await expect(page.getByText(/[1-9]\d* 个卖点/u)).toBeVisible();

  await replaceFormulaSource(page, parameterizedMacdSource);
  await page.getByRole('textbox', { name: '参数名称' }).fill('SHORT');
  await page.getByRole('spinbutton', { name: '参数默认值' }).fill('10');
  await page.getByRole('textbox', { name: '显示名称' }).fill('短周期');
  await page.getByRole('button', { name: '新增参数' }).click();
  await page.getByRole('button', { name: '立即校验' }).click();
  await expect(
    page.getByText('语法、函数支持度和未来函数检查已通过。'),
  ).toBeVisible();
  await page.getByRole('button', { name: '保存为新版本' }).click();
  await expect(page.getByText('已保存版本 v2')).toBeVisible();
  const versionSelector = page.getByRole('combobox', {
    name: '查看历史版本',
  });
  const versionOneValue = await versionSelector
    .locator('option')
    .filter({ hasText: /^v1 ·/u })
    .getAttribute('value');
  expect(versionOneValue).not.toBeNull();
  await versionSelector.selectOption(versionOneValue ?? '');
  await expect(
    page.getByRole('textbox', { name: '历史版本公式源码' }),
  ).toHaveValue(macdSource);
  await expect(page.getByRole('spinbutton', { name: '短周期' })).toHaveValue(
    '10',
  );

  await replaceFormulaSource(page, 'X:UNKNOWN(C);');
  await page.getByRole('button', { name: '立即校验' }).click();
  const diagnosticPanel = page.locator('.formula-diagnostic-panel');
  await expect(
    diagnosticPanel.getByText('函数 UNKNOWN 不在 tdx-v1 兼容清单中。'),
  ).toBeVisible();
  await expect(diagnosticPanel.getByText(/第 1 行，第 3 列/u)).toBeVisible();
  await expect(
    page.getByRole('button', { name: '保存为新版本' }),
  ).toBeDisabled();
  await expect(page.getByRole('button', { name: '运行预览' })).toBeDisabled();

  await replaceFormulaSource(page, 'X:REF(C,-1);');
  await page.getByRole('button', { name: '立即校验' }).click();
  await expect(page.getByText('argument N is below its minimum')).toBeVisible();
  await expect(
    page.getByRole('button', { name: '保存为新版本' }),
  ).toBeDisabled();
  await expect(page.getByRole('button', { name: '运行预览' })).toBeDisabled();

  await page.getByRole('button', { name: /MACD 金叉\/死叉/u }).click();
  await page.getByRole('radio', { name: '主图叠加' }).click();
  await page.getByRole('button', { name: '立即校验' }).click();
  await expect(
    page.getByText('语法、函数支持度和未来函数检查已通过。'),
  ).toBeVisible();
  await page.getByRole('button', { name: '保存为新版本' }).click();
  await expect(page.getByText('已保存版本 v1')).toBeVisible();
  const mainOverlayPreview = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === '/api/market/bars' &&
      url.searchParams.has('formula_version_id') &&
      response.status() === 200
    );
  });
  await page.getByRole('button', { name: '运行预览' }).click();
  await mainOverlayPreview;
  await expect(
    page.getByRole('heading', { name: 'K 线主图与公式叠加' }),
  ).toBeVisible();
  await expect(
    page
      .getByRole('region', { name: 'K 线主图与公式叠加' })
      .getByRole('img', { name: /公式输出及买卖信号/u }),
  ).toBeVisible();

  await page.getByRole('button', { name: '复制公式' }).click();
  await expect(page.getByText(/已复制为独立公式版本/u)).toBeVisible();

  for (const excluded of [/条件选股/u, /五彩\s*K/u, /AI.*公式/u]) {
    await expect(page.getByRole('button', { name: excluded })).toHaveCount(0);
    await expect(page.getByRole('link', { name: excluded })).toHaveCount(0);
  }
});

test('tablet Formula Studio keeps all low-code work areas reachable', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1024, height: 1366 });
  await page.goto('/formulas');

  await expect(
    page.getByRole('complementary', { name: '函数与模板库' }),
  ).toBeVisible();
  await expect(
    page.getByRole('region', { name: '公式代码与参数' }),
  ).toBeVisible();
  await expect(
    page.getByRole('region', { name: '公式图表预览' }),
  ).toBeVisible();
  await expect(page.getByRole('button', { name: '立即校验' })).toBeVisible();
  await expect(page.getByRole('button', { name: '运行预览' })).toBeVisible();
  expect(await noHorizontalOverflow(page)).toBe(true);
});
