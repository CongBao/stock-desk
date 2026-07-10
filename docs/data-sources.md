# Data-source configuration and diagnostics

Stock Desk keeps market-source routing local and explicit. Public routing settings live in the application database; the Tushare token is encrypted separately and is never returned to the browser. The settings workspace is available at `/settings`.

## Security boundary

Set `STOCK_DESK_MASTER_KEY` before saving a Tushare token. The token endpoint accepts a write-only `token` field and returns only:

- whether a token is configured;
- whether the configured key can read secure storage; and
- a short masked hint when decryption succeeds.

Application GET responses, diagnostics, and validation errors do not return the token. Standard Python logging dispatch is protected by process-stable base `Handler.handle` and `Handler.format` hooks. While a diagnostic scope or service lease is active, existing logger and handler filters run first, the exact selected `LogRecord` is sanitized and annotated with a secret-free union snapshot, and the handler emits that same record without changing handler configuration. Redispatch unions an existing snapshot with the currently active secrets in one longest-first pass. Base `Handler.format` uses whichever formatter is current when formatting begins, then sanitizes the composed output with the record's snapshot. Concurrent formatter replacement is therefore preserved, and queued or delayed in-process records remain protected after the originating lease closes. For socket or multiprocessing serialization, the private snapshot reduces to builtin `None`: no secret, lock, or Stock Desk class crosses the boundary, while the prepared record message is already sanitized. This covers ordinary logger calls, `Logger.callHandlers`, direct base-handler dispatch, dynamically created handlers, custom logger subclasses that still dispatch to base handlers, and record-factory replacement. Active scopes and leases contribute to one longest-first union, so overlapping/prefix secrets are removed as one value. Each settings service retains at most the current and immediately previous Tushare token and TDX path to protect delayed rotation logs; a third rotation evicts the oldest values, and explicit close or garbage collection unregisters the lease. Provider construction, probes, and provider close remain inside the invocation redaction scope. Caller-owned/injected services must therefore be closed explicitly; application-owned services close during lifespan shutdown.

This guarantee is intentionally limited to application responses and standard logging dispatch. An SDK that writes directly to a file or `stderr`, a `Handler` subclass that deliberately bypasses base `Handler.handle` or `Handler.format`, or code that monkeypatches either base hook is outside the boundary. No Python in-process hook can prevent arbitrary code from exfiltrating a value it already received.

Omitting the token field or sending `null` preserves the existing token; the browser only sends a token when the operator types a replacement, and clears its input immediately after save begins. A missing master key makes secure storage unavailable and token writes return `503`. A wrong key, corrupt ciphertext, or changed database identity is reported generically without exposing ciphertext, filesystem identity, or decryption details. Once a service or secret-store instance observes a missing or changed database identity it remains permanently compromised and rejects later operations, even if the pool later returns an older matching inode. Database operations and poison/close transitions share one reentrant lease, so an already-validated old-inode operation must finish before a later mismatch becomes visible.

Treat the database, master key, and host as sensitive even though the token is encrypted at rest. Do not commit the master key, include it in support bundles, or expose this local single-user service to an untrusted network.

## Routing priorities

Each data category has an independent ordered list:

| Category | Default order |
| --- | --- |
| Daily bars | Tushare → AKShare → BaoStock → local TDX → Eastmoney |
| Weekly bars | Tushare → AKShare → BaoStock → Eastmoney |
| 60-minute bars | Tushare → BaoStock → Eastmoney |
| Instruments | Tushare → AKShare → BaoStock → Eastmoney |
| Trading calendar | Tushare → BaoStock → Eastmoney |
| Backtest execution status | Tushare |

Stock Desk tries the next provider only when the current provider is unavailable, denied, unsupported, missing coverage, or returns no usable data. It does not splice providers together within one requested series. A saved list must be non-empty, contain no duplicates or unknown names, and retain at least one currently implemented source for its category. Settings are written atomically as one canonical JSON document, so readers see either the old complete order or the new complete order.

Execution status is routed and cached independently from price bars. Tushare is the only authoritative v1 source because it combines a complete exchange calendar with explicit historical suspension and daily price-limit datasets. AKShare, BaoStock, local TDX, and Eastmoney report this capability as unsupported; they never infer tradability from a missing bar or approximate historical board/ST/IPO limits. A local TDX or fallback price series may therefore be paired with a Tushare execution-status snapshot, and both routing manifests remain pinned for replay.

Eastmoney is intentionally shown as a reserved fallback but its Stage 1 adapter is not implemented. Its connection test therefore reports `unsupported` rather than implying live coverage.

Production updates read a fresh, immutable settings snapshot at task start. The snapshot selects separate daily/weekly/60-minute priorities and carries a secret-free configuration fingerprint into the task result; a multi-symbol task cannot mix policies after a concurrent settings save. Configured providers that lack a token, local path, or optional SDK remain in the routing attempt list with a typed safe failure. The worker constructs only usable adapters, runs capability/fetch/close inside the active redaction scope, and attempts every close in reverse order.

The market page explicitly creates `market.catalog.update` and `market.update` tasks. Catalog refresh writes a provenance-backed instrument snapshot and Full-A pool first, then independently refreshes current major-index and provider-discovered industry compositions through AKShare. A partial composition failure is itemized and preserves the last valid preset snapshot. Chart GET requests never invoke providers: they read only the local immutable cache.

## AKShare research bounds

AKShare research responses cross a separate, versioned `akshare-research-projection-v1` boundary before they enter the shared strict table normalizer. Fundamentals retain the latest 24 report periods, including identity, report/notice/update dates, core earnings, revenue, profit, cash-flow, return, leverage, and applicable bank or insurer indicators. The report date remains the fundamental-data cutoff; notice and update dates are validated provenance fields but do not turn a fundamental section into published news. Announcements are requested with an Asia/Shanghai clock-derived inclusive 366-day window and retain at most the latest 256 items. News retains at most the latest 100 items. Both published-data categories preserve their identity, publication timestamp, and canonicalized source URL.

The exact ordered projection field set is represented by a SHA-256 field-set digest; that digest, maximum item count, projection version, and applicable window are recorded in each AKShare research section's adapter contract and contribute to its dataset digest. Every row is validated before ordering or truncation: malformed or future dates, invalid URLs, missing or conflicting identities, and oversized selected cells fail closed even in a row that would otherwise be discarded. A fundamental row must contain at least one projected financial metric, an announcement must have a non-empty title, and a news row must have a non-empty title or body. DataFrame columns are selected before values are materialized; unselected provider columns are never copied into persisted research evidence. Raw shape/selected-byte limits and the existing item, byte, depth, and node budgets remain enforced. These provider-specific bounds do not relax the global market-table `MAX_COLUMNS=128` guard and do not merge or synthesize evidence.

## Local TDX

The TDX setting must be an absolute path of at least four characters to a plausible local `vipdoc` directory. On POSIX systems the reader opens the filesystem anchor and traverses every path component with descriptor-relative `O_NOFOLLOW` directory opens, so a symbolic link anywhere in the ancestor chain is rejected. Dot components, relative or implausibly short paths, surrounding whitespace, control characters, overlong paths, missing layouts, corrupt records, non-directories, and reparse points are rejected or reported as capability gaps. Windows retains its handle-based reparse/final-path validation. The diagnostic performs the same local preflight used by the provider; it never returns the configured path in an error.

Local TDX is a fallback for supported local bar files. It is not a source for the instrument catalogue, trading calendar, suspension history, or price-limit evidence, and unsupported periods remain visible as gaps.

For Compose, set `STOCK_DESK_TDX_HOST_PATH` to the host directory containing `vipdoc`, then configure `/app/tdx` in the UI. API and worker share that read-only mount.

## Connection diagnostics

Execution-status diagnostics exercise the Tushare calendar, suspension, limit, and raw-open permissions independently. Missing permission or incomplete evidence is actionable and fail-closed; it is never shown as tradable.

“Test connection” constructs the real provider adapter. Tushare runs independent bounded historical probes for daily, weekly, and 60-minute bars, instruments, and one trading-calendar day, so a calendar success cannot mask a denied bar entitlement. Its successful calendar batch must contain exactly one unique SH row for every natural date in the requested half-open window. BaoStock performs login/logout and TDX performs filesystem preflight through their adapters; providers that expose a close operation are always closed. AKShare is capability-only because its SDK does not offer a comparable session probe; Eastmoney honestly reports unsupported. Every generic capability report must match both the requested source and provider identity. Non-available reports become one coherent fixed failure only when a matching validated gap supports their state; otherwise they fall back to provider unavailable. The result is a point-in-time preflight, not a guarantee that a later download will succeed.

The workspace displays:

- overall state and check time;
- reported capabilities and supported periods (`1d`, `1w`, `60m`);
- per-category permission state and capability gaps;
- provider data cutoff and last-update fields when known; and
- a fixed, safe fallback reason.

Diagnostic states have these meanings:

| State | Meaning |
| --- | --- |
| `available` | The adapter initialized and reported the requested capability. |
| `permission_denied` | Credentials or provider entitlement do not permit the capability. |
| `unsupported` | The adapter or provider does not implement the capability or period. |
| `transient_failure` | A temporary provider, timeout, or concurrent-local-file condition prevented the check. |
| `unavailable` | Required configuration/data is missing, corrupt, invalid, or otherwise unavailable. |

Reasons and details are deliberately fixed and low-context. Raw SDK exceptions can contain credentials, URLs, query parameters, or local paths, so neither the API response nor logs include exception text.

`last_update` is the latest validated `MarketDataset.created_at` for the selected source, and `data_cutoff` is that source's maximum cached provenance cutoff. Both remain `null` when no cache evidence exists. A diagnostic snapshots public configuration, credentials, and an in-memory configuration revision while holding the settings lock, then releases that lock for provider construction, probes, and provider close. Ordinary settings reads, successful updates, and service close therefore do not wait for provider code. The service reacquires the lock before reading cache evidence; a changed revision discards the provider result and returns a fixed `transient_failure`, while a closed service discards the result and returns the fixed storage error. Cache evidence is read only after the revision check and provider close; `last_checked` is then sampled after that evidence read and must not regress behind the provider-completion sample. This admits a valid dataset committed during an unchanged probe while still rejecting evidence later than the completed diagnostic. A provider-supplied cutoff is independently required to be an aware UTC-normalizable instant from 1990 through final completion even when no cache row exists; valid cache evidence then replaces it. A fully successful multi-category probe may provide a conservative point-in-time cutoff; partial probes do not synthesize one, and probe time is never presented as a data update time. Malformed, future, incomplete, or identity-mismatched evidence fails closed instead of being displayed.

Diagnostic results are bound to the browser's current persisted configuration revision. Editing a token, TDX path, or priority clears affected results, aborts in-flight checks, shows `配置已变更，请重新检测`, and disables all connection-test buttons until a save succeeds. Saving, successful completion, and save failure each invalidate pending diagnostic controllers; buttons remain disabled while saving and after an error. A response started under an older revision is ignored even if the underlying request ignores cancellation. After a successful save, tests are re-enabled and the stale label remains until a fresh check completes.

## API contract

All endpoints are under `/api`:

| Method and path | Purpose |
| --- | --- |
| `GET /settings/sources` | Read public priorities, TDX path, and masked Tushare status. |
| `PUT /settings/sources` | Atomically replace public priorities and TDX path. |
| `GET /settings/sources/tushare` | Read masked Tushare configuration status. |
| `PUT /settings/sources/tushare` | Save a new write-only token, or preserve it when omitted. |
| `POST /settings/sources/{source}/test` | Run a bounded capability diagnostic for one provider. |
| `POST /market/catalog/updates` | Queue an explicit instrument/preset refresh. |
| `POST /market/updates` | Queue an explicit symbol or frozen-pool bar update. |
| `GET /market/updates/{task_id}/items` | Read durable per-symbol results. |
| `GET/PUT /market/schedules/daily` | Read or replace the singleton Asia/Shanghai daily schedule. |

Requests use `application/json`. Contracts are strict: unknown fields, invalid enums, duplicate providers, unsafe paths, and malformed stored settings are rejected without echoing request values. Lazy migration, engine creation, and database-identity failures return the same fixed JSON `503` storage response. Tushare probe outcomes are accepted only when their source, exact query or operation context, successful batch item type, and calendar coverage match the category being tested. Generic reports are likewise bound to the requested provider/source identity, so evidence lookup cannot be redirected by a mismatched report. The supported provider identifiers are `tushare`, `akshare`, `baostock`, `tdx_local`, and `eastmoney`.

## Operator workflow

1. Configure `STOCK_DESK_MASTER_KEY` outside source control and start the API.
2. Open `/settings`, enter a Tushare token only if adding or replacing it, and optionally set the absolute TDX `vipdoc` path.
3. Save once, then test each source that should participate in routing.
4. Review period and permission gaps before moving a source higher in a category.
5. Re-run diagnostics after changing provider entitlements, credentials, SDK versions, or local TDX files.

When troubleshooting, record only the provider, safe state/reason, check time, and affected category. Never copy the token, master key, encrypted database value, raw provider exception, or local path into an issue.
