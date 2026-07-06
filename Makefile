.PHONY: bootstrap dev test acceptance benchmark e2e e2e-foundation e2e-market lint typecheck build smoke container-smoke public-tree check-public-tree security release-check

bootstrap:
	uv sync --frozen --all-groups --extra providers
	pnpm install --frozen-lockfile

dev:
	uv run --frozen python scripts/dev.py

test:
	uv run --frozen pytest -W error --ignore=tests/acceptance/test_market_flow.py --ignore=tests/performance/test_chart_query.py --cov=src/stock_desk --cov=scripts --cov=migrations --cov-branch --cov-report=term-missing --cov-report=xml:coverage.xml --cov-fail-under=85
	pnpm test

acceptance:
	uv run --frozen pytest -W error tests/acceptance/test_market_flow.py

benchmark:
	uv run --frozen pytest -W error tests/performance/test_chart_query.py

e2e: e2e-foundation e2e-market

e2e-foundation:
	pnpm exec playwright test web/e2e/foundation.spec.ts --project=chromium

e2e-market:
	pnpm exec playwright test web/e2e/market.spec.ts --project=chromium

lint:
	uv run --frozen ruff format --check .
	uv run --frozen ruff check .
	uv run --frozen bandit -q -ll -r src scripts
	pnpm format:check
	pnpm lint

typecheck:
	uv run --frozen mypy --strict src scripts
	pnpm typecheck

build:
	uv run --frozen python scripts/clean_build_artifacts.py
	uv build --no-build-isolation
	pnpm build

smoke:
	STOCK_DESK_CONTAINER_TESTS=1 uv run --frozen pytest -W error -m container tests/acceptance/test_container_smoke.py

container-smoke:
	@set -eu; \
	trap 'status=$$?; docker compose down --volumes --remove-orphans || true; exit $$status' EXIT; \
	docker compose down --volumes --remove-orphans; \
	docker compose build --pull; \
	docker compose up --wait --no-build; \
	$(MAKE) smoke

public-tree:
	uv run --frozen python scripts/check_public_tree.py

check-public-tree: public-tree

security:
	uv audit --locked --no-dev
	pnpm install --lockfile-only --frozen-lockfile --ignore-scripts
	pnpm audit --prod --audit-level high

release-check: test acceptance benchmark e2e-foundation e2e-market lint typecheck build public-tree security container-smoke
