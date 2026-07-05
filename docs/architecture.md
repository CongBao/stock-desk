# Architecture

## Scope and shape

Stock Desk Stage 0 is a local-first modular monolith. One Python package owns the HTTP API, migrations, task persistence, worker logic, configuration, and security utilities; a separate React application provides the browser shell. Docker assembles both into one immutable API image, while Compose runs separate API and worker processes over one local SQLite volume.

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

- `stock_desk.api` defines health and durable-task HTTP contracts.
- `stock_desk.tasks` owns task states, repository transitions, claiming, cancellation, and the worker. Only `demo.double` is registered as an executable demonstration.
- `stock_desk.storage` owns SQLAlchemy engine behavior and Alembic migration coordination.
- `stock_desk.security` provides Fernet-backed local secret storage and log-safe redaction helpers. No UI writes credentials in Stage 0.
- `stock_desk.web` serves the compiled React application in the container deployment.
- `web` is the terminal-style workspace shell. Market is a static layout preview; formulas, backtests, analysis, tasks, and settings are planned/placeholder product pages.

The API and worker share schema and repository code but run as separate processes. SQLite is the durable coordination boundary: tasks are persisted before execution and state transitions are transactional. This keeps the foundation deployable without introducing a network queue.

## Planned boundaries

Later stages are expected to add market-provider adapters, normalized market storage, formula parsing/execution, backtest engines, and model-assisted analysis. These should depend on explicit application interfaces rather than provider SDKs leaking into API or UI contracts. Provider data, formula output, backtest results, and model responses must retain provenance and reproducibility metadata.

Those modules are plans, not present capabilities. The Stage 0 route shell does not imply live data, formula compatibility, backtest correctness, or model integration.

## Deployment

Native development runs uvicorn, the task worker, and Vite as supervised child processes. The container build compiles the web application, installs the locked Python runtime, packages migrations, and runs application processes as a non-root identity after the entrypoint prepares the mounted data directory. Compose exposes port 8000 and gives API and worker the same `./data` mount.

The runtime image is intended to be immutable except for `/app/data`. SQLite database files and encrypted secret-store files belong in that writable boundary; source, dependencies, and web assets remain read-only at runtime.

## Trust and security boundaries

Stage 0 assumes one trusted local operator and a trusted host. It has no authentication, authorization, multi-user isolation, or TLS. The browser, API, worker, `.env`, master key, and SQLite volume are within the local trust boundary; market providers and model providers will be external, untrusted inputs when implemented.

`STOCK_DESK_MASTER_KEY` must be supplied outside source control. Secret values are encrypted before local persistence. Redacting filters and formatters are available for logging handlers that explicitly configure them; they are not installed globally in Stage 0. Encryption cannot protect a compromised host that also has the key. Back up keys separately from encrypted data and never include either in diagnostics.

Do not expose the Stage 0 service directly to an untrusted network. A future shared or remote deployment would require authentication, authorization, TLS termination, request limits, audit policy, database changes, and a revised threat model.
