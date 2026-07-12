import { expect, test, type Page } from '@playwright/test';

const defaultInstrument = {
  symbol: '000001.SS',
  name: '上证指数',
  exchange: 'SH',
  kind: 'index',
};

const onboardingCompleted = {
  schema_version: 1,
  revision: 5,
  status: 'completed',
  current_step: 'completed',
  source: {
    id: 'akshare',
    label: 'AKShare',
    catalog_manifest_record_id: `sha256:${'a'.repeat(64)}`,
    catalog_dataset_version: `sha256:${'b'.repeat(64)}`,
    data_cutoff: '2026-07-12T06:00:00Z',
  },
  instrument: {
    ...defaultInstrument,
    instrument_kind: 'index',
  },
  sync: {
    status: 'verified',
    provider_id: 'akshare',
    manifest_record_id: `sha256:${'c'.repeat(64)}`,
    dataset_version: `sha256:${'d'.repeat(64)}`,
    data_cutoff: '2026-07-12T06:00:00Z',
    row_count: 240,
  },
  error: null,
  demo_mode: false,
};

function state(workspace: Record<string, unknown>, revision = 4) {
  return {
    schema_version: 1,
    revision,
    updated_at: '2026-07-12T06:00:00Z',
    expires_at: '2027-01-08T06:00:00Z',
    restored: true,
    notice: null,
    workspace,
  };
}

const initialWorkspace = {
  current_page: '/market',
  instrument: {
    symbol: '600000.SH',
    name: '浦发银行',
    exchange: 'SH',
    kind: 'stock',
  },
  period: '1w',
  adjustment: 'hfq',
  zoom: { start: 20, end: 80 },
  main_chart: 'candlestick',
  subchart: { kind: 'volume' },
};

async function installCommonRoutes(page: Page) {
  await page.route('**/api/**', async (route) => {
    if (new URL(route.request().url()).pathname.startsWith('/api/')) {
      await route.fulfill({ status: 503, json: { code: 'test_unavailable' } });
      return;
    }
    await route.fallback();
  });
  await page.route('**/api/v1/onboarding/state', async (route) => {
    await route.fulfill({ json: onboardingCompleted });
  });
  await page.route('**/api/v1/market/navigation', async (route) => {
    await route.fulfill({
      json: {
        schema_version: 1,
        revision: 0,
        watchlist: [],
        recent: [],
        notice: null,
      },
    });
  });
}

test('desktop restart restores the versioned workspace and persists valid changes', async ({
  page,
}) => {
  await installCommonRoutes(page);
  let workspace: Record<string, unknown> = { ...initialWorkspace };
  let revision = 4;
  const writes: Record<string, unknown>[] = [];
  await page.route('**/api/v1/workspace', async (route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      writes.push(body);
      revision += 1;
      workspace = Object.fromEntries(
        Object.entries(body).filter(([key]) => key !== 'expected_revision'),
      );
    }
    await route.fulfill({ json: state(workspace, revision) });
  });

  await page.goto('/market');
  const shell = page.locator('.app-shell');
  await expect(shell).toHaveAttribute('data-workspace-symbol', '600000.SH');
  await expect(shell).toHaveAttribute('data-workspace-period', '1w');
  await expect(shell).toHaveAttribute('data-workspace-adjustment', 'hfq');
  await expect(shell).toHaveAttribute('data-workspace-zoom-start', '20');
  await expect(shell).toHaveAttribute('data-workspace-zoom-end', '80');

  await page.getByRole('radio', { name: '60 分钟' }).click();
  await page.getByRole('link', { name: '任务中心' }).click();
  await expect.poll(() => writes.length).toBeGreaterThan(0);
  expect(JSON.stringify(writes)).not.toMatch(/token|session|https?:|[?#]/u);
  expect(workspace).toMatchObject({ current_page: '/tasks', period: '60m' });

  await page.reload();
  await expect(page).toHaveURL(/\/tasks$/u);
  await expect(shell).toHaveAttribute('data-workspace-symbol', '600000.SH');
  await expect(shell).toHaveAttribute('data-workspace-period', '60m');
  await expect(shell).toHaveAttribute('data-workspace-adjustment', 'hfq');

  await page.setViewportSize({ width: 720, height: 900 });
  await expect(shell).toHaveAttribute('data-navigation-collapsed', 'true');
  const toggle = page.getByRole('button', { name: '展开主导航' });
  await toggle.focus();
  await page.keyboard.press('Enter');
  await expect(shell).toHaveAttribute('data-navigation-collapsed', 'false');
});

test('corrupt or illegal restored state safely falls back without navigating to it', async ({
  page,
}) => {
  await installCommonRoutes(page);
  await page.route('**/api/v1/workspace', async (route) => {
    await route.fulfill({
      json: state({
        ...initialWorkspace,
        current_page: 'https://evil.invalid/market?token=secret',
      }),
    });
  });

  await page.goto('/tasks');
  await expect(page).toHaveURL(/\/market$/u);
  const shell = page.locator('.app-shell');
  await expect(shell).toHaveAttribute('data-workspace-symbol', '000001.SS');
  await expect(shell).toHaveAttribute('data-workspace-period', '1d');
  await expect(page.locator('.workspace-restore-notice')).toContainText(
    '工作区恢复暂不可用，已安全打开默认行情。',
  );
  await expect(page.getByText(/evil|token|secret/u)).toHaveCount(0);
});
