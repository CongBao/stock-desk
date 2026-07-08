# Architecture

Stock Desk is a local-first modular monolith: one Python package owns the HTTP
API, migrations, durable tasks, market storage, formula engine, backtest engine,
analysis workflow, configuration, and security utilities; React supplies the
browser workspace. Process and configuration topology differ by deployment.

## Deployment model

### Native installer topology

The source-free Windows executable and macOS application contain Python, locked
runtime dependencies, migrations, provider adapters, and compiled web assets.
One frozen parent launcher owns the application lifecycle:

```text
frozen parent launcher
  ├─ generated private settings and single-instance lock
  ├─ API child ── random 127.0.0.1 port ── browser
  └─ worker child
           │
           v
  per-user SQLite database and market lake
```

Before spawning children, the parent creates the OS-private data tree, generates
or loads `config/master.key`, migrates SQLite, and reserves the listening socket
without a port-selection race. It then uses multiprocessing `spawn` for one API
child and one worker child. Both receive the same in-memory settings payload and
write to the same log. The parent waits for worker readiness and API health,
records the selected random port, opens the browser, and coordinates shutdown.

Native state lives at `%LOCALAPPDATA%\stock-desk` on Windows and
`~/Library/Application Support/stock-desk` on macOS. It does not use the source
development `.env` contract.

### Source development topology

`make dev` runs a source supervisor that starts FastAPI on port 8000, the durable
worker, and Vite on port 5173. The three children share settings from environment
variables or `.env`, including an operator-provided `STOCK_DESK_MASTER_KEY`,
database URL, and data directory.

### Container topology

The runtime image includes the Python application, migrations, providers, and
compiled web assets. Compose starts separate API and worker containers over the
same `/app/data` mount and settings. Host port 8000 is bound to loopback by
default. The runtime image is immutable except for the mounted data boundary and
does not include source-tree operator scripts.

Across all profiles, SQLite is the durable coordination boundary. Tasks are
committed before workers claim them, and state transitions are transactional; a
network queue is not required for the supported single-host deployment.

## Modules and boundaries

- `stock_desk.api` exposes bounded health, settings, market, formula, backtest,
  analysis, schedule, and task contracts.
- `stock_desk.tasks` owns durable states, events, claiming, leases,
  cancellation, and worker dispatch.
- `stock_desk.market` owns provider routing, provenance, instruments, pools,
  schedules, updates, and immutable local market objects.
- `stock_desk.formula` parses and evaluates a versioned, constrained
  TDX-compatible subset. Formula text is data, never Python or shell code.
- `stock_desk.backtest` freezes formula/data/rule identities, applies explicit
  A-share execution and cost rules, and produces deterministic reports, exports,
  and replay references.
- `stock_desk.analysis` freezes evidence and model settings before a bounded
  multi-role workflow. Missing critical evidence suppresses the rating.
- `stock_desk.storage` owns SQLAlchemy and Alembic coordination;
  `stock_desk.security` owns encrypted local secrets and log redaction.
- `web` owns the responsive market, Formula Studio, backtest, analysis, tasks,
  and settings workspaces; `stock_desk.web` serves compiled assets.

Analysis cannot submit formulas, backtests, broker actions, target prices, or
position sizes. Backtest pool output is independent per-symbol trade samples,
not a shared-capital portfolio. Market chart reads are cache-only and do not
silently fetch or splice providers.

## Data and storage

API and worker must resolve one database and data directory. The writable
boundary contains SQLite, encrypted provider credentials, immutable market
objects, routing manifests, task history, reports, and exports. Code,
dependencies, and compiled assets remain read-only in packaged deployments.

Each backtest freezes its formula version, parameters, scope, period, adjustment,
dates, costs, and signal/execution/status manifests. Each analysis freezes
market, fundamental, announcement, news, prompt, role, and model evidence.
Replay and evidence views validate those identities and never fall back to
latest data.

The source-tree backup tool uses SQLite's backup API plus referenced immutable
market objects and excludes the master key, `.env`, external TDX inputs, and
unreferenced files. It is not bundled in frozen native installers or the runtime
container image. See [backup and restore](backup-and-restore.md).

## Trust and security

The supported threat model is one trusted operator on one trusted host. Stock
Desk has no authentication, authorization, multi-user isolation, or TLS. Keep it
on loopback; a remote or shared deployment needs a different security design.

The browser, API, worker, configuration, master key, database, and market lake
are inside the local trust boundary. Market/model providers, provider responses,
external research text, archives, and pasted formulas are untrusted. Inputs are
bounded, formula execution is constrained, model endpoints are validated,
external text is treated as potential prompt injection, and mixed or corrupt
provenance fails closed.

Native installers generate and restrict a per-user key. Source and container
operators provide `STOCK_DESK_MASTER_KEY` outside source control. In either case,
encryption does not protect a host compromised together with its key. Never put
secrets, licensed data, databases, or backups in issues. See
[configuration](configuration.md), [troubleshooting](troubleshooting.md), and
[SECURITY.md](../SECURITY.md).
