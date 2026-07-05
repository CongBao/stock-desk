# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
