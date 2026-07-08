import { expect, test, type BrowserContext, type Page } from '@playwright/test';
import { execFile } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

import {
  canonicalDigest as digest,
  completedGenerationAfter,
  parseProcessRows,
  ProcessIdentityTracker,
  progressWindowsDemonstrateChange,
  providerEvidence,
  type RootExpectation,
  selectProcessTree,
  type RoutingManifest,
} from './performanceEvidence';

const SAMPLE_COUNT = 20;
const OUTPUT = process.env['STOCK_DESK_PERFORMANCE_RAW_OUTPUT'];
const PROCESS_FILE = process.env['STOCK_DESK_PERFORMANCE_PROCESS_FILE'];
const FIXTURE_FILE = process.env['STOCK_DESK_PERFORMANCE_FIXTURE'];
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1']);

type TimedSample = {
  wall_seconds: number;
  local_seconds: number;
  external_wait_seconds: number;
  provider_span_count: number;
  provider_spans: readonly {
    source: string;
    decision: string;
  }[];
  blocked_external_request_count: number;
  rss_start_bytes: number;
  rss_peak_bytes: number;
  rss_delta_bytes: number;
  correctness_hash: string;
};

function processRoles(commands: readonly string[]): string[] {
  const roles = new Set<string>();
  for (const command of commands) {
    const lower = command.toLowerCase();
    if (lower.includes('uvicorn')) roles.add('api');
    if (lower.includes('scripts.e2e_dev --worker')) roles.add('worker');
    if (lower.includes('vite')) roles.add('web');
    if (/chrom(?:e|ium)/u.test(lower)) roles.add('browser');
    if (lower.includes('playwright')) roles.add('playwright');
    if (lower.includes('multiprocessing')) roles.add('formula-child');
  }
  return [...roles].sort();
}

function serviceRole(command: readonly string[]): 'api' | 'web' | 'worker' {
  const joined = command.join(' ');
  if (joined.includes('uvicorn')) return 'api';
  if (joined.includes('--worker')) return 'worker';
  return 'web';
}

function percentile95(values: readonly number[]): number {
  const ordered = [...values].sort((left, right) => left - right);
  return ordered[Math.ceil(0.95 * ordered.length) - 1] ?? Number.NaN;
}

function aggregate(samples: readonly TimedSample[], budget: number) {
  const walls = samples.map((sample) => sample.wall_seconds);
  return {
    samples,
    mean_seconds: walls.reduce((sum, value) => sum + value, 0) / walls.length,
    p95_seconds: percentile95(walls),
    budget_seconds: budget,
    correctness_hash: samples[0]?.correctness_hash,
  };
}

function processList(): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      'ps',
      ['-axo', 'pid=,ppid=,rss=,lstart=,command='],
      { encoding: 'utf8', env: { ...process.env, LC_ALL: 'C' } },
      (error, stdout) => {
        if (error !== null) {
          reject(new Error('process-list command failed', { cause: error }));
        } else resolve(stdout);
      },
    );
  });
}

async function processTreeSnapshot(
  roots: readonly number[],
  identities?: ProcessIdentityTracker,
) {
  const selected = selectProcessTree(
    roots,
    parseProcessRows(await processList()),
  );
  identities?.observe(selected);
  return {
    rssBytes: selected.reduce((sum, row) => sum + row.rssBytes, 0),
    commands: [...new Set(selected.map((row) => row.command))].sort(),
  };
}

class RssSampler {
  readonly start: number;
  peak: number;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private inFlight: Promise<void> = Promise.resolve();
  private running = false;
  private failure: Error | undefined;

  private constructor(
    private readonly roots: readonly number[],
    private readonly identities: ProcessIdentityTracker,
    snapshot: Awaited<ReturnType<typeof processTreeSnapshot>>,
  ) {
    this.start = snapshot.rssBytes;
    this.peak = this.start;
  }

  static async create(
    roots: readonly number[],
    rootRoles: ReadonlyMap<number, RootExpectation>,
  ): Promise<RssSampler> {
    const identities = new ProcessIdentityTracker(rootRoles);
    return new RssSampler(
      roots,
      identities,
      await processTreeSnapshot(roots, identities),
    );
  }

  begin() {
    this.running = true;
    const sample = async () => {
      try {
        const snapshot = await processTreeSnapshot(this.roots, this.identities);
        this.peak = Math.max(this.peak, snapshot.rssBytes);
      } catch (error) {
        this.failure =
          error instanceof Error
            ? error
            : new Error('RSS sampling failed with a non-Error value');
        this.running = false;
      } finally {
        if (this.running) {
          this.timer = setTimeout(() => {
            this.inFlight = sample();
          }, 50);
        }
      }
    };
    this.timer = setTimeout(() => {
      this.inFlight = sample();
    }, 50);
  }

  async finish() {
    this.running = false;
    if (this.timer !== undefined) clearTimeout(this.timer);
    await this.inFlight;
    if (this.failure !== undefined) throw this.failure;
    const snapshot = await processTreeSnapshot(this.roots, this.identities);
    this.peak = Math.max(this.peak, snapshot.rssBytes);
    return {
      rss_start_bytes: this.start,
      rss_peak_bytes: this.peak,
      rss_delta_bytes: this.peak - this.start,
    };
  }
}

async function forbidExternalNetwork(context: BrowserContext) {
  const counter = { blockedExternalRequests: 0 };
  await context.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (
      (url.protocol === 'http:' || url.protocol === 'https:') &&
      !LOOPBACK.has(url.hostname)
    ) {
      counter.blockedExternalRequests += 1;
      await route.abort('blockedbyclient');
      return;
    }
    await route.continue();
  });
  return counter;
}

async function proveChartInteractionHandshake(
  page: Page,
  chart: ReturnType<Page['locator']>,
) {
  const canvas = chart.locator('canvas');
  const readout = page.getByRole('status', { name: '当前 K 线 OHLCV' });
  const zoom = page.getByRole('status', { name: '图表缩放范围' });
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  if (box === null) throw new Error('chart canvas has no interaction bounds');
  const poll: { timeout: number; intervals: number[] } = {
    timeout: 350,
    intervals: [10, 20, 30],
  };

  const beforeReadout = await readout.textContent();
  await page.mouse.move(box.x + box.width * 0.35, box.y + 120);
  await expect.poll(() => readout.textContent(), poll).not.toBe(beforeReadout);

  await page.getByRole('button', { name: '重置图表缩放' }).click();
  await expect.poll(() => zoom.textContent(), poll).toContain('0%–100%');
  await page.mouse.move(box.x + box.width * 0.5, box.y + 120);
  await page.mouse.wheel(0, -500);
  await expect.poll(() => zoom.textContent(), poll).not.toContain('0%–100%');

  const beforeDrag = await zoom.textContent();
  await page.mouse.move(box.x + box.width * 0.7, box.y + 120);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.5, box.y + 120, { steps: 2 });
  await page.mouse.up();
  await expect.poll(() => zoom.textContent(), poll).not.toBe(beforeDrag);
}

async function chartAction(
  page: Page,
  network: { blockedExternalRequests: number },
  roots: readonly number[],
  rootRoles: ReadonlyMap<number, RootExpectation>,
) {
  const blockedBefore = network.blockedExternalRequests;
  const sampler = await RssSampler.create(roots, rootRoles);
  const started = performance.now();
  sampler.begin();
  const responsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === '/api/market/bars' &&
      url.searchParams.get('period') === '1d' &&
      url.searchParams.get('adjustment') === 'qfq'
    );
  });
  await page.getByRole('combobox', { name: '搜索证券' }).fill('600000');
  await page
    .getByRole('option', {
      name: 'Stock Desk Synthetic Alpha (CC0 Demo) 600000.SH',
      exact: true,
    })
    .click();
  const response = await responsePromise;
  const body = (await response.json()) as {
    bars: readonly unknown[];
    dataset_version: string;
    route_version: string;
    routing_manifest: RoutingManifest;
  };
  const chart = page.locator('[data-chart-ready="true"]');
  await expect(chart).toBeVisible();
  await proveChartInteractionHandshake(page, chart);
  const wall = (performance.now() - started) / 1000;
  const rss = await sampler.finish();
  const blockedDuringMeasurement =
    network.blockedExternalRequests - blockedBefore;
  expect(body.bars.length).toBeGreaterThanOrEqual(2400);
  expect(blockedDuringMeasurement).toBe(0);
  expect(network.blockedExternalRequests - blockedBefore).toBe(0);
  const provider = providerEvidence(body.routing_manifest);
  return {
    wall_seconds: wall,
    local_seconds: wall,
    ...provider,
    blocked_external_request_count: blockedDuringMeasurement,
    ...rss,
    correctness_hash: digest({
      bars: body.bars,
      dataset: body.dataset_version,
      route: body.route_version,
    }),
  } satisfies TimedSample;
}

async function completedChartGeneration(
  chart: ReturnType<Page['locator']>,
): Promise<number> {
  let generation: number | null = null;
  await expect
    .poll(async () => {
      generation = completedGenerationAfter(
        0,
        await chart.getAttribute('data-chart-ready'),
        await chart.getAttribute('data-chart-generation'),
      );
      return generation;
    })
    .not.toBeNull();
  if (generation === null) throw new Error('chart generation did not complete');
  return generation;
}

async function observeNextChartGeneration(
  chart: ReturnType<Page['locator']>,
  previousGeneration: number,
): Promise<number> {
  let generation: number | null = null;
  await expect
    .poll(async () => {
      generation = completedGenerationAfter(
        previousGeneration,
        await chart.getAttribute('data-chart-ready'),
        await chart.getAttribute('data-chart-generation'),
      );
      return generation;
    })
    .not.toBeNull();
  if (generation === null)
    throw new Error('new chart generation did not finish');
  return generation;
}

async function warmChartAction(
  page: Page,
  network: { blockedExternalRequests: number },
  roots: readonly number[],
  rootRoles: ReadonlyMap<number, RootExpectation>,
) {
  const adjustment = page.getByRole('combobox', { name: '复权方式' });
  const chart = page.locator('.market-chart-canvas');
  const qfqGeneration = await completedChartGeneration(chart);
  const noneResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === '/api/market/bars' &&
      url.searchParams.get('adjustment') === 'none'
    );
  });
  await adjustment.selectOption('none');
  await noneResponse;
  const noneGeneration = await observeNextChartGeneration(chart, qfqGeneration);

  const blockedBefore = network.blockedExternalRequests;
  const sampler = await RssSampler.create(roots, rootRoles);
  const started = performance.now();
  sampler.begin();
  const responsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === '/api/market/bars' &&
      url.searchParams.get('adjustment') === 'qfq'
    );
  });
  await adjustment.selectOption('qfq');
  const response = await responsePromise;
  const qfqFinishedGeneration = await observeNextChartGeneration(
    chart,
    noneGeneration,
  );
  const body = (await response.json()) as {
    bars: readonly unknown[];
    dataset_version: string;
    route_version: string;
    routing_manifest: RoutingManifest;
  };
  expect(qfqFinishedGeneration).toBeGreaterThan(noneGeneration);
  await proveChartInteractionHandshake(page, chart);
  const wall = (performance.now() - started) / 1000;
  const rss = await sampler.finish();
  const blockedDuringMeasurement =
    network.blockedExternalRequests - blockedBefore;
  expect(body.bars.length).toBeGreaterThanOrEqual(2400);
  expect(blockedDuringMeasurement).toBe(0);
  expect(network.blockedExternalRequests - blockedBefore).toBe(0);
  return {
    wall_seconds: wall,
    local_seconds: wall,
    ...providerEvidence(body.routing_manifest),
    blocked_external_request_count: blockedDuringMeasurement,
    ...rss,
    correctness_hash: digest({
      bars: body.bars,
      dataset: body.dataset_version,
      route: body.route_version,
    }),
  } satisfies TimedSample;
}

async function openFormula(page: Page, index: number) {
  await page.goto('/formulas');
  const selector = page.getByRole('combobox', { name: '打开已保存公式' });
  await selector.selectOption({
    label: `Performance MACD ${String(index + 1).padStart(2, '0')} (CC0 synthetic) · v1`,
  });
  await expect(page.getByText(/已打开：Performance MACD/u)).toBeVisible();
  await expect(page.getByRole('button', { name: '运行预览' })).toBeEnabled();
}

async function formulaAction(
  page: Page,
  network: { blockedExternalRequests: number },
  roots: readonly number[],
  rootRoles: ReadonlyMap<number, RootExpectation>,
) {
  const blockedBefore = network.blockedExternalRequests;
  const sampler = await RssSampler.create(roots, rootRoles);
  const started = performance.now();
  sampler.begin();
  const responsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === '/api/market/bars' &&
      url.searchParams.has('formula_version_id')
    );
  });
  await page.getByRole('button', { name: '运行预览' }).click();
  const response = await responsePromise;
  const body = (await response.json()) as {
    bars: readonly unknown[];
    formula: {
      numeric_outputs: readonly unknown[];
      signals: readonly unknown[];
    };
    routing_manifest: RoutingManifest;
  };
  await expect(page.locator('[data-chart-ready="true"]')).toBeVisible();
  await expect(
    page.getByRole('heading', { name: 'K 线主图与公式副图' }),
  ).toBeVisible();
  await expect(page.getByText('3 条输出')).toBeVisible();
  await expect(page.getByText(/[1-9]\d* 个买点/u)).toBeVisible();
  await expect(page.getByText(/[1-9]\d* 个卖点/u)).toBeVisible();
  const wall = (performance.now() - started) / 1000;
  const rss = await sampler.finish();
  expect(body.bars.length).toBeGreaterThanOrEqual(2400);
  expect(network.blockedExternalRequests - blockedBefore).toBe(0);
  const provider = providerEvidence(body.routing_manifest);
  return {
    wall_seconds: wall,
    local_seconds: wall,
    ...provider,
    blocked_external_request_count:
      network.blockedExternalRequests - blockedBefore,
    ...rss,
    correctness_hash: digest({
      bars: body.bars,
      numeric_outputs: body.formula.numeric_outputs,
      signals: body.formula.signals,
    }),
  } satisfies TimedSample;
}

async function loadPerformanceFormulaVersion(page: Page): Promise<string> {
  const formulasResponse = await page.request.get('/api/formulas?limit=100');
  expect(formulasResponse.ok()).toBe(true);
  const formulas = (await formulasResponse.json()) as {
    items: readonly { id: string; name: string }[];
  };
  const formula = formulas.items.find(
    (item) => item.name === 'Performance MACD 01 (CC0 synthetic)',
  );
  expect(formula).toBeDefined();
  const versionsResponse = await page.request.get(
    `/api/formulas/${formula?.id ?? ''}/versions`,
  );
  const versions = (await versionsResponse.json()) as {
    items: readonly { id: string }[];
  };
  expect(versions.items).toHaveLength(1);
  return versions.items[0]?.id ?? '';
}

async function backtestAction(
  page: Page,
  versionId: string,
  network: { blockedExternalRequests: number },
  roots: readonly number[],
  rootRoles: ReadonlyMap<number, RootExpectation>,
  cachedManifest: RoutingManifest,
  cachedManifestId: string,
) {
  const blockedBefore = network.blockedExternalRequests;
  const sampler = await RssSampler.create(roots, rootRoles);
  const started = performance.now();
  sampler.begin();
  const submission = await page.request.post('/api/backtests', {
    data: {
      scope: { kind: 'single', symbol: '600000.SH' },
      formula_version_id: versionId,
      formula_parameters: {},
      period: '1d',
      adjustment: 'qfq',
      scoring_start: '2016-01-01T00:00:00+08:00',
      scoring_end: '2026-01-01T00:00:00+08:00',
      quantity_shares: 1000,
      commission_bps: '2.5',
      minimum_commission: '5',
      sell_tax_bps: '5',
      slippage_bps: '1',
    },
  });
  expect(submission.status()).toBe(202);
  const submitted = (await submission.json()) as { run_id: string };
  let report: Record<string, unknown> | undefined;
  await expect
    .poll(
      async () => {
        const response = await page.request.get(
          `/api/backtests/${submitted.run_id}/report`,
        );
        if (!response.ok()) return false;
        report = (await response.json()) as Record<string, unknown>;
        return true;
      },
      { timeout: 15_000 },
    )
    .toBe(true);
  await page.goto(`/backtests/${submitted.run_id}`);
  await expect(page.getByRole('heading', { name: '回测结论' })).toBeVisible();
  const wall = (performance.now() - started) / 1000;
  const rss = await sampler.finish();
  expect(network.blockedExternalRequests - blockedBefore).toBe(0);
  const symbolsResponse = await page.request.get(
    `/api/backtests/${submitted.run_id}/symbols?limit=100`,
  );
  const symbols = (await symbolsResponse.json()) as {
    items: readonly {
      provenance: {
        signal_manifest_record_id?: string;
        execution_manifest_record_id?: string;
      };
    }[];
  };
  expect(symbols.items[0]?.provenance.signal_manifest_record_id).toBe(
    cachedManifestId,
  );
  expect(symbols.items[0]?.provenance.execution_manifest_record_id).toBe(
    cachedManifestId,
  );
  const provider = providerEvidence(cachedManifest);
  const typedReport = report as
    | {
        formula_checksum?: unknown;
        formula_engine_version?: unknown;
        compatibility_version?: unknown;
        backtest_engine_version?: unknown;
        provenance?: unknown;
        period?: unknown;
        adjustment?: unknown;
        quantity_shares?: unknown;
        costs?: unknown;
        execution_rules_version?: unknown;
        cost_model_version?: unknown;
        sizing_version?: unknown;
        warmup_policy_version?: unknown;
        metrics?: unknown;
        outcomes?: unknown;
      }
    | undefined;
  const normalizedReport = {
    formula_checksum: typedReport?.formula_checksum,
    formula_engine_version: typedReport?.formula_engine_version,
    compatibility_version: typedReport?.compatibility_version,
    backtest_engine_version: typedReport?.backtest_engine_version,
    provenance: typedReport?.provenance,
    period: typedReport?.period,
    adjustment: typedReport?.adjustment,
    quantity_shares: typedReport?.quantity_shares,
    costs: typedReport?.costs,
    execution_rules_version: typedReport?.execution_rules_version,
    cost_model_version: typedReport?.cost_model_version,
    sizing_version: typedReport?.sizing_version,
    warmup_policy_version: typedReport?.warmup_policy_version,
    metrics: typedReport?.metrics,
    outcomes: typedReport?.outcomes,
  };
  return {
    wall_seconds: wall,
    local_seconds: wall,
    ...provider,
    blocked_external_request_count:
      network.blockedExternalRequests - blockedBefore,
    ...rss,
    correctness_hash: digest(normalizedReport),
  } satisfies TimedSample;
}

async function beginLongTaskWindow(page: Page) {
  await page.evaluate(() => {
    const durations: number[] = [];
    const observer = new PerformanceObserver((entries) => {
      for (const entry of entries.getEntries()) durations.push(entry.duration);
    });
    (
      observer as unknown as {
        observe(options: { type: string }): void;
      }
    ).observe({ type: 'longtask' });
    Object.assign(globalThis, {
      __stockDeskLongTaskWindow: { durations, observer },
    });
  });
}

async function endLongTaskWindow(page: Page): Promise<number> {
  const durations = await page.evaluate(() => {
    const state = (
      globalThis as unknown as {
        __stockDeskLongTaskWindow: {
          durations: number[];
          observer: {
            takeRecords(): { duration: number }[];
            disconnect(): void;
          };
        };
      }
    ).__stockDeskLongTaskWindow;
    state.observer
      .takeRecords()
      .forEach((entry: { duration: number }) =>
        state.durations.push(entry.duration),
      );
    state.observer.disconnect();
    return state.durations;
  });
  return durations.filter((duration) => duration > 50).length;
}

type ProgressState = {
  readonly status: string;
  readonly stage: string;
  readonly processed: number;
  readonly total: number;
  readonly failed: number;
};

function progressKey(state: ProgressState): string {
  return [
    state.status,
    state.stage,
    state.processed,
    state.total,
    state.failed,
  ].join('|');
}

function parseRenderedProgress(raw: string | null): ProgressState | null {
  if (raw === null) return null;
  const [status, stage, processedRaw, totalRaw, failedRaw, ...extra] =
    raw.split('|');
  const processed = Number(processedRaw);
  const total = Number(totalRaw);
  const failed = Number(failedRaw);
  if (
    extra.length !== 0 ||
    status === undefined ||
    stage === undefined ||
    !Number.isInteger(processed) ||
    !Number.isInteger(total) ||
    !Number.isInteger(failed)
  ) {
    throw new Error('rendered progress tuple is malformed');
  }
  return { status, stage, processed, total, failed };
}

async function observeMatchedProgress(
  page: Page,
  runId: string,
  previousKey: string | null,
) {
  const progress = page.getByRole('region', { name: '运行进度' });
  let matched:
    { rendered_state: ProgressState; api_state: ProgressState } | undefined;
  await expect
    .poll(
      async () => {
        const rendered = parseRenderedProgress(
          await progress.getAttribute('data-rendered-progress'),
        );
        if (rendered === null || progressKey(rendered) === previousKey)
          return '';
        const response = await page.request.get(`/api/backtests/${runId}`);
        if (!response.ok()) return '';
        const overview = (await response.json()) as ProgressState;
        const apiState = {
          status: overview.status,
          stage: overview.stage,
          processed: overview.processed,
          total: overview.total,
          failed: overview.failed,
        };
        if (progressKey(rendered) !== progressKey(apiState)) return '';
        matched = { rendered_state: rendered, api_state: apiState };
        return progressKey(rendered);
      },
      { timeout: 12_000, intervals: [25, 50, 100, 200] },
    )
    .not.toBe('');
  if (matched === undefined)
    throw new Error('rendered/API progress did not match');
  return matched;
}

test('records aggregate 2/3/5 budgets and worker-backed UI responsiveness', async ({
  browser,
}) => {
  test.setTimeout(12 * 60_000);
  expect(
    OUTPUT,
    'runner must provide STOCK_DESK_PERFORMANCE_RAW_OUTPUT',
  ).toBeTruthy();
  expect(
    PROCESS_FILE,
    'runner must provide STOCK_DESK_PERFORMANCE_PROCESS_FILE',
  ).toBeTruthy();
  expect(
    FIXTURE_FILE,
    'runner must provide STOCK_DESK_PERFORMANCE_FIXTURE',
  ).toBeTruthy();
  const fixtureEvidence = JSON.parse(
    readFileSync(FIXTURE_FILE ?? '', 'utf8'),
  ) as {
    content_digest: string;
    scope_instrument_count: number;
    runnable_symbol_count: number;
  };
  expect(fixtureEvidence).toMatchObject({
    scope_instrument_count: 5_000,
    runnable_symbol_count: 40,
  });
  const processEvidence = JSON.parse(
    readFileSync(PROCESS_FILE ?? '', 'utf8'),
  ) as {
    supervisor_pid: number;
    service_pids: number[];
    service_processes: { pid: number; command: string[] }[];
  };
  const roots = [
    ...new Set([
      process.pid,
      process.ppid,
      processEvidence.supervisor_pid,
      ...processEvidence.service_pids,
    ]),
  ]
    .filter((pid) => Number.isInteger(pid) && pid > 0)
    .sort((left, right) => left - right);
  const rootRoles = new Map<number, RootExpectation>([
    [process.pid, { role: 'playwright' }],
    [process.ppid, { role: 'playwright' }],
    [processEvidence.supervisor_pid, { role: 'supervisor' }],
    ...processEvidence.service_processes.map(
      (service) =>
        [
          service.pid,
          {
            role: serviceRole(service.command),
            commandTokens: service.command,
          },
        ] as const,
    ),
  ]);
  expect(rootRoles.size).toBe(roots.length);
  const declaredProcessTree = await processTreeSnapshot(roots);
  const sampledProcessRoles = processRoles(declaredProcessTree.commands);
  for (const required of ['uvicorn', 'scripts.e2e_dev --worker', 'vite']) {
    expect(
      declaredProcessTree.commands.some((command) =>
        command.includes(required),
      ),
    ).toBe(true);
  }
  expect(
    declaredProcessTree.commands.some((command) =>
      /chrom(?:e|ium)/iu.test(command),
    ),
  ).toBe(true);
  const chartCold: TimedSample[] = [];
  for (let index = 0; index < SAMPLE_COUNT; index += 1) {
    const context = await browser.newContext();
    const network = await forbidExternalNetwork(context);
    const page = await context.newPage();
    await page.goto('/market');
    chartCold.push(await chartAction(page, network, roots, rootRoles));
    await context.close();
  }

  const context = await browser.newContext();
  const network = await forbidExternalNetwork(context);
  const page = await context.newPage();
  const chartWarm: TimedSample[] = [];
  await page.goto('/market');
  await chartAction(page, network, roots, rootRoles);
  for (let index = 0; index < SAMPLE_COUNT; index += 1) {
    chartWarm.push(await warmChartAction(page, network, roots, rootRoles));
  }

  const formulaSamples: TimedSample[] = [];
  for (let index = 0; index < SAMPLE_COUNT; index += 1) {
    await openFormula(page, index);
    formulaSamples.push(await formulaAction(page, network, roots, rootRoles));
  }

  const versionId = await loadPerformanceFormulaVersion(page);
  const cachedResponse = await page.request.get(
    '/api/market/bars?symbol=600000.SH&period=1d&adjustment=qfq',
  );
  const cachedBody = (await cachedResponse.json()) as {
    manifest_record_id: string;
    routing_manifest: RoutingManifest;
  };
  const backtestSamples: TimedSample[] = [];
  for (let index = 0; index < SAMPLE_COUNT; index += 1) {
    backtestSamples.push(
      await backtestAction(
        page,
        versionId,
        network,
        roots,
        rootRoles,
        cachedBody.routing_manifest,
        cachedBody.manifest_record_id,
      ),
    );
  }

  await page.goto('/backtests');
  await page
    .getByLabel('保存的交易公式')
    .selectOption({ label: 'Performance MACD 01 (CC0 synthetic)' });
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByRole('radio', { name: '预设股票池' }).click();
  const poolSelect = page.locator('.backtest-step select');
  const poolValue = await poolSelect
    .locator('option')
    .filter({
      hasText: 'Perf Full-A Scope: 5000 metadata / 40 runnable (CC0)',
    })
    .getAttribute('value');
  expect(poolValue).not.toBeNull();
  await poolSelect.selectOption(poolValue ?? '');
  const poolDetailResponse = await page.request.get(
    `/api/market/pools/${encodeURIComponent(poolValue ?? '')}`,
  );
  expect(poolDetailResponse.ok()).toBe(true);
  const poolDetail = (await poolDetailResponse.json()) as {
    members: readonly {
      ordinal: number;
      symbol: string;
      name: string;
      instrument_kind: string;
      listing_status: string;
    }[];
    provenance: { instrument_dataset_version: string };
  };
  expect(poolDetail.members).toHaveLength(5_000);
  const poolMembershipDigest = digest(poolDetail.members);
  const poolDataDigest = digest({
    fixture_content_digest: fixtureEvidence.content_digest,
    instrument_dataset_version:
      poolDetail.provenance.instrument_dataset_version,
    runnable_symbol_count: fixtureEvidence.runnable_symbol_count,
  });
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByLabel('开始日期（上海时区，含）').fill('2016-01-01');
  await page.getByLabel('结束日期（上海时区，不含）').fill('2026-01-01');
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByRole('button', { name: '下一步' }).click();
  const preflightResponsePromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === '/api/backtests/preflight' &&
      response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '运行预检' }).click();
  const preflightResponse = await preflightResponsePromise;
  expect(preflightResponse.ok()).toBe(true);
  const preflightEvidence = (await preflightResponse.json()) as {
    formula: { formula_checksum: string };
    scope: { total: number; runnable: number };
  };
  expect(preflightEvidence.scope).toMatchObject({ total: 5_000, runnable: 40 });
  const preflight = page.getByLabel('服务端预检结果');
  await expect(preflight).toContainText(/可运行 40 \/ 5000/u);
  const confirmation = preflight.getByRole('checkbox');
  if (await confirmation.isVisible()) await confirmation.check();
  const submissionPromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === '/api/backtests' &&
      response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '提交回测' }).click();
  const poolSubmission = (await (await submissionPromise).json()) as {
    run_id: string;
    task_id: string;
    snapshot_id: string;
  };
  await expect(page).toHaveURL(`/backtests/${poolSubmission.run_id}`);
  const observedProgressStates: ProgressState[] = [];
  const poolSamples: {
    long_task_count: number;
    interaction_kind: 'progress' | 'navigation' | 'cancel';
    interactive: boolean;
    rendered_state: ProgressState;
    api_state: ProgressState;
    correctness_hash: string;
  }[] = [];
  await expect
    .poll(async () => {
      const response = await page.request.get(
        `/api/backtests/${poolSubmission.run_id}`,
      );
      const overview = (await response.json()) as { status: string };
      return overview.status;
    })
    .toBe('running');
  const initialProgress = await observeMatchedProgress(
    page,
    poolSubmission.run_id,
    null,
  );
  const initialProgressKey = progressKey(initialProgress.rendered_state);
  let requiredChangeFrom = initialProgressKey;
  for (let index = 0; index < SAMPLE_COUNT - 2; index += 1) {
    await beginLongTaskWindow(page);
    const progressEvidence = await observeMatchedProgress(
      page,
      poolSubmission.run_id,
      index < 2 ? requiredChangeFrom : null,
    );
    if (index < 2)
      requiredChangeFrom = progressKey(progressEvidence.rendered_state);
    observedProgressStates.push(progressEvidence.rendered_state);
    const cancelVisible = await page
      .getByRole('button', { name: '取消回测' })
      .isVisible();
    const progressVisible = await page
      .getByRole('progressbar', { name: '运行进度' })
      .isVisible();
    await page.waitForTimeout(25);
    poolSamples.push({
      long_task_count: await endLongTaskWindow(page),
      interaction_kind: 'progress',
      interactive: cancelVisible && progressVisible,
      ...progressEvidence,
      correctness_hash: '',
    });
  }
  expect(
    progressWindowsDemonstrateChange(
      initialProgressKey,
      observedProgressStates.map(progressKey),
    ),
  ).toBe(true);
  await beginLongTaskWindow(page);
  await page.getByRole('link', { name: '任务' }).click();
  const taskCenterHeading = page.getByRole('heading', { name: '任务中心' });
  await expect(taskCenterHeading).toBeVisible();
  const taskCenterVisible = await taskCenterHeading.isVisible();
  await page.goBack();
  const runPageHeading = page.getByRole('heading', { name: '回测运行' });
  const runProgress = page.getByRole('region', { name: '运行进度' });
  await expect(runPageHeading).toBeVisible();
  await expect(runProgress).toBeVisible();
  const runPageVisible = await runPageHeading.isVisible();
  const progressVisible = await runProgress.isVisible();
  const navigationEvidence = await observeMatchedProgress(
    page,
    poolSubmission.run_id,
    null,
  );
  poolSamples.push({
    long_task_count: await endLongTaskWindow(page),
    interaction_kind: 'navigation',
    interactive: taskCenterVisible && runPageVisible && progressVisible,
    ...navigationEvidence,
    correctness_hash: '',
  });
  await beginLongTaskWindow(page);
  const cancelButton = page.getByRole('button', { name: '取消回测' });
  await expect(cancelButton).toBeVisible();
  await cancelButton.click();
  await expect(
    page.locator('.run-progress .status-badge[data-status="cancelled"]'),
  ).toBeVisible({ timeout: 45_000 });
  const cancellationEvidence = await observeMatchedProgress(
    page,
    poolSubmission.run_id,
    null,
  );
  const finalOverview = cancellationEvidence.api_state;
  expect(finalOverview.status).toBe('cancelled');
  poolSamples.push({
    long_task_count: await endLongTaskWindow(page),
    interaction_kind: 'cancel',
    interactive: finalOverview.status === 'cancelled',
    ...cancellationEvidence,
    correctness_hash: '',
  });
  const poolSemanticEvidence = {
    formula_checksum: preflightEvidence.formula.formula_checksum,
    pool_membership_digest: poolMembershipDigest,
    pool_data_digest: poolDataDigest,
    terminal_status: finalOverview.status,
  };
  const poolCorrectness = digest(poolSemanticEvidence);
  for (const sample of poolSamples) {
    sample.correctness_hash = poolCorrectness;
  }
  const totalLongTasks = poolSamples.reduce(
    (sum, sample) => sum + sample.long_task_count,
    0,
  );
  expect(totalLongTasks).toBe(0);
  await context.close();

  const result = {
    schema_version: 'stock-desk-performance-v1',
    definitions: {
      chart_cold:
        'new Chromium context with empty browser and HTTP cache; timer starts at selecting the cached symbol and ends at ECharts finished plus hover/zoom/drag/crosshair proof',
      chart_warm:
        'same Chromium and service processes after one unmeasured completed render; timer captures the prior generation and requires a strictly newer finished ECharts generation before the chart_cold interaction boundary',
      formula_cache_cold:
        'one distinct pre-seeded immutable formula version per sample; timer starts at preview action and ends at ECharts finished with main/subchart/BUY/SELL visible',
      single_backtest_fresh:
        'new submitted task per sample; timer starts at POST submit and ends after worker-persisted report is visible',
      pool_ui:
        '20 raw windows from one worker-backed pool task: 18 rendered/API-matched progress windows with at least two distinct states, one SPA navigation window, and one actual cancellation window',
    },
    process_tree: {
      declared_roots: roots,
      declared_services: processEvidence.service_processes
        .map((service) => ({
          pid: service.pid,
          role: serviceRole(service.command),
          command: service.command,
        }))
        .sort((left, right) => left.pid - right.pid),
      sampled_process_roles: sampledProcessRoles,
      role_set_digest: digest(sampledProcessRoles),
    },
    metrics: {
      chart_cold: aggregate(chartCold, 2),
      chart_warm: aggregate(chartWarm, 2),
      formula_preview: aggregate(formulaSamples, 3),
      single_backtest: aggregate(backtestSamples, 5),
      pool_ui: {
        samples: poolSamples,
        long_task_count: totalLongTasks,
        observed_progress_states: observedProgressStates,
        worker_claim_observed: observedProgressStates.some(
          (value) => value.status === 'running',
        ),
        cancel_status: finalOverview.status,
        semantic_evidence: poolSemanticEvidence,
        correctness_hash: poolSamples[0]?.correctness_hash,
      },
    },
  };
  Object.assign(result, { browser_version: `Chromium ${browser.version()}` });
  if (OUTPUT !== undefined) {
    await mkdir(dirname(OUTPUT), { recursive: true });
    await writeFile(OUTPUT, `${JSON.stringify(result, null, 2)}\n`, 'utf8');
  }
});
