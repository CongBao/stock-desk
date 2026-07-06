# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
