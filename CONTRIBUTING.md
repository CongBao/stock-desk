# Contributing to Stock Desk

Thank you for helping improve Stock Desk. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md), keep changes focused, and never include credentials, personal data, or licensed market data in commits or issues.

## Development setup

Use Python `>=3.12,<3.13`, uv, Node.js 22 or 24 LTS, and pnpm 11.

```bash
git clone https://github.com/CongBao/stock-desk.git
cd stock-desk
make bootstrap
make dev
```

The native development command starts the API on port 8000, the worker, and Vite on port 5173. Read the [architecture overview](docs/architecture.md) before changing module boundaries.

## Quality gates

Use test-driven development for behavior changes: add a focused failing test, confirm that it fails for the intended reason, implement the smallest change, then refactor while green. Keep generated files and lockfiles consistent with their source manifests.

Run the Docker-free gates before opening a pull request:

```bash
make test
make lint
make typecheck
make build
make public-tree
```

For the complete release gate, Docker must be running. The command starts an isolated Compose stack for the smoke test and removes it on exit:

```bash
make release-check
```

## Pull requests

- Explain the problem and the user-visible effect.
- Link relevant issues without including sensitive data.
- Include tests for changed behavior and state the RED/GREEN evidence.
- Update public documentation and `CHANGELOG.md` when appropriate.
- Confirm formatting, lint, type checks, tests, builds, and the public-tree audit pass.
- Keep commits reviewable and use clear imperative messages.

Maintainers may ask for changes or close contributions that conflict with the project scope, security model, license, or community standards.
