# Performance reference and target gate

Stock Desk v1 measures the 2/3/5-second requirements on a network-forbidden,
cached, CC0 synthetic workload. Results are not vendor-data benchmarks. A local
run on faster hardware is a `reference`; only a qualifying GitHub-hosted Ubuntu
x64 standard runner can emit `target_baseline` evidence for the ordinary
4-CPU/16GB requirement. The committed baseline is the byte-for-byte reviewed
target evidence, not a locally regenerated approximation.

## Reproduce a local reference

Install the locked Python and Web dependencies plus Playwright Chromium, then
run from the repository root:

```bash
uv sync --frozen --all-groups --extra providers
pnpm install --frozen-lockfile
pnpm exec playwright install chromium
make performance-reference
```

`make performance` is an alias for `make performance-reference`. The runner
seeds normal repositories and the real backtest worker, blocks non-loopback
browser traffic, writes `test-results/performance/current.json` atomically, and
compares semantic correctness hashes with `tests/performance/baseline.json`.
That committed file is ordinary-machine proof; a local `reference` run is only
an implementation regression check and never replaces its runner claim.

To replace that reference, first commit every implementation change so the
worktree is clean, then run:

```bash
uv run --frozen python scripts/run_performance_baseline.py \
  --fixture full-a-scope-bounded-ten-year --evidence-kind reference --record-baseline
```

The command has no timing injection or raw-browser-output override. Recording
refuses dirty Git state, an invalid/current-checkout SHA, stale fixture content,
unavailable tools, undersized hardware, or any failed schema, correctness, or
budget gate.

## Target baseline in GitHub Actions

The CI target uses the pinned `ubuntu-24.04` x64 standard runner documented in
[GitHub-hosted runner specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
and executes `make performance-target`. Evidence is accepted as
`target_baseline` only when
the measured environment reports all of the following:

- GitHub Actions runner metadata, Linux, and `RUNNER_ARCH=X64`;
- exactly four logical and four effective CPUs after affinity/quota limits;
- nominal 16GB physical memory and at least 15 GiB usable memory;
- the exact byte counts, hosted-image identifiers, repository, run ID, and run
  attempt in the artifact.

The workflow uploads `target-baseline-ubuntu-x64-4c16g`. R-053 is verified by
the reviewed artifact from CI run `28968553479`, measured at source commit
`dfac5a7d1f1cf1b8bb465c27a623b664eceb90d2` (H0). Its exact committed file
SHA-256 is
`debe271724a85ec69f3eb2ed2a37cdc5a0a7ab2aac8a3969b45b65abe3037f01`.
The import commit (H1) must descend from H0; CI fetches full history and rejects
testing the baseline on H0 itself, so the evidence cannot certify its own
unreviewed import. Merely adding the workflow or passing on a faster host does
not verify this requirement.

## Fixed workload and raw windows

`tests/fixtures/performance/full-a-scope-bounded-ten-year.json` stores compact generator
metadata rather than committed bars. It defines 5,000 stable synthetic A-share
instrument metadata rows and 40 bounded runnable symbols with ten-year data.
The single-stock series contains 2,632 weekday bars, including 2,608 scoring
sessions from 2016-01-01 through 2025-12-31 plus warm-up. This fixture tests a
full-scope UI and asynchronous worker behavior; it does not claim 5,000-stock
backtest throughput.

Each summary uses exactly 20 raw measurements. They are not all independent:

- Chart cold uses 20 new Chromium contexts with empty browser/HTTP cache. The
  timer starts with cached-symbol selection and stops only after the active
  ECharts generation emits `finished` and a bounded real hover/crosshair,
  reset/zoom, and drag handshake succeeds.
- Chart warm uses 20 adjustment windows on one shared warm page and the same
  interaction-complete timing boundary. Each window captures the prior completed
  generation and waits for the adjustment response plus a strictly newer ECharts
  `finished` generation before interactions and timing/RSS shutdown. The
  transient pending DOM state is component-tested but is not a polling gate. The
  page, React tree, ECharts instance, browser cache, and local services are shared.
- Formula cache-cold uses 20 distinct pre-seeded immutable formula versions.
  Each timer covers preview action through main/subchart, BUY/SELL, summary,
  and active-generation ECharts readiness.
- Single backtest fresh uses 20 new tasks. Each timer covers submission, worker
  claim/execution, report persistence/fetch, and visible conclusion readiness.
- Pool UI uses 20 Long Task windows from one worker-backed pool task: 18 windows
  each record a rendered `processed/total/stage/failed` tuple and an exact API
  match from the successful page-response ledger. This avoids a second-request
  race while proving the DOM tuple came from an authoritative API response.
  Repeated snapshots are valid, but the windows must contain at least two
  distinct rendered states and show a change from the initial state. One SPA
  navigation window and one actual cancellation window follow. Every window
  must contain zero Long Tasks over 50 ms.

## Evidence and trust rules

Every timed sample records wall/local time, exact zero provider wait, immutable
routing-attempt labels, blocked external requests, RSS start/peak/delta, and a
canonical correctness hash. Object keys are recursively sorted before hashing;
array order is preserved. The pool hash contains only the semantic formula
checksum, membership digest, data digest, and terminal status—never random run,
task, formula-version, or snapshot UUIDs.

The cached routing manifest has no duration field. Therefore the gate can prove
only an empty attempt ledger: zero attempts gives exact zero calls and wait, and
any nonempty attempt is rejected rather than assigned a fabricated duration.
The separate cached-loading UI contract remains covered by
`web/src/features/market/MarketChart.test.tsx`; this gate makes no unavailable
nonzero provider-duration claim.

RSS is sampled through asynchronous `ps` calls on Linux/macOS. The timed Node
loop never blocks on `execFileSync` and excludes the `ps` helper. Every observed
root or late child is identified by PID plus portable process start time; a
late descendant's legal `exec`/process-title command evolution is retained in
its incarnation history, while a reused PID with a new start time is tracked as
a new process and remains in RSS. Declared roots must match the exact
launch-manifest command-token sequence before their full observed command/start
identity is frozen; root changes or disappearance fail the sample. The bounded
role set and digest are persisted once. Windows performance measurement fails
before browser startup; Windows product packaging remains a separate release
concern.

The strict validator requires exact keys and primitive types, a real UTC
datetime, clean 40-hex Git commit provenance, expected-source equality and a
local `git cat-file` commit-object check, finite CPU values, sorted unique
positive roots/services, exact service-role/command relationships, semantic
DuckDB/Playwright/pnpm/Python/Node/Chromium version formats, recomputed role and
semantic digests, exactly 20 raw windows, exact integer zero forbidden
requests/Long Tasks, and stable correctness against the reference. Target
artifacts additionally require the literal `CongBao/stock-desk` repository,
positive integer run ID/attempt, and Ubuntu image identifier/version patterns.
These are locally verifiable artifact fields, not independent proof that GitHub
issued the artifact; review of the uploaded workflow artifact remains required.
The budgets remain absolute release ceilings and must never be relaxed to make
a run pass.
