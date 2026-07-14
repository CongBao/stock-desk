# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Desktop transport compatibility evidence now covers twelve normal-market
  cells combining MACD/custom-formula, single-symbol/pool, and
  daily/weekly/60-minute inputs against the current direct service, including
  deterministic recovery by a fresh worker from a persisted pool checkpoint.
  This is transport regression coverage, not an independent v1.0 or complete
  A-share edge-rule oracle.
- Expanded automated desktop coverage for authenticated Analysis and Task
  Center flows, including masked domestic-model configuration, fixed prompt
  boundaries, untrusted-data isolation, evidence timing and sources, partial
  retry, insufficient evidence, safe task projections, contextual guidance,
  and model-cost recovery consent.
- Desktop shutdown now asks for confirmation, checkpoints active market,
  backtest, and analysis work at durable safe points, waits at most ten seconds
  for acknowledgement, and keeps the application open with actionable recovery
  when a safe checkpoint cannot be confirmed.
- Interrupted desktop work is summarized on the next launch and can be resumed
  or cancelled explicitly; analysis resumption requires a separate model-cost
  confirmation and defaults keyboard focus to the safer cancel action.
- Formula, backtest, analysis, and task APIs now have an authenticated desktop
  integration slice, while the v1.1 prerelease workflow publishes only the
  exact version-bound unsigned Windows candidate already proved by main CI.
- `v1.1.0-beta.2` completes the desktop UX stage with searchable/restorable
  workspaces, responsive themes and guidance, bounded sidecar recovery,
  local-only redacted diagnostics, unified Windows icons, and exact-SHA
  evidence captured from the installed packaged Tauri candidate.
- `v1.1.0-alpha.2` adds the standalone Tauri Windows shell, current-user NSIS
  installer, controlled Python sidecar, startup recovery, and exact-SHA Windows
  candidate reuse without rebuilding or rerunning source tests during release.
- `v1.1.0-alpha.1` establishes a conservative PR risk graph, exact-SHA Python
  test shards with combined branch coverage, deterministic browser evidence,
  content-addressed build manifests, and reusable immutable main proofs.
- CI now separates build and verification responsibilities so candidate and
  release workflows can reuse proved artifacts without repeating unit or E2E
  tests.

### Security

- The current-user Windows installer now verifies the production WebView2
  Evergreen registration and locked minimum version after the bundled offline
  installer runs. Missing, malformed, sentinel, or outdated runtimes stop
  installation without an Ignore path and provide bilingual recovery code
  `SD-WV2-VERIFY-01`.
- A machine-readable desktop privacy policy and fail-closed repository guard
  now reject known telemetry or crash SDK signatures, automatic diagnostic
  upload, premature updater enablement, stable device identifiers, and
  non-anonymous future update requests; exact Windows candidate and main
  evidence bind the policy and verifier.
- Diagnostic export is desktop-session authenticated, schema allowlisted,
  validated independently by React and Rust, saved only after explicit user
  choice, and never uploaded automatically; telemetry and crash SDKs remain
  forbidden by a locked-source gate.
- Cache and artifact consumers fail closed on stale source identities,
  incomplete lock/toolchain keys, forbidden cached conclusions, substituted
  payloads, or missing attestations.
- The SignPath Foundation application is submitted and pending. Prerelease assets
  remain explicitly unsigned prereleases until trusted signing is integrated.

## [1.0.0] - 2026-07-08

Stage 5 integrated v1 release.

### Added

- A complete personal A-share desk joining cached market charts, TDX-compatible
  formulas, reproducible backtests, evidence-linked multi-agent research, and a
  safe task center in one low-code interface.
- Source-free Windows x86_64 and macOS x86_64/arm64 installers with clean-runner
  first-launch, persistence, upgrade, shutdown, and uninstall verification.
- A fixed, network-forbidden performance workload and 2/3/5-second chart,
  formula-preview, and single-stock-backtest release budgets.
- Release-candidate backup, restore, rollback, migration, secret-redaction,
  formula-sandbox, dependency, container, and public-history checks.
- A concise bilingual README and screenshot-complete bilingual GitHub Wiki.

### Changed

- Native Windows now stores canonical OHLCV payloads transactionally in the
  private SQLite catalog and revalidates dataset and routing identities on
  every read; POSIX deployments retain the immutable Parquet market lake.
- The responsive application shell now covers wide desktop, narrow desktop,
  tablet portrait/landscape, short landscape, and 200% effective zoom. Narrow
  layouts automatically collapse to accessible icons and remain manually
  expandable without component overlap.
- AKShare research now applies the auditable
  `akshare-research-projection-v1` contract: the latest 24 fundamental report
  periods, a 366-day/256-item announcement boundary, and at most 100 news
  items, while preserving required identity, date, and URL provenance and
  retaining the existing global payload and table guards.
- Cache-only market research now applies the versioned
  `market-research-projection-v1` contract, retaining the largest recent bar
  suffix within a 60 KiB canonical-section budget and rejecting oversized
  stage artifacts before SQL persistence.
- Public documentation, packaging, architecture, support, security, and release
  evidence now describe the complete v1 product instead of an intermediate
  capability stage.

### Security

- Release publication fails closed on incomplete requirements, local/internal
  paths, credentials, private-key material, unsafe README commands, stale
  artifacts, or unverified native installer provenance.
- Provider secrets remain encrypted and masked, external research text remains
  untrusted data, and formulas remain constrained to the parsed compatibility
  language rather than arbitrary code execution.

## [0.5.0] - 2026-07-08

Stage 4 evidence-linked multi-agent research release.

### Added

- Low-code DeepSeek-oriented, generic OpenAI-compatible, and local Ollama model configuration with immutable successors, connection verification, masked credentials, and per-run frozen model settings.
- Cache-only A-share research snapshots with category-specific Tushare-to-AKShare fallback, explicit permission and missing-data diagnostics, and no silent provider merging.
- Durable nine-stage analysis runs covering market, fundamentals, announcements, news, technical and fundamental/news analysis, bull/bear review, and final risk decision, with immutable history, cancellation, bounded retries, partial reports, and linked child runs for failed-stage retries.
- Evidence-linked five-level research reports with confidence, source and timing metadata, insufficient-evidence rating suppression, persistent history, and a responsive conclusion/process/evidence workspace.
- Deterministic API, Worker, security, and browser acceptance coverage for complete, partial, insufficient-evidence, retry, cancellation, and evidence-navigation flows.

### Security

- Model API keys are encrypted at rest and remain masked across HTTP, logs, errors, and diagnostics; model URLs and resolved remote endpoints are constrained by provider-specific SSRF protections.
- External announcements and news remain bounded untrusted data, analysis APIs exclude formula/backtest/trading inputs, and reports remain research-only without targets, position sizing, or order actions.

## [0.4.0] - 2026-07-07

Stage 3 reproducible A-share backtesting release.

### Added

- Five-step low-code backtest configuration for saved MACD/custom trading formulas, single stocks or frozen pools, daily/weekly/60-minute periods, adjustment, sizing, commissions, tax, and slippage.
- Durable asynchronous pool execution with A-share next-open, T+1, suspension, historical side-specific price-limit, pending/cancellation, partial-failure, and expired-lease recovery semantics.
- Conclusion-first reports with exact realized win-rate definitions, reliability, return distribution, grouped samples, open positions, gross-to-net cost disclosure, failures, logs, deterministic exports, and run-pinned K-line/formula replay.
- Responsive application navigation that automatically condenses to an accessible SVG icon rail on narrow screens, remains manually expandable, and keeps core workspaces free of accidental overlap across desktop and tablet ratios.
- Dedicated semantic acceptance, ten-year single-stock performance, real browser, and packaged API/Worker release gates.

### Security

- Backtest requests, cursors, stored results, exports, and replay responses use bounded strict contracts; run/symbol/trade, formula/SignalSeries, and signal/execution/status manifest identities are cross-checked and fail closed without latest-data fallback.

## [0.3.0] - 2026-07-07

Stage 2 formula-system and Formula Studio release.

### Added

- A documented, versioned TDX-compatible formula subset with a constrained parser, typed compiler, deterministic vector evaluator, future/repainting detection, and bounded isolated preview workers.
- Immutable formula versions, revision-checked drafts and copies, built-in MACD golden-cross/dead-cross signals, and one computation contract shared by direct preview and market charts.
- A desktop-first, tablet-responsive three-column Formula Studio with searchable functions/templates, Monaco assistance, line diagnostics, low-code parameters, explicit preview, K-line/subchart output, and BUY/SELL markers.
- Stage 2 API, acceptance, ten-year preview performance, and real-browser release gates while preserving every Stage 1 gate.

### Security

- Formula text is parsed rather than executed as arbitrary code; unsupported, future-data, repainting, oversized, timed-out, and incompatible-version requests fail closed with bounded public diagnostics.

## [0.2.0] - 2026-07-06

Stage 1 market-data and charting release.

### Added

- Tushare, AKShare, BaoStock, and local TDX adapters with strict normalized contracts, period-specific routing, safe fallback attempts, and source diagnostics.
- Durable instrument/catalog and per-symbol bar updates, cancellation, item results, Asia/Shanghai daily scheduling, and a local immutable Parquet/DuckDB market lake.
- Full-A, current major-index, discovered industry, and editable custom pools with provenance-pinned snapshots.
- Cache-only daily, weekly, and 60-minute K-line/volume charts for none/qfq/hfq adjustment, with crosshair, zoom, drag, and provenance views.
- Bilingual setup/operation guidance plus deterministic backend, performance, and real API/worker browser acceptance coverage.

### Security

- Write-only encrypted Tushare configuration, descriptor-safe local TDX access, process-wide delayed-log redaction, frozen database/lake identities, and loopback-only Compose publishing by default.
- Provider SDKs are installed from the locked `providers` extra; unavailable configuration remains an explicit typed routing failure and secrets/paths are excluded from task results and logs.

## [0.1.0] - 2026-07-05

Stage 0 foundation release.

### Added

- Stage 0 FastAPI health and durable-task APIs with a SQLite-backed worker.
- Alembic migrations with safe local initialization and packaged migration assets.
- React workspace shell for market, formulas, backtests, analysis, tasks, and settings routes, with live local health and recent-task status while planned capabilities remain labeled as previews.
- Encrypted local secret storage and configurable structured-log redaction utilities.
- Native development supervision plus multi-stage Docker and Compose packaging.
- Public governance, contribution, security, support, architecture, CI, CodeQL, and release automation foundations.

### Security

- Runtime containers use a non-root application identity after data-directory initialization.
- GitHub Actions dependencies are pinned to immutable, verified commit SHAs.
