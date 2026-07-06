# Security Policy

## Supported versions

Security fixes currently target the latest code on the default branch and the latest published release, when one exists. Older snapshots may not receive fixes.

## Reporting a vulnerability

Please use [GitHub private vulnerability reporting](https://github.com/CongBao/stock-desk/security/advisories/new). **Do not open a public issue for a vulnerability.** Do not include credentials, secret values, personal information, or exploitable market-data access details in public channels.

Include a concise impact description, affected version or commit, reproduction steps, and any suggested mitigation. Reports are reviewed privately, but the project does not guarantee a response or remediation timeframe. Coordinated disclosure details will be discussed in the private advisory.

For ordinary bugs and usage questions, follow [SUPPORT.md](SUPPORT.md).

## Deployment boundary

Stock Desk is a local, single-user application and does not implement authentication, authorization, multi-tenancy, or TLS termination. Treat the API, SQLite files, local market lake, and mounted TDX data as trusted-local resources and do not expose the service to an untrusted network. Keep `.env`, `STOCK_DESK_MASTER_KEY`, database and market-data files, local paths, and provider credentials out of source control and issue reports.
