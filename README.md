[简体中文](README.zh-CN.md)

# Stock Desk

Stock Desk `v0.5.0` is a local-first A-share market, formula, backtest, and evidence-linked research workspace. Stage 1 provides configurable sources and daily, weekly, and 60-minute charts; Stage 2 adds constrained TDX-compatible formulas; Stage 3 adds reproducible A-share backtests; Stage 4 adds on-demand multi-agent research through DeepSeek-oriented, OpenAI-compatible, or local Ollama models.

Reports bind claims to frozen source evidence and suppress ratings when critical evidence is missing. They are research aids, not investment advice. See the [roadmap](ROADMAP.md).

## Quick start

Native requirements are Python `>=3.12,<3.13`, [uv](https://docs.astral.sh/uv/), Node.js 22 or 24 LTS, and pnpm 11:

```bash
make bootstrap
make dev
```

Open [http://localhost:5173/market](http://localhost:5173/market). `make dev` supervises the API, market worker, and Vite; stop them with `Ctrl-C`.

Docker Compose installs the same locked provider extras and binds the service to loopback:

```bash
docker compose up --build --wait
# open http://localhost:8000/market
docker compose down --volumes --remove-orphans
```

The API health endpoint is [http://localhost:8000/api/health](http://localhost:8000/api/health), and its interactive documentation is at [http://localhost:8000/docs](http://localhost:8000/docs). Persistent native/container data lives under `data/`; API and worker must share the same database and market-lake paths.

The Stage 0 foundation remains available: `/market`, `/formulas`, `/backtests`, `/analysis`, `/tasks`, and `/settings` share one shell, and the `demo.double` durable task remains useful for worker diagnostics. Stages 1–4 complete market data, formulas, backtests, and intelligent analysis respectively.

The shared shell adapts to wide desktop, narrow desktop, and tablet ratios. Its left navigation automatically condenses to labeled SVG icons on narrow screens and can always be expanded or collapsed with the pointer or keyboard; core controls reflow rather than overlapping.

## Configure data sources

Copy the example environment and generate a Fernet master key before saving a Tushare token:

```bash
cp .env.example .env
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Save the output as `STOCK_DESK_MASTER_KEY` in `.env`, then open `/settings`:

- Tushare tokens are write-only and encrypted locally; the browser receives only masked status.
- A TDX path must be an absolute path to the TDX installation containing `vipdoc`. Local TDX supplies supported daily files, not instruments or calendars. With Compose, set `STOCK_DESK_TDX_HOST_PATH` to the host directory and enter `/app/tdx` in Settings; both containers receive the same read-only mount.
- Priorities are independent for daily, weekly, 60-minute, instruments, and calendars. Missing credentials/SDKs remain visible as typed routing failures.
- Eastmoney is reserved in settings but has no Stage 1 runtime adapter; it is never presented as working.

Tushare, AKShare, and BaoStock depend on their upstream services, permissions, network availability, and licensing. Review each provider's terms; do not redistribute data unless permitted.

## Use the market workspace

1. In `/market`, choose **Update instrument catalog** on a fresh installation. A successful catalog refresh publishes Full-A; current major-index and discovered industry compositions are refreshed independently through AKShare and preserve their last valid snapshots on partial failure.
2. Search a symbol or open a preset/custom pool. Custom pools support low-code search, add, reorder, rename, remove, and delete operations.
3. Select period, adjustment, date range, and a symbol or frozen pool scope, then explicitly start an update. Progress, cancellation, and per-symbol success/failure/cancelled results are durable.
4. Configure the single daily Asia/Shanghai schedule if desired. Its symbol list is a snapshot; later pool edits do not silently change it.
5. Inspect chart provenance, provider route, cutoff, and fallback attempts.

Chart browsing is cache-only. A cache miss shows guidance and never triggers a silent external fetch. One requested series is selected from one provider; Stock Desk does not splice provider series together.

## Use Formula Studio

1. Open `/formulas` and choose the built-in MACD template, paste a supported TDX formula, or insert fields and functions from the searchable library.
2. Edit named parameters through forms, use Monaco completion and function help, then run validation. Unsupported or future/repainting behavior is located and blocks save and preview.
3. Save an immutable version before running an explicit preview. The right column keeps K-line as the main chart and renders formula output in its declared main overlay or independent subchart with BUY/SELL markers.
4. Save later revisions as new versions, inspect read-only history, or copy a version into an independent formula. Chart and direct preview use the same engine and provenance-pinned snapshot.

The supported subset and runtime semantics are published in the [formula compatibility reference](docs/formula-compatibility.md). Condition-screening formulas, color-K formulas, and AI formula generation/explanation/repair are intentionally absent.

## Run historical backtests

1. Save a valid trading-system formula, then open `/backtests` or use **Backtest current stock** from the market workspace. The five-step wizard selects the exact formula version, single stock or frozen pool, daily/weekly/60-minute period, half-open dates, adjustment, fixed-lot size, commissions, tax, and slippage.
2. Review preflight coverage and the close-confirm/next-open rules before submitting. Pool jobs run asynchronously, expose durable progress/logs, support cancellation, and retain partial results.
3. Read the conclusion-first report: realized win rate and denominator, net-return statistics, reliability, distribution, grouped samples, open positions, failures, and exact gross-to-net costs. Pool results are independent per-stock trade samples, not portfolio returns.
4. Reopen a trade against the run-pinned market/status manifests and SignalSeries. K-line remains the main chart, formula output the subchart, and weekly fills disclose their exact daily execution evidence. JSON/CSV exports retain reproducibility metadata.

Execution semantics, metric definitions, and limitations are documented in [backtesting semantics](docs/backtesting-semantics.md). Stock Desk does not place orders or connect to brokers.

## Run intelligent analysis

1. Open `/analysis`, create a DeepSeek-oriented, OpenAI-compatible, or Ollama configuration, and pass its connection test. API keys are encrypted locally and only a mask returns to the browser.
2. Enter one A-share symbol and run the four-category preflight. Market data is cache-only; fundamentals, announcements, and news show their actual route, fallback, permission gaps, and cutoff.
3. Start the asynchronous nine-stage run. Technical and fundamental/news analysis run first, bull and bear review follow, and risk decision produces a five-level rating only when critical evidence is sufficient.
4. Select a conclusion to inspect its source and times. Partial reports retain successful work; retrying a failed stage creates a linked child run without overwriting history.

## Current scope and safety

Stage 4 includes the market workspace, technical/trading formulas, reproducible historical backtests, and evidence-linked LLM research. It does not include real-time quotes, a dynamic screener, drawing tools, shared-capital portfolio simulation, broker connectivity, live/automatic trading, target prices, position sizing, or personalized advice.

This is a trusted, single-user local service without authentication, authorization, or TLS. Keep it on loopback, do not commit `.env`, tokens, the master key, local TDX paths, databases, or downloaded market data, and never paste them into issues. See [data-source details](docs/data-sources.md), [security](SECURITY.md), and [architecture](docs/architecture.md).

## Quality gates

```bash
make test
make acceptance
make acceptance-formula
make acceptance-backtest
make benchmark
make benchmark-formula
make benchmark-backtest
make lint
make typecheck
make build
make public-tree
make security
```

After installing Chromium with `pnpm exec playwright install chromium`, run `make e2e-market`, `make e2e-formula`, `make e2e-backtest`, `make e2e-analysis`, and `make e2e-task-center` for the real Stage 1–5 browser flows. `make security` requires network access: it checks Python dependencies with OSV and JavaScript production dependencies through the npm registry after verifying that manifests match their lockfiles. With Docker running, `make release-check` runs all browser slices, security, and an isolated container smoke gate; it starts and cleans up its own Compose stack. The project is licensed under Apache-2.0. Stock Desk is research software, not investment advice; verify data and decisions independently.
