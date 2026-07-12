import {
  type BrowserContext,
  type Page,
  type Response,
  type Route,
} from '@playwright/test';
import { execFile } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { mkdir, readdir, readFile, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

import { expect, installReturningUserState, test } from './fixtures';

import {
  canonicalDigest as digest,
  completedGenerationAfter,
  parseProcessRows,
  parseProcProcessRow,
  portableCommandTokens,
  ProcessIdentityTracker,
  ProgressResponseLedger,
  progressEvidenceState,
  progressWindowsDemonstrateChange,
  providerEvidence,
  type RootExpectation,
  type ProcessRow,
  selectProcessTree,
  type RoutingManifest,
} from './performanceEvidence';

const SAMPLE_COUNT = 20;
const RSS_SAMPLE_INTERVAL_MS = 500;
const PROGRESS_GATE_HEADER = 'x-stock-desk-performance-window';
let progressGateGeneration = 0;
let backtestCorrectnessReference:
  | {
      correctnessHash: string;
      componentHashes: Readonly<Record<string, string>>;
    }
  | undefined;
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

type BacktestReportPayload = {
  overview?: {
    status?: unknown;
    total?: unknown;
    processed?: unknown;
    failed?: unknown;
  };
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
  outcomes?: {
    total?: unknown;
    succeeded?: unknown;
    failed?: unknown;
    data_insufficient?: unknown;
    unprocessed?: unknown;
  };
};

function backtestCorrectness(report: BacktestReportPayload) {
  const normalizedReport = {
    formula_checksum: report.formula_checksum,
    formula_engine_version: report.formula_engine_version,
    compatibility_version: report.compatibility_version,
    backtest_engine_version: report.backtest_engine_version,
    provenance: report.provenance,
    period: report.period,
    adjustment: report.adjustment,
    quantity_shares: report.quantity_shares,
    costs: report.costs,
    execution_rules_version: report.execution_rules_version,
    cost_model_version: report.cost_model_version,
    sizing_version: report.sizing_version,
    warmup_policy_version: report.warmup_policy_version,
    metrics: report.metrics,
    outcomes: report.outcomes,
  };
  return {
    correctnessHash: digest(normalizedReport),
    componentHashes: Object.fromEntries(
      Object.entries(normalizedReport).map(([name, value]) => [
        name,
        digest(value),
      ]),
    ),
  };
}

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

function reportChartMilestones(
  metric: 'chart_cold' | 'chart_warm',
  started: number,
  milestones: Readonly<Record<string, number>>,
) {
  console.log(
    `[performance-chart-milestones] ${JSON.stringify({
      metric,
      ...Object.fromEntries(
        Object.entries(milestones).map(([name, timestamp]) => [
          `${name}_seconds`,
          (timestamp - started) / 1000,
        ]),
      ),
    })}`,
  );
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

async function linuxProcessRows(): Promise<ProcessRow[]> {
  const entries = await readdir('/proc', { withFileTypes: true });
  const rows = await Promise.all(
    entries
      .filter((entry) => entry.isDirectory() && /^\d+$/u.test(entry.name))
      .map(async (entry) => {
        const pid = Number(entry.name);
        const root = `/proc/${entry.name}`;
        try {
          const [stat, status, cmdline] = await Promise.all([
            readFile(`${root}/stat`, 'utf8'),
            readFile(`${root}/status`, 'utf8'),
            readFile(`${root}/cmdline`, 'utf8'),
          ]);
          return parseProcProcessRow(pid, stat, status, cmdline);
        } catch (error) {
          const code =
            typeof error === 'object' && error !== null && 'code' in error
              ? String(error.code)
              : '';
          if (['EACCES', 'ENOENT', 'EPERM', 'ESRCH'].includes(code))
            return null;
          throw error;
        }
      }),
  );
  return rows.filter((row): row is ProcessRow => row !== null);
}

async function processRows(): Promise<ProcessRow[]> {
  if (process.platform === 'linux') return linuxProcessRows();
  return parseProcessRows(await processList());
}

async function processTreeSnapshot(
  roots: readonly number[],
  identities?: ProcessIdentityTracker,
) {
  const selected = selectProcessTree(roots, await processRows());
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
          }, RSS_SAMPLE_INTERVAL_MS);
        }
      }
    };
    this.timer = setTimeout(() => {
      this.inFlight = sample();
    }, RSS_SAMPLE_INTERVAL_MS);
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
  // Keep the correctness handshake below the 2s chart budget without imposing
  // a separate sub-350ms ceiling on slower target-runner animation frames.
  const poll: { timeout: number; intervals: number[] } = {
    timeout: 1_000,
    intervals: [10, 20, 30],
  };
  const zoomRange = async () =>
    `${await zoom.getAttribute('data-zoom-start')}:${await zoom.getAttribute(
      'data-zoom-end',
    )}`;

  const beforeReadout = await readout.textContent();
  await page.mouse.move(box.x + box.width * 0.35, box.y + 120);
  await expect.poll(() => readout.textContent(), poll).not.toBe(beforeReadout);
  const hoveredAt = performance.now();

  const beforeZoom = await zoomRange();
  await page.mouse.move(box.x + box.width * 0.5, box.y + 120);
  await page.mouse.wheel(0, 500);
  await expect.poll(zoomRange, poll).not.toBe(beforeZoom);
  const zoomedAt = performance.now();

  const beforeDrag = await zoomRange();
  const [dragStart] = beforeDrag.split(':').map(Number);
  const dragTargetRatio =
    Number.isFinite(dragStart) && dragStart <= 0.001 ? 0.3 : 0.7;
  await page.mouse.move(box.x + box.width * 0.5, box.y + 120);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * dragTargetRatio, box.y + 120, {
    steps: 1,
  });
  await page.mouse.up();
  await expect.poll(zoomRange, poll).not.toBe(beforeDrag);
  const draggedAt = performance.now();
  return {
    hovered: hoveredAt,
    zoomed: zoomedAt,
    dragged: draggedAt,
  } as const;
}

async function chartAction(
  page: Page,
  network: { blockedExternalRequests: number },
  roots: readonly number[],
  rootRoles: ReadonlyMap<number, RootExpectation>,
) {
  const blockedBefore = network.blockedExternalRequests;
  const sampler = await RssSampler.create(roots, rootRoles);
  await page.getByRole('combobox', { name: '搜索证券' }).fill('600000');
  const option = page.getByRole('option', {
    name: 'Stock Desk Synthetic Alpha (CC0 Demo) 600000.SH',
    exact: true,
  });
  await expect(option).toBeVisible();
  await page.locator('.market-chart-viewport').scrollIntoViewIfNeeded();
  const responsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === '/api/market/bars' &&
      url.searchParams.get('period') === '1d' &&
      url.searchParams.get('adjustment') === 'qfq'
    );
  });
  const started = performance.now();
  sampler.begin();
  await option.click();
  const selectedAt = performance.now();
  const response = await responsePromise;
  const responseAt = performance.now();
  const body = (await response.json()) as {
    bars: readonly unknown[];
    dataset_version: string;
    route_version: string;
    routing_manifest: RoutingManifest;
  };
  const decodedAt = performance.now();
  const chart = page.locator('[data-chart-ready="true"]');
  await expect(chart).toBeVisible();
  const renderedAt = performance.now();
  const interactionMilestones = await proveChartInteractionHandshake(
    page,
    chart,
  );
  const interactedAt = performance.now();
  const wall = (interactedAt - started) / 1000;
  reportChartMilestones('chart_cold', started, {
    selected: selectedAt,
    response: responseAt,
    decoded: decodedAt,
    rendered: renderedAt,
    ...interactionMilestones,
    interacted: interactedAt,
  });
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
  const selectedAt = performance.now();
  const response = await responsePromise;
  const responseAt = performance.now();
  const qfqFinishedGeneration = await observeNextChartGeneration(
    chart,
    noneGeneration,
  );
  const renderedAt = performance.now();
  const body = (await response.json()) as {
    bars: readonly unknown[];
    dataset_version: string;
    route_version: string;
    routing_manifest: RoutingManifest;
  };
  const decodedAt = performance.now();
  expect(qfqFinishedGeneration).toBeGreaterThan(noneGeneration);
  const interactionMilestones = await proveChartInteractionHandshake(
    page,
    chart,
  );
  const interactedAt = performance.now();
  const wall = (interactedAt - started) / 1000;
  reportChartMilestones('chart_warm', started, {
    selected: selectedAt,
    response: responseAt,
    rendered: renderedAt,
    decoded: decodedAt,
    ...interactionMilestones,
    interacted: interactedAt,
  });
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

async function navigateWithinDesktopWorkspace(
  page: Page,
  pathname: string,
): Promise<void> {
  await page.evaluate((target) => {
    const browserGlobal = globalThis as unknown as {
      history: { pushState(data: unknown, unused: string, url: string): void };
      document: {
        createEvent(type: string): { initEvent(type: string): void };
      };
      dispatchEvent(event: unknown): boolean;
    };
    const popState = browserGlobal.document.createEvent('Event');
    popState.initEvent('popstate');
    browserGlobal.history.pushState(null, '', target);
    browserGlobal.dispatchEvent(popState);
  }, pathname);
}

async function submitBacktestReport(page: Page, versionId: string) {
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
  let report: BacktestReportPayload | undefined;
  await expect
    .poll(
      async () => {
        const response = await page.request.get(
          `/api/backtests/${submitted.run_id}/report`,
        );
        if (!response.ok()) return false;
        report = (await response.json()) as BacktestReportPayload;
        return true;
      },
      { timeout: 15_000 },
    )
    .toBe(true);
  expect(report).toBeDefined();
  return { runId: submitted.run_id, report: report ?? {} };
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
  const submitted = await submitBacktestReport(page, versionId);
  await navigateWithinDesktopWorkspace(page, `/backtests/${submitted.runId}`);
  await expect(page.getByRole('heading', { name: '回测结论' })).toBeVisible();
  const wall = (performance.now() - started) / 1000;
  console.log(
    `[performance-backtest-sample] ${JSON.stringify({ wall_seconds: wall })}`,
  );
  const rss = await sampler.finish();
  expect(network.blockedExternalRequests - blockedBefore).toBe(0);
  const symbolsResponse = await page.request.get(
    `/api/backtests/${submitted.runId}/symbols?limit=100`,
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
  const correctness = backtestCorrectness(submitted.report);
  const correctnessHash = correctness.correctnessHash;
  if (backtestCorrectnessReference === undefined) {
    backtestCorrectnessReference = correctness;
  } else if (correctnessHash !== backtestCorrectnessReference.correctnessHash) {
    console.log(
      `[performance-backtest-correctness-drift] ${JSON.stringify({
        expected_hash: backtestCorrectnessReference.correctnessHash,
        actual_hash: correctnessHash,
        expected_components: backtestCorrectnessReference.componentHashes,
        actual_components: correctness.componentHashes,
      })}`,
    );
  }
  return {
    wall_seconds: wall,
    local_seconds: wall,
    ...provider,
    blocked_external_request_count:
      network.blockedExternalRequests - blockedBefore,
    ...rss,
    correctness_hash: correctnessHash,
  } satisfies TimedSample;
}

async function beginLongTaskWindow(page: Page) {
  await page.evaluate(() => {
    const records: Record<string, unknown>[] = [];
    const observer = new PerformanceObserver((entries) => {
      for (const entry of entries.getEntries()) {
        const record: unknown = entry.toJSON();
        if (typeof record === 'object' && record !== null)
          records.push(record as Record<string, unknown>);
      }
    });
    (
      observer as unknown as {
        observe(options: { type: string }): void;
      }
    ).observe({ type: 'longtask' });
    Object.assign(globalThis, {
      __stockDeskLongTaskWindow: { records, observer },
    });
  });
}

async function endLongTaskWindow(page: Page, label: string): Promise<number> {
  const records = await page.evaluate(() => {
    const state = (
      globalThis as unknown as {
        __stockDeskLongTaskWindow: {
          records: Record<string, unknown>[];
          observer: {
            takeRecords(): { toJSON(): Record<string, unknown> }[];
            disconnect(): void;
          };
        };
      }
    ).__stockDeskLongTaskWindow;
    state.observer
      .takeRecords()
      .forEach((entry) => state.records.push(entry.toJSON()));
    state.observer.disconnect();
    return state.records;
  });
  const longTasks = records
    .filter(
      (entry) =>
        typeof entry['duration'] === 'number' && entry['duration'] > 50,
    )
    .map((entry) => ({
      duration: entry['duration'],
      startTime: entry['startTime'],
      name: entry['name'],
      attribution: entry['attribution'],
    }));
  if (longTasks.length > 0)
    console.log(
      `[performance-long-task-window] ${JSON.stringify({ label, longTasks })}`,
    );
  return longTasks.length;
}

type ProgressState = {
  readonly status: string;
  readonly stage: string;
  readonly processed: number;
  readonly total: number;
  readonly failed: number;
};

type RenderSignal = {
  readonly sequence: number;
  readonly pathname: string;
  readonly progressKey: string | null;
  readonly heading: string | null;
  readonly taskCount: number;
};

type RenderSignalWaiter = {
  readonly predicate: (signal: RenderSignal) => boolean;
  readonly resolve: (signal: RenderSignal) => void;
  readonly reject: (error: Error) => void;
  readonly timer: ReturnType<typeof setTimeout>;
};

class RenderSignalLedger {
  private readonly signals: RenderSignal[] = [];
  private readonly waiters = new Set<RenderSignalWaiter>();

  get latestSequence() {
    return this.signals.at(-1)?.sequence ?? 0;
  }

  record(value: unknown) {
    if (typeof value !== 'object' || value === null) return;
    const candidate = value as Record<string, unknown>;
    const { sequence, pathname, progressKey, heading, taskCount } = candidate;
    if (
      !Number.isSafeInteger(sequence) ||
      typeof pathname !== 'string' ||
      (progressKey !== null && typeof progressKey !== 'string') ||
      (heading !== null && typeof heading !== 'string') ||
      !Number.isSafeInteger(taskCount) ||
      Number(taskCount) < 0
    ) {
      return;
    }
    const signal = {
      sequence: Number(sequence),
      pathname,
      progressKey,
      heading,
      taskCount: Number(taskCount),
    } satisfies RenderSignal;
    this.signals.push(signal);
    for (const waiter of this.waiters) {
      if (!waiter.predicate(signal)) continue;
      clearTimeout(waiter.timer);
      this.waiters.delete(waiter);
      waiter.resolve(signal);
    }
  }

  nextMatching(
    predicate: (signal: RenderSignal) => boolean,
    description: string,
  ): Promise<RenderSignal> {
    const existing = this.signals.findLast(predicate);
    if (existing !== undefined) return Promise.resolve(existing);
    return new Promise((resolve, reject) => {
      const waiter: RenderSignalWaiter = {
        predicate,
        resolve,
        reject,
        timer: setTimeout(() => {
          this.waiters.delete(waiter);
          reject(
            new Error(`${description} render-ready signal was not observed`),
          );
        }, 45_000),
      };
      this.waiters.add(waiter);
    });
  }
}

type ProgressPaintSignal = {
  readonly sequence: number;
  readonly token: string;
};

type ProgressPaintWaiter = {
  readonly token: string;
  readonly resolve: (signal: ProgressPaintSignal) => void;
  readonly reject: (error: Error) => void;
  readonly timer: ReturnType<typeof setTimeout>;
};

class ProgressPaintSignalLedger {
  private readonly signals: ProgressPaintSignal[] = [];
  private readonly waiters = new Set<ProgressPaintWaiter>();

  record(value: unknown) {
    if (typeof value !== 'object' || value === null) return;
    const candidate = value as Record<string, unknown>;
    const { sequence, token } = candidate;
    if (
      !Number.isSafeInteger(sequence) ||
      typeof token !== 'string' ||
      token.length === 0
    ) {
      return;
    }
    const signal = {
      sequence: Number(sequence),
      token,
    } satisfies ProgressPaintSignal;
    this.signals.push(signal);
    for (const waiter of this.waiters) {
      if (waiter.token !== token) continue;
      clearTimeout(waiter.timer);
      this.waiters.delete(waiter);
      waiter.resolve(signal);
    }
  }

  next(token: string, description: string): Promise<ProgressPaintSignal> {
    const existing = this.signals.findLast((signal) => signal.token === token);
    if (existing !== undefined) return Promise.resolve(existing);
    return new Promise((resolve, reject) => {
      const waiter: ProgressPaintWaiter = {
        token,
        resolve,
        reject,
        timer: setTimeout(() => {
          this.waiters.delete(waiter);
          reject(
            new Error(`${description} paint-ready signal was not observed`),
          );
        }, 45_000),
      };
      this.waiters.add(waiter);
    });
  }

  dispose() {
    for (const waiter of this.waiters) {
      clearTimeout(waiter.timer);
      waiter.reject(new Error('progress paint signal ledger was disposed'));
    }
    this.waiters.clear();
  }
}

async function installRenderSignalObserver(page: Page) {
  const ledger = new RenderSignalLedger();
  const binding = '__stockDeskPerformanceRenderSignal';
  await page.exposeFunction(binding, (value: unknown) => ledger.record(value));
  await page.evaluate((bindingName) => {
    const browser = globalThis as unknown as {
      document: {
        body: object;
        querySelector(selector: string): {
          getAttribute(name: string): string | null;
          textContent: string | null;
        } | null;
        querySelectorAll(selector: string): { length: number };
      };
      location: { pathname: string };
      MutationObserver: new (callback: () => void) => {
        observe(
          target: object,
          options: {
            attributes: boolean;
            attributeFilter: string[];
            childList: boolean;
            subtree: boolean;
          },
        ): void;
      };
    };
    let sequence = 0;
    const report = (
      globalThis as unknown as Record<
        string,
        (value: RenderSignal) => Promise<void>
      >
    )[bindingName];
    const emit = () => {
      const progress = browser.document.querySelector(
        '[data-rendered-progress]',
      );
      const heading = browser.document.querySelector('[data-page-heading]');
      void report?.({
        sequence: ++sequence,
        pathname: browser.location.pathname,
        progressKey: progress?.getAttribute('data-rendered-progress') ?? null,
        heading: heading?.textContent?.trim() ?? null,
        taskCount: browser.document.querySelectorAll('.task-center-list > li')
          .length,
      });
    };
    const observer = new browser.MutationObserver(emit);
    observer.observe(browser.document.body, {
      attributes: true,
      attributeFilter: ['data-rendered-progress'],
      childList: true,
      subtree: true,
    });
    Object.assign(globalThis, {
      __stockDeskPerformanceRenderObserver: observer,
    });
    emit();
  }, binding);
  return ledger;
}

async function installProgressPaintSignals(page: Page) {
  const ledger = new ProgressPaintSignalLedger();
  const binding = '__stockDeskPerformancePaintReady';
  await page.exposeFunction(binding, (value: unknown) => ledger.record(value));
  await page.evaluate(
    ({ bindingName, headerName }) => {
      const browser = globalThis as unknown as {
        fetch: typeof fetch;
        requestAnimationFrame(callback: (timestamp: number) => void): number;
        __stockDeskPerformancePaintCleanup?: () => void;
      };
      let sequence = 0;
      const originalFetch = browser.fetch;
      const report = (
        globalThis as unknown as Record<
          string,
          (value: ProgressPaintSignal) => Promise<void>
        >
      )[bindingName];
      const wrappedFetch: typeof fetch = async (...args) => {
        const response = await originalFetch.apply(globalThis, args);
        const token = response.headers.get(headerName);
        if (token !== null) {
          const originalJson = response.json.bind(response);
          Object.defineProperty(response, 'json', {
            configurable: true,
            value: async () => {
              const value: unknown = await originalJson();
              browser.requestAnimationFrame(() => {
                browser.requestAnimationFrame(() => {
                  void report?.({ token, sequence: ++sequence });
                });
              });
              return value;
            },
          });
        }
        return response;
      };
      browser.fetch = wrappedFetch;
      browser.__stockDeskPerformancePaintCleanup = () => {
        if (browser.fetch === wrappedFetch) browser.fetch = originalFetch;
        delete browser.__stockDeskPerformancePaintCleanup;
      };
    },
    { bindingName: binding, headerName: PROGRESS_GATE_HEADER },
  );
  return {
    next: (token: string, description: string) =>
      ledger.next(token, description),
    dispose: async () => {
      ledger.dispose();
      await page.evaluate(() => {
        const browser = globalThis as unknown as {
          fetch: typeof fetch;
          __stockDeskPerformancePaintCleanup?: () => void;
        };
        browser.__stockDeskPerformancePaintCleanup?.();
      });
    },
  };
}

function renderReadyAfter<T>(
  ledger: RenderSignalLedger,
  expected: Promise<T>,
  predicate: (signal: RenderSignal, value: T) => boolean,
  description: string,
) {
  return expected.then((value) =>
    ledger.nextMatching((signal) => predicate(signal, value), description),
  );
}

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
  responses: ProgressResponseLedger,
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
        const apiState = responses.match(runId, rendered);
        if (apiState === null) return '';
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

function isProgressResponse(response: Response, runId: string) {
  return (
    new URL(response.url()).pathname === `/api/backtests/${runId}` &&
    response.request().method() === 'GET' &&
    response.ok()
  );
}

async function responseProgressState(
  responsePromise: Promise<Response>,
): Promise<ProgressState> {
  const response = await responsePromise;
  const state = progressEvidenceState(await response.json());
  if (state === null) throw new Error('progress response is malformed');
  return state;
}

function nextProgressState(
  page: Page,
  runId: string,
  predicate: (state: ProgressState) => boolean,
): Promise<ProgressState> {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      page.off('response', inspect);
      reject(new Error('matching progress response was not observed'));
    }, 45_000);
    const inspect = (response: Response) => {
      if (!isProgressResponse(response, runId)) return;
      void response
        .json()
        .then((body: unknown) => {
          const state = progressEvidenceState(body);
          if (state === null || !predicate(state)) return;
          clearTimeout(timeout);
          page.off('response', inspect);
          resolve(state);
        })
        .catch(() => undefined);
    };
    page.on('response', inspect);
  });
}

function isTaskCenterListResponse(response: Response) {
  const url = new URL(response.url());
  return (
    url.pathname === '/api/tasks' &&
    url.searchParams.size === 2 &&
    url.searchParams.get('view') === 'safe' &&
    url.searchParams.get('limit') === '100' &&
    response.request().method() === 'GET' &&
    response.ok()
  );
}

async function taskListResponseCount(responsePromise: Promise<Response>) {
  const response = await responsePromise;
  const body: unknown = await response.json();
  if (!Array.isArray(body)) throw new Error('task list response is malformed');
  return body.length;
}

async function armNextProgressResponse(page: Page, runId: string) {
  const pattern = `**/api/backtests/${runId}`;
  const token = `${runId}:${++progressGateGeneration}`;
  let taggedRequest = false;
  let tokenResponseCount = 0;
  let routing = true;
  let release: () => void = () => undefined;
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  const handler = async (route: Route) => {
    if (taggedRequest) {
      await route.fallback();
      return;
    }
    taggedRequest = true;
    await released;
    const response = await route.fetch({
      headers: {
        ...route.request().headers(),
        [PROGRESS_GATE_HEADER]: token,
      },
    });
    await route.fulfill({
      response,
      headers: {
        ...response.headers(),
        [PROGRESS_GATE_HEADER]: token,
      },
    });
  };
  const capture = (response: Response) => {
    if (response.headers()[PROGRESS_GATE_HEADER] === token)
      tokenResponseCount += 1;
  };
  page.on('response', capture);
  await page.route(pattern, handler);
  return {
    token,
    release,
    matches: (response: Response) =>
      isProgressResponse(response, runId) &&
      response.headers()[PROGRESS_GATE_HEADER] === token,
    stopRouting: async () => {
      if (!routing) return;
      routing = false;
      await page.unroute(pattern, handler);
    },
    finish: () => {
      page.off('response', capture);
      return tokenResponseCount;
    },
  };
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
    row_count: number;
    scope_instrument_count: number;
    runnable_symbol_count: number;
  };
  expect(fixtureEvidence).toMatchObject({
    scope_instrument_count: 5_000,
    runnable_symbol_count: 40,
  });
  const performanceWorkspaceZoom = {
    start: Math.max(0, 100 - (160 / fixtureEvidence.row_count) * 100),
    end: 100,
  };
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
    await installReturningUserState(page, performanceWorkspaceZoom);
    await page.goto('/market');
    chartCold.push(await chartAction(page, network, roots, rootRoles));
    await context.close();
  }

  let context = await browser.newContext();
  let network = await forbidExternalNetwork(context);
  let page = await context.newPage();
  await installReturningUserState(page, performanceWorkspaceZoom);
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
  const backtestWarmup = await submitBacktestReport(page, versionId);
  expect(backtestWarmup.report.overview).toMatchObject({
    status: 'succeeded',
    total: 1,
    processed: 1,
    failed: 0,
  });
  expect(backtestWarmup.report.outcomes).toEqual({
    total: 1,
    succeeded: 1,
    failed: 0,
    data_insufficient: 0,
    unprocessed: 0,
  });
  backtestCorrectnessReference = backtestCorrectness(backtestWarmup.report);
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

  // Pool responsiveness is its own workload. A fresh renderer removes page
  // heap and GC activity from preceding workloads as contamination variables
  // while preserving the strict zero-long-task threshold.
  await context.close();
  context = await browser.newContext();
  network = await forbidExternalNetwork(context);
  page = await context.newPage();
  await installReturningUserState(page, performanceWorkspaceZoom);
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
  const progressResponses = new ProgressResponseLedger();
  const progressResponseErrors: string[] = [];
  const captureProgressResponse = (response: Response) => {
    const match = /^\/api\/backtests\/([^/]+)$/u.exec(
      new URL(response.url()).pathname,
    );
    if (
      match === null ||
      response.request().method() !== 'GET' ||
      !response.ok()
    ) {
      return;
    }
    void response
      .json()
      .then((body: unknown) => {
        if (!progressResponses.record(match[1] ?? '', body))
          progressResponseErrors.push('invalid progress response');
      })
      .catch(() => progressResponseErrors.push('unreadable progress response'));
  };
  page.on('response', captureProgressResponse);
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
  const renderSignals = await installRenderSignalObserver(page);
  const progressPaintSignals = await installProgressPaintSignals(page);
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
    progressResponses,
    null,
  );
  const initialProgressKey = progressKey(initialProgress.rendered_state);
  // Keep Playwright's DOM probes outside each observer window: locator and
  // assertion scripts execute in the renderer and are not product UI work.
  for (let index = 0; index < SAMPLE_COUNT - 2; index += 1) {
    const progressResponseGate = await armNextProgressResponse(
      page,
      poolSubmission.run_id,
    );
    const progressResponsePromise = page.waitForResponse((response) =>
      progressResponseGate.matches(response),
    );
    const progressResponseStatePromise = responseProgressState(
      progressResponsePromise,
    );
    const progressPaintReadyPromise = progressPaintSignals.next(
      progressResponseGate.token,
      `progress-${index}`,
    );
    await beginLongTaskWindow(page);
    progressResponseGate.release();
    const apiState = await progressResponseStatePromise;
    await progressResponseGate.stopRouting();
    await progressPaintReadyPromise;
    const longTaskCount = await endLongTaskWindow(page, `progress-${index}`);
    expect(progressResponseGate.finish()).toBe(1);
    const renderedState = parseRenderedProgress(
      await page
        .getByRole('region', { name: '运行进度' })
        .getAttribute('data-rendered-progress'),
    );
    expect(renderedState).not.toBeNull();
    expect(renderedState).toEqual(apiState);
    const progressEvidence = {
      rendered_state: renderedState ?? apiState,
      api_state: apiState,
    };
    observedProgressStates.push(progressEvidence.rendered_state);
    const cancelVisible = await page
      .getByRole('button', { name: '取消回测' })
      .isVisible();
    const progressVisible = await page
      .getByRole('progressbar', { name: '运行进度' })
      .isVisible();
    poolSamples.push({
      long_task_count: longTaskCount,
      interaction_kind: 'progress',
      interactive: cancelVisible && progressVisible,
      ...progressEvidence,
      correctness_hash: '',
    });
  }
  await progressPaintSignals.dispose();
  expect(
    progressWindowsDemonstrateChange(
      initialProgressKey,
      observedProgressStates.map(progressKey),
    ),
  ).toBe(true);
  const taskCenterLink = page.getByRole('link', { name: '任务' });
  const taskCenterLinkBounds = await taskCenterLink.boundingBox();
  expect(taskCenterLinkBounds).not.toBeNull();
  const navigationRenderSequence = renderSignals.latestSequence;
  const navigationResponsePromise = page.waitForResponse((response) =>
    isTaskCenterListResponse(response),
  );
  const navigationResponseCountPromise = taskListResponseCount(
    navigationResponsePromise,
  );
  const navigationRenderReadyPromise = renderReadyAfter(
    renderSignals,
    navigationResponseCountPromise,
    (signal, taskCount) =>
      signal.sequence > navigationRenderSequence &&
      signal.pathname === '/tasks' &&
      signal.heading === '任务中心' &&
      signal.taskCount === taskCount,
    'navigation',
  );
  await beginLongTaskWindow(page);
  await page.mouse.click(
    (taskCenterLinkBounds?.x ?? 0) + (taskCenterLinkBounds?.width ?? 0) / 2,
    (taskCenterLinkBounds?.y ?? 0) + (taskCenterLinkBounds?.height ?? 0) / 2,
  );
  const navigationResponseCount = await navigationResponseCountPromise;
  const navigationRenderReady = await navigationRenderReadyPromise;
  const navigationLongTaskCount = await endLongTaskWindow(page, 'navigation');
  expect(navigationRenderReady).toMatchObject({
    pathname: '/tasks',
    heading: '任务中心',
    taskCount: navigationResponseCount,
  });
  const taskCenterHeading = page.getByRole('heading', { name: '任务中心' });
  await expect(page).toHaveURL('/tasks');
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
    progressResponses,
    null,
  );
  poolSamples.push({
    long_task_count: navigationLongTaskCount,
    interaction_kind: 'navigation',
    interactive: taskCenterVisible && runPageVisible && progressVisible,
    ...navigationEvidence,
    correctness_hash: '',
  });
  const cancelButton = page.getByRole('button', { name: '取消回测' });
  await expect(cancelButton).toBeVisible();
  const cancelButtonBounds = await cancelButton.boundingBox();
  expect(cancelButtonBounds).not.toBeNull();
  const cancellationRenderSequence = renderSignals.latestSequence;
  const cancelRequestResponsePromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `/api/backtests/${poolSubmission.run_id}/cancel` &&
      response.request().method() === 'POST',
  );
  const cancelledProgressStatePromise = nextProgressState(
    page,
    poolSubmission.run_id,
    (state) => state.status === 'cancelled',
  );
  const cancellationRenderReadyPromise = renderReadyAfter(
    renderSignals,
    cancelledProgressStatePromise,
    (signal, state) =>
      signal.sequence > cancellationRenderSequence &&
      signal.pathname === `/backtests/${poolSubmission.run_id}` &&
      signal.progressKey === progressKey(state),
    'cancellation',
  );
  await beginLongTaskWindow(page);
  await page.mouse.click(
    (cancelButtonBounds?.x ?? 0) + (cancelButtonBounds?.width ?? 0) / 2,
    (cancelButtonBounds?.y ?? 0) + (cancelButtonBounds?.height ?? 0) / 2,
  );
  const cancelRequestResponse = await cancelRequestResponsePromise;
  const finalOverview = await cancelledProgressStatePromise;
  await cancellationRenderReadyPromise;
  const cancellationLongTaskCount = await endLongTaskWindow(page, 'cancel');
  expect(cancelRequestResponse.ok()).toBe(true);
  await expect(
    page.locator('.run-progress .status-badge[data-status="cancelled"]'),
  ).toBeVisible({ timeout: 45_000 });
  const finalRenderedState = parseRenderedProgress(
    await page
      .getByRole('region', { name: '运行进度' })
      .getAttribute('data-rendered-progress'),
  );
  expect(finalRenderedState).toEqual(finalOverview);
  const cancellationEvidence = {
    rendered_state: finalRenderedState ?? finalOverview,
    api_state: finalOverview,
  };
  expect(finalOverview.status).toBe('cancelled');
  poolSamples.push({
    long_task_count: cancellationLongTaskCount,
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
  expect(progressResponseErrors).toEqual([]);
  page.off('response', captureProgressResponse);
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
          command: portableCommandTokens(service.command),
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
