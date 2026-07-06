# Architecture

## Scope and shape

Stock Desk Stage 1 is a local-first modular monolith. One Python package owns the HTTP API, migrations, task persistence, provider routing, immutable market storage, worker logic, configuration, and security utilities; a separate React application provides the browser workspace. Docker assembles both into one immutable API image, while Compose runs separate API and worker processes over one local SQLite/market-lake volume.

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
- `stock_desk.storage` owns SQLAlchemy engine behavior and Alembic migration coordination.
- `stock_desk.security` provides Fernet-backed local secret storage and delayed/queued log-safe redaction.
- `stock_desk.web` serves the compiled React application in the container deployment.
- `web` is the terminal-style workspace. Market data and source settings are functional; formulas, backtests, and analysis remain planned previews.

The API and worker share schema and repository code but run as separate processes. SQLite is the durable coordination boundary: tasks are persisted before execution and state transitions are transactional. This keeps the foundation deployable without introducing a network queue.

## Planned boundaries

Later stages are expected to add formula parsing/execution, backtest engines, and model-assisted analysis. These should depend on explicit application interfaces rather than provider SDKs leaking into API or UI contracts. Formula output, backtest results, and model responses must retain provenance and reproducibility metadata.

Those modules are plans, not present capabilities. Stage 1 does not imply real-time data, formula compatibility, backtest correctness, trading, or model integration.

## Deployment

Native development runs uvicorn, the task worker, and Vite as supervised child processes. The container build compiles the web application, installs the locked Python runtime, packages migrations, and runs application processes as a non-root identity after the entrypoint prepares the mounted data directory. Compose exposes port 8000 and gives API and worker the same `./data` mount.

The runtime image is intended to be immutable except for `/app/data`. SQLite database files and encrypted secret-store files belong in that writable boundary; source, dependencies, and web assets remain read-only at runtime.

## Trust and security boundaries

Stage 1 assumes one trusted local operator and a trusted host. It has no authentication, authorization, multi-user isolation, or TLS. The browser, API, worker, `.env`, master key, SQLite volume, and market lake are within the local trust boundary; market providers are external, untrusted inputs.

`STOCK_DESK_MASTER_KEY` must be supplied outside source control. Secret values are encrypted before local persistence. Standard logging dispatch is sanitized while provider/settings secret leases are active; SDKs that bypass Python logging remain outside that boundary. Encryption cannot protect a compromised host that also has the key. Back up keys separately from encrypted data and never include either in diagnostics.

Do not expose the Stage 1 service directly to an untrusted network. A future shared or remote deployment would require authentication, authorization, TLS termination, request limits, audit policy, database changes, and a revised threat model.
