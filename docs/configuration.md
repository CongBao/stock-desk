# Configuration

Stock Desk has three deployment profiles. Native installers own their private
per-user configuration automatically. Source development and Compose read
`STOCK_DESK_` environment variables and an optional `.env`; environment values
override file values. Never commit `.env`, keys, credentials, data, or backups.

## Native installers

Native applications require no Python, Node.js, source checkout, Docker, or
operator-managed `.env`. Their mutable data is always separate from installed
program files, but the exact directory and launch topology are version-specific.

### v1.1 Windows desktop

The source-free v1.1 Windows x64 application stores all mutable state under
`%LOCALAPPDATA%\Stock Desk\v1.1`. On first launch, the Tauri host creates that
private per-user tree and generates a Fernet key at `config/master.key` within
it. The host loads bundled React assets in WebView2, starts the bundled API and
Worker sidecar on a random loopback port, and does not open or require an
external browser.

The legacy v1 tree is deliberately outside the v1.1 ownership boundary. v1.1
does not read, migrate, modify, or delete `%LOCALAPPDATA%\stock-desk`.

### Version-specific native paths

| Version and platform | Per-user data directory |
| --- | --- |
| Windows v1.1 | `%LOCALAPPDATA%\Stock Desk\v1.1` |
| Windows v1.0 | `%LOCALAPPDATA%\stock-desk` |
| macOS v1.0 | `~/Library/Application Support/stock-desk` |

### Historical v1.0 installers

The stable v1.0 Windows and macOS installers use the historical paths in the
table above and open the local workspace in an external browser.

The data directory contains `stock-desk.db`, market objects, `config/master.key`,
`logs/stock-desk.log`, and runtime coordination under `runtime/`. POSIX modes or
Windows ACLs restrict the directories and key to the current user. Packaged
launchers pass the generated key, database, data, and bundled web paths directly
to their API and Worker; users do not set `STOCK_DESK_MASTER_KEY` for a native
profile.

Each launch reserves a random available port on `127.0.0.1`; the port is
intentionally not 8000 and may change on the next launch. One desktop instance
is allowed per user. In v1.1 the loopback endpoint is private sidecar plumbing
behind the Tauri host proxy, not a browser entry point.

Protect the whole directory for the exact installed version and back up
`config/master.key` separately from encrypted data. For v1.1 that means
`%LOCALAPPDATA%\Stock Desk\v1.1`, not the lowercase v1 path. Losing the key makes
saved provider ciphertext unreadable. Native installers do not bundle the
source-tree backup/restore operator CLI. The complete workflow is unsupported
on native Windows filesystems in this release unless a later release adds and
verifies a frozen native command. See [backup and restore](backup-and-restore.md).

## Source development

Source development requires Python `>=3.12,<3.13`, uv, Node.js, and pnpm. Install
locked dependencies, copy the sample file, and generate a Fernet key:

```bash
make bootstrap
cp .env.example .env
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the result as `STOCK_DESK_MASTER_KEY`, then run `make dev`. The supervisor
starts FastAPI on port 8000, the task worker, and Vite on port 5173. All children
must use the same database and data directory.

## Container deployment

Compose runs separate API and worker containers over the same `./data` mount and
publishes port 8000 only on host loopback:

```bash
docker compose up --build --wait
docker compose down --volumes --remove-orphans
```

Provide `STOCK_DESK_MASTER_KEY` through the local environment or uncommitted
`.env`; both containers must receive the same value. `STOCK_DESK_UID` and
`STOCK_DESK_GID` override the non-root runtime identity and must be nonzero.
`STOCK_DESK_IMAGE` supplies a distinct local image name.

`STOCK_DESK_TDX_HOST_PATH` mounts a host TDX directory read-only at `/app/tdx`;
enter `/app/tdx` in Settings. The host directory must contain `vipdoc`. Keep port
8000 private and use a trusted tunnel for remote access; the service has no
authentication or TLS.

## Application settings

These variables configure source and container deployments. Native installers
derive equivalent values from their private per-user directory instead.

| Variable | Default | Purpose |
| --- | --- | --- |
| `STOCK_DESK_APP_NAME` | `stock-desk` | Displayed application name. |
| `STOCK_DESK_DATA_DIR` | `data` | Writable database, market lake, reports, and task data. |
| `STOCK_DESK_DATABASE_URL` | `sqlite:///data/stock-desk.db` | SQLite URL shared by API and worker. |
| `STOCK_DESK_MASTER_KEY` | unset | Fernet key required before encrypted provider credentials can be saved. |
| `STOCK_DESK_WEB_DIST_DIR` | auto-detected | Optional compiled web asset directory for packaged serving. |

The database URL and data directory identify one storage instance. Do not point
API and worker at different values, copy a live SQLite file, or move one path
without the other.

## Container settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `STOCK_DESK_UID` | existing owner or `10001` | Non-root runtime user ID. |
| `STOCK_DESK_GID` | existing owner or `10001` | Non-root runtime group ID. |
| `STOCK_DESK_IMAGE` | local Compose default | Optional image name for parallel checkouts. |
| `STOCK_DESK_TDX_HOST_PATH` | `./data/tdx` | Host TDX tree mounted read-only at `/app/tdx`. |

Keep database and market data on one local POSIX filesystem for SQLite locking,
atomic restore replacement, and directory synchronization semantics.

## Provider credentials

In every profile, configure market and model providers in `/settings` and
`/analysis`, not in source files. Tushare tokens and remote model API keys are
write-only: the API stores encrypted values and returns only masked status.
DeepSeek-oriented and generic OpenAI-compatible endpoints must pass endpoint
validation; local Ollama is intended for a local endpoint.

Provider availability still depends on bundled or locked extras, upstream
permissions, network access, quotas, and licensing. A configured provider does
not guarantee every instrument, period, field, announcement, or news item. See
[data sources](data-sources.md) and [model providers](model-providers.md).
