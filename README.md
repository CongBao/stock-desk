[简体中文](README.zh-CN.md)

# Stock Desk

Stock Desk `v0.2.0` is a local-first A-share market workspace. Stage 1 provides configurable Tushare, AKShare, BaoStock, and local TDX adapters; durable catalog and bar updates; local Parquet/DuckDB-backed chart reads; provenance; preset and custom stock pools; daily schedules; and interactive daily, weekly, and 60-minute K-line/volume charts.

Formula execution, backtesting, and LLM-assisted analysis are planned later stages. Their navigation entries are previews, not completed capabilities. See the [roadmap](ROADMAP.md).

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

The Stage 0 foundation remains available: the `/market`, `/formulas`, `/backtests`, `/analysis`, `/tasks`, and `/settings` workspace routes share one shell, and the `demo.double` durable task remains useful for worker diagnostics. Stage 1 completes the market-data route; the other research routes are still previews.

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

## Current scope and safety

Stage 1 includes instruments, Full-A/major-index/industry/custom pools, manual and daily bar updates, provenance, and cached daily/weekly/60-minute charts with none/qfq/hfq adjustment. It does not include real-time quotes, a dynamic screener, drawing tools, formulas, backtests, portfolios, trading, or LLM analysis.

This is a trusted, single-user local service without authentication, authorization, or TLS. Keep it on loopback, do not commit `.env`, tokens, the master key, local TDX paths, databases, or downloaded market data, and never paste them into issues. See [data-source details](docs/data-sources.md), [security](SECURITY.md), and [architecture](docs/architecture.md).

## Quality gates

```bash
make test
make acceptance
make benchmark
make lint
make typecheck
make build
make public-tree
make security
```

After installing Chromium with `pnpm exec playwright install chromium`, run `make e2e-market` for the real API/worker Stage 1 browser flow. `make security` requires network access: it checks Python dependencies with OSV and JavaScript production dependencies through the npm registry after verifying that manifests match their lockfiles. With Docker running, `make release-check` runs both browser slices, security, and an isolated container smoke gate; it starts and cleans up its own Compose stack. The project is licensed under Apache-2.0. Stock Desk is research software, not investment advice; verify data and decisions independently.
