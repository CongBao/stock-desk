[简体中文](README.zh-CN.md)

# Stock Desk

Stock Desk `v0.5.0` is a local-first A-share research workspace for market data,
TDX-compatible formulas, reproducible historical backtests, and evidence-linked
multi-agent analysis. It runs for one trusted local operator and does not place
orders or connect to a broker.

## Quick start

For Windows and macOS, prefer the source-free native installer published with a
release. The verified artifact naming contract is:

- Windows x64: `stock-desk-<version>-windows-x86_64.exe`
- macOS Intel: `stock-desk-<version>-macos-x86_64.dmg`
- macOS Apple silicon: `stock-desk-<version>-macos-arm64.dmg`

Download the matching installer, `.sha256` checksum, target manifest, SBOM, and
provenance from that version's release assets. This README does not link to an
unpublished release. Verify the checksum and provenance before installation;
then run the Windows per-user installer or copy the macOS application from the
DMG into Applications. First launch starts the bundled API and worker on a
random loopback port and opens the browser. It needs no source checkout, Python,
Node.js, or pnpm.

For Linux or a private server, use the loopback-only container deployment. Keep
port 8000 private; use a trusted tunnel rather than exposing this unauthenticated
service directly:

```bash
docker compose up --build --wait
# open http://localhost:8000/market
docker compose down --volumes --remove-orphans
```

Contributors working from source need Python `>=3.12,<3.13`,
[uv](https://docs.astral.sh/uv/), Node.js 22 or 24 LTS, and pnpm 11:

```bash
make bootstrap
make dev
```

Open [http://localhost:5173/market](http://localhost:5173/market). `make dev`
starts the API, task worker, and Vite development server; stop them with
`Ctrl-C`.

The API health endpoint is
[http://localhost:8000/api/health](http://localhost:8000/api/health). See the
[configuration guide](docs/configuration.md) before adding data or model
credentials.

## Core workflows

- **Tasks:** `/tasks` shows durable progress, events, cancellation, failures,
  and recovery diagnostics for market, backtest, and analysis work.
- **Market:** configure a source in `/settings`, refresh the instrument catalog,
  update a symbol or frozen pool, then inspect cache-only daily, weekly, or
  60-minute charts with provenance. See [data sources](docs/data-sources.md).
- **Formulas:** validate and version a constrained TDX-compatible expression in
  `/formulas`, then preview it against a pinned market snapshot. See the
  [compatibility reference](docs/formula-compatibility.md).
- **Backtests:** select an immutable formula version and data scope in
  `/backtests`; review preflight coverage, explicit A-share execution rules,
  costs, partial failures, exports, and pinned replay. See
  [backtesting semantics](docs/backtesting-semantics.md).
- **Research:** configure a DeepSeek-oriented, OpenAI-compatible, or local
  Ollama provider in `/analysis`; preflight evidence before starting the
  nine-stage analysis. Ratings are suppressed when critical evidence is
  insufficient. See [model providers](docs/model-providers.md).
- **Backup and restore:** create a verified application snapshot before an
  upgrade and restore it only with coordinated process shutdown. See
  [backup and restore](docs/backup-and-restore.md).

## Documentation

- [Architecture and trust boundaries](docs/architecture.md)
- [Configuration and secrets](docs/configuration.md)
- [Troubleshooting and recovery](docs/troubleshooting.md)
- [Backup, restore, upgrade, and rollback](docs/backup-and-restore.md)
- [Accessibility](docs/accessibility.md) and
  [performance methodology](docs/performance.md)
- [Changelog](CHANGELOG.md), [roadmap](ROADMAP.md), and
  [support](SUPPORT.md)

Run the public documentation contract locally with:

```bash
uv run --frozen python scripts/verify_docs.py
```

## Safety and scope

Stock Desk is research software, not investment advice. Market data can be
delayed, incomplete, adjusted, or restricted by upstream terms; model output can
be wrong. Verify data, formulas, assumptions, and conclusions independently. See
the full [disclaimer](docs/disclaimer.md).

The service has no authentication, authorization, or TLS. Keep it on loopback.
Never commit or share `.env`, tokens, model keys, `STOCK_DESK_MASTER_KEY`, local
TDX paths, databases, backups, or downloaded market data. Review
[SECURITY.md](SECURITY.md) before reporting a vulnerability.

The current release does not provide real-time quotes, a dynamic screener,
shared-capital portfolio simulation, personalized advice, target prices,
position sizing, broker connectivity, or live/automatic trading.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). Changes should include focused tests and
updated public documentation. The project is licensed under
[Apache-2.0](LICENSE).
