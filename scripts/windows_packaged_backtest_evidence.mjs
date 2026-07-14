import { createHash } from "node:crypto";
import { readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonical(value[key])]),
    );
  }
  return value;
}

function digest(value) {
  return createHash("sha256")
    .update(JSON.stringify(canonical(value)))
    .digest("hex");
}

const RETRYABLE_FILE_ERROR_CODES = new Set([
  "ENOENT",
  "EACCES",
  "EPERM",
  "EBUSY",
]);

async function captureHandshake(name, payload) {
  const syncDir = process.env.STOCK_DESK_RESTART_SYNC_DIR;
  const nonce = process.env.STOCK_DESK_CAPTURE_NONCE;
  if (
    syncDir === undefined ||
    nonce === undefined ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u.test(
      nonce,
    )
  ) {
    throw new Error("Windows capture handshake identity is unavailable");
  }
  const marker = path.join(syncDir, `${name}.json`);
  const acknowledgment = path.join(syncDir, `${name}.ack`);
  const temporaryMarker = path.join(syncDir, `.${name}.${nonce}.tmp`);
  await writeFile(
    temporaryMarker,
    `${JSON.stringify({ capture_nonce: nonce, ...payload })}\n`,
    "utf8",
  );
  await rename(temporaryMarker, marker);
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    try {
      if ((await readFile(acknowledgment, "utf8")).trim() === nonce) return;
    } catch (error) {
      if (!RETRYABLE_FILE_ERROR_CODES.has(error?.code)) throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Windows capture acknowledgment timed out: ${name}`);
}

async function selectFixture(name, markerName = name) {
  await captureHandshake(`fixture-${markerName}`, { fixture_id: name });
}

async function invoke(page, method, requestPath, body = null) {
  const response = await page.evaluate(
    async ({ bodyValue, methodValue, pathValue }) => {
      const internals = globalThis.__TAURI_INTERNALS__;
      if (internals === undefined || typeof internals.invoke !== "function") {
        throw new Error("Tauri invoke bridge is unavailable");
      }
      return internals.invoke("desktop_api_request", {
        request: { method: methodValue, path: pathValue, body: bodyValue },
      });
    },
    {
      bodyValue: body === null ? null : JSON.stringify(body),
      methodValue: method,
      pathValue: requestPath,
    },
  );
  let payload;
  try {
    payload = JSON.parse(response.body);
  } catch {
    throw new Error(`host IPC returned non-JSON for ${method} ${requestPath}`);
  }
  if (response.status < 200 || response.status >= 300) {
    throw new Error(
      `host IPC rejected ${method} ${requestPath}: ${response.status} ${JSON.stringify(payload)}`,
    );
  }
  return payload;
}

async function waitForRuntimeReady(page) {
  const deadline = Date.now() + 90_000;
  let latest;
  while (Date.now() < deadline) {
    latest = await page.evaluate(() =>
      globalThis.__TAURI_INTERNALS__.invoke("desktop_runtime_state"),
    );
    if (latest.state === "ready") return latest;
    await page.waitForTimeout(250);
  }
  throw new Error(
    `packaged sidecar did not become ready: ${JSON.stringify(latest)}`,
  );
}

async function waitForRuntimeRecovery(page) {
  const deadline = Date.now() + 90_000;
  let latest;
  while (Date.now() < deadline) {
    latest = await page.evaluate(() =>
      globalThis.__TAURI_INTERNALS__.invoke("desktop_runtime_state"),
    );
    if (latest.state === "recovery" && latest.can_restart === true)
      return latest;
    await page.waitForTimeout(100);
  }
  throw new Error(
    `packaged sidecar did not enter restartable recovery: ${JSON.stringify(latest)}`,
  );
}

async function task(page, taskId) {
  const currentTasks = await invoke(page, "GET", "/api/tasks?limit=100");
  const match = currentTasks.find((item) => item.id === taskId);
  if (match === undefined)
    throw new Error(`backtest task is missing: ${taskId}`);
  return match;
}

async function waitForCheckpointBacklog(page, taskIds, timeout = 10_000) {
  const deadline = Date.now() + timeout;
  let latest = [];
  while (Date.now() < deadline) {
    latest = await invoke(page, "GET", "/api/tasks?limit=100");
    const selected = latest.filter((item) => taskIds.has(item.id));
    const running = selected.filter((item) => item.status === "running");
    const queued = selected.filter((item) => item.status === "queued");
    if (running.length === 1 && queued.length >= 8) return selected;
    if (
      selected.length === taskIds.size &&
      selected.every((item) =>
        ["succeeded", "failed", "cancelled"].includes(item.status),
      )
    ) {
      break;
    }
    await page.waitForTimeout(10);
  }
  throw new Error(
    `packaged checkpoint backlog did not expose one running and eight queued tasks: ${JSON.stringify(latest)}`,
  );
}

async function waitForCheckpointBacklogSuccess(
  page,
  taskIds,
  timeout = 120_000,
) {
  const deadline = Date.now() + timeout;
  let selected = [];
  while (Date.now() < deadline) {
    const currentTasks = await invoke(page, "GET", "/api/tasks?limit=100");
    selected = currentTasks.filter((item) => taskIds.has(item.id));
    if (
      selected.length === taskIds.size &&
      selected.every((item) => item.status === "succeeded")
    ) {
      return selected;
    }
    if (
      selected.some((item) => ["failed", "cancelled"].includes(item.status))
    ) {
      break;
    }
    await page.waitForTimeout(50);
  }
  throw new Error(
    `packaged checkpoint backlog did not finish successfully: ${JSON.stringify(selected)}`,
  );
}

async function waitForTask(page, taskId, statuses, timeout = 90_000) {
  const deadline = Date.now() + timeout;
  let latest;
  while (Date.now() < deadline) {
    latest = await task(page, taskId);
    if (statuses.includes(latest.status)) return latest;
    await page.waitForTimeout(150);
  }
  throw new Error(
    `packaged backtest task did not reach ${statuses.join("/")}: ${JSON.stringify(latest)}`,
  );
}

async function pageAll(page, runId, collection) {
  const items = [];
  let cursor = null;
  do {
    const suffix =
      cursor === null ? "" : `&cursor=${encodeURIComponent(cursor)}`;
    const response = await invoke(
      page,
      "GET",
      `/api/backtests/${runId}/${collection}?limit=100${suffix}`,
    );
    if (!Array.isArray(response.items)) {
      throw new Error(`invalid packaged collection page: ${collection}`);
    }
    items.push(...response.items);
    cursor = response.next_cursor;
  } while (cursor !== null);
  return { items };
}

function intent(seed, formulaId, scope, period) {
  const formula = seed.formulas[formulaId];
  const range = seed.periods[period];
  const pool = seed.pools[period];
  return {
    scope:
      scope === "single"
        ? { kind: "single", symbol: pool.symbols[0] }
        : {
            kind: "preset",
            pool_id: pool.pool_id,
            snapshot_id: pool.snapshot_id,
          },
    formula_version_id: formula.version_id,
    formula_parameters: formula.parameters,
    period,
    adjustment: "none",
    scoring_start: range.scoring_start,
    scoring_end: range.scoring_end,
    quantity_shares: seed.costs.quantity_shares,
    commission_bps: seed.costs.commission_bps,
    minimum_commission: seed.costs.minimum_commission,
    sell_tax_bps: seed.costs.sell_tax_bps,
    slippage_bps: seed.costs.slippage_bps,
  };
}

async function completedEvidence(page, seed, formulaId, scope, period) {
  const caseId = `${formulaId}_${scope}_${period}`;
  const request = intent(seed, formulaId, scope, period);
  const preflight = await invoke(
    page,
    "POST",
    "/api/backtests/preflight",
    request,
  );
  if (preflight.scope.runnable !== (scope === "single" ? 1 : 2)) {
    throw new Error(`unexpected runnable scope for ${caseId}`);
  }
  const submission = await invoke(page, "POST", "/api/backtests", request);
  const finishedTask = await waitForTask(page, submission.task_id, [
    "succeeded",
    "failed",
    "cancelled",
  ]);
  if (finishedTask.status !== "succeeded") {
    throw new Error(`packaged Worker failed ${caseId}: ${finishedTask.status}`);
  }
  const overview = await invoke(
    page,
    "GET",
    `/api/backtests/${submission.run_id}`,
  );
  const report = await invoke(
    page,
    "GET",
    `/api/backtests/${submission.run_id}/report`,
  );
  const collections = {};
  for (const name of [
    "groups",
    "trades",
    "open",
    "failures",
    "logs",
    "symbols",
  ]) {
    collections[name] = await pageAll(page, submission.run_id, name);
  }
  if (
    overview.snapshot_id !== submission.snapshot_id ||
    report.overview.snapshot_id !== submission.snapshot_id ||
    overview.result_hash !== report.overview.result_hash
  ) {
    throw new Error(`frozen identity mismatch for ${caseId}`);
  }
  return {
    case_id: caseId,
    formula: {
      kind: formulaId,
      version_id: seed.formulas[formulaId].version_id,
      checksum: seed.formulas[formulaId].checksum,
      parameters: seed.formulas[formulaId].parameters,
    },
    scope,
    period,
    run_id: submission.run_id,
    task_id: submission.task_id,
    snapshot_id: submission.snapshot_id,
    result_hash: overview.result_hash,
    worker_id: finishedTask.worker_id,
    oracle_semantic_digest: seed.oracle_case_semantic_digests[caseId],
    preflight_sha256: digest(preflight),
    overview_sha256: digest(overview),
    report_sha256: digest(report),
    collections_sha256: digest(collections),
    report_semantics: {
      formula_checksum: report.formula_checksum,
      formula_parameters: report.formula_parameters,
      formula_engine_version: report.formula_engine_version,
      compatibility_version: report.compatibility_version,
      backtest_engine_version: report.backtest_engine_version,
      symbol_count: report.provenance.symbol_count,
      runnable_count: report.provenance.runnable_count,
      gap_count: report.provenance.gap_count,
      signal_source_ids: report.provenance.source_ids.signal,
      execution_source_ids: report.provenance.source_ids.execution,
      status_source_ids: report.provenance.source_ids.status,
      period: report.period,
      adjustment: report.adjustment,
      quantity_shares: report.quantity_shares,
      commission_bps: report.costs.commission_bps,
      minimum_commission: report.costs.minimum_commission,
      sell_tax_bps: report.costs.sell_tax_bps,
      slippage_bps: report.costs.slippage_bps,
      execution_rules_version: report.execution_rules_version,
      cost_model_version: report.cost_model_version,
      sizing_version: report.sizing_version,
      warmup_policy_version: report.warmup_policy_version,
      metrics: report.metrics,
      disclaimer: report.disclaimer,
      outcomes: report.outcomes,
    },
  };
}

async function specialEvidence(page, seed, caseId) {
  const special = seed.special_cases[caseId];
  const request = {
    scope: special.scope,
    formula_version_id: special.formula.version_id,
    formula_parameters: special.formula.parameters,
    period: special.period,
    adjustment: "none",
    scoring_start: special.scoring_start,
    scoring_end: special.scoring_end,
    quantity_shares: seed.costs.quantity_shares,
    commission_bps: seed.costs.commission_bps,
    minimum_commission: seed.costs.minimum_commission,
    sell_tax_bps: seed.costs.sell_tax_bps,
    slippage_bps: seed.costs.slippage_bps,
  };
  const preflight = await invoke(
    page,
    "POST",
    "/api/backtests/preflight",
    request,
  );
  const submission = await invoke(page, "POST", "/api/backtests", request);
  const finished = await waitForTask(page, submission.task_id, [
    "succeeded",
    "failed",
    "cancelled",
  ]);
  if (finished.status !== "succeeded") {
    throw new Error(`packaged Worker failed special case ${caseId}`);
  }
  const overview = await invoke(
    page,
    "GET",
    `/api/backtests/${submission.run_id}`,
  );
  const report = await invoke(
    page,
    "GET",
    `/api/backtests/${submission.run_id}/report`,
  );
  const collections = {};
  for (const name of [
    "groups",
    "trades",
    "open",
    "failures",
    "logs",
    "symbols",
  ]) {
    collections[name] = await pageAll(page, submission.run_id, name);
  }
  return {
    case_id: caseId,
    run_id: submission.run_id,
    task_id: submission.task_id,
    snapshot_id: submission.snapshot_id,
    result_hash: overview.result_hash,
    worker_id: finished.worker_id,
    oracle_semantic_digest: seed.oracle_case_semantic_digests[caseId],
    preflight_sha256: digest(preflight),
    overview_sha256: digest(overview),
    report_sha256: digest(report),
    collections_sha256: digest(collections),
  };
}

async function checkpointEvidence(page, seed, baseline) {
  const request = intent(seed, "custom", "pool", "1d");
  const runtimeBefore = await waitForRuntimeReady(page);
  // A two-symbol packaged run can finish before WebView polling observes its
  // transient `running` state. Queue identical real runs serially so the
  // single packaged Worker has a deterministic backlog without turning this
  // evidence probe into a concurrent write load test. Then let the production
  // shutdown barrier identify the task it actually paused. This preserves a
  // real in-flight checkpoint instead of treating a fast terminal task as
  // equivalent evidence.
  const submissions = [];
  // Eight earlier evidence tasks plus this backlog and eight later matrix
  // tasks remain below the authenticated task endpoint's 100-row bound.
  for (let index = 0; index < 64; index += 1) {
    submissions.push(await invoke(page, "POST", "/api/backtests", request));
  }
  const taskIds = new Set(submissions.map((item) => item.task_id));
  if (taskIds.size !== submissions.length) {
    throw new Error("packaged checkpoint backlog reused a task identity");
  }
  await waitForCheckpointBacklog(page, taskIds);
  const checkpoint = await invoke(page, "POST", "/api/desktop/shutdown", {
    checkpoint_active: true,
  });
  if (!checkpoint.recovery_required || checkpoint.running !== 1) {
    throw new Error(
      "packaged custom pool did not create a recovery checkpoint",
    );
  }
  const pausedTasks = (
    await invoke(page, "GET", "/api/tasks?limit=100")
  ).filter((item) => taskIds.has(item.id) && item.status === "running");
  if (pausedTasks.length !== 1) {
    throw new Error(
      `packaged checkpoint did not identify exactly one paused task: ${JSON.stringify(pausedTasks)}`,
    );
  }
  const running = pausedTasks[0];
  const submission = submissions.find((item) => item.task_id === running.id);
  if (
    submission === undefined ||
    typeof running.worker_id !== "string" ||
    running.worker_id.length === 0
  ) {
    throw new Error("packaged checkpoint paused task identity is incomplete");
  }
  await captureHandshake("restart-before", {
    run_id: submission.run_id,
    task_id: submission.task_id,
    worker_id: running.worker_id,
    runtime_state: runtimeBefore.state,
  });
  await invoke(page, "POST", "/api/desktop/shutdown/commit");
  const runtimeRecovery = await waitForRuntimeRecovery(page);
  await page.evaluate(() =>
    globalThis.__TAURI_INTERNALS__.invoke("desktop_restart_service"),
  );
  // The Windows capture harness proves the OS-process generation change. Do
  // not require observing the potentially brief `starting` wire state here.
  const runtimeAfter = await waitForRuntimeReady(page);
  await captureHandshake("restart-after", {
    run_id: submission.run_id,
    task_id: submission.task_id,
    runtime_state: runtimeAfter.state,
  });
  const recovery = await invoke(page, "GET", "/api/desktop/recovery");
  if (!recovery.required)
    throw new Error("new packaged sidecar did not require recovery");
  await invoke(page, "POST", "/api/desktop/recovery/resume", {});
  const finished = await waitForTask(
    page,
    submission.task_id,
    ["succeeded", "failed", "cancelled"],
    120_000,
  );
  if (
    finished.status !== "succeeded" ||
    running.worker_id === finished.worker_id
  ) {
    throw new Error("packaged checkpoint did not resume on a new Worker");
  }
  // Do not let evidence-only duplicate runs overlap a later fixture switch.
  // Every queued copy must reach the same successful production terminal state.
  await waitForCheckpointBacklogSuccess(page, taskIds);
  const report = await invoke(
    page,
    "GET",
    `/api/backtests/${submission.run_id}/report`,
  );
  return {
    case_id: "custom_pool_1d_checkpoint_resume",
    run_id: submission.run_id,
    task_id: submission.task_id,
    snapshot_id: submission.snapshot_id,
    result_hash: report.overview.result_hash,
    worker_before: running.worker_id,
    worker_after: finished.worker_id,
    runtime_state_before: runtimeBefore.state,
    runtime_state_recovery: runtimeRecovery.state,
    runtime_state_after: runtimeAfter.state,
    runtime_restart_observed: true,
    recovery_required: recovery.required,
    report_sha256: digest(report),
    baseline_run_id: baseline.run_id,
    baseline_task_id: baseline.task_id,
    baseline_snapshot_id: baseline.snapshot_id,
    baseline_result_hash: baseline.result_hash,
    baseline_worker_id: baseline.worker_id,
  };
}

export async function runPackagedBacktestEvidence(page, outputDir) {
  const seedPath = process.env.STOCK_DESK_PACKAGED_BACKTEST_SEED;
  const candidateSha256 = process.env.STOCK_DESK_CANDIDATE_SHA256;
  const captureNonce = process.env.STOCK_DESK_CAPTURE_NONCE;
  if (
    seedPath === undefined ||
    !/^[0-9a-f]{64}$/u.test(candidateSha256 ?? "") ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u.test(
      captureNonce ?? "",
    )
  ) {
    throw new Error("packaged backtest evidence identity is unavailable");
  }
  const seedBytes = await readFile(seedPath);
  const seed = JSON.parse(seedBytes.toString("utf8"));
  if (seed.read_only_demo !== false || seed.matrix_case_ids.length !== 12) {
    throw new Error(
      "packaged backtest evidence seed is not writable or complete",
    );
  }
  const backtestLink = page.locator('#primary-navigation a[href="/backtests"]');
  await backtestLink.click();
  await page
    .getByRole("heading", { name: "策略回测" })
    .waitFor({ state: "visible" });

  const specialCases = [];
  for (const caseId of [
    "a_share_constraints_60m",
    "open_position_costs_1d",
    "partial_pool_gap_1d",
  ]) {
    await selectFixture(caseId);
    specialCases.push(await specialEvidence(page, seed, caseId));
  }
  const cells = [];
  let checkpoint;
  for (const period of ["1d", "1w", "60m"]) {
    await selectFixture(`matrix_${period}`);
    for (const formulaId of ["macd", "custom"]) {
      for (const scope of ["single", "pool"]) {
        cells.push(
          await completedEvidence(page, seed, formulaId, scope, period),
        );
      }
    }
    // Capture the daily restart before the weekly fixture publishes its
    // longer daily execution companion into the shared immutable catalog.
    // Otherwise a later daily submission can correctly select that newer
    // execution dataset while the daily status fixture still ends at June,
    // producing an intentionally fail-closed but non-oracle input pairing.
    if (period === "1d") {
      await selectFixture("matrix_1d", "checkpoint-matrix-1d");
      const baseline = await completedEvidence(
        page,
        seed,
        "custom",
        "pool",
        "1d",
      );
      checkpoint = await checkpointEvidence(page, seed, baseline);
    }
  }
  if (checkpoint === undefined) {
    throw new Error("packaged daily checkpoint evidence is missing");
  }
  const manifest = {
    schema_version: "stock-desk-packaged-backtest-evidence-v1",
    source_sha: seed.source_sha,
    source_tree: seed.source_tree,
    candidate_sha256: candidateSha256,
    capture_nonce: captureNonce,
    actual_packaged_tauri: true,
    actual_tauri_webview: true,
    authenticated_host_ipc: true,
    packaged_sidecar_worker: true,
    read_only_demo: false,
    submission_surface: "installed-tauri-webview-host-ipc",
    seed: {
      file: path.basename(seedPath),
      sha256: createHash("sha256").update(seedBytes).digest("hex"),
    },
    oracle: seed.oracle,
    cells,
    special_cases: specialCases,
    checkpoint,
  };
  await writeFile(
    path.join(outputDir, "packaged-backtest-evidence.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
    "utf8",
  );
  return manifest;
}
