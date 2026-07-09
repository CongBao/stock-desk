[简体中文](README.md)

# Stock Desk

## Product positioning

Stock Desk v1.0.0 is a local-first personal A-share research desk for traceable
market charts, TDX-compatible formulas, reproducible historical backtests, and
evidence-linked multi-agent research. It does not connect to a broker or place orders.

![A-share market chart with provenance](docs/images/market-data-and-charts.png)

## Core features

- Inspect cached daily, weekly, and 60-minute charts with source, cutoff, adjustment, dataset-version, and route evidence.
- Build and version formulas in a low-code TDX-compatible editor, then preview main charts, subcharts, and signals.
- Backtest saved formula versions with explicit A-share T+1, costs, lots, data coverage, and immutable results.
- Run DeepSeek, OpenAI-compatible, or local Ollama research workflows with conclusions linked to persisted evidence.

| Formula preview | Backtest conclusion | Evidence-linked research |
| --- | --- | --- |
| ![Formula editor and preview](docs/images/formula-studio.png) | ![Backtest result](docs/images/backtesting.png) | ![Multi-agent research report](docs/images/multi-agent-research.png) |

## Download and install

Download the source-free installer for your platform from the
[Latest Release](https://github.com/CongBao/stock-desk/releases/latest):

- `stock-desk-<version>-windows-x86_64.exe`
- `stock-desk-<version>-macos-x86_64.dmg`
- `stock-desk-<version>-macos-arm64.dmg`

1. Choose the installer matching your platform and processor architecture.
2. Run the EXE on Windows; on macOS, open the DMG and copy the app to Applications.
3. Launch Stock Desk for the first time and wait for its bundled services and application window.

Ordinary users do not need GitHub CLI, a source checkout, Docker, or development tools. Checksums,
build attestations, and advanced deployment guidance remain available on the release page and in the guides.

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
