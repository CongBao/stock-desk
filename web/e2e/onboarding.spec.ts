import { expect, test } from '@playwright/test';
import { createHash } from 'node:crypto';
import { mkdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';

function canonicalDigest(value: unknown): string {
  const sort = (item: unknown): unknown => {
    if (Array.isArray(item)) return item.map(sort);
    if (item === null || typeof item !== 'object') return item;
    return Object.fromEntries(
      Object.entries(item as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, nested]) => [key, sort(nested)]),
    );
  };
  return `sha256:${createHash('sha256')
    .update(JSON.stringify(sort(value)))
    .digest('hex')}`;
}

test('first run wizard searches and opens a non-default stock', async ({
  page,
}) => {
  let step = 'welcome';
  let status = 'pending';
  let revision = 1;
  let source: Record<string, unknown> | null = null;
  let instrument: Record<string, unknown> | null = null;
  let sync: Record<string, unknown> | null = null;
  const digest = `sha256:${'a'.repeat(64)}`;

  const currentState = () => ({
    schema_version: 1,
    revision,
    current_step: step,
    status,
    source,
    instrument,
    sync,
    error: null,
    demo_mode: false,
  });

  await page.route('**/api/v1/onboarding/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/sources')) {
      await route.fulfill({
        json: {
          items: [
            {
              id: 'akshare',
              label: 'AKShare',
              description: '无需密钥的 A 股行情来源',
              recommended: true,
              requires_token: false,
              status: 'ready',
              data_cutoff: '2026-07-11T07:00:00Z',
            },
          ],
        },
      });
      return;
    }
    if (url.pathname.endsWith('/instruments')) {
      await route.fulfill({
        json: {
          items: [
            {
              symbol: '600000.SH',
              name: '浦发银行',
              exchange: 'SH',
              instrument_kind: 'stock',
            },
          ],
        },
      });
      return;
    }
    if (url.pathname.endsWith('/progress')) {
      const body = route.request().postDataJSON() as {
        current_step: string;
        source_id?: string;
      };
      step = body.current_step;
      status = 'in_progress';
      revision += 1;
      if (body.source_id) {
        source = {
          id: body.source_id,
          label: 'AKShare',
          catalog_manifest_record_id: digest,
          catalog_dataset_version: digest,
          data_cutoff: '2026-07-11T07:00:00Z',
        };
      }
      await route.fulfill({ json: currentState() });
      return;
    }
    if (url.pathname.endsWith('/sync')) {
      step = 'synchronization';
      revision += 1;
      instrument = {
        symbol: '600000.SH',
        name: '浦发银行',
        exchange: 'SH',
        instrument_kind: 'stock',
      };
      sync = {
        status: 'verified',
        provider_id: 'akshare',
        manifest_record_id: digest,
        dataset_version: digest,
        data_cutoff: '2026-07-11T07:00:00Z',
        row_count: 240,
      };
      await route.fulfill({ json: currentState() });
      return;
    }
    if (url.pathname.endsWith('/complete')) {
      step = 'completed';
      status = 'completed';
      revision += 1;
      await route.fulfill({ json: currentState() });
      return;
    }
    await route.fulfill({ json: currentState() });
  });

  const fixturePath = new URL(
    '../src/features/market/fixtures/backend-bars-response.json',
    import.meta.url,
  );
  const bars = JSON.parse(await readFile(fixturePath, 'utf8')) as Record<
    string,
    unknown
  >;
  const routing = bars['routing_manifest'] as Record<string, unknown>;
  const payload = { ...routing };
  delete payload['upstream_fetched_at'];
  delete payload['route_version'];
  routing['route_version'] = canonicalDigest(payload);
  bars['route_version'] = routing['route_version'];
  bars['manifest_record_id'] = canonicalDigest(routing);
  await page.route('**/api/market/bars?**', async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('symbol') === '600000.SH') {
      await route.fulfill({
        json: bars,
      });
      return;
    }
    await route.continue();
  });
  await page.route('**/api/v1/workspace', async (route) => {
    await route.fulfill({
      json: {
        schema_version: 1,
        revision: 1,
        updated_at: '2026-07-12T06:00:00Z',
        expires_at: '2027-01-08T06:00:00Z',
        restored: true,
        notice: null,
        workspace: {
          current_page: '/market',
          instrument: {
            symbol: '600000.SH',
            name: '浦发银行',
            exchange: 'SH',
            kind: 'stock',
          },
          period: '1d',
          adjustment: 'qfq',
          zoom: { start: 0, end: 100 },
          main_chart: 'candlestick',
          subchart: { kind: 'volume' },
        },
      },
    });
  });

  await page.goto('/market');
  await page.getByRole('button', { name: '开始' }).click();
  await page.getByRole('button', { name: '继续' }).click();
  await page
    .getByRole('combobox', { name: '按代码、中文或拼音搜索证券' })
    .fill('600000');
  await page.getByRole('option', { name: /浦发银行/u }).click();
  await expect(page.getByText('浦发银行', { exact: true })).toBeVisible();
  await expect(page.getByText('600000.SH', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '加载行情' }).click();
  await expect(page.getByText('可以开始使用了')).toBeVisible();
  await page.getByRole('button', { name: '打开行情' }).click();

  await expect(page).toHaveURL(/\/market$/u);
  await expect(page.getByText('浦发银行 · 600000.SH')).toBeVisible();
  await expect(page.locator('.market-chart-canvas canvas')).toHaveCount(1);
  await expect(
    page.getByRole('status', { name: '当前 K 线 OHLCV' }),
  ).toContainText('量');
});

test('readonly demo notice stays in flow without covering controls at 200% equivalent scale', async ({
  page,
}) => {
  let demoMode = false;
  const state = () => ({
    schema_version: 1,
    revision: demoMode ? 2 : 1,
    current_step: 'welcome',
    status: 'pending',
    source: null,
    instrument: {
      symbol: '600000.SH',
      name: 'Stock Desk 合成演示标的（非真实行情）',
      exchange: 'SH',
      instrument_kind: 'stock',
    },
    sync: null,
    error: demoMode ? { code: 'demo_read_only', actions: ['exit_demo'] } : null,
    demo_mode: demoMode,
  });
  await page.route('**/api/v1/onboarding/**', async (route) => {
    if (new URL(route.request().url()).pathname.endsWith('/actions/demo')) {
      demoMode = true;
    }
    await route.fulfill({ json: state() });
  });

  await page.setViewportSize({ width: 683, height: 384 });
  await page.goto('/market');
  await page.getByRole('button', { name: '进入演示模式' }).click();

  const banner = page.locator('.onboarding-demo-banner');
  await expect(banner).toHaveCSS('position', 'relative');
  const exitDemo = page.getByRole('button', { name: '设置真实行情' });
  const overlaps = await exitDemo.evaluate((target) => {
    const targetBox = target.getBoundingClientRect();
    return Array.from(
      document.querySelectorAll(
        'a, button, input, select, textarea, [role="tab"]',
      ),
    ).flatMap((candidate) => {
      if (!(candidate instanceof HTMLElement) || candidate === target)
        return [];
      const style = getComputedStyle(candidate);
      const box = candidate.getBoundingClientRect();
      if (
        style.display === 'none' ||
        style.visibility === 'hidden' ||
        box.width <= 0 ||
        box.height <= 0
      )
        return [];
      const intersects = !(
        targetBox.right <= box.left ||
        box.right <= targetBox.left ||
        targetBox.bottom <= box.top ||
        box.bottom <= targetBox.top
      );
      return intersects
        ? [
            candidate.getAttribute('aria-label') ??
              candidate.textContent?.trim().slice(0, 80) ??
              candidate.tagName,
          ]
        : [];
    });
  });
  expect(overlaps).toEqual([]);
});

test('onboarding ready page retains visual evidence across themes and widths', async ({
  page,
}, testInfo) => {
  test.setTimeout(60_000);
  const digest = `sha256:${'b'.repeat(64)}`;
  const readyState = {
    schema_version: 1,
    revision: 4,
    current_step: 'synchronization',
    status: 'in_progress',
    source: {
      id: 'baostock',
      label: 'BaoStock',
      catalog_manifest_record_id: digest,
      catalog_dataset_version: digest,
      data_cutoff: '2026-07-17T07:00:00Z',
    },
    instrument: {
      symbol: '000001.SS',
      name: '上证指数',
      exchange: 'SH',
      instrument_kind: 'index',
    },
    sync: {
      status: 'verified',
      provider_id: 'baostock',
      manifest_record_id: digest,
      dataset_version: digest,
      data_cutoff: '2026-07-17T07:00:00Z',
      row_count: 240,
    },
    error: null,
    demo_mode: false,
  };
  await page.route('**/api/v1/onboarding/**', async (route) => {
    await route.fulfill({ json: readyState });
  });
  await page.goto('/market');
  await expect(page.getByText('可以开始使用了')).toBeVisible();

  const themes = [
    { preference: 'light', resolved: 'light' },
    { preference: 'dark', resolved: 'dark' },
    { preference: 'system', resolved: 'light' },
    { preference: 'system', resolved: 'dark' },
  ] as const;
  const viewports = [
    { height: 900, label: 'normal', width: 1440 },
    { height: 700, label: 'narrow', width: 900 },
  ] as const;

  for (const theme of themes) {
    if (theme.preference === 'system') {
      await page.emulateMedia({ colorScheme: theme.resolved });
    }
    await page
      .getByRole('combobox', { name: '界面主题' })
      .selectOption(theme.preference);
    await expect(page.locator('html')).toHaveAttribute(
      'data-theme',
      theme.resolved,
    );

    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      const layout = await page.evaluate(() => ({
        cardBackground: getComputedStyle(
          document.querySelector('.onboarding-card') as Element,
        ).backgroundColor,
        clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
        summaryBackground: getComputedStyle(
          document.querySelector('.onboarding-ready-summary dl div') as Element,
        ).backgroundColor,
      }));
      expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth + 1);
      if (theme.resolved === 'light') {
        expect(layout.cardBackground).toBe('rgb(255, 255, 255)');
        expect(layout.summaryBackground).toBe('rgb(237, 242, 248)');
      } else {
        expect(layout.cardBackground).toBe('rgba(12, 26, 43, 0.97)');
        expect(layout.summaryBackground).toBe('rgba(7, 17, 31, 0.55)');
      }
      await expect(
        page.getByRole('heading', { name: '可以开始使用了' }),
      ).toHaveCSS('outline-style', 'none');
      await page.evaluate(async () => {
        await document.fonts.ready;
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        );
      });
      const name = `onboarding-${theme.preference}-${theme.resolved}-${viewport.label}`;
      const directory = join(
        process.cwd(),
        'test-results',
        'page-visual-matrix',
      );
      const output = join(directory, `${name}.png`);
      await mkdir(directory, { recursive: true });
      await page.screenshot({
        animations: 'disabled',
        caret: 'hide',
        path: output,
      });
      await testInfo.attach(name, { contentType: 'image/png', path: output });
    }
  }
});
