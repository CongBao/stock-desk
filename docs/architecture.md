# Architecture

Stock Desk is a local-first modular monolith. One Python package owns the HTTP
API, migrations, durable tasks, market storage, formula and backtest engines,
analysis workflow, configuration, and security utilities. A separate React
application provides the browser workspace.

## Deployment model

Native development supervises three processes: FastAPI on port 8000, the durable
task worker, and Vite on port 5173. The container build compiles the web
application into the API image; Compose runs API and worker processes against the
same local data mount and publishes the API only on loopback by default.

```text
Browser
  ├─ native: Vite :5173
  └─ container: API :8000
              │
              v
        FastAPI + web assets
              │
        SQLite task store <──── worker
              │
              v
   database, market lake, and local secrets
```

SQLite is the durable coordination boundary. Tasks are committed before workers
claim them, and state transitions are transactional. A network queue is not
required for the supported single-host deployment.

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
- `web` owns the responsive market, formula, backtest, analysis, tasks, and
  settings workspaces; `stock_desk.web` serves its compiled assets.

Analysis cannot submit formulas, backtests, broker actions, target prices, or
position sizes. Backtest pool output is a set of independent per-symbol trade
samples, not a shared-capital portfolio. Market chart reads are cache-only and do
not silently fetch or splice providers.

## Data and storage

The API and worker must resolve the same database and data directory. The
writable boundary contains the SQLite database, encrypted secret rows, immutable
market objects, routing manifests, task history, reports, and operator-created
exports. Source code, dependencies, and compiled web assets remain read-only in a
container deployment.

Each backtest freezes its formula version, normalized parameters, scope, period,
adjustment, half-open dates, costs, and signal/execution/status manifests. Each
analysis freezes market, fundamental, announcement, news, prompt, role, and model
evidence before execution. Public replay and evidence views validate those
identities and do not fall back to latest data.

Portable backup uses SQLite's backup API plus referenced immutable market
objects; it excludes the master key, `.env`, external TDX inputs, and unreferenced
files. Restore validates and stages owned components before atomic replacement.
See [backup and restore](backup-and-restore.md).

## Trust and security

The supported threat model is one trusted operator on one trusted host. Stock
Desk has no authentication, authorization, multi-user isolation, or TLS. Keep it
on loopback; a remote or shared deployment needs a different security design.

The browser, API, worker, `.env`, master key, database, and market lake are inside
the local trust boundary. Market/model providers, provider responses, external
research text, archives, and pasted formulas are untrusted. Inputs are bounded,
formula execution is constrained, model endpoints are validated, and mixed or
corrupt provenance fails closed.

Secrets are encrypted before local persistence and masked across HTTP and normal
diagnostics. Encryption does not protect a host compromised together with its
master key. Store `STOCK_DESK_MASTER_KEY` outside source control and back it up
separately from encrypted data. Never include secrets, licensed data, databases,
or backups in issues. See [configuration](configuration.md),
[troubleshooting](troubleshooting.md), and [SECURITY.md](../SECURITY.md).
