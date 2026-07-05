.PHONY: bootstrap dev test lint typecheck build smoke public-tree security release-check

bootstrap:
	uv sync --frozen --all-groups
	pnpm install --frozen-lockfile

dev:
	uv run --frozen python scripts/dev.py

test:
	uv run --frozen pytest -W error --cov=src/stock_desk --cov=scripts --cov=migrations --cov-branch --cov-report=term-missing --cov-report=xml:coverage.xml --cov-fail-under=85
	pnpm test

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

public-tree:
	uv run --frozen python scripts/check_public_tree.py

security:
	uv audit --locked --no-dev
	pnpm install --lockfile-only --frozen-lockfile --ignore-scripts
	pnpm audit --prod --audit-level high

release-check: test lint typecheck build public-tree security smoke
