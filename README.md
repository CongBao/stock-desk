[简体中文](README.zh-CN.md)

# Stock Desk

Stock Desk is a local-first, open-source foundation for a personal A-share research workspace. The current `0.1.0` line is **Stage 0: Foundation**: it provides a FastAPI service, SQLite migrations, a durable task API and worker, encrypted local secret storage, a React workspace shell, and native/container development paths.

Live A-share data, formula execution, backtests, and analysis agents are **not implemented yet**. Their routes are honest previews for later stages; see the [roadmap](ROADMAP.md).

## Prerequisites

- Python `>=3.12,<3.13` (Python 3.12 exactly)
- [uv](https://docs.astral.sh/uv/)
- Node.js 22 or 24 LTS and pnpm 11
- Docker with Compose v2, only for the container path

## Run natively

```bash
make bootstrap
make dev
```

Open the UI at [http://localhost:5173](http://localhost:5173), health at [http://localhost:8000/api/health](http://localhost:8000/api/health), and API docs at [http://localhost:8000/docs](http://localhost:8000/docs). `make dev` supervises the API, durable-task worker, and Vite server; stop all three with `Ctrl-C`.

## Run with Compose

```bash
docker compose up --build --wait
```

The built UI and API are served together at [http://localhost:8000](http://localhost:8000). Stop and remove the stack with:

```bash
docker compose down --volumes --remove-orphans
```

`make release-check` includes a container smoke test and therefore requires this repository's Compose stack to be running first:

```bash
docker compose up --build --wait
make release-check
docker compose down --volumes --remove-orphans
```

## What you can use today

- `/market` shows a static workspace/layout preview with explicitly non-real chart data.
- `/formulas`, `/backtests`, and `/analysis` describe planned capabilities only.
- `/tasks` and `/settings` are UI placeholders; they do not yet manage tasks or secrets.
- `POST /api/tasks`, `GET /api/tasks`, `GET /api/tasks/{id}`, and `POST /api/tasks/{id}/cancel` expose the Stage 0 durable-task API. The worker currently handles only the `demo.double` demonstration task.

With either native or Compose services running:

```bash
curl -sS -X POST http://localhost:8000/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"kind":"demo.double","payload":{"value":21}}'
curl -sS http://localhost:8000/api/tasks
```

The first response is durable in local SQLite; the worker claims it and stores `{"value":42}` as the result. This is infrastructure demonstration behavior, not a market-data job.

## Data and security boundaries

No market-data provider is bundled or contacted in Stage 0. Future users will need to evaluate provider licensing, availability, quality, and redistribution terms themselves.

Before storing any future provider credential, generate a Fernet key and place it in an untracked `.env` as `STOCK_DESK_MASTER_KEY`:

```bash
cp .env.example .env
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The local secret store encrypts values, and the security package provides redacting filters/formatters for handlers that explicitly configure them. Stage 0 does not install redaction globally and has no authentication, authorization, or TLS. Do not commit `.env`, paste secrets into issues, or expose the service to an untrusted network. See [security reporting](SECURITY.md) and the [architecture trust boundaries](docs/architecture.md).

## Project information

- [Architecture](docs/architecture.md)
- [Contributing](CONTRIBUTING.md) and [Code of Conduct](CODE_OF_CONDUCT.md)
- [Security](SECURITY.md) and [Support](SUPPORT.md)
- [Roadmap](ROADMAP.md) and [Changelog](CHANGELOG.md)
- [Apache-2.0 license](LICENSE)

Stock Desk is research software, not investment advice. Verify data and decisions independently; you remain responsible for any financial action.
