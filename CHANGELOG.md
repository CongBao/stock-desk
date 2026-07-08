# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- A public documentation contract covering required pages and sections,
  reciprocal English/Chinese entry points, local links, supported commands,
  documented settings, public-tree boundaries, and external Wiki readiness.

### Changed

- The English and Chinese entry points now provide concise, equivalent setup,
  workflow, safety, and documentation routes.
- Public architecture, configuration, troubleshooting, support, contribution,
  roadmap, and research-risk guidance are aligned with the current `v0.5.0`
  release and unreleased maintenance tooling without declaring a later release.

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
