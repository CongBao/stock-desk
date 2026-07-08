# Security Policy

## Supported versions

Security fixes currently target the latest code on the default branch and the latest published release, when one exists. Older snapshots may not receive fixes.

## Reporting a vulnerability

Please use [GitHub private vulnerability reporting](https://github.com/CongBao/stock-desk/security/advisories/new). **Do not open a public issue for a vulnerability.** Do not include credentials, secret values, personal information, or exploitable market-data access details in public channels.

Include a concise impact description, affected version or commit, reproduction steps, and any suggested mitigation. Reports are reviewed privately, but the project does not guarantee a response or remediation timeframe. Coordinated disclosure details will be discussed in the private advisory.

For ordinary bugs and usage questions, follow [SUPPORT.md](SUPPORT.md).

## Deployment boundary

Stock Desk is a local, single-user application and does not implement authentication, authorization, multi-tenancy, or TLS termination. Treat the API, SQLite files, local market lake, and mounted TDX data as trusted-local resources and do not expose the service to an untrusted network. Keep `.env`, `STOCK_DESK_MASTER_KEY`, database and market-data files, local paths, and provider credentials out of source control and issue reports.

The Compose runtime grants persistent write access only to `/app/data`; the optional `/app/tdx` input is read-only. Application logs go to standard output and standard error for collection by the container runtime. Stock Desk does not require or mount a writable `/app/logs` directory.

## Automated security evidence

Pull requests are subject to dependency-diff review, locked Python and Web production-dependency audits, application boundary tests, and Bandit checks that fail on medium or high severity findings. Container builds generate an SPDX JSON software bill of materials (SBOM) and fail when the scanner reports an unaccepted high or critical vulnerability. A missing vulnerability database, scanner failure, or unavailable audit service fails the workflow; it is not treated as a passing result.

Successful tagged-release workflows generate a release SBOM and request GitHub artifact provenance and SBOM attestations through short-lived OpenID Connect credentials. These artifacts and attestations exist only for workflow runs that complete their corresponding steps; consult the release run rather than assuming that an unverified local build is attested.
