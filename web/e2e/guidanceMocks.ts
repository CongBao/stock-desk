import type { Page } from '@playwright/test';

export async function mockCompletedGuidance(page: Page): Promise<void> {
  await page.route('**/api/v1/guidance/preferences', async (route) => {
    await route.fulfill({
      json: {
        schema_version: 1,
        revision: 1,
        pages: {
          market: { content_version: 2, status: 'completed' },
          formula: { content_version: 1, status: 'completed' },
          backtest: { content_version: 1, status: 'completed' },
          analysis: { content_version: 1, status: 'completed' },
          tasks: { content_version: 1, status: 'completed' },
        },
      },
    });
  });
}
