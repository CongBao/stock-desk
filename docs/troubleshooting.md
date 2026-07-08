# Troubleshooting

Start with the smallest reproducible operation. Record the Stock Desk version,
run mode, operating system, Python/Node versions, task ID, sanitized error code,
and relevant timestamps. Never share credentials, master keys, database files,
licensed data, backups, or raw model prompts containing private material.

## Startup and health

**Symptom:** the browser cannot reach the API, or Compose reports an unhealthy
service.

1. Open [http://localhost:8000/api/health](http://localhost:8000/api/health).
2. Confirm port 8000 is not used by another process and that API and worker use
   the same `STOCK_DESK_DATA_DIR` and `STOCK_DESK_DATABASE_URL`.
3. For native development, stop the process group with `Ctrl-C` and restart it:

```bash
make dev
```

For containers, inspect sanitized Compose logs, then recreate the local stack:

```bash
docker compose down --volumes --remove-orphans
docker compose up --build --wait
```

Do not delete the data directory to solve a migration or restore-journal error.
Preserve the message and files for diagnosis.

## Data and charts

**Symptom:** a symbol is missing, a chart is empty, or an update uses an
unexpected provider.

1. Refresh the instrument catalog on a new installation.
2. In Settings, verify the provider is enabled for the requested daily, weekly,
   or 60-minute period and that its credential/path test passes.
3. Start an explicit update and open its task details. Chart browsing is
   cache-only; it never silently fetches a missing range.
4. Inspect route attempts, cutoff, adjustment, and provenance. A request uses one
   provider series and does not splice fallbacks.

Permission, quota, SDK, network, symbol, and upstream-coverage failures require
different recovery. Fix the typed cause, then submit a new update; do not edit
market objects or routing manifests by hand.

## Tasks and workers

**Symptom:** work stays queued, appears stalled, fails, or was cancelled.

1. Open `/tasks` and select the task to inspect status, progress, events, item
   results, timestamps, and error code.
2. Confirm the worker is running against the same database and data directory as
   the API.
3. Request cancellation once and wait for a terminal state. A running handler may
   stop only at its next safe checkpoint.
4. Correct the reported input, provider, coverage, or worker problem and create a
   new task. Analysis failed-stage retry creates a linked child run; it does not
   overwrite history.

Do not change task rows directly. Expired leases and interrupted restore state
have explicit recovery paths and fail closed when identities are ambiguous.

## Model providers

**Symptom:** provider connection tests or analysis stages fail.

1. Re-enter the write-only API key if its local secret is unavailable, and verify
   `STOCK_DESK_MASTER_KEY` has not changed unexpectedly.
2. Check provider type, base URL, model identifier, network reachability, quota,
   and upstream status with the built-in connection test.
3. Run analysis preflight. Market evidence is cache-only; fundamentals,
   announcements, and news report their own route and permission gaps.
4. If one retryable stage fails, keep the partial report and use failed-stage
   retry. Missing critical evidence intentionally suppresses a rating.

Treat external text and generated output as untrusted. Never work around endpoint
validation or paste provider keys into logs or issues.

## Backup and restore

**Symptom:** backup cannot drain/checkpoint, restore refuses to start, or startup
finds an interrupted restore journal.

1. Stop submitting work and wait for running tasks to finish before retrying a
   backup. A busy migration lock, claim gate, WAL checkpoint, or changing market
   object must be resolved rather than bypassed.
2. Stop API, workers, schedulers, and all other processes using the destination
   before restoring a non-empty instance. Supply the required `--offline`
   assertion only after doing so.
3. Preserve `.stock-desk-restore-journal.json`, recovery archives, and staging
   directories. Never edit or delete them by hand.
4. For a source/container POSIX deployment, stop every application process or
   the Compose stack, then request explicit journal recovery from the matching
   source checkout:

```bash
uv run python scripts/restore.py --data-dir /path/to/data --recover-only
```

If recovery still refuses, keep the complete filesystem state and seek support.
The tool refuses ambiguous or changed components instead of guessing. Follow the
full [backup, restore, upgrade, and rollback guide](backup-and-restore.md). This
source CLI is not bundled in native installers, and the complete workflow is not
supported on native Windows filesystems in this release.
