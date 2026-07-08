import { expect, test, type BrowserContext, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

const SAMPLE_COUNT = 20;
const OUTPUT = process.env['STOCK_DESK_PERFORMANCE_RAW_OUTPUT'];
const PROCESS_FILE = process.env['STOCK_DESK_PERFORMANCE_PROCESS_FILE'];
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1']);

type TimedSample = {
  wall_seconds: number;
  local_seconds: number;
  external_wait_seconds: number;
  provider_span_count: number;
  provider_spans: readonly {
    source: string;
    decision: string;
    elapsed_seconds: number;
  }[];
  blocked_external_request_count: number;
  rss_start_bytes: number;
  rss_peak_bytes: number;
  rss_delta_bytes: number;
  rss_process_set_digest: string;
  correctness_hash: string;
};

function digest(value: unknown): string {
  return `sha256:${createHash('sha256')
    .update(JSON.stringify(value))
    .digest('hex')}`;
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

function processTreeSnapshot(roots: readonly number[]) {
  const rows = execFileSync('ps', ['-axo', 'pid=,ppid=,rss=,command='], {
    encoding: 'utf8',
  })
    .trim()
    .split('\n')
    .flatMap((line) => {
      const match = /^\s*(\d+)\s+(\d+)\s+(\d+)\s+(.+)$/u.exec(line);
      return match === null
        ? []
        : [
            {
              pid: Number(match[1]),
              parent: Number(match[2]),
              rss: Number(match[3]),
              command: match[4] ?? '',
            },
          ];
    });
  const descendants = new Set<number>(roots);
  let changed = true;
  while (changed) {
    changed = false;
    for (const row of rows) {
      if (descendants.has(row.parent) && !descendants.has(row.pid)) {
        descendants.add(row.pid);
        changed = true;
      }
    }
  }
  const selected = rows.filter((row) => descendants.has(row.pid));
  return {
    rssBytes: selected.reduce((sum, row) => sum + row.rss, 0) * 1024,
    commands: [...new Set(selected.map((row) => row.command))].sort(),
  };
}

class RssSampler {
  readonly start: number;
  peak: number;
  private readonly commands = new Set<string>();
  private timer: ReturnType<typeof setInterval> | undefined;

  constructor(private readonly roots: readonly number[]) {
    const snapshot = processTreeSnapshot(roots);
    this.start = snapshot.rssBytes;
    this.peak = this.start;
    snapshot.commands.forEach((command) => this.commands.add(command));
  }

  begin() {
    this.timer = setInterval(() => {
      const snapshot = processTreeSnapshot(this.roots);
      this.peak = Math.max(this.peak, snapshot.rssBytes);
      snapshot.commands.forEach((command) => this.commands.add(command));
    }, 50);
  }

  finish() {
    if (this.timer !== undefined) clearInterval(this.timer);
    const snapshot = processTreeSnapshot(this.roots);
    this.peak = Math.max(this.peak, snapshot.rssBytes);
    snapshot.commands.forEach((command) => this.commands.add(command));
    return {
      rss_start_bytes: this.start,
      rss_peak_bytes: this.peak,
      rss_delta_bytes: this.peak - this.start,
      rss_process_set_digest: digest(processRoles([...this.commands])),
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

type RoutingManifest = {
  selected_source: string;
  attempts: readonly {
    source: string;
    decision: string;
  }[];
};

function providerEvidence(manifest: RoutingManifest) {
  const providerSpans = manifest.attempts.map((attempt) => ({
    source: attempt.source,
    decision: attempt.decision,
    // The fixture contract forbids provider attempts in measured spans. A
    // non-empty list fails below rather than inventing an unavailable duration.
    elapsed_seconds: Number.NaN,
  }));
  expect(manifest.selected_source).toBe('stock_desk_demo');
  expect(providerSpans).toEqual([]);
  return {
    provider_spans: providerSpans,
    provider_span_count: providerSpans.length,
    external_wait_seconds: providerSpans.reduce(
      (sum, span) => sum + span.elapsed_seconds,
      0,
    ),
  };
}

async function chartAction(
  page: Page,
  network: { blockedExternalRequests: number },
  roots: readonly number[],
) {
  const blockedBefore = network.blockedExternalRequests;
  const sampler = new RssSampler(roots);
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
  const wall = (performance.now() - started) / 1000;
  const rss = sampler.finish();
  const blockedAtReady = network.blockedExternalRequests - blockedBefore;
  const canvas = chart.locator('canvas');
  const readout = page.getByRole('status', { name: '当前 K 线 OHLCV' });
  const zoom = page.getByRole('status', { name: '图表缩放范围' });
  const beforeReadout = await readout.textContent();
  const beforeZoom = await zoom.textContent();
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  if (box !== null) {
    await page.mouse.move(box.x + box.width * 0.35, box.y + 120);
    await expect.poll(() => readout.textContent()).not.toBe(beforeReadout);
    await page.getByRole('button', { name: '重置图表缩放' }).click();
    await expect(zoom).toContainText('0%–100%');
    await page.mouse.move(box.x + box.width * 0.5, box.y + 120);
    await page.mouse.wheel(0, -500);
    await expect.poll(() => zoom.textContent()).not.toBe(beforeZoom);
    const beforeDrag = await zoom.textContent();
    await page.mouse.move(box.x + box.width * 0.7, box.y + 120);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.5, box.y + 120);
    await page.mouse.up();
    await expect.poll(() => zoom.textContent()).not.toBe(beforeDrag);
  }
  expect(body.bars.length).toBeGreaterThanOrEqual(2400);
  expect(blockedAtReady).toBe(0);
  expect(network.blockedExternalRequests - blockedBefore).toBe(0);
  const provider = providerEvidence(body.routing_manifest);
  return {
    wall_seconds: wall,
    local_seconds: wall,
    ...provider,
    blocked_external_request_count: blockedAtReady,
    ...rss,
    correctness_hash: digest({
      bars: body.bars,
      dataset: body.dataset_version,
      route: body.route_version,
    }),
  } satisfies TimedSample;
}

async function warmChartAction(
  page: Page,
  network: { blockedExternalRequests: number },
  roots: readonly number[],
) {
  const adjustment = page.getByRole('combobox', { name: '复权方式' });
  await Promise.all([
    page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        url.pathname === '/api/market/bars' &&
        url.searchParams.get('adjustment') === 'none'
      );
    }),
    adjustment.selectOption('none'),
  ]);
  await expect(page.locator('[data-chart-ready="true"]')).toBeVisible();

  const blockedBefore = network.blockedExternalRequests;
  const sampler = new RssSampler(roots);
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
  const body = (await response.json()) as {
    bars: readonly unknown[];
    dataset_version: string;
    route_version: string;
    routing_manifest: RoutingManifest;
  };
  const chart = page.locator('[data-chart-ready="true"]');
  await expect(chart).toBeVisible();
  const wall = (performance.now() - started) / 1000;
  const rss = sampler.finish();
  const blockedAtReady = network.blockedExternalRequests - blockedBefore;
  const canvas = chart.locator('canvas');
  const readout = page.getByRole('status', { name: '当前 K 线 OHLCV' });
  const zoom = page.getByRole('status', { name: '图表缩放范围' });
  const beforeReadout = await readout.textContent();
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  if (box !== null) {
    await page.mouse.move(box.x + box.width * 0.35, box.y + 120);
    await expect.poll(() => readout.textContent()).not.toBe(beforeReadout);
    await page.getByRole('button', { name: '重置图表缩放' }).click();
    await expect(zoom).toContainText('0%–100%');
    await page.mouse.move(box.x + box.width * 0.5, box.y + 120);
    await page.mouse.wheel(0, -500);
    await expect.poll(() => zoom.textContent()).not.toContain('0%–100%');
    const beforeDrag = await zoom.textContent();
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.4, box.y + 120);
    await page.mouse.up();
    await expect.poll(() => zoom.textContent()).not.toBe(beforeDrag);
  }
  expect(body.bars.length).toBeGreaterThanOrEqual(2400);
  expect(blockedAtReady).toBe(0);
  expect(network.blockedExternalRequests - blockedBefore).toBe(0);
  return {
    wall_seconds: wall,
    local_seconds: wall,
    ...providerEvidence(body.routing_manifest),
    blocked_external_request_count: blockedAtReady,
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
) {
  const blockedBefore = network.blockedExternalRequests;
  const sampler = new RssSampler(roots);
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
  const rss = sampler.finish();
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
  cachedManifest: RoutingManifest,
  cachedManifestId: string,
) {
  const blockedBefore = network.blockedExternalRequests;
  const sampler = new RssSampler(roots);
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
  const rss = sampler.finish();
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
  const processEvidence = JSON.parse(
    readFileSync(PROCESS_FILE ?? '', 'utf8'),
  ) as {
    supervisor_pid: number;
    service_pids: number[];
    service_processes: { pid: number; command: string[] }[];
  };
  const roots = [
    process.pid,
    process.ppid,
    processEvidence.supervisor_pid,
    ...processEvidence.service_pids,
  ];
  const declaredProcessTree = processTreeSnapshot(roots);
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
    chartCold.push(await chartAction(page, network, roots));
    await context.close();
  }

  const context = await browser.newContext();
  const network = await forbidExternalNetwork(context);
  const page = await context.newPage();
  const chartWarm: TimedSample[] = [];
  await page.goto('/market');
  await chartAction(page, network, roots);
  for (let index = 0; index < SAMPLE_COUNT; index += 1) {
    chartWarm.push(await warmChartAction(page, network, roots));
  }

  const formulaSamples: TimedSample[] = [];
  for (let index = 0; index < SAMPLE_COUNT; index += 1) {
    await openFormula(page, index);
    formulaSamples.push(await formulaAction(page, network, roots));
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
    .filter({ hasText: 'Performance Full A (CC0 synthetic)' })
    .getAttribute('value');
  expect(poolValue).not.toBeNull();
  await poolSelect.selectOption(poolValue ?? '');
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByLabel('开始日期（上海时区，含）').fill('2016-01-01');
  await page.getByLabel('结束日期（上海时区，不含）').fill('2026-01-01');
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByRole('button', { name: '运行预检' }).click();
  const preflight = page.getByLabel('服务端预检结果');
  await expect(preflight).toContainText(/可运行 [1-9]\d* \/ 12/u);
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
  const observedProgressStates: string[] = [];
  const poolSamples: {
    long_task_count: number;
    interaction_kind: 'progress' | 'navigation' | 'cancel';
    interactive: boolean;
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
  for (let index = 0; index < SAMPLE_COUNT - 2; index += 1) {
    await beginLongTaskWindow(page);
    const overviewResponse = await page.request.get(
      `/api/backtests/${poolSubmission.run_id}`,
    );
    const overview = (await overviewResponse.json()) as {
      status: string;
      stage: string;
      processed: number;
    };
    observedProgressStates.push(
      `${overview.status}:${overview.stage}:${String(overview.processed)}`,
    );
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
      interactive: overviewResponse.ok() && cancelVisible && progressVisible,
      correctness_hash: '',
    });
  }
  await expect
    .poll(
      async () => {
        const response = await page.request.get(
          `/api/backtests/${poolSubmission.run_id}`,
        );
        const overview = (await response.json()) as {
          status: string;
          stage: string;
          processed: number;
        };
        observedProgressStates.push(
          `${overview.status}:${overview.stage}:${String(overview.processed)}`,
        );
        return new Set(
          observedProgressStates.map((value) => value.split(':').at(-1)),
        ).size;
      },
      { timeout: 10_000 },
    )
    .toBeGreaterThan(1);
  await beginLongTaskWindow(page);
  const navigationStarted = performance.now();
  await page.getByRole('link', { name: '任务' }).click();
  await expect(page.getByRole('heading', { name: '任务中心' })).toBeVisible();
  await page.goBack();
  await expect(page.getByRole('heading', { name: '回测运行' })).toBeVisible();
  const navigationInteractive = performance.now() - navigationStarted < 1000;
  poolSamples.push({
    long_task_count: await endLongTaskWindow(page),
    interaction_kind: 'navigation',
    interactive: navigationInteractive,
    correctness_hash: '',
  });
  await beginLongTaskWindow(page);
  const cancelButton = page.getByRole('button', { name: '取消回测' });
  await expect(cancelButton).toBeVisible();
  await cancelButton.click();
  await expect(
    page.locator('.run-progress .status-badge[data-status="cancelled"]'),
  ).toBeVisible({ timeout: 45_000 });
  const finalOverviewResponse = await page.request.get(
    `/api/backtests/${poolSubmission.run_id}`,
  );
  const finalOverview = (await finalOverviewResponse.json()) as {
    status: string;
  };
  expect(finalOverview.status).toBe('cancelled');
  observedProgressStates.push(
    `${finalOverview.status}:terminal:${String(observedProgressStates.length)}`,
  );
  poolSamples.push({
    long_task_count: await endLongTaskWindow(page),
    interaction_kind: 'cancel',
    interactive: finalOverview.status === 'cancelled',
    correctness_hash: '',
  });
  const poolCorrectness = digest({
    snapshot_id: poolSubmission.snapshot_id,
    status: finalOverview.status,
  });
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
        'same Chromium and service processes after one unmeasured completed render; timer boundaries match chart_cold',
      formula_cache_cold:
        'one distinct pre-seeded immutable formula version per sample; timer starts at preview action and ends at ECharts finished with main/subchart/BUY/SELL visible',
      single_backtest_fresh:
        'new submitted task per sample; timer starts at POST submit and ends after worker-persisted report is visible',
    },
    process_tree: {
      declared_roots: roots,
      declared_services: processEvidence.service_processes.map((service) => ({
        pid: service.pid,
        role: service.command.includes('uvicorn')
          ? 'api'
          : service.command.includes('--worker')
            ? 'worker'
            : 'web',
      })),
      sampled_process_roles: processRoles(declaredProcessTree.commands),
    },
    metrics: {
      chart_cold: aggregate(chartCold, 2),
      chart_warm: aggregate(chartWarm, 2),
      formula_preview: aggregate(formulaSamples, 3),
      single_backtest: aggregate(backtestSamples, 5),
      pool_ui: {
        samples: poolSamples,
        long_task_count: totalLongTasks,
        observed_progress_states: [...new Set(observedProgressStates)],
        worker_claim_observed: observedProgressStates.some((value) =>
          value.startsWith('running:'),
        ),
        cancel_status: finalOverview.status,
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
