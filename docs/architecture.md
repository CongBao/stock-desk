# Architecture

Stock Desk is a local-first modular monolith: one Python package owns the HTTP
API, migrations, durable tasks, market storage, formula engine, backtest engine,
analysis workflow, configuration, and security utilities; React supplies the
shared interface used by the WebView2 desktop and by browser-based source and
container profiles. Process and configuration topology differ by deployment.

## Deployment model

### v1.1 Windows desktop topology

The source-free v1.1 Windows package uses a Tauri v2 host as the only desktop
entry point. WebView2 loads the bundled React assets inside the main window;
normal desktop operation does not open or depend on an external browser. The
package also contains the locked Python runtime, migrations, provider adapters,
and one frozen sidecar external binary:

```text
Tauri v2 host ── bundled assets ── WebView2 / React main window
  ├─ single-instance window and Windows Job Object
  ├─ exact-origin, session-authenticated host proxy
  └─ controlled frozen Python sidecar ── random 127.0.0.1 port
       ├─ FastAPI application
       ├─ joined durable task worker
       └─ per-user SQLite database and market storage
```

Before the product workspace becomes available, the host creates the new
per-user v1.1 data tree, generates a high-entropy session authority, starts the
sidecar on a random loopback port, and verifies its health, versions, and source
revision. React can reach the API only through the closed Tauri command; the
sidecar port and authority are not exposed to product code in the WebView.
Unexpected host termination is bounded by the Job Object, while a normal exit
uses the cooperative protocol described below.

The current-user installer places program files under
`%LOCALAPPDATA%\Programs\Stock Desk`. The packaged application treats that tree
as read-only. All v1.1 mutable state lives under
`%LOCALAPPDATA%\Stock Desk\v1.1` (or an explicitly bounded user temporary
location). The desktop runtime does not use the source-development `.env`
contract and does not read, migrate, modify, or delete the old
`%LOCALAPPDATA%\stock-desk` v1 data tree.

The v1.1 uninstaller shows an explicit, default-off data-removal choice. When
selected outside an update, its NSIS hook copies the installed host to a private
temporary directory and invokes one exact internal command before application
files are removed. That command resolves Local AppData through the Windows Known
Folder API, accepts no caller path, targets only the fixed `Stock Desk\v1.1`
child, rejects reparse points and other special entries during full preflight,
then performs a same-parent tombstone rename and no-follow deletion. Retry is
interactive; cancel continues uninstall while retaining data, and silent failure
uses a stable nonzero exit. The old lowercase v1 tree remains outside the target.

Closing the main window, `Alt+F4`, and the application exit command enter one
confirmation state with Cancel as the safe default. After explicit confirmation,
the API closes the task-claim gate and accepts shutdown only when work is already
durably queued or running handlers have acknowledged a safe checkpoint. A missed
ten-second checkpoint deadline keeps the application open and restores claiming;
it does not force-kill a healthy sidecar. The next launch offers explicit resume
or cancel choices for incomplete work, and analysis resume requires a separate
model-cost confirmation.

The desktop host also contains a source-bound, default-off trusted-update
runtime. Its state machine accepts only increasing stable `X.Y.Z` releases for
the fixed Windows x64 target and rejects prereleases, build metadata, downgrades,
replays, cross-repository URLs, excess redirects, and identity mismatches. The
host streams `latest.json` through a 32 KiB limit and the exact GitHub release
redirect chain, then binds the immutable redirect version to every strict
metadata field. It does not issue a second unbounded plugin metadata request.

After a host-owned native confirmation, the host streams the installer through
a 512 MiB limit. Tauri Minisign/Ed25519, SHA-256, and WinVerifyTrust all cover
the same bytes written to a uniquely created file. After verification the host
reopens that exact file read-only, compares its Windows file identity, locks the
staging directory against replacement, refreshes Authenticode chain and revocation
evidence before stopping the sidecar, and requires real process and primary-thread
handles from `CreateProcessW`. The graceful-exit controller closes and checkpoints the
bundled service for the exact process generation; shutdown acceptance, commit
acceptance, and a later matching termination are all required before it may
persist `pending-install.json` and launch the installer. The next exact-version,
exact-source binary commits
`installed-watermark.json` and removes the pending journal. The old binary never
advances the watermark after a failed handoff. Missing local-data paths, keys,
or identity evidence fail closed without creating relative state.

WebView code can request a check or native prompt but cannot supply metadata,
bytes, consent, a path, a digest, a trust result, or an install decision. The
runtime has no environment-variable activation and performs no background
check. The unsigned v1.1.0 release does not enable it. Its checked-in configuration
remains disabled until the production key, an approved signing channel, and real
Windows 10/11 update evidence pass. The
checked-in formal release scaffold is unconditionally disabled before signing.
Its literal gate can be removed only by a reviewed change that adds NSIS
control-semantics equivalence and real SmartScreen/MOTW evidence.
On POSIX, publication hard-links the locked staged object and fsyncs its parent
directory. Windows does not reuse that directory-fsync model: Microsoft documents
that `FlushFileBuffers` requires a `GENERIC_WRITE` file handle and does not list it
as a supported directory-handle operation. Stock Desk instead fsyncs the temporary
file before verification and retains its `CreateFileW(CREATE_NEW)` handle from the
instant of creation. It derives the read-only verifier stream with `DuplicateHandle`
for that same file object before closing the write stream, so no close-and-reopen
namespace window exists. The retained handle denies concurrent writes. Stock Desk
then moves that same object within the same directory using `MoveFileExW` with
`MOVEFILE_WRITE_THROUGH` and without replacement. It finally rechecks the moved
path against the still-open handle. The final path therefore remains absent until
verification finishes, a raced destination fails closed, and Win32 does not return
until the move is complete on disk. If publication reports an error after the move,
cleanup unconditionally revokes the staged object through that still-open handle,
even if another process renamed it to an unrelated third path. It never deletes a
final or temporary pathname on this failure path, so concurrently created decoys
remain untouched. Before the locked handle exists, cleanup may reopen the temporary
path only to compare its file ID with the ID captured at creation and then revoke
that opened object by handle. The production Win32 path, read-only cleanup,
post-move error, attacker target, third-path rename with decoys, and
identity-mismatch revocation are required tests on the pinned GitHub Windows runner.
See Microsoft's
[`FlushFileBuffers`](https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers),
[`MoveFileEx`](https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-movefileexw),
[`SetFileInformationByHandle`](https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle),
and [directory handle](https://learn.microsoft.com/windows/win32/fileio/obtaining-a-handle-to-a-directory)
contracts.
Rust's Cargo and Tauri package versions must match and are injected into React at
build time; an unavailable or mismatched identity is displayed as unavailable,
never as a hard-coded stable version. The WebView has no direct updater
capability, and a user must explicitly confirm any future download and install.
No private signing key is stored in this repository. The historical SignPath and
release-control scaffold is present but its signing job is literally disabled;
the production private key, an approved production signing channel, external VM broker,
NSIS control proof, SmartScreen evidence, and real Windows update receipts are
also absent, so the runtime remains disabled and verification fails closed.

Market payload storage follows the host's verified filesystem capabilities.
POSIX deployments use immutable, content-addressed Parquet partitions plus the
SQLite catalog. Native Windows stores the same canonical OHLCV rows inside the
per-user SQLite catalog because its filesystem does not provide the POSIX
descriptor and directory durability primitives required by that Parquet
publication protocol. The Windows rows are immutable, transactionally committed,
and revalidated against the dataset version, timestamp seal, and routing manifest
when read. Both backends expose the same market, formula, and backtest contract.

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
committed before workers claim them, state transitions are transactional, and
desktop checkpoint requests are persisted at handler-defined safe points; a
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
objects, routing manifests, task history, reports, and exports. In the container
profile, code, dependencies, and compiled assets remain read-only in the runtime
image. The v1.1 desktop application likewise treats installed program files as
read-only and routes every mutable object to its per-user data root.

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

The supported threat model is one trusted operator on one trusted host. Source
and container profiles have no product accounts, remote authorization, multi-user
isolation, or TLS and must remain on loopback. The v1.1 desktop boundary adds an
ephemeral exact-origin and bearer session between the Tauri host and sidecar so
unrelated local processes cannot invoke desktop APIs; it does not turn Stock
Desk into a remotely accessible multi-user service.

The Tauri host, WebView, sidecar, worker, configuration, master key, database,
and market storage are inside the local trust boundary. Market/model providers,
provider responses, external research text, archives, and pasted formulas are
untrusted. Inputs are bounded, formula execution is constrained, model endpoints
are validated, external text is treated as potential prompt injection, and mixed
or corrupt provenance fails closed.

Native installers generate and restrict a per-user key. Source and container
operators provide `STOCK_DESK_MASTER_KEY` outside source control. In either case,
encryption does not protect a host compromised together with its key. Never put
secrets, licensed data, databases, or backups in issues. See
[configuration](configuration.md), [troubleshooting](troubleshooting.md), and
[SECURITY.md](../SECURITY.md).
