import { defineConfig, devices } from "@playwright/test";

const externalBaseUrl = process.env.STOCK_DESK_E2E_BASE_URL;
const performanceMode = process.env.STOCK_DESK_PERFORMANCE_MODE === "1";

export default defineConfig({
  testDir: "./web/e2e",
  testIgnore: performanceMode ? [] : ["**/performance.spec.ts"],
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  // A retry may supplement diagnosis, but cannot replace a failed first run in
  // immutable main evidence. Keep the authoritative CI invocation retry-free.
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  outputDir: "test-results/playwright",
  reporter: process.env.CI
    ? [
        ["line"],
        ["html", { open: "never" }],
        ["junit", { outputFile: "test-results/playwright/junit.xml" }],
      ]
    : "line",
  use: {
    baseURL: externalBaseUrl ?? "http://127.0.0.1:5173",
    trace: performanceMode ? "off" : "retain-on-failure",
    screenshot: performanceMode ? "off" : "only-on-failure",
    video: performanceMode ? "off" : "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: externalBaseUrl
    ? undefined
    : {
        command:
          "UV_CACHE_DIR=/tmp/stock-desk-uv-cache uv run --frozen --no-sync python scripts/e2e_dev.py",
        gracefulShutdown: {
          signal: "SIGTERM",
          timeout: 15_000,
        },
        url: "http://127.0.0.1:5173",
        reuseExistingServer: false,
        timeout: performanceMode ? 300_000 : 120_000,
      },
});
