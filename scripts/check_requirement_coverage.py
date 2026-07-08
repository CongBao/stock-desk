from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import yaml  # type: ignore[import-untyped]
from yaml.events import AliasEvent  # type: ignore[import-untyped]


MAX_YAML_BYTES = 512_000
MAX_YAML_DEPTH = 20
MAX_YAML_NODES = 12_000
MAX_TEXT_LENGTH = 1_200

ROOT_FIELDS = {"schema_version", "requirements", "non_goals"}
ITEM_FIELDS = {
    "id",
    "category",
    "kind",
    "behavior_key",
    "acceptance",
    "source_refs",
    "owning_stage",
    "status",
    "evidence",
}
SOURCE_REF_FIELDS = {"capability", "requirement", "scenario"}
SELECTOR_EVIDENCE_FIELDS = {"state", "runner", "kind", "path", "selector", "assertion"}
GATE_EVIDENCE_FIELDS = {"state", "runner", "kind", "gate_id", "assertion"}
MANUAL_EVIDENCE_FIELDS = {
    "state",
    "runner",
    "kind",
    "procedure_id",
    "artifact_kind",
    "required_by_gate",
    "final_artifact_contract",
    "completed",
    "assertion",
}

CATEGORIES = {
    "platform",
    "market",
    "formula",
    "backtest",
    "analysis",
    "security",
    "performance",
    "operations",
    "publication",
}
KINDS = {
    "user_visible",
    "architecture",
    "security",
    "performance",
    "operational",
    "publication",
    "non_goal",
}
STATUSES = {"mapped", "verified"}
STATES = {"existing", "planned", "manual"}
RUNNERS = {"pytest", "vitest", "playwright", "github-actions", "gate", "manual"}
EVIDENCE_KINDS = {
    "acceptance",
    "integration",
    "component",
    "contract",
    "security",
    "performance",
    "absence",
    "gate",
    "manual",
}
SEMANTIC_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BEHAVIOR_KEY = re.compile(r"^[a-z][a-z0-9_]{4,79}$")
FORBIDDEN_PUBLIC_TEXT = re.compile(
    r"(?:^|[/\\])(?:openspec|outputs|\.agents|\.codex|\.superpowers|work)(?:[/\\]|$)"
    r"|(?:^|[/\\])docs[/\\]superpowers(?:[/\\]|$)"
    r"|(?:^|\s)~[/\\]"
    r"|(?:^|\s)/(?:Users|home)/[^\s/\\]+(?:[/\\]|$)"
    r"|(?:^|\s)/root(?:[/\\]|$)"
    r"|(?:^|\s)/(?:private/)?var/folders(?:[/\\]|$)"
    r"|(?:^|\s)[A-Za-z]:[/\\]Users[/\\][^\s/\\]+(?:[/\\]|$)",
    re.IGNORECASE,
)
DOC_DIGEST = re.compile(r"requirements-yaml-sha256: ([0-9a-f]{64})")
MARKDOWN_LINK = re.compile(
    r"(?<!!)\[[^\]]+\]\((?P<target><[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)"
)
COLLECTION_TIMEOUT_SECONDS = 120


def _canonical(
    semantic_type: str,
    category: str,
    kind: str,
    behavior_key: str,
    owning_stage: int,
    capability: str,
    requirement: str,
    *scenarios: str,
) -> dict[str, Any]:
    return {
        "semantic_type": semantic_type,
        "category": category,
        "kind": kind,
        "behavior_key": behavior_key,
        "owning_stage": owning_stage,
        "capability": capability,
        "requirement": requirement,
        "scenarios": frozenset(scenarios),
    }


# Machine-maintained canonical data; registry-shape tests lock every entry.
# fmt: off
CANONICAL_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "R-001": _canonical("product", "market", "user_visible", "market_data_scope_is_complete", 1, "market-data-charting", "base-a-share-data-scope", "load-stock-analysis-data", "report-missing-category"),
    "R-002": _canonical("product", "market", "user_visible", "stock_search_and_chart_interactions", 1, "market-data-charting", "stock-search-and-chart-interaction", "search-and-switch-stock", "inspect-history"),
    "R-003": _canonical("product", "formula", "user_visible", "builtin_and_custom_formulas", 2, "formula-system", "builtin-and-custom-formulas", "use-builtin-macd", "create-custom-formula"),
    "R-004": _canonical("product", "formula", "architecture", "formula_versions_are_traceable", 2, "formula-system", "traceable-formula-versions", "modify-used-formula", "copy-saved-formula"),
    "R-005": _canonical("product", "publication", "publication", "bilingual_readme_baseline", 5, "market-data-charting", "bilingual-open-source-readme", "english-to-chinese", "chinese-to-english"),
    "R-006": _canonical("product", "platform", "user_visible", "responsive_navigation_baseline", 0, "market-data-charting", "responsive-navigation-and-nonoverlap", "narrow-screen-auto-collapse", "manual-navigation-toggle", "preserve-layout-at-supported-ratios"),
    "R-007": _canonical("product", "market", "user_visible", "chart_periods_and_adjustments", 1, "market-data-charting", "periods-and-adjustments", "switch-period", "switch-adjustment"),
    "R-008": _canonical("product", "market", "user_visible", "candlestick_main_and_formula_subchart", 2, "market-data-charting", "candlestick-main-and-formula-subchart", "preview-subchart-formula"),
    "R-009": _canonical("product", "analysis", "architecture", "constrained_multi_agent_sequence", 4, "multi-agent-analysis", "compact-research-workflow", "complete-research-workflow", "inspect-analysis-trace"),
    "R-010": _canonical("product", "platform", "architecture", "durable_async_task_lifecycle", 0, "product-design", "durable-async-task-lifecycle", "run-cancel-and-inspect-task"),
    "R-011": _canonical("product", "security", "security", "market_provider_secrets_are_protected", 1, "market-data-charting", "market-secret-protection", "save-market-token", "market-source-call-fails"),
    "R-012": _canonical("product", "formula", "user_visible", "formula_types_have_typed_outputs", 2, "formula-system", "formula-types-and-outputs", "evaluate-technical-indicator", "evaluate-trading-system"),
    "R-013": _canonical("product", "backtest", "architecture", "backtest_uses_builtin_or_saved_signals", 3, "backtesting-reporting", "builtin-and-custom-signal-backtest", "backtest-builtin-macd", "backtest-custom-formula"),
    "R-014": _canonical("product", "backtest", "user_visible", "single_and_pool_backtests", 3, "backtesting-reporting", "single-and-pool-backtests", "run-single-stock", "run-stock-pool"),
    "R-015": _canonical("product", "backtest", "user_visible", "realized_statistics_are_explicit", 3, "backtesting-reporting", "win-rate-and-statistics", "calculate-win-rate", "inspect-detailed-statistics"),
    "R-016": _canonical("product", "backtest", "architecture", "backtest_snapshot_is_reproducible", 3, "backtesting-reporting", "reproducible-backtest-snapshot", "rerun-same-snapshot"),
    "R-017": _canonical("product", "backtest", "architecture", "signals_execute_next_open", 3, "backtesting-reporting", "signal-confirmation-and-execution-time", "daily-buy-signal", "weekly-sell-signal"),
    "R-018": _canonical("product", "backtest", "architecture", "a_share_execution_constraints", 3, "backtesting-reporting", "a-share-execution-constraints", "t-plus-one-blocks-sale", "suspension-blocks-fill", "price-limit-blocks-fill"),
    "R-019": _canonical("product", "backtest", "user_visible", "pool_backtest_is_async_and_cancellable", 3, "backtesting-reporting", "asynchronous-pool-task", "run-pool-task", "cancel-pool-task"),
    "R-020": _canonical("product", "backtest", "architecture", "costs_and_slippage_are_disclosed", 3, "backtesting-reporting", "costs-and-slippage", "calculate-net-trade-return"),
    "R-021": _canonical("product", "market", "architecture", "pluggable_sources_route_by_category", 1, "market-data-charting", "pluggable-sources-and-priority", "primary-source-succeeds", "primary-source-fails", "inspect-tushare-configuration"),
    "R-022": _canonical("product", "analysis", "user_visible", "analysis_report_aligns_claims_and_evidence", 4, "multi-agent-analysis", "side-by-side-conclusion-and-evidence", "open-completed-report"),
    "R-023": _canonical("product", "analysis", "architecture", "analysis_claims_are_traceable", 4, "multi-agent-analysis", "evidence-and-source-traceability", "inspect-claim-evidence", "source-data-is-missing"),
    "R-024": _canonical("product", "analysis", "user_visible", "analysis_is_research_only", 4, "multi-agent-analysis", "first-release-analysis-boundary", "view-final-report"),
    "R-025": _canonical("product", "platform", "user_visible", "private_single_user_web_access", 0, "market-data-charting", "desktop-first-single-user-web-workstation", "open-workstation-in-browser", "open-workstation-on-tablet"),
    "R-026": _canonical("product", "backtest", "user_visible", "open_trades_stay_out_of_realized_results", 3, "backtesting-reporting", "open-trade-handling", "end-with-open-position"),
    "R-027": _canonical("product", "formula", "user_visible", "three_column_formula_studio", 2, "formula-system", "three-column-formula-editor", "insert-function", "preview-formula"),
    "R-028": _canonical("product", "market", "user_visible", "professional_terminal_visual_structure", 1, "market-data-charting", "professional-terminal-visual-structure", "open-market-workspace"),
    "R-029": _canonical("product", "formula", "user_visible", "formula_editing_assistance", 2, "formula-system", "formula-editing-assistance", "use-function-assistance", "locate-formula-error"),
    "R-030": _canonical("product", "backtest", "architecture", "first_release_has_no_live_trading", 3, "backtesting-reporting", "first-release-trading-boundary", "inspect-backtest-actions"),
    "R-031": _canonical("product", "market", "user_visible", "preset_and_custom_stock_pools", 1, "market-data-charting", "preset-and-custom-pools", "use-preset-pool", "save-custom-pool"),
    "R-032": _canonical("product", "analysis", "user_visible", "pluggable_model_configuration", 4, "multi-agent-analysis", "pluggable-model-interfaces", "configure-domestic-model", "switch-model-configuration"),
    "R-033": _canonical("product", "formula", "security", "future_or_repainting_formula_is_blocked", 2, "formula-system", "forbid-future-and-repainting", "detect-future-function", "detect-signal-drift"),
    "R-034": _canonical("product", "security", "security", "external_content_is_untrusted_data", 4, "multi-agent-analysis", "external-content-prompt-injection-defense", "news-contains-instructions"),
    "R-035": _canonical("product", "platform", "architecture", "modular_monolith_with_worker_boundary", 0, "product-design", "modular-monolith-worker-and-persistence", "run-heavy-work-in-worker"),
    "R-036": _canonical("product", "market", "architecture", "provenance_prevents_silent_splicing", 1, "market-data-charting", "unified-model-and-provenance", "display-data-source", "switch-series-source"),
    "R-037": _canonical("product", "formula", "architecture", "preview_chart_backtest_share_formula_result", 2, "formula-system", "single-formula-calculation-source", "preview-to-backtest"),
    "R-038": _canonical("product", "platform", "user_visible", "stock_desk_product_identity", 0, "market-data-charting", "product-identity", "open-product"),
    "R-039": _canonical("product", "formula", "architecture", "versioned_tdx_compatibility_subset", 2, "formula-system", "common-tdx-syntax-compatibility", "paste-supported-formula", "use-unsupported-function"),
    "R-040": _canonical("product", "backtest", "user_visible", "guided_backtest_configuration", 3, "backtesting-reporting", "guided-backtest-wizard", "configure-and-review-backtest"),
    "R-041": _canonical("product", "market", "user_visible", "local_tdx_is_safe_fallback", 1, "market-data-charting", "local-tdx-fallback", "configure-valid-directory", "local-directory-unavailable"),
    "R-042": _canonical("product", "market", "user_visible", "local_data_updates_are_observable", 1, "market-data-charting", "local-storage-and-update", "manually-update-data", "scheduled-update-partially-fails"),
    "R-043": _canonical("product", "backtest", "architecture", "backtest_supports_three_periods", 3, "backtesting-reporting", "backtest-periods", "run-weekly-backtest", "run-sixty-minute-backtest"),
    "R-044": _canonical("product", "formula", "security", "formula_execution_is_controlled", 2, "formula-system", "controlled-formula-execution", "reject-arbitrary-code"),
    "R-045": _canonical("product", "backtest", "architecture", "one_position_and_pending_order_state", 3, "backtesting-reporting", "single-position-and-pending-orders", "repeated-buy-while-held", "opposite-signal-cancels-pending-buy"),
    "R-046": _canonical("product", "analysis", "user_visible", "on_demand_analysis_is_persisted", 4, "multi-agent-analysis", "on-demand-single-stock-report", "start-complete-analysis", "open-historical-report"),
    "R-047": _canonical("product", "analysis", "user_visible", "rating_has_five_levels_and_confidence", 4, "multi-agent-analysis", "five-level-rating-and-confidence", "produce-final-conclusion"),
    "R-048": _canonical("product", "analysis", "user_visible", "retry_preserves_partial_and_parent_runs", 4, "multi-agent-analysis", "retry-and-partial-report", "noncritical-module-fails", "retry-failed-module"),
    "R-049": _canonical("product", "analysis", "architecture", "missing_critical_evidence_suppresses_rating", 4, "multi-agent-analysis", "no-rating-without-critical-evidence", "all-critical-data-unavailable"),
    "R-050": _canonical("product", "platform", "architecture", "failures_are_diagnostic_and_bounded", 5, "product-design", "bounded-diagnostic-failure-handling", "inspect-and-recover-failure"),
    "R-051": _canonical("product", "security", "security", "model_secrets_and_logs_are_protected", 4, "multi-agent-analysis", "model-secret-and-log-protection", "save-model-key", "model-call-fails"),
    "R-052": _canonical("product", "formula", "security", "invalid_formula_cannot_be_saved", 2, "formula-system", "validate-before-save", "formula-passes-validation", "formula-fails-validation"),
    "R-053": _canonical("product", "backtest", "user_visible", "conclusion_first_backtest_report", 3, "backtesting-reporting", "conclusion-first-report", "open-completed-backtest-report"),
    "R-054": _canonical("product", "performance", "performance", "cached_chart_is_interactive_within_two_seconds", 1, "market-data-charting", "market-interface-performance", "open-cached-daily-chart", "wait-for-external-data"),
    "R-055": _canonical("product", "performance", "performance", "formula_preview_finishes_within_three_seconds", 2, "formula-system", "formula-preview-performance", "preview-ten-years-daily"),
    "R-056": _canonical("product", "market", "architecture", "first_release_market_scope_is_bounded", 1, "market-data-charting", "first-release-market-boundary", "inspect-first-release-market-entries"),
    "R-057": _canonical("product", "formula", "architecture", "first_release_formula_scope_is_bounded", 2, "formula-system", "first-release-formula-boundary", "inspect-formula-types"),
    "R-058": _canonical("product", "backtest", "architecture", "pool_results_are_independent_samples", 3, "backtesting-reporting", "independent-signal-sample-model", "simultaneous-pool-buy-signals"),
    "R-059": _canonical("product", "analysis", "architecture", "analysis_is_decoupled_from_formula_backtest", 4, "multi-agent-analysis", "analysis-decoupled-from-formula-and-backtest", "report-has-technical-opinion"),
    "R-060": _canonical("product", "performance", "performance", "single_backtest_finishes_within_five_seconds", 3, "backtesting-reporting", "single-backtest-performance", "run-ten-year-single-stock", "run-all-a-pool"),
    "R-061": _canonical("operational", "operations", "operational", "approved_local_requirements_store", 5, "delivery-governance", "approved-requirements-authority", "reconcile-release-coverage"),
    "R-062": _canonical("operational", "operations", "operational", "execute_all_confirmed_plans", 5, "delivery-governance", "complete-confirmed-plan-scope", "confirm-full-scope-completion"),
    "R-063": _canonical("operational", "operations", "operational", "stages_are_independently_deliverable", 5, "delivery-governance", "independent-stage-execution-cycle", "plan-implement-verify-submit"),
    "R-064": _canonical("operational", "operations", "operational", "every_stage_is_published_and_released", 5, "delivery-governance", "stage-publication-and-release", "push-review-merge-tag-release"),
    "R-065": _canonical("operational", "publication", "publication", "private_inputs_stay_out_of_public_history", 5, "publication-boundary", "private-input-exclusion", "audit-public-tree-and-history"),
    "R-066": _canonical("operational", "publication", "publication", "open_source_repository_is_release_ready", 5, "release-publication", "open-source-repository-quality", "inspect-community-security-release-configuration"),
    "R-067": _canonical("operational", "operations", "operational", "stock_desk_public_repo_and_remote_identity", 5, "delivery-governance", "public-repository-and-remote-identity", "verify-stock-desk-repository-and-remote"),
    "R-068": _canonical("operational", "operations", "operational", "delivery_uses_canonical_local_repository", 5, "delivery-governance", "canonical-delivery-checkout", "verify-session-checkout"),
    "R-069": _canonical("operational", "operations", "operational", "release_commits_use_congbao_identity", 5, "delivery-governance", "git-object-identity", "verify-commits-and-tag"),
    "R-070": _canonical("operational", "operations", "operational", "github_ssh_key_and_tag_signing", 5, "delivery-governance", "github-ssh-and-tag-signing", "verify-remote-key-and-signed-tag"),
    "R-071": _canonical("operational", "operations", "operational", "browser_controls_pr_merge_and_release", 5, "delivery-governance", "browser-publication-protocol", "publish-and-verify-release"),
    "R-072": _canonical("operational", "publication", "publication", "final_release_artifacts_are_audited", 5, "release-publication", "final-release-audit", "verify-main-history-coverage-assets-and-signatures"),
    "R-073": _canonical("operational", "publication", "publication", "readmes_are_concise_reciprocal_and_verified", 5, "release-publication", "verified-reciprocal-readme", "verify-readme-pair"),
    "R-074": _canonical("operational", "publication", "publication", "every_feature_has_real_wiki_screenshot", 5, "release-publication", "feature-wiki-screenshots-and-steps", "validate-feature-page-image-and-steps"),
    "R-075": _canonical("operational", "publication", "publication", "wiki_pages_are_reciprocal_bilingual_pairs", 5, "release-publication", "reciprocal-bilingual-wiki", "navigate-language-pair"),
    "R-076": _canonical("operational", "publication", "publication", "installers_need_no_source_checkout", 5, "release-packaging", "source-checkout-free-installation", "install-and-first-launch-windows", "install-and-first-launch-macos"),
    "R-077": _canonical("operational", "platform", "user_visible", "responsive_icon_navigation_never_overlaps", 5, "release-quality", "strengthened-all-route-responsive-ui", "verify-all-routes-ratios-icons-and-nonoverlap"),
}

# fmt: on


def _canonical_non_goal(
    category: str,
    behavior_key: str,
    owning_stage: int,
    requirement: str,
) -> dict[str, Any]:
    return {
        "category": category,
        "kind": "non_goal",
        "behavior_key": behavior_key,
        "owning_stage": owning_stage,
        "capability": "product-boundaries",
        "requirement": requirement,
        "scenario": "inventory-public-surfaces",
    }


# Machine-maintained canonical data; registry-shape tests lock every entry.
# fmt: off
CANONICAL_NON_GOALS: dict[str, dict[str, Any]] = {
    "N-001": _canonical_non_goal("backtest", "no_broker_or_live_ordering", 3, "no-live-trading"),
    "N-002": _canonical_non_goal("backtest", "no_shared_capital_portfolio", 3, "no-shared-capital-portfolio"),
    "N-003": _canonical_non_goal("market", "no_realtime_tick_or_level2_data", 1, "no-realtime-tick-level2"),
    "N-004": _canonical_non_goal("analysis", "no_target_price_or_specific_allocation", 4, "no-target-price-allocation"),
    "N-005": _canonical_non_goal("platform", "no_second_native_product_interface", 5, "no-native-product-ui"),
    "N-006": _canonical_non_goal("platform", "no_accounts_rbac_subscription_billing", 0, "no-account-or-billing-system"),
    "N-007": _canonical_non_goal("market", "no_dynamic_market_screening", 1, "no-dynamic-screening"),
    "N-008": _canonical_non_goal("formula", "no_condition_selection_or_color_k", 2, "no-condition-selection-color-k"),
    "N-009": _canonical_non_goal("market", "no_drawing_multistock_or_linked_periods", 1, "no-drawing-multistock-linked-periods"),
    "N-010": _canonical_non_goal("formula", "no_ai_formula_assistance", 2, "no-ai-formula-assistance"),
}

# fmt: on
class ValidationError(ValueError):
    """A public-safe, deterministic manifest validation failure."""


class BoundedUniqueKeyLoader(yaml.SafeLoader):  # type: ignore[misc]
    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.coverage_node_count = 0
        self.coverage_depth = 0

    def compose_node(
        self, parent: yaml.nodes.Node | None, index: int | None
    ) -> yaml.nodes.Node:
        event = self.peek_event()
        if isinstance(event, AliasEvent) or getattr(event, "anchor", None) is not None:
            raise ValidationError("YAML aliases are not allowed")
        self.coverage_node_count += 1
        if self.coverage_node_count > MAX_YAML_NODES:
            raise ValidationError(f"YAML node limit exceeded ({MAX_YAML_NODES})")
        self.coverage_depth += 1
        if self.coverage_depth > MAX_YAML_DEPTH:
            raise ValidationError(f"YAML depth limit exceeded ({MAX_YAML_DEPTH})")
        try:
            return super().compose_node(parent, index)
        finally:
            self.coverage_depth -= 1

    def construct_mapping(
        self,
        node: yaml.nodes.MappingNode,
        deep: bool = False,
    ) -> dict[object, object]:
        self.flatten_mapping(node)
        result: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError as exc:
                raise ValidationError(
                    "YAML mapping keys must be scalar and hashable"
                ) from exc
            if duplicate:
                raise ValidationError(f"duplicate YAML key: {key!r}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _expect_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValidationError(f"{label} must be a mapping with string keys")
    return value


def _expect_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be a list")
    return value


def _expect_exact_fields(
    value: Mapping[str, Any], expected: set[str], label: str
) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise ValidationError(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        raise ValidationError(f"{label} is missing fields: {', '.join(missing)}")


def _expect_text(
    value: object,
    label: str,
    *,
    minimum: int = 1,
    maximum: int = MAX_TEXT_LENGTH,
) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a string")
    if value != value.strip() or len(value) < minimum or len(value) > maximum:
        raise ValidationError(
            f"{label} must be nonempty, trimmed, and {minimum}..{maximum} characters"
        )
    return value


def _expect_enum(value: object, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(f"{label} must be one of: {', '.join(sorted(allowed))}")
    return value


def _reject_publication_boundary(value: object, location: str = "root") -> None:
    if isinstance(value, str):
        if FORBIDDEN_PUBLIC_TEXT.search(value):
            raise ValidationError(f"publication-boundary string at {location}")
    elif isinstance(value, dict):
        for key, child in value.items():
            _reject_publication_boundary(key, f"{location}.<key>")
            _reject_publication_boundary(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_publication_boundary(child, f"{location}[{index}]")


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read requirement manifest: {exc}") from exc
    if len(payload) > MAX_YAML_BYTES:
        raise ValidationError(f"YAML byte limit exceeded ({MAX_YAML_BYTES})")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("requirement manifest must be UTF-8") from exc
    loader: BoundedUniqueKeyLoader | None = None
    try:
        loader = BoundedUniqueKeyLoader(text)
        loaded = loader.get_single_data()
    except ValidationError:
        raise
    except yaml.YAMLError as exc:
        raise ValidationError(f"invalid YAML: {exc}") from exc
    finally:
        if loader is not None:
            loader.dispose()
    matrix = _expect_mapping(loaded, "manifest")
    _reject_publication_boundary(matrix)
    return matrix


def _tracked_paths(repo_root: Path) -> frozenset[str]:
    try:
        output = subprocess.check_output(
            ["git", "-C", os.fspath(repo_root), "ls-files", "-z"],
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as exc:
        raise ValidationError("unable to enumerate tracked repository files") from exc
    return frozenset(os.fsdecode(item) for item in output.split(b"\0") if item)


def _validate_repo_path(
    path_value: object,
    repo_root: Path,
    *,
    existing: bool,
    tracked_paths: frozenset[str],
) -> tuple[str, Path]:
    path = _expect_text(path_value, "evidence.path", maximum=240)
    if "*" in path or "?" in path or "[" in path or "\\" in path:
        raise ValidationError(f"evidence path is unsafe or broad: {path}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or path.endswith("/"):
        raise ValidationError(
            f"evidence path must be a safe repo-relative file: {path}"
        )
    if any(part.startswith("-") for part in pure.parts):
        raise ValidationError(
            f"evidence path contains an option-shaped component: {path}"
        )
    candidate = repo_root
    for index, part in enumerate(pure.parts):
        candidate /= part
        if candidate.is_symlink():
            raise ValidationError(
                f"evidence path contains a symlinked path component: {path}"
            )
        if (
            index < len(pure.parts) - 1
            and candidate.exists()
            and not candidate.is_dir()
        ):
            raise ValidationError(f"evidence path parent must be a directory: {path}")
    if candidate.exists() and not candidate.is_file():
        raise ValidationError(f"evidence path must identify one file: {path}")
    try:
        candidate.resolve(strict=False).relative_to(repo_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValidationError(f"evidence path escapes repository: {path}") from exc
    if existing:
        if not candidate.is_file() or path not in tracked_paths:
            raise ValidationError(
                f"existing evidence must be a tracked regular file: {path}"
            )
    return path, candidate


def _public_tree_gate(
    repo_root: Path,
    tracked_paths: frozenset[str] | None = None,
) -> None:
    forbidden = (
        "openspec/",
        "docs/superpowers/",
        "outputs/",
        ".agents/",
        ".codex/",
        ".superpowers/",
        "work/",
    )
    tracked = _tracked_paths(repo_root) if tracked_paths is None else tracked_paths
    blocked = sorted(path for path in tracked if path.startswith(forbidden))
    if blocked:
        raise ValidationError("public-tree gate found internal tracked paths")


def _rendered_markdown(document: str) -> str:
    without_comments = re.sub(r"<!--.*?-->", "", document, flags=re.DOTALL)
    rendered: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    for line in without_comments.splitlines():
        stripped = line.lstrip()
        fence = re.match(r"(?P<fence>`{3,}|~{3,})", stripped)
        if fence_character is None:
            if fence is None:
                rendered.append(line)
            else:
                marker = fence.group("fence")
                fence_character = marker[0]
                fence_length = len(marker)
            continue
        if (
            fence is not None
            and fence.group("fence")[0] == fence_character
            and len(fence.group("fence")) >= fence_length
            and stripped[len(fence.group("fence")) :].strip() == ""
        ):
            fence_character = None
            fence_length = 0
    visible = "\n".join(rendered)
    without_inline_code: list[str] = []
    index = 0
    while index < len(visible):
        if visible[index] != "`":
            without_inline_code.append(visible[index])
            index += 1
            continue
        delimiter_end = index
        while delimiter_end < len(visible) and visible[delimiter_end] == "`":
            delimiter_end += 1
        delimiter = visible[index:delimiter_end]
        closing = re.search(
            rf"(?<!`)({re.escape(delimiter)})(?!`)",
            visible[delimiter_end:],
        )
        if closing is None:
            without_inline_code.append(delimiter)
            index = delimiter_end
            continue
        index = delimiter_end + closing.end()
        without_inline_code.append(" ")
    return "".join(without_inline_code)


def _is_escaped(document: str, index: int) -> bool:
    backslashes = 0
    while index > 0 and document[index - 1] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _markdown_link_targets(document: str) -> frozenset[str]:
    rendered = _rendered_markdown(document)
    return frozenset(
        PurePosixPath(match.group("target").strip("<>")).as_posix()
        for match in MARKDOWN_LINK.finditer(rendered)
        if not _is_escaped(rendered, match.start())
    )


def _bilingual_readme_gate(
    repo_root: Path,
    _tracked_paths_for_run: frozenset[str] | None = None,
) -> None:
    english = (repo_root / "README.md").read_text(encoding="utf-8")
    chinese = (repo_root / "README.zh-CN.md").read_text(encoding="utf-8")
    if "README.zh-CN.md" not in _markdown_link_targets(
        english
    ) or "README.md" not in _markdown_link_targets(chinese):
        raise ValidationError(
            "bilingual-readme gate requires reciprocal Markdown links"
        )


GATE_REGISTRY: dict[
    str,
    Callable[[Path, frozenset[str] | None], None],
] = {
    "public-tree": _public_tree_gate,
    "bilingual-readme": _bilingual_readme_gate,
}


def _validate_source_refs(value: object, item_id: str) -> None:
    refs = _expect_list(value, f"{item_id}.source_refs")
    if not refs or len(refs) > 8:
        raise ValidationError(
            f"{item_id}.source_refs must contain 1..8 semantic references"
        )
    for index, raw in enumerate(refs):
        ref = _expect_mapping(raw, f"{item_id}.source_refs[{index}]")
        _expect_exact_fields(ref, SOURCE_REF_FIELDS, f"{item_id}.source_refs[{index}]")
        for field in SOURCE_REF_FIELDS:
            semantic = _expect_text(
                ref[field], f"{item_id}.source_refs[{index}].{field}", maximum=80
            )
            if not SEMANTIC_KEY.fullmatch(semantic):
                raise ValidationError(
                    f"{item_id} source_refs {field} must be a semantic key"
                )


def _validate_canonical_requirement(item: Mapping[str, Any], item_id: str) -> None:
    canonical = CANONICAL_REQUIREMENTS[item_id]
    for field in ("category", "kind", "behavior_key", "owning_stage"):
        if item[field] != canonical[field]:
            raise ValidationError(
                f"{item_id}.{field} does not match the canonical requirement registry"
            )
    seen: set[tuple[str, str, str]] = set()
    for ref in item["source_refs"]:
        key = (ref["capability"], ref["requirement"], ref["scenario"])
        if key in seen:
            raise ValidationError(
                f"{item_id}.source_refs contains a duplicate canonical scenario"
            )
        seen.add(key)
        if key[:2] != (canonical["capability"], canonical["requirement"]):
            raise ValidationError(
                f"{item_id}.source_refs does not match its canonical semantic requirement"
            )
        if key[2] not in canonical["scenarios"]:
            raise ValidationError(
                f"{item_id}.source_refs contains a non-canonical scenario"
            )
    actual_scenarios = {ref["scenario"] for ref in item["source_refs"]}
    if actual_scenarios != canonical["scenarios"]:
        raise ValidationError(
            f"{item_id}.source_refs must equal the exact canonical scenario set"
        )


def _validate_canonical_non_goal(item: Mapping[str, Any], item_id: str) -> None:
    canonical = CANONICAL_NON_GOALS[item_id]
    for field in ("category", "kind", "behavior_key", "owning_stage"):
        if item[field] != canonical[field]:
            raise ValidationError(
                f"{item_id}.{field} does not match the canonical non-goal registry"
            )
    expected_ref = {
        "capability": canonical["capability"],
        "requirement": canonical["requirement"],
        "scenario": canonical["scenario"],
    }
    if item["source_refs"] != [expected_ref]:
        raise ValidationError(
            f"{item_id}.source_refs does not match the canonical non-goal registry"
        )


def _validate_evidence(
    value: object,
    item: Mapping[str, Any],
    repo_root: Path,
    mode: str,
    tracked_paths: frozenset[str],
) -> list[dict[str, Any]]:
    item_id = str(item["id"])
    evidence_list = _expect_list(value, f"{item_id}.evidence")
    if not evidence_list or len(evidence_list) > 12:
        raise ValidationError(f"{item_id}.evidence must contain 1..12 assertions")
    validated: list[dict[str, Any]] = []
    for index, raw in enumerate(evidence_list):
        evidence = _expect_mapping(raw, f"{item_id}.evidence[{index}]")
        label = f"{item_id}.evidence[{index}]"
        state = _expect_enum(evidence.get("state"), STATES, f"{label}.state")
        runner = _expect_enum(evidence.get("runner"), RUNNERS, f"{label}.runner")
        kind = _expect_enum(evidence.get("kind"), EVIDENCE_KINDS, f"{label}.kind")
        compatible_kinds = {
            "pytest": {
                "acceptance",
                "integration",
                "contract",
                "security",
                "performance",
                "absence",
            },
            "vitest": {"component", "acceptance", "integration", "absence"},
            "playwright": {"acceptance", "integration", "performance", "absence"},
        }
        if runner == "gate" and kind != "gate":
            raise ValidationError(f"{label} gate runner requires gate kind")
        if runner in compatible_kinds and kind not in compatible_kinds[runner]:
            raise ValidationError(
                f"{label} {runner} runner does not support {kind} kind"
            )
        if state != "manual" and (runner == "manual" or kind == "manual"):
            raise ValidationError(
                f"{label} non-manual evidence cannot use manual runner or kind"
            )
        if state == "manual":
            _expect_exact_fields(
                evidence, MANUAL_EVIDENCE_FIELDS, f"{label} manual evidence fields"
            )
            if (
                runner != "manual"
                or kind != "manual"
                or item["kind"] not in {"operational", "publication"}
            ):
                raise ValidationError(
                    f"{label} manual evidence is limited to operational/publication items"
                )
            for field in ("procedure_id", "artifact_kind", "required_by_gate"):
                semantic = _expect_text(evidence[field], f"{label}.{field}", maximum=80)
                if not SEMANTIC_KEY.fullmatch(semantic):
                    raise ValidationError(f"{label}.{field} must be a semantic key")
            _expect_text(
                evidence["final_artifact_contract"],
                f"{label}.final_artifact_contract",
                minimum=20,
                maximum=500,
            )
            if not isinstance(evidence["completed"], bool):
                raise ValidationError(f"{label}.completed must be boolean")
        elif runner == "gate":
            _expect_exact_fields(evidence, GATE_EVIDENCE_FIELDS, label)
            gate_id = _expect_text(evidence["gate_id"], f"{label}.gate_id", maximum=80)
            if gate_id not in GATE_REGISTRY:
                raise ValidationError(f"{label} must use a registered gate_id")
        else:
            _expect_exact_fields(evidence, SELECTOR_EVIDENCE_FIELDS, label)
            _validate_repo_path(
                evidence["path"],
                repo_root,
                existing=state == "existing",
                tracked_paths=tracked_paths,
            )
            selector = _expect_text(
                evidence["selector"], f"{label}.selector", maximum=300
            )
            if runner == "pytest" and "::" not in selector:
                raise ValidationError(f"{label} requires an assertion-level selector")
            if runner == "pytest" and selector.partition("::")[0] != evidence["path"]:
                raise ValidationError(
                    f"{label} selector path must match its evidence file"
                )
            if runner == "pytest" and not selector.rsplit("::", maxsplit=1)[-1].split(
                "[", maxsplit=1
            )[0].startswith("test_"):
                raise ValidationError(
                    f"{label} requires a terminal function-level selector"
                )
            if runner in {"vitest", "playwright"} and len(selector) < 8:
                raise ValidationError(f"{label} requires an assertion-level selector")
            if any(
                token in selector for token in ("*", "?", "[all]")
            ) or selector.endswith("/"):
                raise ValidationError(f"{label} requires an assertion-level selector")
        _expect_text(
            evidence["assertion"], f"{label}.assertion", minimum=20, maximum=500
        )
        validated.append(evidence)
    return validated


def _validate_evidence_strength(
    item: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]]
) -> None:
    item_id = str(item["id"])
    kinds = {entry["kind"] for entry in evidence}
    if item["kind"] == "user_visible" and not any(
        entry["state"] in {"existing", "planned"}
        and entry["kind"] in {"acceptance", "integration", "component"}
        and entry["runner"] in {"pytest", "vitest", "playwright"}
        for entry in evidence
    ):
        raise ValidationError(
            f"{item_id} user-visible behavior lacks acceptance/integration/component proof"
        )
    if item["kind"] == "performance" and "performance" not in kinds:
        raise ValidationError(
            f"{item_id} performance behavior lacks performance evidence"
        )
    if item["kind"] == "security" and not kinds.intersection(
        {"contract", "security", "integration"}
    ):
        raise ValidationError(
            f"{item_id} security behavior lacks contract/security/integration evidence"
        )
    if item["kind"] == "non_goal" and "absence" not in kinds:
        raise ValidationError(f"{item_id} non-goal lacks absence/inventory evidence")


def listed_test_titles(runner: str, listing: str) -> set[str]:
    separator = " > " if runner == "vitest" else " › "
    return {
        line.strip().rsplit(separator, maxsplit=1)[-1]
        for line in listing.splitlines()
        if separator in line
    }


def _validate_item(
    raw: object,
    expected_id: str,
    repo_root: Path,
    mode: str,
    behavior_keys: set[str],
    tracked_paths: frozenset[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    item = _expect_mapping(raw, expected_id)
    _expect_exact_fields(item, ITEM_FIELDS, expected_id)
    item_id = _expect_text(item["id"], f"{expected_id}.id", minimum=5, maximum=5)
    if item_id != expected_id:
        raise ValidationError(f"{expected_id}.id must equal {expected_id}")
    _expect_enum(item["category"], CATEGORIES, f"{expected_id}.category")
    _expect_enum(item["kind"], KINDS, f"{expected_id}.kind")
    behavior = _expect_text(
        item["behavior_key"], f"{expected_id}.behavior_key", maximum=80
    )
    if not BEHAVIOR_KEY.fullmatch(behavior):
        raise ValidationError(f"{expected_id}.behavior_key is invalid")
    if behavior in behavior_keys:
        raise ValidationError(f"duplicate behavior_key: {behavior}")
    behavior_keys.add(behavior)
    _expect_text(
        item["acceptance"],
        f"{expected_id}.acceptance",
        minimum=30,
        maximum=MAX_TEXT_LENGTH,
    )
    _validate_source_refs(item["source_refs"], expected_id)
    stage = item["owning_stage"]
    if isinstance(stage, bool) or not isinstance(stage, int) or not 0 <= stage <= 5:
        raise ValidationError(
            f"{expected_id}.owning_stage must be an integer from 0 through 5"
        )
    _expect_enum(item["status"], STATUSES, f"{expected_id}.status")
    if expected_id.startswith("R-"):
        _validate_canonical_requirement(item, expected_id)
    else:
        _validate_canonical_non_goal(item, expected_id)
    evidence = _validate_evidence(
        item["evidence"],
        item,
        repo_root,
        mode,
        tracked_paths,
    )
    _validate_evidence_strength(item, evidence)
    if item["status"] == "verified" and any(
        entry["state"] == "planned"
        or (entry["state"] == "manual" and not entry["completed"])
        for entry in evidence
    ):
        raise ValidationError(
            f"verified item {expected_id} contains planned or incomplete manual evidence"
        )
    return item, evidence


def _run_collection_command(
    command: list[str],
    *,
    repo_root: Path,
    runner: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=COLLECTION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValidationError(f"{runner} selector collection timed out") from exc


def _collect_existing_selectors(
    items: Iterable[Mapping[str, Any]],
    repo_root: Path,
    tracked_paths: frozenset[str] | None = None,
) -> None:
    pytest_selectors: list[str] = []
    frontend: dict[tuple[str, str], list[str]] = {}
    gates: set[str] = set()
    for item in items:
        for evidence in item["evidence"]:
            if evidence["state"] != "existing":
                continue
            runner = evidence["runner"]
            if runner == "pytest":
                pytest_selectors.append(evidence["selector"])
            elif runner in {"vitest", "playwright"}:
                frontend.setdefault((runner, evidence["path"]), []).append(
                    evidence["selector"]
                )
            elif runner == "gate":
                gates.add(evidence["gate_id"])
    if pytest_selectors:
        unique = list(dict.fromkeys(pytest_selectors))
        result = _run_collection_command(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", *unique],
            repo_root=repo_root,
            runner="pytest",
        )
        if result.returncode != 0:
            raise ValidationError(
                f"pytest selectors did not collect:\n{result.stdout}{result.stderr}"
            )
    for (runner, path), selectors in sorted(frontend.items()):
        command = (
            [
                "pnpm",
                "--dir",
                "web",
                "exec",
                "vitest",
                "list",
                path.removeprefix("web/"),
            ]
            if runner == "vitest"
            else ["pnpm", "exec", "playwright", "test", "--list", path]
        )
        result = _run_collection_command(
            command,
            repo_root=repo_root,
            runner=runner,
        )
        listing = result.stdout + result.stderr
        if result.returncode != 0:
            raise ValidationError(f"{runner} could not list {path}:\n{listing}")
        titles = listed_test_titles(runner, listing)
        missing = sorted(set(selectors) - titles)
        if missing:
            raise ValidationError(
                f"{runner} selectors not listed in {path}: {', '.join(missing)}"
            )
    for gate_id in sorted(gates):
        GATE_REGISTRY[gate_id](repo_root, tracked_paths)


def validate_manifest(
    matrix: dict[str, Any],
    *,
    repo_root: Path,
    mode: str,
    verify_selectors: bool = True,
) -> dict[str, int]:
    if not isinstance(mode, str) or mode not in {"mapping", "release"}:
        raise ValidationError("mode must be mapping or release")
    _reject_publication_boundary(matrix)
    _expect_exact_fields(matrix, ROOT_FIELDS, "manifest")
    if type(matrix["schema_version"]) is not int or matrix["schema_version"] != 1:
        raise ValidationError("schema_version must be integer 1")
    requirements = _expect_list(matrix["requirements"], "requirements")
    non_goals = _expect_list(matrix["non_goals"], "non_goals")
    expected_requirements = [f"R-{number:03d}" for number in range(1, 78)]
    expected_non_goals = [f"N-{number:03d}" for number in range(1, 11)]
    if len(requirements) != len(expected_requirements):
        raise ValidationError("requirements must contain exactly R-001 through R-077")
    if len(non_goals) != len(expected_non_goals):
        raise ValidationError("non_goals must contain exactly N-001 through N-010")
    if list(CANONICAL_REQUIREMENTS) != expected_requirements:
        raise ValidationError(
            "canonical requirement registry must contain exactly R-001 through R-077"
        )
    if list(CANONICAL_NON_GOALS) != expected_non_goals:
        raise ValidationError(
            "canonical non-goal registry must contain exactly N-001 through N-010"
        )
    canonical_semantics = {
        (entry["capability"], entry["requirement"])
        for entry in CANONICAL_REQUIREMENTS.values()
    }
    if len(canonical_semantics) != 77:
        raise ValidationError(
            "canonical requirement registry contains duplicate semantics"
        )
    canonical_non_goal_semantics = {
        (entry["capability"], entry["requirement"])
        for entry in CANONICAL_NON_GOALS.values()
    }
    if len(canonical_non_goal_semantics) != 10:
        raise ValidationError(
            "canonical non-goal registry contains duplicate semantics"
        )
    if (
        sum(
            entry["semantic_type"] == "product"
            for entry in CANONICAL_REQUIREMENTS.values()
        )
        != 60
        or sum(
            entry["semantic_type"] == "operational"
            for entry in CANONICAL_REQUIREMENTS.values()
        )
        != 17
    ):
        raise ValidationError(
            "canonical requirement registry must contain 60 product and 17 operational semantics"
        )
    tracked_paths = _tracked_paths(repo_root)
    behavior_keys: set[str] = set()
    validated: list[dict[str, Any]] = []
    for raw, expected_id in zip(requirements, expected_requirements, strict=True):
        item, _ = _validate_item(
            raw,
            expected_id,
            repo_root,
            mode,
            behavior_keys,
            tracked_paths,
        )
        if item["kind"] == "non_goal":
            raise ValidationError(f"{expected_id} cannot use non_goal kind")
        validated.append(item)
    for raw, expected_id in zip(non_goals, expected_non_goals, strict=True):
        item, _ = _validate_item(
            raw,
            expected_id,
            repo_root,
            mode,
            behavior_keys,
            tracked_paths,
        )
        if item["kind"] != "non_goal":
            raise ValidationError(f"{expected_id} must use non_goal kind")
        validated.append(item)
    _reject_publication_boundary(matrix)
    if mode == "release":
        planned = sorted(
            item["id"]
            for item in validated
            if any(evidence["state"] == "planned" for evidence in item["evidence"])
        )
        incomplete_manual = sorted(
            item["id"]
            for item in validated
            if any(
                evidence["state"] == "manual" and not evidence["completed"]
                for evidence in item["evidence"]
            )
        )
        release_errors: list[str] = []
        if planned:
            release_errors.append("planned evidence: " + ", ".join(planned))
        if incomplete_manual:
            release_errors.append(
                "incomplete manual evidence: " + ", ".join(incomplete_manual)
            )
        if release_errors:
            raise ValidationError("; ".join(release_errors))
    if verify_selectors:
        _collect_existing_selectors(validated, repo_root, tracked_paths)
    return {
        "requirements": len(requirements),
        "non_goals": len(non_goals),
        "planned": sum(
            evidence["state"] == "planned"
            for item in validated
            for evidence in item["evidence"]
        ),
        "manual": sum(
            evidence["state"] == "manual"
            for item in validated
            for evidence in item["evidence"]
        ),
    }


def verify_document_digest(manifest_path: Path, document_path: Path) -> None:
    expected = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    try:
        document = document_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"cannot read acceptance document: {exc}") from exc
    match = DOC_DIGEST.search(document)
    if match is None or match.group(1) != expected:
        raise ValidationError(
            "acceptance document YAML digest does not match requirements.yml"
        )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate executable v1 requirement coverage"
    )
    parser.add_argument("--mode", required=True, choices=("mapping", "release"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    manifest_path = repo_root / "tests" / "acceptance" / "requirements.yml"
    document_path = repo_root / "docs" / "acceptance.md"
    try:
        matrix = load_manifest(manifest_path)
        counts = validate_manifest(matrix, repo_root=repo_root, mode=args.mode)
        verify_document_digest(manifest_path, document_path)
    except (OSError, ValidationError) as exc:
        print(f"requirement coverage error: {exc}", file=sys.stderr)
        return 1
    print(
        f"{counts['requirements']}/77 requirements mapped; "
        f"{counts['non_goals']}/10 non-goals mapped to absence checks; "
        "existing selectors collect successfully; "
        "planned/manual evidence explicitly enumerated "
        f"({counts['planned']} planned, {counts['manual']} manual)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
