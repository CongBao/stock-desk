import { expect, test as base, type Page } from '@playwright/test';

const completedOnboarding = {
  schema_version: 1,
  revision: 1,
  current_step: 'completed',
  status: 'completed',
  source: {
    id: 'akshare',
    label: 'AKShare',
    catalog_manifest_record_id: `sha256:${'a'.repeat(64)}`,
    catalog_dataset_version: `sha256:${'a'.repeat(64)}`,
    data_cutoff: '2026-07-11T07:00:00Z',
  },
  instrument: {
    symbol: '000001.SS',
    name: '上证指数',
    exchange: 'SH',
    instrument_kind: 'index',
  },
  sync: {
    status: 'verified',
    provider_id: 'akshare',
    manifest_record_id: `sha256:${'a'.repeat(64)}`,
    dataset_version: `sha256:${'a'.repeat(64)}`,
    data_cutoff: '2026-07-11T07:00:00Z',
    row_count: 240,
  },
  error: null,
  demo_mode: false,
};

const allowedWorkspacePages = new Set([
  '/market',
  '/formulas',
  '/backtests',
  '/analysis',
  '/tasks',
  '/settings',
]);

type WorkspaceZoom = {
  readonly start: number;
  readonly end: number;
};

function workspaceState(
  currentPage: string,
  revision: number,
  zoom: WorkspaceZoom,
) {
  return {
    schema_version: 1,
    revision,
    updated_at: '2026-07-12T06:00:00Z',
    expires_at: '2027-01-08T06:00:00Z',
    restored: true,
    notice: null,
    workspace: {
      current_page: currentPage,
      instrument: {
        symbol: '000001.SS',
        name: '上证指数',
        exchange: 'SH',
        kind: 'index',
      },
      period: '1d',
      adjustment: 'qfq',
      zoom,
      main_chart: 'candlestick',
      subchart: { kind: 'volume' },
    },
  };
}

export async function installReturningUserState(
  page: Page,
  zoom: WorkspaceZoom = { start: 0, end: 100 },
): Promise<void> {
  let revision = 1;
  let currentPage = '/market';
  await page.route('**/api/v1/onboarding/state', async (route) => {
    await route.fulfill({ json: completedOnboarding });
  });
  await page.route('**/api/v1/workspace', async (route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON() as {
        current_page?: string;
      };
      if (
        typeof body.current_page === 'string' &&
        allowedWorkspacePages.has(body.current_page)
      ) {
        currentPage = body.current_page;
      }
      revision += 1;
    } else {
      const requestedPage = new URL(page.url()).pathname;
      currentPage = allowedWorkspacePages.has(requestedPage)
        ? requestedPage
        : '/market';
    }
    await route.fulfill({
      json: workspaceState(currentPage, revision, zoom),
    });
  });
}

/**
 * Returning-user fixture for E2E scenarios that exercise the application
 * behind the first-run gate. The onboarding contract itself deliberately uses
 * Playwright's base fixture in onboarding.spec.ts.
 */
export const test = base.extend({
  page: async ({ page }, use) => {
    await installReturningUserState(page);
    await use(page);
  },
});

export { expect };
