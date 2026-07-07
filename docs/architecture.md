# Architecture

## Scope and shape

Stock Desk Stage 3 is a local-first modular monolith. One Python package owns the HTTP API, migrations, task persistence, provider routing, immutable market storage, formula engine, backtest engine, worker logic, configuration, and security utilities; a separate React application provides the browser workspace. Docker assembles both into one immutable API image, while Compose runs separate API and worker processes over one local SQLite/market-lake volume.

```text
Browser
  ├─ native: Vite :5173 ───────┐
  └─ container: API :8000      │
                               v
FastAPI /api + packaged web assets
              │
              v
        SQLite task store <──── task worker
              │
              v
        local data directory
```

## Current modules

- `stock_desk.api` defines health, market, settings, schedule, and durable-task HTTP contracts.
- `stock_desk.tasks` owns task states, repository transitions, claiming, cancellation, and the worker. Production also registers catalog and market-update handlers.
- `stock_desk.market` owns canonical provider contracts, per-period routing, provenance, instruments, pools, schedules, updates, and immutable local market data.
- `stock_desk.formula` owns the constrained TDX-compatible parser/compiler, deterministic evaluator, future/repainting checks, immutable formula versions, bounded isolated preview execution, and shared signal-series contract.
- `stock_desk.backtest` owns immutable run snapshots, A-share execution constraints, costs and trade lifecycles, asynchronous single/pool execution, independent-sample metrics, deterministic exports, and pinned trade replay.
- `stock_desk.storage` owns SQLAlchemy engine behavior and Alembic migration coordination.
- `stock_desk.security` provides Fernet-backed local secret storage and delayed/queued log-safe redaction.
- `stock_desk.web` serves the compiled React application in the container deployment.
- `web` is the terminal-style workspace. Market data, source settings, Formula Studio, the five-step backtest wizard, progress, conclusion-first reports, and pinned K-line/formula replay are functional; intelligent analysis remains a planned preview.

The shared Web shell owns responsive navigation rather than duplicating it in feature pages. It uses a full rail on wide screens and a keyboard-operable semantic SVG icon rail at narrow desktop/tablet ratios; feature layouts retain their own bounded reflow rules so navigation, charts, forms, tables, and primary actions do not accidentally overlap.

The API and worker share schema and repository code but run as separate processes. SQLite is the durable coordination boundary: tasks are persisted before execution and state transitions are transactional. This keeps the foundation deployable without introducing a network queue.

## Backtest boundary

Every run freezes its formula/version, normalized parameters, instrument and pool identity, signal/execution/status manifests, period, adjustment, half-open dates, warm-up policy, lot size, costs, and rule versions. The worker reopens only those pins, persists per-symbol checkpoints and exact SignalSeries identities, and computes trades independently per stock. Pool output therefore describes independent trade samples—not a shared-capital portfolio—and deliberately has no equity curve.

Signals are confirmed at the selected period close and attempted at the next eligible open. Weekly signals use pinned daily companion execution data. T+1, suspension, historical side-specific limits, pending/cancel behavior, costs, open positions, failures, and incomplete data are explicit auditable events. Public replay is bound to run/symbol/trade identities and never accepts arbitrary manifest identifiers or falls back to latest data.

## Planned boundaries

Later stages are expected to add model-assisted analysis through explicit application interfaces. Stage 3 does not imply real-time data, shared-capital portfolio simulation, broker integration, live/automatic trading, or model-generated investment decisions.

## Deployment

Native development runs uvicorn, the task worker, and Vite as supervised child processes. The container build compiles the web application, installs the locked Python runtime, packages migrations, and runs application processes as a non-root identity after the entrypoint prepares the mounted data directory. Compose exposes port 8000 and gives API and worker the same `./data` mount.

The runtime image is intended to be immutable except for `/app/data`. SQLite database files and encrypted secret-store files belong in that writable boundary; source, dependencies, and web assets remain read-only at runtime.

## Trust and security boundaries

Stage 3 assumes one trusted local operator and a trusted host. It has no authentication, authorization, multi-user isolation, or TLS. The browser, API, worker, `.env`, master key, SQLite volume, and market lake are within the local trust boundary; market providers and pasted formula text are untrusted inputs. Formula text is parsed against bounded syntax and a versioned function allowlist, then evaluated in a hard-deadline process without file, network, or system-call language capabilities. Backtest requests, cursors, stored payloads, and replay identities are strictly bounded and fail closed on mixed or corrupt provenance.

`STOCK_DESK_MASTER_KEY` must be supplied outside source control. Secret values are encrypted before local persistence. Standard logging dispatch is sanitized while provider/settings secret leases are active; SDKs that bypass Python logging remain outside that boundary. Encryption cannot protect a compromised host that also has the key. Back up keys separately from encrypted data and never include either in diagnostics.

Do not expose the Stage 3 service directly to an untrusted network. A future shared or remote deployment would require authentication, authorization, TLS termination, request limits, audit policy, database changes, and a revised threat model.
