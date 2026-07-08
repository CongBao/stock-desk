# Performance release gate

Stock Desk v1 applies the 2/3/5-second requirements to end-user actions on a
network-forbidden, cached, synthetic workload. The gate is not a vendor-data
benchmark and must not be used to make comparative hardware or data-provider
claims.

## Reproduce the gate

Install the locked Python and Web dependencies plus Playwright Chromium, then
run from the repository root:

```bash
uv sync --frozen --all-groups --extra providers
pnpm install --frozen-lockfile
pnpm exec playwright install chromium
make performance
```

`make performance` runs Playwright with retries disabled. The runner generates
and seeds the fixture through `InstrumentRepository`, `MarketLake`,
`ExecutionStatusLake`, `FormulaRepository`, and the normal backtest worker. It
writes `test-results/performance/current.json` atomically, validates that file,
and compares correctness hashes with `tests/performance/baseline.json`.
`test-results/` is ignored and is uploaded by CI together with Playwright
traces. The CLI accepts paths and the fixed fixture name only; it has no timing
override. A baseline cannot be recorded from an external raw result or a
skipped browser run.

To produce a candidate baseline, first commit every implementation change so
the worktree is clean, then run:

```bash
uv run --frozen python scripts/run_performance_baseline.py \
  --fixture ten-year-a-share --record-baseline
```

Recording refuses a dirty tree, a stale fixture digest, undersized effective
hardware, or any failing schema/correctness/budget gate. Review the JSON before
committing it. Do not edit summaries by hand.

## Fixed workload and timing boundaries

`tests/fixtures/performance/ten-year-a-share.json` is visibly labelled CC0
synthetic data with `network_policy: forbidden`. It stores generator metadata,
not thousands of committed rows. The deterministic generator produces 2,632
weekday daily bars, including 2,608 scoring sessions from 2016-01-01 through
2025-12-31 and an earlier warm-up. Its canonical content digest is checked
before seeding and again by the result gate.

Every repeated metric contains at least 20 independent raw samples. Summary
mean and nearest-rank p95 are recomputed from those samples:

- Chart cold: a new Chromium context with empty browser/HTTP cache. Timing
  starts when the user selects the cached security and ends only after the
  ECharts `finished` event. Hover/crosshair, zoom, and drag must then work.
- Chart warm: the same page, React tree, ECharts process, local services, and
  browser cache after an untimed completed render. A same-page adjustment
  switch and reselect starts the next sample; navigation/reload is excluded.
- Formula cache-cold: each sample uses a different immutable, pre-seeded formula
  version with identical source. Seeding is outside the timer. Timing ends only
  after the main chart, subchart, BUY/SELL signals, summary, and ECharts
  `finished` readiness are visible.
- Single backtest fresh: each sample submits a new task. Timing includes submit,
  claim, independent worker execution, report persistence, report fetch, and
  visible conclusion readiness.
- Pool UI: a production-repository-backed synthetic full-A preset is submitted
  through the visible wizard. Eighteen progress-render windows, one SPA
  navigation window, and the actual cancellation window are observed
  separately. Every window must contain exactly zero browser Long Tasks over
  50 ms and remain interactive. Authoritative worker progress must change and
  the final task state must be `cancelled`.

## Evidence and trust rules

For each timed sample the current file records wall time, local time, provider
span count and duration, blocked external browser requests, start/peak/delta
RSS for the declared process tree, a bounded process-role-set digest, and a normalized
correctness hash. The top-level evidence records only redacted role labels; absolute
commands and local paths are used for runtime assertions but are never persisted.
The process roots cover the Playwright runner/browser and the
supervisor, API, worker, and Vite service processes. The runner also records
effective CPU affinity/cgroup quota, effective cgroup memory limit, physical
memory, OS/architecture, Python/Node/tool versions, actual
`browser.version()`, fixture rows/digest, UTC measurement time, Git SHA, and
dirty state.

Provider wait is derived from the routed provenance manifest attempts; it is
not inserted as a zero. The CC0 cached route must select `stock_desk_demo` with
an empty attempt list, so its measured provider span count and summed external
wait are both zero. Playwright separately blocks and counts every non-loopback
HTTP(S) request. Any attempt fails the gate.

The validator rejects fewer than 20 samples, NaN/negative values, mismatched
mean or nearest-rank p95, stale digests, insufficient effective hardware,
missing process-tree or provenance evidence, any Long Task, interaction
failure, or changed chart/formula/backtest/pool correctness hashes.

## Hardware normalization and interpretation

The product requirement names an ordinary effective 4-core/16GB machine. The
committed baseline records the host truthfully. A run qualifies when effective
CPU is at least four cores and usable memory is at least 15 GiB (the bounded
allowance for a nominal 16GB host after firmware/runner reservation) after
affinity, CPU quota, and memory limits are applied. A faster host is not
relabelled as 4-core/16GB;
its actual model/count/limits remain in the evidence. The budgets are absolute
release ceilings, not relative tolerances against the baseline.

Timing includes local orchestration noise and is unsuitable for microbenchmark
rankings. If a p95 fails, profile that path with a Playwright trace and the
relevant Python/DuckDB/Polars tools before changing implementation. Never relax
the budget, fixture, formula isolation, trade semantics, signal correctness, or
provenance contract to make a run pass.
