# Backup, restore, upgrade, and rollback

Stock Desk backups are application-level snapshots, not copies of a live SQLite
file. Use them before changing an image, package, schema, or storage layout.

## Deployment support

The commands in this guide are **source/container POSIX only**:

- For a source deployment on Linux or macOS, run them from the matching release
  checkout and locked environment.
- The container runtime image does not contain `scripts/backup.py` or
  `scripts/restore.py`. For Compose, run the tools from the matching release
  checkout on the POSIX host and point them at the bind-mounted data and
  database. Do not present `docker exec` or in-container `uv` as available.
- The source-free Windows and macOS installers do not bundle this operator CLI.
  A macOS operator may use a matching source checkout as a separate POSIX
  operation, but that is not a frozen native command.
- The complete workflow is unsupported on native Windows filesystems in this
  release. Do not attempt it unless a later release adds and verifies a frozen
  native command.

Never copy a live SQLite file as a substitute. Preserve the per-user native
`config/master.key` or source/container `STOCK_DESK_MASTER_KEY` separately from
encrypted data.

## Create a portable backup

For a source/container POSIX deployment, keep Stock Desk running if desired,
then run from the matching source checkout:

```bash
uv run python scripts/backup.py /safe/path/desk.stockdesk-backup \
  --data-dir /path/to/data \
  --database-url sqlite:////path/to/data/stock-desk.db
```

Backup temporarily blocks new task claims, but scheduler enqueue remains available.
It waits for running tasks to finish, obtains the migration lock, requires a
non-busy WAL checkpoint, and clones SQLite through its backup API. A timeout fails
without publishing a partial archive; adjust it with `--drain-timeout`.
Enqueues committed before the SQLite clone begins are present in that consistent
snapshot; enqueues committed after it begins belong to the next backup.

The backup uses a verified ZIP64-capable container with a canonical manifest and
digest. The container itself is not claimed to have one byte-for-byte canonical
encoding. It contains:

- the consistent SQLite clone;
- the MarketLake ownership marker; and
- only immutable, regular, single-link Parquet objects referenced by the cloned
  catalog, with catalog and physical hashes.

It does **not** contain unreferenced files, `.locks`, TDX inputs, exports, `.env`,
or `STOCK_DESK_MASTER_KEY`. Portable backups also remove every `secret.*` row by
default. `--include-encrypted-secrets` is intended only for local recovery; it
includes ciphertext rows but still never includes the master key. Store and back
up that key separately or the ciphertext cannot be decrypted.

The manifest records external TDX configuration as a dependency, not as bundled
data. Restoring a backup does not make a missing external TDX tree available.

## Restore

Stop the API, workers, schedulers, containers, and every other Stock Desk process
that uses the destination. Then run from the matching source checkout on the
POSIX host:

```bash
uv run python scripts/restore.py /safe/path/desk.stockdesk-backup \
  --data-dir /path/to/data \
  --database-url sqlite:////path/to/data/stock-desk.db \
  --offline
```

`--offline` is mandatory for a non-empty destination and is an operator assertion;
the tool cannot prevent an uncoordinated remote supervisor from manipulating the
files directly. The packaged API and worker runtime register cross-process service
markers and refuse to start while restore owns its lifecycle gate; restore refuses
before creating its recovery backup if either service is active. An empty
destination can be restored without `--offline`. After a successful restore,
restart the API, workers, and scheduler as one coordinated service restart.

While that lifecycle gate is held, restore also holds the migration and task-claim
gates. Before the local recovery snapshot, an expired running lease is resolved:
an ordinary abandoned task is requeued, while an expired task with cancellation
already requested is terminalized as cancelled together with its backtest or
analysis domain state. This keeps an abandoned lease from making offline restore
wait forever and makes the recovery archive internally consistent.

Before changing an owned destination component, restore performs all of these
steps:

1. validates entry paths, duplicates, ZIP flags and attributes, types, counts,
   compressed and expanded aggregate limits, compression ratios, the canonical
   manifest/digest, and every content hash before extraction;
2. creates a local `pre-restore-*.stockdesk-backup` under
   `.stock-desk-recovery/` for an existing instance, including encrypted secret
   rows but never the master key;
3. extracts on the destination filesystem, checks SQLite integrity and foreign
   keys, verifies catalog inventory, migrates the staged database forward to the
   installed schema, and exercises every restored MarketLake routing manifest;
4. replaces only the owned database and `market/` components through atomic
   renames, recording and fsyncing each phase in
   `.stock-desk-restore-journal.json`.

The whole data directory is never swapped. In particular, a TDX mount below
`data/tdx`, exports, and other operator-owned paths remain in place.

If power loss or a process crash interrupts component replacement, the packaged
runtime checks the journal before starting. The journal binds the archived
manifest and the content identities of the original and installed database and
MarketLake components. Recovery verifies regular-file, single-link, non-symlink,
directory, marker, and content-hash expectations before every rollback or cleanup.
An unfinished replacement is rolled back; a committed replacement is finalized.
A corrupt, missing-stage, changed-component, or ambiguous journal state makes
startup refuse to run instead of guessing. With the application or Compose stack
stopped, source/container POSIX recovery can be requested from the matching
checkout:

```bash
uv run python scripts/restore.py --data-dir /path/to/data --recover-only
```

Do not delete or edit a journal or `.stock-desk-restore-*` staging tree by hand.
Preserve both and investigate the filesystem state if automatic recovery refuses.

## Upgrade and rollback procedure

An upgrade record must bind data to the exact executable identity for its
deployment. Do not treat a version label alone as a rollback artifact.

### Compose

1. Record the exact Compose image digest (`repository@sha256:...`), Compose file,
   environment excluding secrets, and bind-mounted data identity. A mutable tag
   is not sufficient.
2. From the matching release checkout on the POSIX host, create and verify an
   untouched portable backup of the bind-mounted data.
3. Pin the new image by digest and run the coordinated Compose upgrade.
4. To roll back, stop the complete stack, restore the untouched backup with the
   matching source tool, pin the previous image digest, and start API and worker
   together. Never start the old image against data already migrated by the new
   image.

### Source checkout or package

1. Record the immutable source commit, a clean-worktree assertion, lockfile
   hashes, and—when installing a built package—the exact wheel/sdist filename and
   SHA-256 digest.
2. Create and verify the backup with that same checkout and locked environment.
3. Upgrade to a separately verified immutable commit or package identity and
   recreate the locked environment before coordinated startup.
4. To roll back, stop every source process, restore the untouched backup with the
   previous matching checkout, restore that exact commit/package and lockfiles,
   then restart API and worker together.

### Native macOS installer

1. Record the exact macOS installer artifact: DMG filename, architecture,
   SHA-256, release manifest, GitHub attestations, and the installed application
   identity. Keep the verified prior DMG or an exact verified copy of the prior
   `.app` outside the live Applications location.
2. The app has no frozen backup command. Before replacement, create a compatible
   portable backup only through a matching source checkout on POSIX, and protect
   `~/Library/Application Support/stock-desk/config/master.key` separately.
3. Quit Stock Desk, verify the new DMG and attestations, then replace the app and
   launch it. Do not run old and new app copies against the same data.
4. To roll back, quit the app, restore the untouched data backup with the matching
   source tool, and reinstall the exact prior verified DMG/`.app` artifact. If no
   compatible pre-upgrade backup exists, replacing the app does not safely roll
   back migrated data.

Native Windows has no complete supported backup/restore/rollback workflow in
this release.

Never use Alembic downgrade as an operational rollback. Newer code may have
changed data outside a reversible schema operation.

The automatically created `.stock-desk-recovery/` archive protects the immediate
pre-restore destination. It is not a substitute for the independently stored
pre-upgrade archive because it shares the destination's failure domain.

## Important limits and risks

- A backup cannot complete while running tasks fail to drain, the migration lock
  is held, the WAL checkpoint is busy, or a referenced catalog object changes.
- Plan for free space of roughly three times the owned database and MarketLake
  payload, in addition to the source archive. Restore needs the full staging tree,
  a local recovery archive, and temporarily both old and new owned components.
  Exact compression and filesystem overhead vary; staging failure is detected
  before component replacement.
- Filesystem atomic rename and directory `fsync` semantics are required. Keep the
  destination on one local filesystem; do not place the database and `market/` on
  separate mounts.
- The complete backup, restore, and interrupted-restore recovery workflow requires
  POSIX no-follow, directory-descriptor, atomic-rename, and directory-`fsync`
  behavior. It is not supported on native Windows filesystems in this release,
  including instances with no MarketLake partitions.
- Archive validation protects integrity and structure, not confidentiality. A
  portable backup can contain private research, formulas, task history, and model
  output. Encrypt backup storage and control access to it.
- The manifest hash is not a digital signature. Accept archives only from trusted
  storage and transfer channels.
