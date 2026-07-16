import { expect, test } from '@playwright/test';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';

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
  await page.getByRole('button', { name: '开始设置' }).click();
  await page.getByRole('button', { name: '继续' }).click();
  await page
    .getByRole('combobox', { name: '按代码、中文或拼音搜索证券' })
    .fill('600000');
  await page.getByRole('option', { name: /浦发银行/u }).click();
  await expect(page.getByText('浦发银行', { exact: true })).toBeVisible();
  await expect(page.getByText('600000.SH', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '准备并继续' }).click();
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
  await page.getByRole('button', { name: '先看只读演示' }).click();

  const banner = page.locator('.onboarding-demo-banner');
  await expect(banner).toHaveCSS('position', 'relative');
  const exitDemo = page.getByRole('button', {
    name: '退出演示并配置真实数据',
  });
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
