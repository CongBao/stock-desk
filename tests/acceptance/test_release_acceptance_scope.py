from __future__ import annotations

from pathlib import Path

import scripts.verify_release as verify_release_module


DOMAIN_SELECTORS = (
    "tests/acceptance/test_market_period_adjustment_contract.py::test_period_and_adjustment_switches_recalculate_visible_market_and_indicator_values",
    "tests/acceptance/test_backtest_scope_matrix.py::test_all_a_index_industry_custom_failure_and_insufficient_scopes",
    "tests/acceptance/test_formula_safety_boundary.py::test_future_or_repainting_formula_cannot_be_saved_or_backtested",
    "tests/acceptance/test_formula_validation_boundary.py::test_all_validation_stages_block_invalid_save_preview_and_backtest_while_preserving_draft",
    "tests/acceptance/test_tdx_local_user_flow.py::test_valid_tdx_directory_shows_markets_period_and_data_cutoff",
    "tests/acceptance/test_tdx_local_user_flow.py::test_unsupported_tdx_file_format_is_rejected_before_enablement",
    "tests/acceptance/test_architecture_boundaries.py::test_module_inventory_and_heavy_work_use_independent_worker",
    "tests/acceptance/test_release_acceptance_scope.py::test_all_first_release_acceptance_domains_and_full_journey_are_gated",
)


def _target_block(makefile: str, target: str) -> str:
    marker = f"{target}:"
    start = makefile.index(marker)
    next_target = makefile.find("\n\n", start)
    return makefile[start:] if next_target < 0 else makefile[start:next_target]


def test_all_first_release_acceptance_domains_and_full_journey_are_gated() -> None:
    root = Path(__file__).resolve().parents[2]
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    domain_gate = _target_block(makefile, "acceptance-domain-contracts")
    journey_gate = _target_block(makefile, "acceptance-full-journey")
    release_line = next(
        line for line in makefile.splitlines() if line.startswith("release-check:")
    )

    assert (
        tuple(token for token in domain_gate.split() if token.startswith("tests/"))
        == DOMAIN_SELECTORS
    )
    assert "tests/acceptance/test_full_user_journey.py" in journey_gate
    assert "::" not in journey_gate
    candidate_gates = verify_release_module._candidate_gates(target_performance=True)
    candidate_targets = {
        gate.command[1] for gate in candidate_gates if gate.command[:1] == ("make",)
    }
    assert {
        "acceptance-domain-contracts",
        "acceptance-full-journey",
    } <= candidate_targets
    assert candidate_gates[0] == verify_release_module.PRE_PUBLISH_EVIDENCE_GATE
    assert candidate_gates.count(verify_release_module.PRE_PUBLISH_EVIDENCE_GATE) == 1
    assert "scripts/verify_release.py" in workflow
    assert "main-validation-proof-$GITHUB_SHA" in workflow
    assert "make acceptance-domain-contracts" not in workflow
    assert "make acceptance-full-journey" not in workflow
    assert set(release_line.removeprefix("release-check:").split()) == {
        "test",
        "acceptance",
        "acceptance-formula",
        "acceptance-backtest",
        "acceptance-analysis",
        "acceptance-domain-contracts",
        "acceptance-full-journey",
        "performance-regressions",
        "performance",
        "e2e-foundation",
        "e2e-market",
        "e2e-formula",
        "e2e-backtest",
        "e2e-analysis",
        "e2e-task-center",
        "e2e-accessibility",
        "lint",
        "typecheck",
        "build",
        "public-tree",
        "security",
        "container-smoke",
    }
