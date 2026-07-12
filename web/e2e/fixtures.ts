import { expect, test as base } from '@playwright/test';

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

/**
 * Returning-user fixture for E2E scenarios that exercise the application
 * behind the first-run gate. The onboarding contract itself deliberately uses
 * Playwright's base fixture in onboarding.spec.ts.
 */
export const test = base.extend({
  page: async ({ page }, use) => {
    await page.route('**/api/v1/onboarding/state', async (route) => {
      await route.fulfill({ json: completedOnboarding });
    });
    await use(page);
  },
});

export { expect };
