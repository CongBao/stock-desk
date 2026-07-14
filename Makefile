.PHONY: bootstrap dev test acceptance acceptance-formula acceptance-backtest acceptance-analysis acceptance-domain-contracts acceptance-full-journey benchmark benchmark-formula benchmark-backtest performance performance-reference performance-target performance-regressions e2e e2e-foundation e2e-market e2e-formula e2e-backtest e2e-analysis e2e-task-center e2e-accessibility lint typecheck build smoke container-smoke public-tree check-public-tree security release-check

bootstrap:
	uv sync --frozen --all-groups --extra providers
	pnpm install --frozen-lockfile

dev:
	uv run --frozen python scripts/dev.py

test:
	uv run --frozen pytest -W error --ignore=tests/acceptance/test_market_flow.py --ignore=tests/acceptance/test_formula_consistency.py --ignore=tests/acceptance/test_macd_formula_flow.py --ignore=tests/acceptance/test_formula_editing_assistance.py --ignore=tests/acceptance/test_backtest_semantics.py --ignore=tests/acceptance/test_full_user_journey.py --ignore=tests/performance/test_chart_query.py --ignore=tests/performance/test_formula_preview.py --ignore=tests/performance/test_single_backtest.py --ignore=tests/performance/test_v1_budgets.py --cov=src/stock_desk --cov=scripts --cov=migrations --cov-branch --cov-report=term-missing --cov-report=xml:coverage.xml --cov-fail-under=85
	pnpm test

acceptance:
	uv run --frozen pytest -W error tests/acceptance/test_market_flow.py

acceptance-formula:
	uv run --frozen pytest -W error tests/acceptance/test_formula_consistency.py tests/acceptance/test_macd_formula_flow.py tests/acceptance/test_formula_editing_assistance.py

acceptance-backtest:
	uv run --frozen pytest -W error tests/acceptance/test_backtest_semantics.py

acceptance-analysis:
	uv run --frozen pytest -W error tests/acceptance/test_analysis_flow.py tests/security/test_analysis_boundaries.py

acceptance-domain-contracts:
	uv run --frozen pytest -W error \
		tests/acceptance/test_market_period_adjustment_contract.py::test_period_and_adjustment_switches_recalculate_visible_market_and_indicator_values \
		tests/acceptance/test_backtest_scope_matrix.py::test_all_a_index_industry_custom_failure_and_insufficient_scopes \
		tests/acceptance/test_formula_safety_boundary.py::test_future_or_repainting_formula_cannot_be_saved_or_backtested \
		tests/acceptance/test_formula_validation_boundary.py::test_all_validation_stages_block_invalid_save_preview_and_backtest_while_preserving_draft \
		tests/acceptance/test_tdx_local_user_flow.py::test_valid_tdx_directory_shows_markets_period_and_data_cutoff \
		tests/acceptance/test_tdx_local_user_flow.py::test_unsupported_tdx_file_format_is_rejected_before_enablement \
		tests/acceptance/test_architecture_boundaries.py::test_module_inventory_and_heavy_work_use_independent_worker \
		tests/acceptance/test_release_acceptance_scope.py::test_all_first_release_acceptance_domains_and_full_journey_are_gated

acceptance-full-journey:
	uv run --frozen pytest -W error tests/acceptance/test_full_user_journey.py

benchmark:
	uv run --frozen pytest -W error tests/performance/test_chart_query.py -q

benchmark-formula:
	uv run --frozen pytest -W error tests/performance/test_formula_preview.py -q

benchmark-backtest:
	uv run --frozen pytest -W error tests/performance/test_single_backtest.py -q

performance-regressions: benchmark benchmark-formula benchmark-backtest

performance: performance-reference

performance-reference:
	uv run --frozen python scripts/run_performance_baseline.py --fixture full-a-scope-bounded-ten-year --evidence-kind reference --compare tests/performance/baseline.json
	uv run --frozen pytest -W error tests/performance/test_v1_budgets.py -q

performance-target:
	uv run --frozen python scripts/run_performance_baseline.py --fixture full-a-scope-bounded-ten-year --evidence-kind target_baseline --output test-results/performance/target-baseline.json --compare tests/performance/baseline.json
	STOCK_DESK_PERFORMANCE_RESULT=test-results/performance/target-baseline.json uv run --frozen pytest -W error tests/performance/test_v1_budgets.py -q

e2e:
	pnpm exec playwright test --project=chromium

e2e-foundation:
	pnpm exec playwright test web/e2e/foundation.spec.ts --project=chromium

e2e-market:
	pnpm exec playwright test web/e2e/market.spec.ts web/e2e/market-pools.spec.ts web/e2e/market-visual-identity.spec.ts --project=chromium

e2e-formula:
	pnpm exec playwright test web/e2e/formula-studio.spec.ts --project=chromium

e2e-backtest:
	pnpm exec playwright test web/e2e/backtest.spec.ts --project=chromium

e2e-analysis:
	pnpm exec playwright test web/e2e/analysis.spec.ts web/e2e/model-provider-matrix.spec.ts --project=chromium

e2e-task-center:
	pnpm exec playwright test web/e2e/task-center.spec.ts --project=chromium

e2e-accessibility:
	pnpm exec playwright test web/e2e/accessibility.spec.ts web/e2e/responsive.spec.ts --project=chromium

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
	uv run --frozen pytest -W error tests/security -q
	uv run --frozen bandit -q -ll -r src scripts
	uv audit --locked --no-dev
	pnpm install --lockfile-only --frozen-lockfile --ignore-scripts
	pnpm audit --prod --audit-level high
	cargo audit --file src-tauri/Cargo.lock --target-os windows --deny yanked

release-check: test acceptance acceptance-formula acceptance-backtest acceptance-analysis acceptance-domain-contracts acceptance-full-journey performance-regressions performance e2e-foundation e2e-market e2e-formula e2e-backtest e2e-analysis e2e-task-center e2e-accessibility lint typecheck build public-tree security container-smoke
