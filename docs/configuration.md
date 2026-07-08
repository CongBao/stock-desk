# Configuration

Stock Desk reads variables with the `STOCK_DESK_` prefix from the environment
and an optional local `.env` file. Environment variables override file values.
Do not commit `.env` or place secrets in command history, screenshots, or issue
reports.

## Native development

Install locked dependencies, copy the sample file, and generate a Fernet key:

```bash
make bootstrap
cp .env.example .env
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the generated value as `STOCK_DESK_MASTER_KEY`, then start the supervised
API, worker, and Vite processes with `make dev`. Relative paths are resolved from
the process working directory, so use one checkout and one data directory for all
three processes.

## Container deployment

Compose binds port 8000 to loopback and mounts the same `./data` directory into
the API and worker:

```bash
docker compose up --build --wait
docker compose down --volumes --remove-orphans
```

`STOCK_DESK_UID` and `STOCK_DESK_GID` override the non-root runtime identity;
both must be nonzero. `STOCK_DESK_IMAGE` gives a checkout a distinct local image
name. `STOCK_DESK_TDX_HOST_PATH` mounts a host TDX directory read-only at
`/app/tdx`; enter `/app/tdx` in Settings. The host directory must contain the
expected `vipdoc` tree.

## Application settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `STOCK_DESK_APP_NAME` | `stock-desk` | Displayed application name. |
| `STOCK_DESK_DATA_DIR` | `data` | Writable database, market lake, reports, and task data. |
| `STOCK_DESK_DATABASE_URL` | `sqlite:///data/stock-desk.db` | SQLite URL shared by API and worker. |
| `STOCK_DESK_MASTER_KEY` | unset | Fernet key required before encrypted provider credentials can be saved. |
| `STOCK_DESK_WEB_DIST_DIR` | auto-detected | Optional compiled web asset directory for packaged serving. |

The database URL and data directory describe one storage identity. Do not point
API and worker at different values, copy a live SQLite file, or move one path
without the other. Use the [backup and restore workflow](backup-and-restore.md)
for migration or rollback.

## Container settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `STOCK_DESK_UID` | existing owner or `10001` | Non-root runtime user ID. |
| `STOCK_DESK_GID` | existing owner or `10001` | Non-root runtime group ID. |
| `STOCK_DESK_IMAGE` | local Compose default | Optional image name for parallel checkouts. |
| `STOCK_DESK_TDX_HOST_PATH` | `./data/tdx` | Host TDX tree mounted read-only at `/app/tdx`. |

Keep the data directory on one local filesystem for SQLite locking, atomic
restore replacement, and directory synchronization semantics. Native Windows
filesystems do not support the complete backup/restore workflow in this release.

## Provider credentials

Configure market and model providers in `/settings` and `/analysis`, not in
source files. Tushare tokens and remote model API keys are write-only: the API
stores encrypted values and returns only masked status. DeepSeek-oriented and
generic OpenAI-compatible endpoints must pass endpoint validation; local Ollama
is intended for a local endpoint.

Provider availability still depends on installed locked extras, upstream
permissions, network access, quotas, and licensing. A configured provider is not
a guarantee that every instrument, period, fundamental field, announcement, or
news item is available. Review [data sources](data-sources.md) and
[model providers](model-providers.md).
