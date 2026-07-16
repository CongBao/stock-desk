[简体中文](README.md)

# Stock Desk

> The current stable release is Windows x64 `v1.1.0`. It is an explicitly labelled unsigned desktop release; see the [v1.1.0 notes](docs/releases/v1.1.0.md) for scope and risks.

## Product positioning

Stock Desk is a local-first personal A-share desktop research app for traceable
market charts, TDX-compatible formulas, reproducible historical backtests, and
evidence-linked multi-agent research. It does not connect to a broker or place orders.

![A-share market chart with provenance](docs/images/market-data-and-charts.png)

Kweichow Moutai `600519.SH`; BaoStock daily/qfq data; cutoff `2026-07-08T07:00:00Z`. For feature demonstration only; not investment advice. （仅作功能演示，不构成投资建议。）

## Core features

- Inspect cached daily, weekly, and 60-minute charts with source, cutoff, adjustment, dataset-version, and route evidence.
- Build and version formulas in a low-code TDX-compatible editor, then preview main charts, subcharts, and signals.
- Backtest saved formula versions with explicit A-share T+1, costs, lots, data coverage, and immutable results.
- Run DeepSeek, OpenAI-compatible, or local Ollama research workflows with conclusions linked to persisted evidence.

Backtest compatibility is protected by an offline immutable `v1.0.0` oracle bound to the release commit and Git tree. It covers twelve MACD or parameterized-custom formula, single or pool, and daily, weekly, or 60-minute combinations, plus A-share constraints, open-position costs, and partial data gaps. CI authenticates the oracle, inputs, and generator and rejects drift outside the closed allowlist.

| Real formula preview | Blocked real backtest preflight | Analysis readiness |
| --- | --- | --- |
| ![CATL MACD BUY/SELL formula preview](docs/images/formula-studio.png)<br>CATL `300750.SZ`; BaoStock, 1d/qfq; cutoff `2026-07-08T07:00:00Z`; MACD BUY/SELL are visible. For feature demonstration only; not investment advice. （仅作功能演示，不构成投资建议。） | ![Ping An Bank MACD strict preflight blocked](docs/images/backtesting.png)<br>Real MACD configuration for Ping An Bank `000001.SZ`; BaoStock, 1d/qfq; cutoff `2026-07-08T07:00:00Z`. Strict preflight is blocked because no authorized Tushare execution-status snapshot exists. No task or report was created; this is not a successful backtest, result, or win rate. For feature demonstration only; not investment advice. （仅作功能演示，不构成投资建议。） | ![China Merchants Bank model and evidence readiness](docs/images/multi-agent-research.png)<br>Model/evidence readiness for China Merchants Bank `600036.SH`: no verified model, no model call started, and no report generated. |

## Download and install

Download `stock-desk-1.1.0-unsigned-x64-setup.exe` from the
[Latest Release](https://github.com/CongBao/stock-desk/releases/latest). It has no Authenticode signature, so Windows may show an Unknown Publisher or SmartScreen warning. Verify it against `UNSIGNED-WINDOWS-SHA256SUMS` on the release page first.

To install `v1.1.0`:

1. Run the installer as an ordinary Windows user; administrator rights are not required.
2. Open Stock Desk from the Start menu; its bundled service starts with the desktop window.
3. Complete the first-run data and stock setup. If no stock is chosen, the Shanghai Composite `000001.SS` opens by default.

Ordinary users do not need GitHub CLI, a source checkout, Docker, or development tools. SignPath Foundation rejected the free-signing application because the project did not yet have enough exposure, so v1.1.0 is released unsigned by user decision; it is not self-signed. The production updater remains disabled. The planned v1.2 will move to Microsoft Store / MSIX. See the [download guide](docs/download.md) and [code-signing policy](docs/code-signing-policy.md).

`v1.1.0` uses zero telemetry and does not automatically upload crash reports, diagnostics, or usage data. A read-only demo remains visibly labelled and cannot complete onboarding.
It does not ship macOS, Linux, Android, or ARM64 installers and does not read, import, migrate, modify, or delete legacy v1 data. Its uninstaller only lets the user explicitly remove **v1.1-only** local data; cancellation or failure preserves that data.
The release reuses the exact-SHA Windows candidate proved on protected `main`, without rebuilding or rerunning source tests.

## Documentation

The default entry is the [Simplified-Chinese GitHub Wiki](https://github.com/CongBao/stock-desk/wiki),
with an [English Wiki](https://github.com/CongBao/stock-desk/wiki/Home-en) switch. The Wiki covers
installation, market data, formulas, backtests, analysis, tasks, configuration, backup, and recovery.

Repository references: [architecture](docs/architecture.md), [configuration](docs/configuration.md),
[troubleshooting](docs/troubleshooting.md), [backup and restore](docs/backup-and-restore.md), and the
[disclaimer](docs/disclaimer.md).

## Safety and scope

Stock Desk is research software, not investment advice. Data may be delayed, incomplete, adjusted,
or licensed; formulas, backtests, and model output can be wrong. Independently verify every decision.

Never publish credentials, `.env`, keys, TDX paths, databases, backups, or licensed market data.
Local deployments have no authentication, authorization, or TLS; do not expose them to untrusted networks.
Report security issues privately through
[GitHub Security Advisories](https://github.com/CongBao/stock-desk/security/advisories/new) and read
[SECURITY.md](SECURITY.md).

See the bilingual [Code signing policy](docs/code-signing-policy.md) for signing status,
manual approval, and trusted-build boundaries, and the [privacy policy](docs/privacy.md)
for local data and network behavior.
See the [CI guide](docs/ci.md) for immutable build and proof contracts.
