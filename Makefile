.PHONY: bootstrap dev test lint typecheck build smoke public-tree release-check

bootstrap:
	uv sync --frozen --all-groups
	pnpm install --frozen-lockfile

dev:
	uv run --frozen python scripts/dev.py

test:
	uv run --frozen pytest -W error
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
	uv build --no-build-isolation
	pnpm build

smoke:
	STOCK_DESK_CONTAINER_TESTS=1 uv run --frozen pytest -W error -m container tests/acceptance/test_container_smoke.py

public-tree:
	uv run --frozen python scripts/check_public_tree.py

release-check: test lint typecheck build public-tree
