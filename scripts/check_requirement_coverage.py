from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final

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


@dataclass(frozen=True, slots=True)
class ReleaseEvidenceTimeoutBudget:
    reference_slow_run_seconds: int
    collection_timeout_seconds: int
    cleanup_margin_seconds: int
    outer_gate_timeout_seconds: int

    def __post_init__(self) -> None:
        if self.reference_slow_run_seconds <= 0:
            raise ValueError("evidence reference duration must be positive")
        if self.collection_timeout_seconds < 2 * self.reference_slow_run_seconds:
            raise ValueError("evidence collection must allow a 2x slow-runner margin")
        if self.cleanup_margin_seconds < 60:
            raise ValueError("evidence gate must reserve at least 60s for cleanup")
        if (
            self.collection_timeout_seconds + self.cleanup_margin_seconds
            > self.outer_gate_timeout_seconds
        ):
            raise ValueError("evidence collection and cleanup must fit the outer gate")


# A loaded GitHub runner exhausted the previous 240s collection limit. The inner
# budget doubles that observed boundary, while the outer gate reserves a separate
# minute for subprocess teardown, reporting, and source-integrity checks.
RELEASE_EVIDENCE_TIMEOUT_BUDGET: Final = ReleaseEvidenceTimeoutBudget(
    reference_slow_run_seconds=240,
    collection_timeout_seconds=480,
    cleanup_margin_seconds=60,
    outer_gate_timeout_seconds=540,
)


# Public-safe frozen authority. Stable IDs are bound to behavior keys, exact
# acceptance-text digests, metadata, and semantic references so reordering complete
# capabilities cannot silently change the meaning of an identifier.
def _authority(
    category: str,
    kind: str,
    behavior_key: str,
    owning_stage: int,
    acceptance_sha256: str,
    *source_refs: tuple[str, str, str],
) -> dict[str, Any]:
    return {
        "category": category,
        "kind": kind,
        "behavior_key": behavior_key,
        "owning_stage": owning_stage,
        "acceptance_sha256": acceptance_sha256,
        "source_refs": frozenset(source_refs),
    }


# fmt: off
CANONICAL_REQUIREMENTS: dict[str, dict[str, Any]] = {
    'R-001': _authority('market', 'architecture', 'a_share_analysis_focus', 1, '310c6443191356137f3c1d6d1255d7a10d08fdad6dc7bb6e20e9aa897486be88', ('market-data-charting', 'base-a-share-data-scope', 'load-stock-analysis-data'), ('market-data-charting', 'base-a-share-data-scope', 'report-missing-category')),
    'R-002': _authority('market', 'user_visible', 'market_chart_workstation', 1, 'dfc0ac0e1c55ce95e66abe6c6aa6a8b4922cf646e2d2ebe73011721da74e02cb', ('market-data-charting', 'stock-search-and-chart-interaction', 'search-and-switch-stock'), ('market-data-charting', 'stock-search-and-chart-interaction', 'inspect-history'), ('market-data-charting', 'periods-and-adjustments', 'switch-period'), ('market-data-charting', 'periods-and-adjustments', 'switch-adjustment'), ('market-data-charting', 'candlestick-main-and-formula-subchart', 'preview-subchart-formula')),
    'R-003': _authority('formula', 'user_visible', 'user_defined_indicators', 2, 'a4fd5998d189f40f445b26249de8ca722f8d85f004ea38afd721f9e1e810ea57', ('formula-system', 'builtin-and-custom-formulas', 'use-builtin-macd'), ('formula-system', 'builtin-and-custom-formulas', 'create-custom-formula')),
    'R-004': _authority('backtest', 'user_visible', 'indicator_strategy_backtesting', 3, 'e87f4a8e62ad47f0e76c294683b97cd88a34c706ab9182e04d1721583c5850c7', ('backtesting-reporting', 'builtin-and-custom-signal-backtest', 'backtest-builtin-macd'), ('backtesting-reporting', 'builtin-and-custom-signal-backtest', 'backtest-custom-formula'), ('backtesting-reporting', 'win-rate-and-statistics', 'calculate-win-rate'), ('backtesting-reporting', 'win-rate-and-statistics', 'inspect-detailed-statistics')),
    'R-005': _authority('operations', 'operational', 'requirements_frozen_before_implementation', 0, '9372c686bd5ce7f0c5dddbe8b2823395eb1aa61c6530539703cd28311d19d34b', ('delivery-governance', 'requirements-frozen-before-implementation', 'verify-authoritative-requirement')),
    'R-006': _authority('operations', 'operational', 'confirmed_specs_are_hosted_in_openspec', 0, 'b041ae605568b8691b7f3658f7a905d6856f17a3cc18086880938a2b5850367f', ('delivery-governance', 'confirmed-specs-are-hosted-in-openspec', 'verify-authoritative-requirement')),
    'R-007': _authority('market', 'architecture', 'base_a_share_data_sources', 1, '8983c08732b2b51e123f706febc5f33250e9bf606daa5d894f1d980cedd14a05', ('market-data-charting', 'pluggable-sources-and-priority', 'primary-source-succeeds'), ('market-data-charting', 'pluggable-sources-and-priority', 'primary-source-fails'), ('market-data-charting', 'pluggable-sources-and-priority', 'inspect-tushare-configuration')),
    'R-008': _authority('operations', 'operational', 'tradingagents_cn_product_reference', 0, '8d6bb8c199684e46394532c590747c2c61a52a5cbbf37baf0b5ad746e5c7b708', ('delivery-governance', 'tradingagents-cn-product-reference', 'verify-authoritative-requirement')),
    'R-009': _authority('analysis', 'architecture', 'compact_multi_agent_analysis', 4, 'd58736b1792c05f50f0aba0de4fffc290630726271cafcbc5a11428873a4f228', ('multi-agent-analysis', 'compact-research-workflow', 'complete-research-workflow'), ('multi-agent-analysis', 'compact-research-workflow', 'inspect-analysis-trace')),
    'R-010': _authority('platform', 'user_visible', 'low_programming_personal_audience', 0, 'c76380b49476e7da4cb943ffc9103839201c8d32dd7ac57f0ba297456c32d139', ('market-data-charting', 'desktop-first-single-user-web-workstation', 'open-workstation-in-browser'), ('market-data-charting', 'desktop-first-single-user-web-workstation', 'open-workstation-on-tablet')),
    'R-011': _authority('platform', 'user_visible', 'visual_low_code_primary_workflows', 0, '947b005de6b34bea4d1f8dc7a5cfd024e86f86fcd1de113a43687ce2532b0d41', ('market-data-charting', 'desktop-first-single-user-web-workstation', 'open-workstation-in-browser'), ('market-data-charting', 'desktop-first-single-user-web-workstation', 'open-workstation-on-tablet'), ('formula-system', 'three-column-formula-editor', 'insert-function'), ('formula-system', 'three-column-formula-editor', 'preview-formula')),
    'R-012': _authority('formula', 'user_visible', 'tdx_style_formula_editor_and_subset', 2, '826c4181d67e294c48c1f3bf849833a2e8568600091e3d85400cdbe4bff2f4d9', ('formula-system', 'common-tdx-syntax-compatibility', 'paste-supported-formula'), ('formula-system', 'common-tdx-syntax-compatibility', 'use-unsupported-function'), ('formula-system', 'three-column-formula-editor', 'insert-function'), ('formula-system', 'three-column-formula-editor', 'preview-formula')),
    'R-013': _authority('backtest', 'architecture', 'backtest_consumes_tdx_signals', 3, '69c75f76bcf3ac94f65132e80dd09e89110d02557c1cc62c1103a59cfbe2c44e', ('backtesting-reporting', 'builtin-and-custom-signal-backtest', 'backtest-builtin-macd'), ('backtesting-reporting', 'builtin-and-custom-signal-backtest', 'backtest-custom-formula')),
    'R-014': _authority('backtest', 'user_visible', 'selected_pool_batch_backtest', 3, 'ab9f8e821858295fc1aeed5217e75b657b9f9a44dbaca6c80d3fb766f8f86381', ('backtesting-reporting', 'single-and-pool-backtests', 'run-single-stock'), ('backtesting-reporting', 'single-and-pool-backtests', 'run-stock-pool')),
    'R-015': _authority('backtest', 'architecture', 'independent_pool_signal_samples', 3, '845dfc7f933c31ae9e1f3d45b4cfdf3e9bba70aa981b8b2ca486918e9dbef38f', ('backtesting-reporting', 'independent-signal-sample-model', 'simultaneous-pool-buy-signals'), ('backtesting-reporting', 'win-rate-and-statistics', 'calculate-win-rate'), ('backtesting-reporting', 'win-rate-and-statistics', 'inspect-detailed-statistics')),
    'R-016': _authority('backtest', 'architecture', 'daily_weekly_sixty_minute_backtests', 3, '14020be1a0a0610be7498f11106f2e2c36bbcf6283a572eb2eb9b16d2e96dd4d', ('backtesting-reporting', 'backtest-periods', 'run-weekly-backtest'), ('backtesting-reporting', 'backtest-periods', 'run-sixty-minute-backtest')),
    'R-017': _authority('backtest', 'architecture', 'close_confirmed_next_open_execution', 3, 'efaade23153ce4746b1a5f8c64d275dbfb363711345e160f3adfd368ac3cc37c', ('backtesting-reporting', 'signal-confirmation-and-execution-time', 'daily-buy-signal'), ('backtesting-reporting', 'signal-confirmation-and-execution-time', 'weekly-sell-signal')),
    'R-018': _authority('backtest', 'architecture', 'a_share_constraints_costs_and_slippage', 3, 'c256fe785d29b4090e565c64b12d0ca07636a296691ca6388ed49264afb5b9f3', ('backtesting-reporting', 'a-share-execution-constraints', 't-plus-one-blocks-sale'), ('backtesting-reporting', 'a-share-execution-constraints', 'suspension-blocks-fill'), ('backtesting-reporting', 'a-share-execution-constraints', 'price-limit-blocks-fill'), ('backtesting-reporting', 'costs-and-slippage', 'calculate-net-trade-return')),
    'R-019': _authority('backtest', 'user_visible', 'complete_backtest_statistics_and_trades', 3, 'e47898496321a927bb718112e6440002055b832c35cc5010db33e7759def0e47', ('backtesting-reporting', 'win-rate-and-statistics', 'calculate-win-rate'), ('backtesting-reporting', 'win-rate-and-statistics', 'inspect-detailed-statistics')),
    'R-020': _authority('market', 'user_visible', 'daily_weekly_sixty_and_latest_market_data', 1, '82af0a74e59c3029487f5ff864e9c9dcc0247b2cc7c61a42372961632a21b2b1', ('market-data-charting', 'periods-and-adjustments', 'switch-period'), ('market-data-charting', 'periods-and-adjustments', 'switch-adjustment')),
    'R-021': _authority('market', 'user_visible', 'complete_baseline_analysis_data', 1, '1590d0f56bfa20b2ccdc1789ae3e886532c4c51e4c88ed01bf1b992c10ca0bfb', ('market-data-charting', 'base-a-share-data-scope', 'load-stock-analysis-data'), ('market-data-charting', 'base-a-share-data-scope', 'report-missing-category')),
    'R-022': _authority('analysis', 'architecture', 'analysis_is_independent_from_formula_backtest', 4, '352b3a222c0da389eba0a3cc2c73b48b68107061a37b8ba1fdb2d7900775381b', ('multi-agent-analysis', 'analysis-decoupled-from-formula-and-backtest', 'report-has-technical-opinion')),
    'R-023': _authority('analysis', 'user_visible', 'on_demand_structured_traceable_analysis', 4, '428bb9e97b38525bc0745d4ca8ee520bd5812c240a3e68838de28e8d065ba568', ('multi-agent-analysis', 'on-demand-single-stock-report', 'start-complete-analysis'), ('multi-agent-analysis', 'on-demand-single-stock-report', 'open-historical-report'), ('multi-agent-analysis', 'evidence-and-source-traceability', 'inspect-claim-evidence'), ('multi-agent-analysis', 'evidence-and-source-traceability', 'source-data-is-missing')),
    'R-024': _authority('analysis', 'user_visible', 'five_level_rating_confidence_evidence_risk', 4, '5d47e506e226dd50318bc6772aa50e3021371863844e0cf9197b8f8ce608a34a', ('multi-agent-analysis', 'five-level-rating-and-confidence', 'produce-final-conclusion')),
    'R-025': _authority('platform', 'user_visible', 'desktop_first_web_with_tablet_support', 0, '6f3c4c50cc037e65a92f883d4236ac2f6b61e0872eaf75bee286ddd39cd5f856', ('market-data-charting', 'desktop-first-single-user-web-workstation', 'open-workstation-in-browser'), ('market-data-charting', 'desktop-first-single-user-web-workstation', 'open-workstation-on-tablet')),
    'R-026': _authority('platform', 'architecture', 'private_single_user_deployment', 0, '6c433fbbc0700dc8f2d7c60f52919895c8117e7eb519d3cc77412605645de9b1', ('market-data-charting', 'desktop-first-single-user-web-workstation', 'open-workstation-in-browser'), ('market-data-charting', 'desktop-first-single-user-web-workstation', 'open-workstation-on-tablet')),
    'R-027': _authority('formula', 'user_visible', 'indicator_and_trading_formula_outputs', 2, '093afb6e4ebddc3380b782c285daf88ff63755dcd4469758c4f4da88334292c0', ('formula-system', 'formula-types-and-outputs', 'evaluate-technical-indicator'), ('formula-system', 'formula-types-and-outputs', 'evaluate-trading-system')),
    'R-028': _authority('market', 'user_visible', 'preset_and_custom_stock_lists', 1, 'fab99086abdf7a505dcbb973b9183b4a9b0dbcffd7b70bd34889b97f09cfcfdc', ('market-data-charting', 'preset-and-custom-pools', 'use-preset-pool'), ('market-data-charting', 'preset-and-custom-pools', 'save-custom-pool')),
    'R-029': _authority('formula', 'user_visible', 'formula_editor_assistance_preview_save_copy', 2, '55b94336569568d2b3961924b10a82e6a41adab94eae4137c63eb7b3c90f423c', ('formula-system', 'formula-editing-assistance', 'use-function-assistance'), ('formula-system', 'formula-editing-assistance', 'locate-formula-error')),
    'R-030': _authority('backtest', 'user_visible', 'durable_cancellable_pool_backtest', 3, 'd2634a00f4e657102c634cfe86757113a414c04f2c7c92d6dd6fa8336f19c1f2', ('backtesting-reporting', 'asynchronous-pool-task', 'run-pool-task'), ('backtesting-reporting', 'asynchronous-pool-task', 'cancel-pool-task')),
    'R-031': _authority('market', 'user_visible', 'pluggable_market_sources_with_local_tdx', 1, '94d077f7ac8d6c04a8b171d8d02276677acd56575f3c5726de497a8f87f1c596', ('market-data-charting', 'pluggable-sources-and-priority', 'primary-source-succeeds'), ('market-data-charting', 'pluggable-sources-and-priority', 'primary-source-fails'), ('market-data-charting', 'pluggable-sources-and-priority', 'inspect-tushare-configuration'), ('market-data-charting', 'local-tdx-fallback', 'configure-valid-directory'), ('market-data-charting', 'local-tdx-fallback', 'local-directory-unavailable')),
    'R-032': _authority('analysis', 'user_visible', 'domestic_llm_openai_ollama_adapters', 4, '0df56f93b856f67fde738b6991caecccaffdd310a06b710dea80dbb204564c75', ('multi-agent-analysis', 'pluggable-model-interfaces', 'configure-domestic-model'), ('multi-agent-analysis', 'pluggable-model-interfaces', 'switch-model-configuration')),
    'R-033': _authority('formula', 'security', 'future_and_repainting_formulas_blocked', 2, '2d1667ce47da4927c75d0c96145ae881456591a69ea45f87c18d0f08da259df5', ('formula-system', 'forbid-future-and-repainting', 'detect-future-function'), ('formula-system', 'forbid-future-and-repainting', 'detect-signal-drift')),
    'R-034': _authority('analysis', 'user_visible', 'analysis_partial_failure_retry_and_no_rating', 4, 'fb010a405d5c964e39ccb5cc128ffc46ed047eafcfe43e365d8f65eb4d967466', ('multi-agent-analysis', 'retry-and-partial-report', 'noncritical-module-fails'), ('multi-agent-analysis', 'retry-and-partial-report', 'retry-failed-module'), ('multi-agent-analysis', 'no-rating-without-critical-evidence', 'all-critical-data-unavailable')),
    'R-035': _authority('platform', 'architecture', 'modular_monolith_worker_and_capability_specs', 0, '888625dc400ddc160074d2ffcf57c02dad9a5dae425b7831e339c01752400431', ('product-design', 'modular-monolith-worker-and-persistence', 'run-heavy-work-in-worker')),
    'R-036': _authority('market', 'architecture', 'normalized_market_model_with_provenance', 1, '3a7cae81516b372ea916d1de0a2ba52dc9a3fe365c870e660863a0b10193136f', ('market-data-charting', 'unified-model-and-provenance', 'display-data-source'), ('market-data-charting', 'unified-model-and-provenance', 'switch-series-source')),
    'R-037': _authority('formula', 'architecture', 'shared_formula_engine_result', 2, 'f13f35baee19e23dafe73c1726c8084df74773b917d9f9f2812109ca62b7f642', ('formula-system', 'single-formula-calculation-source', 'preview-to-backtest')),
    'R-038': _authority('platform', 'user_visible', 'desktop_workstation_primary_navigation', 1, 'f82bf0d774ec49766bbbc1460eea4f21b9c23b93559aa34d8375c00893aae4c1', ('market-data-charting', 'professional-terminal-visual-structure', 'open-market-workspace'), ('market-data-charting', 'desktop-first-single-user-web-workstation', 'open-workstation-in-browser'), ('market-data-charting', 'desktop-first-single-user-web-workstation', 'open-workstation-on-tablet')),
    'R-039': _authority('formula', 'user_visible', 'template_or_pasted_formula_pre_save_validation', 2, 'adf2761106607e388fb221e7fb1917aa5ab82b98f141c19bfe68cd785db0e024', ('formula-system', 'builtin-and-custom-formulas', 'use-builtin-macd'), ('formula-system', 'builtin-and-custom-formulas', 'create-custom-formula'), ('formula-system', 'common-tdx-syntax-compatibility', 'paste-supported-formula'), ('formula-system', 'common-tdx-syntax-compatibility', 'use-unsupported-function'), ('formula-system', 'validate-before-save', 'formula-passes-validation'), ('formula-system', 'validate-before-save', 'formula-fails-validation')),
    'R-040': _authority('backtest', 'user_visible', 'guided_backtest_and_drilldown_report', 3, '0aa6008fa3de3a1a9b14fbc266109c9cbecaf80b2c83cc5a87200c03cc02d219', ('backtesting-reporting', 'guided-backtest-wizard', 'configure-and-review-backtest'), ('backtesting-reporting', 'conclusion-first-report', 'open-completed-backtest-report')),
    'R-041': _authority('market', 'architecture', 'category_priority_fallback_without_splicing', 1, '9ad1534820e9b42f654ec3170a1fdf23c442d69c2b71f6bd201bba29de808d6f', ('market-data-charting', 'pluggable-sources-and-priority', 'primary-source-succeeds'), ('market-data-charting', 'pluggable-sources-and-priority', 'primary-source-fails'), ('market-data-charting', 'pluggable-sources-and-priority', 'inspect-tushare-configuration'), ('market-data-charting', 'unified-model-and-provenance', 'display-data-source'), ('market-data-charting', 'unified-model-and-provenance', 'switch-series-source')),
    'R-042': _authority('market', 'user_visible', 'local_manual_and_scheduled_updates', 1, '2ce9442a986fa361b5f80922ecad0cc354a675df161765fbe317ee00a56a40a9', ('market-data-charting', 'local-storage-and-update', 'manually-update-data'), ('market-data-charting', 'local-storage-and-update', 'scheduled-update-partially-fails')),
    'R-043': _authority('backtest', 'architecture', 'reproducible_backtest_snapshot', 3, '8a6fc306170cba20997004927a6d14b4ba8f9aa904719f45964a6942f6c5be7d', ('backtesting-reporting', 'reproducible-backtest-snapshot', 'rerun-same-snapshot')),
    'R-044': _authority('formula', 'security', 'controlled_tdx_parser_and_function_registry', 2, '5f5bde9bc536e4a48e9800204df24508d344e3312ab163dd2fabd8bb54455097', ('formula-system', 'controlled-formula-execution', 'reject-arbitrary-code'), ('formula-system', 'common-tdx-syntax-compatibility', 'paste-supported-formula'), ('formula-system', 'common-tdx-syntax-compatibility', 'use-unsupported-function')),
    'R-045': _authority('backtest', 'architecture', 'single_position_pending_orders_and_open_trades', 3, 'f9425afe4b6ba207edd3b981e207b88fa31e0d8b24322a91b7eec079f0a0b6d6', ('backtesting-reporting', 'single-position-and-pending-orders', 'repeated-buy-while-held'), ('backtesting-reporting', 'single-position-and-pending-orders', 'opposite-signal-cancels-pending-buy'), ('backtesting-reporting', 'open-trade-handling', 'end-with-open-position')),
    'R-046': _authority('analysis', 'architecture', 'ordered_evidence_bound_agent_workflow', 4, '000ca6474dfc843dd8ade9989f644669e1318ccf3b40d451bdd8f43bce2c5233', ('multi-agent-analysis', 'compact-research-workflow', 'complete-research-workflow'), ('multi-agent-analysis', 'compact-research-workflow', 'inspect-analysis-trace')),
    'R-047': _authority('analysis', 'architecture', 'analysis_claim_and_run_traceability', 4, '493f1ac46e1a96a0c54097c35273bcb62562ef6564f356fd46f38b7147309991', ('multi-agent-analysis', 'evidence-and-source-traceability', 'inspect-claim-evidence'), ('multi-agent-analysis', 'evidence-and-source-traceability', 'source-data-is-missing')),
    'R-048': _authority('security', 'security', 'external_content_is_data_and_report_is_non_advice', 4, '392f290d181a7aa706b5e93813e81105aa10a55087db0047526d411d430b30ca', ('multi-agent-analysis', 'external-content-prompt-injection-defense', 'news-contains-instructions'), ('multi-agent-analysis', 'first-release-analysis-boundary', 'view-final-report')),
    'R-049': _authority('analysis', 'architecture', 'bounded_model_retry_preserves_runs', 4, '60898fd95ea05bbac4fa226630f5499e5ec92ceb37f2849bd7a993243f3c740a', ('multi-agent-analysis', 'retry-and-partial-report', 'noncritical-module-fails'), ('multi-agent-analysis', 'retry-and-partial-report', 'retry-failed-module')),
    'R-050': _authority('security', 'security', 'all_sensitive_configuration_is_protected', 4, 'e6709539a52eaccf1edac889a743d8beae360d66e7ef3e329575df09a62a9614', ('market-data-charting', 'market-secret-protection', 'save-market-token'), ('market-data-charting', 'market-secret-protection', 'market-source-call-fails'), ('multi-agent-analysis', 'model-secret-and-log-protection', 'save-model-key'), ('multi-agent-analysis', 'model-secret-and-log-protection', 'model-call-fails')),
    'R-051': _authority('operations', 'operational', 'four_capability_stages_are_sequential', 5, '3a0c8c43edf674454beb430586c6f6f21703a6b900208542264c8ba2ede6d4be', ('delivery-governance', 'four-capability-stages-are-sequential', 'verify-authoritative-requirement')),
    'R-052': _authority('operations', 'operational', 'first_release_acceptance_scope', 5, '7aa14a38bed987e90f85b4f7175ed4343fbce7879a39a33e63670a7e66af9f29', ('delivery-governance', 'first-release-acceptance-scope', 'verify-authoritative-requirement')),
    'R-053': _authority('performance', 'performance', 'four_core_sixteen_gb_performance_budgets', 5, 'f0a6636bfe5d67650ac7bd8b35ec8cffb0c66bebfef75f7a625347c843904a76', ('market-data-charting', 'market-interface-performance', 'open-cached-daily-chart'), ('market-data-charting', 'market-interface-performance', 'wait-for-external-data'), ('formula-system', 'formula-preview-performance', 'preview-ten-years-daily'), ('backtesting-reporting', 'single-backtest-performance', 'run-ten-year-single-stock'), ('backtesting-reporting', 'single-backtest-performance', 'run-all-a-pool')),
    'R-054': _authority('market', 'user_visible', 'three_region_market_terminal', 1, 'dcbcec3e3963a0b3511eb98e6692b9c3a9b06663fba1fa949772fb3e9b2a7ff4', ('market-data-charting', 'professional-terminal-visual-structure', 'open-market-workspace')),
    'R-055': _authority('market', 'user_visible', 'deep_navy_red_rise_green_fall', 1, '4271af6891b32f201918fb0420cd132f2eace1e9332a84113ebac4331ddb4717', ('market-data-charting', 'professional-terminal-visual-structure', 'open-market-workspace')),
    'R-056': _authority('market', 'user_visible', 'candlestick_main_formula_subchart', 2, 'accca687bf078d74996a2d0618b00f52de12d4f849fcc8f45e67ffec14c72695', ('market-data-charting', 'candlestick-main-and-formula-subchart', 'preview-subchart-formula')),
    'R-057': _authority('formula', 'user_visible', 'three_column_formula_studio', 2, 'e4a07016c7840966afa1d77775659b02ade33b9e22cb63526e134d1ab4dfc51d', ('formula-system', 'three-column-formula-editor', 'insert-function'), ('formula-system', 'three-column-formula-editor', 'preview-formula')),
    'R-058': _authority('backtest', 'user_visible', 'conclusion_first_backtest_report', 3, '4df4fecaee52e86ff40c23a426fea05f0a857edfb93671c0e2056993d0cb2431', ('backtesting-reporting', 'conclusion-first-report', 'open-completed-backtest-report')),
    'R-059': _authority('analysis', 'user_visible', 'side_by_side_analysis_and_evidence', 4, '462f8a83ac2e56f11507b1838117d0cb264419ab22fdd1b55d847592a9a1af49', ('multi-agent-analysis', 'side-by-side-conclusion-and-evidence', 'open-completed-report')),
    'R-060': _authority('platform', 'user_visible', 'stock_desk_name_and_repository_identity', 0, '07db35ef7975db0e31c12922d09fed927bca75ca9357531c80c8d6075f22993f', ('market-data-charting', 'product-identity', 'open-product')),
    'R-061': _authority('operations', 'operational', 'canonical_github_remote', 5, 'f83e8239ca6f171258f5ec341bb73e9f776a058bb76b12cf4000786c4ee1b96d', ('delivery-governance', 'public-repository-and-remote-identity', 'verify-stock-desk-repository-and-remote')),
    'R-062': _authority('operations', 'operational', 'congbao_commit_identity', 5, '869555b05458fd99ee1cae355ab010b118b245e8eaf431794fea18324f77ff21', ('delivery-governance', 'git-object-identity', 'verify-commits-and-tag')),
    'R-063': _authority('operations', 'operational', 'dedicated_github_ssh_identity', 5, '42d23044b02be4ea9a68c7b774265ca9787e9e72e4c74ac430c4904366208e94', ('delivery-governance', 'github-ssh-and-tag-signing', 'verify-remote-key-and-signed-tag')),
    'R-064': _authority('operations', 'operational', 'canonical_workspace_checkout', 5, 'b6bb0eb0840ffa5a84e76a63ce97882d9c59f69fd3ed9e556485c6a2773e5c65', ('delivery-governance', 'canonical-delivery-checkout', 'verify-session-checkout')),
    'R-065': _authority('operations', 'operational', 'main_is_default_and_published_upstream', 5, '1c688c93bc05d8995d0d8a12a63fe715d862800796cd57254a8efb0d34d54570', ('delivery-governance', 'main-is-default-and-published-upstream', 'verify-authoritative-requirement')),
    'R-066': _authority('publication', 'publication', 'only_public_content_in_main_history', 5, 'ca319582db4710ee395d4e4e68fbf6130eef652b60eac95514fb1e74ce84dfd0', ('publication-boundary', 'private-input-exclusion', 'audit-public-tree-and-history')),
    'R-067': _authority('operations', 'operational', 'complete_multi_stage_delivery_plan', 5, '779036800e4ec0f3ff3291a143c6c72d91d1f781d3b2aec7bc3b7fe1c280dde5', ('delivery-governance', 'complete-confirmed-plan-scope', 'confirm-full-scope-completion')),
    'R-068': _authority('operations', 'operational', 'each_stage_has_mergeable_subplan', 5, '8fb566ab9d91c41b3c457b971427c821f58082cb43130270bcd7bfd447a0f5af', ('delivery-governance', 'independent-stage-execution-cycle', 'plan-implement-verify-submit')),
    'R-069': _authority('publication', 'publication', 'mature_open_source_repository_configuration', 5, 'f711589954416300ce175f4de01ac339a6b73eead20f44dd23d394b4371f5993', ('release-publication', 'open-source-repository-quality', 'inspect-community-security-release-configuration')),
    'R-070': _authority('publication', 'publication', 'openspec_is_local_and_excluded', 5, 'daf4b181846477725c482dad526b6c8d1cd2484255dc24b23165fb2822a679e9', ('publication-boundary', 'private-input-exclusion', 'audit-public-tree-and-history')),
    'R-071': _authority('publication', 'publication', 'bilingual_readme_with_verified_basics', 5, '270277162cdc9b44f120efa2cd2156be1fae1ab1a80d35321e605cfe609280e0', ('market-data-charting', 'bilingual-open-source-readme', 'english-to-chinese'), ('market-data-charting', 'bilingual-open-source-readme', 'chinese-to-english')),
    'R-072': _authority('operations', 'operational', 'stage_plan_implementation_browser_pr_cycle', 5, '830ba1f94b098dc7aa88f79236f1dbef0ebacfb03c83b1976d5f41b1e1dbc8a9', ('delivery-governance', 'stage-publication-and-release', 'push-review-merge-tag-release')),
    'R-073': _authority('publication', 'publication', 'final_readme_is_concise_bilingual_entry', 5, 'b7ca726cbc4a6389da7a73e980f37272c0721333ee8e64f4041bf160f15821d7', ('release-publication', 'verified-reciprocal-readme', 'verify-readme-pair')),
    'R-074': _authority('publication', 'publication', 'every_feature_has_wiki_steps_and_real_screenshot', 5, '21996be94a21c7c0547299c430d7086357ec66bf74d6b02e96908498f363c1a9', ('release-publication', 'feature-wiki-screenshots-and-steps', 'validate-feature-page-image-and-steps')),
    'R-075': _authority('publication', 'publication', 'wiki_is_complete_and_bilingual', 5, 'b652e52ccc0a8977baa38f7dca45ea40cb8042118600c0e5bebfade70abb9633', ('release-publication', 'reciprocal-bilingual-wiki', 'navigate-language-pair')),
    'R-076': _authority('publication', 'publication', 'source_free_windows_and_macos_installers', 5, '5f9e292b05b9eb53306b6d59941c94e79d952ed65f5c25f8936349a78fee7d0f', ('release-packaging', 'source-checkout-free-installation', 'install-and-first-launch-windows'), ('release-packaging', 'source-checkout-free-installation', 'install-and-first-launch-macos')),
    'R-077': _authority('platform', 'user_visible', 'responsive_ui_across_screen_ratios', 5, 'fe7d96bc05f113cb91a0aa3773a229f729aec23218eedc0e0c50fd2e6d15a753', ('market-data-charting', 'responsive-navigation-and-nonoverlap', 'narrow-screen-auto-collapse'), ('market-data-charting', 'responsive-navigation-and-nonoverlap', 'manual-navigation-toggle'), ('market-data-charting', 'responsive-navigation-and-nonoverlap', 'preserve-layout-at-supported-ratios'), ('release-quality', 'strengthened-all-route-responsive-ui', 'verify-all-routes-ratios-icons-and-nonoverlap')),
    'R-078': _authority('publication', 'publication', 'chinese_default_readme_and_wiki', 5, '6607c75bac97ae00a7f19adb87510f1ac16c4125ca16198e2afa0888eb39d754', ('release-publication', 'verified-reciprocal-readme', 'verify-readme-pair'), ('release-publication', 'reciprocal-bilingual-wiki', 'navigate-language-pair')),
    'R-079': _authority('publication', 'publication', 'real_stock_data_in_public_screenshots', 5, '27c8065004c7e434674b0c21b2e0e35df31f14443918ce464d3b6c1a0187cedf', ('release-publication', 'feature-wiki-screenshots-and-steps', 'validate-feature-page-image-and-steps')),
    'R-080': _authority('operations', 'operational', 'twenty_hour_release_priority_without_gate_weakening', 5, '032d47d73c6187570b7e03ffc17cd96a8ee7d0b8d09abc155c2fc33a702736ac', ('delivery-governance', 'time-bounded-v1-release-priority', 'review-twenty-hour-target-and-preserved-gates')),
    'R-081': _authority('operations', 'operational', 'preferred_and_hard_v1_release_deadlines', 5, 'fbd32a5fc83610021312a7000e6e07121fd78995f0457b8a2844f0cb3577b184', ('delivery-governance', 'time-bounded-v1-release-priority', 'review-preferred-and-hard-deadlines')),
    'R-082': _authority('operations', 'operational', 'exact_main_proof_reuse_without_release_gate_loss', 5, '8771c9c3772ad765381985803f0cb43f2783e56a110248af239224dcc99453e1', ('delivery-governance', 'exact-main-validation-proof-reuse', 'select-pr-and-main-test-scope'), ('delivery-governance', 'exact-main-validation-proof-reuse', 'reuse-exact-main-proof-for-release'), ('delivery-governance', 'exact-main-validation-proof-reuse', 'reject-mismatched-proof-or-release-input')),
}

CANONICAL_NON_GOALS: dict[str, dict[str, Any]] = {
    'N-001': _authority('backtest', 'non_goal', 'no_broker_or_live_ordering', 3, '95c0f34daf925b301a5111510bdb986264fa4a7925e75d102f8773e211722c69', ('product-boundaries', 'no-live-trading', 'inventory-public-surfaces')),
    'N-002': _authority('backtest', 'non_goal', 'no_shared_capital_portfolio', 3, '560bbd8fc9a5c9cdca454785876dfae60888a49ccd5f91a2e2cf3e345d961797', ('product-boundaries', 'no-shared-capital-portfolio', 'inventory-public-surfaces')),
    'N-003': _authority('market', 'non_goal', 'no_realtime_tick_or_level2_data', 1, 'b2fa8f0d5f028afe0c0fbd5e6813ad007ff2af26cc312b396201b9fd976a6b6a', ('product-boundaries', 'no-realtime-tick-level2', 'inventory-public-surfaces')),
    'N-004': _authority('analysis', 'non_goal', 'no_target_price_or_specific_allocation', 4, 'fd7a30d4030bdea4973121ec39ea7db413ef4868b35dd8c32deb29b447355ba7', ('product-boundaries', 'no-target-price-allocation', 'inventory-public-surfaces')),
    'N-005': _authority('platform', 'non_goal', 'no_second_native_product_interface', 5, 'ae33b12126441e0db39debaa7faf2e1e35e2f57b7a706e53554fdd0d5e2aabf3', ('product-boundaries', 'no-native-product-ui', 'inventory-public-surfaces')),
    'N-006': _authority('platform', 'non_goal', 'no_accounts_rbac_subscription_billing', 0, 'ea5371a3671ca0c3cdd816d25c73106063ea70c20b3076c043c792ab60b91e96', ('product-boundaries', 'no-account-or-billing-system', 'inventory-public-surfaces')),
    'N-007': _authority('market', 'non_goal', 'no_drawing_multistock_or_linked_periods', 1, '3e28c7269f5e32ce7d5a55097c083c288883886e7ecbada8bdf8ab637c591095', ('product-boundaries', 'no-drawing-multistock-linked-periods', 'inventory-public-surfaces')),
    'N-008': _authority('formula', 'non_goal', 'no_condition_selection_or_color_k', 2, 'ebe69e78f881e46874a487bbb36a8947fc80bd641464d310809f254da2b5917c', ('product-boundaries', 'no-condition-selection-color-k', 'inventory-public-surfaces')),
    'N-009': _authority('market', 'non_goal', 'no_dynamic_market_screening', 1, 'e668de24bc06e29592458792206c8896ead97d922d8ceb8f79627ca4f2792472', ('product-boundaries', 'no-dynamic-screening', 'inventory-public-surfaces')),
    'N-010': _authority('formula', 'non_goal', 'no_ai_formula_assistance', 2, '428419da0b4b0a65ede2a8e8d855f5e7b677d5acc9b6fad27cca8e3822bbd00e', ('product-boundaries', 'no-ai-formula-assistance', 'inventory-public-surfaces')),
}
# fmt: on

V11_CANONICAL_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "V11-R-001": _authority(
        "market",
        "architecture",
        "index_and_equity_identity_are_distinct",
        1,
        "a02b472acd9f4db5b667ca96a62e57891634142cfe9c307802bcd4e129bc0878",
        (
            "desktop-onboarding",
            "canonical-instrument-identity",
            "distinguish-index-from-equity",
        ),
        (
            "desktop-onboarding",
            "safe-default-instrument",
            "open-without-a-selection",
        ),
    ),
    "V11-R-002": _authority(
        "platform",
        "user_visible",
        "four_step_first_run_happy_path",
        1,
        "05cc8d9d6f2657756c7d9d6d030270097c0b7b55d6943622eedea54851e1014e",
        (
            "desktop-onboarding",
            "guided-first-run-setup",
            "complete-minimum-data-configuration",
        ),
        (
            "desktop-onboarding",
            "guided-first-run-setup",
            "continue-with-supported-deferred-choice",
        ),
        (
            "desktop-onboarding",
            "ready-after-onboarding",
            "open-default-market-workspace",
        ),
    ),
    "V11-R-003": _authority(
        "market",
        "architecture",
        "whole_provider_onboarding_fallback",
        1,
        "0eeb8d67f4a675a0521740fe1e1e206b32b63327a823ffb1575797837f29ff13",
        (
            "desktop-onboarding",
            "whole-provider-fallback",
            "fall-back-after-complete-provider-failure",
        ),
        (
            "desktop-onboarding",
            "whole-provider-fallback",
            "pin-first-fully-verified-provider",
        ),
    ),
    "V11-R-004": _authority(
        "platform",
        "user_visible",
        "demo_isolation_and_real_setup_recovery",
        1,
        "d73f6d196ef8acffa1a4c76ce4431a412a6a70f7802571ba3df90ac87b19fe1b",
        (
            "desktop-onboarding",
            "read-only-demo-isolation",
            "do-not-complete-real-setup",
        ),
        (
            "desktop-onboarding",
            "read-only-demo-isolation",
            "exit-demo-and-resume-real-setup",
        ),
    ),
    "V11-R-005": _authority(
        "platform",
        "architecture",
        "atomic_onboarding_resume",
        1,
        "1937f177014a519150c59bb375ee73a49fb13b4aaa97621638ece3d113628499",
        (
            "desktop-onboarding",
            "persistent-progress",
            "restart-from-last-committed-step",
        ),
        (
            "desktop-onboarding",
            "persistent-progress",
            "preserve-valid-state-after-interrupted-write",
        ),
    ),
    "V11-R-006": _authority(
        "platform",
        "user_visible",
        "workspace_restore_and_safe_fallback",
        1,
        "0f3bb3cf8e1dced951cff965d00cd6635abdd07b3790e8a89e7f8977de93c730",
        (
            "desktop-workspace",
            "versioned-workspace-restore",
            "restore-valid-session-after-restart",
        ),
        (
            "desktop-workspace",
            "versioned-workspace-restore",
            "fall-back-from-invalid-state",
        ),
    ),
    "V11-R-007": _authority(
        "formula",
        "user_visible",
        "formula_studio_desktop_accessibility",
        2,
        "67572b08f996785f17d109d2ebd5af4211847aaa13eee49f4632718992649be9",
        (
            "desktop-user-experience",
            "responsive-desktop-layout",
            "high-display-scaling",
        ),
        (
            "desktop-user-experience",
            "system-theme-and-non-color-expression",
            "follow-system-theme-changes",
        ),
        (
            "desktop-user-experience",
            "keyboard-and-focus-usability",
            "complete-primary-journey-with-keyboard",
        ),
        (
            "desktop-onboarding",
            "core-flow-contextual-guidance",
            "first-visit-core-page",
        ),
    ),
    "V11-R-008": _authority(
        "formula",
        "security",
        "authenticated_desktop_formula_boundary",
        2,
        "05aa28c981bf359f2b3dec01f0c46f0664bab6710152c05a78285d8464e2f757",
        (
            "windows-desktop-shell",
            "sidecar-lifecycle-and-session-security",
            "unauthorized-local-request",
        ),
        (
            "windows-desktop-shell",
            "core-capability-compatibility-baseline",
            "use-existing-core-capability-after-desktopization",
        ),
    ),
    "V11-R-009": _authority(
        "formula",
        "architecture",
        "desktop_formula_semantic_identity",
        2,
        "690bcc22a624a7c2b0a658baa378ba7ea55bdaeaf06b5d056c2b3989388e36a5",
        (
            "windows-desktop-shell",
            "core-capability-compatibility-baseline",
            "use-existing-core-capability-after-desktopization",
        ),
    ),
    "V11-R-010": _authority(
        "platform",
        "user_visible",
        "bounded_actionable_desktop_recovery",
        3,
        "a68dce93efaf6e503a3ab92f809b91bb18342e227ee8cb0f19c15ca470fba3f1",
        (
            "desktop-user-experience",
            "actionable-empty-error-and-offline-states",
            "sidecar-unavailable",
        ),
        (
            "windows-desktop-shell",
            "sidecar-lifecycle-and-session-security",
            "unexpected-sidecar-exit",
        ),
    ),
    "V11-R-011": _authority(
        "security",
        "security",
        "explicit_local_redacted_diagnostics_and_zero_telemetry",
        3,
        "ee2c1e52484d108288a498748570cf4af5f4afd66c459d1166498b87deea36af",
        (
            "desktop-user-experience",
            "local-redacted-diagnostic-bundle",
            "explicitly-export-diagnostic-bundle",
        ),
        (
            "desktop-user-experience",
            "default-zero-telemetry",
            "normal-application-use",
        ),
    ),
    "V11-R-012": _authority(
        "platform",
        "user_visible",
        "exact_sha_windows_icon_and_packaged_desktop_matrix",
        3,
        "79eaf9e03c8d0eff63b53379645c7ef8c3c1df3cd6fe00b280e6cbcd25bcb312",
        (
            "desktop-user-experience",
            "unified-desktop-icon",
            "consistent-icons-across-desktop-entry-points",
        ),
        (
            "desktop-user-experience",
            "responsive-desktop-layout",
            "high-display-scaling",
        ),
    ),
    "V11-R-013": _authority(
        "platform",
        "user_visible",
        "checkpointed_desktop_exit_and_explicit_recovery",
        4,
        "e5c38ced7c8493a1cc7562851715ffde662afaa033f189fc9b1485ab6a73705c",
        (
            "windows-desktop-shell",
            "safe-exit-confirmation",
            "confirm-exit",
        ),
        (
            "windows-desktop-shell",
            "task-checkpoint-and-recovery-choice",
            "active-tasks-on-confirmed-exit",
        ),
        (
            "windows-desktop-shell",
            "task-checkpoint-and-recovery-choice",
            "checkpoint-exceeds-ten-seconds",
        ),
        (
            "windows-desktop-shell",
            "task-checkpoint-and-recovery-choice",
            "next-start-handles-incomplete-tasks",
        ),
    ),
    "V11-R-014": _authority(
        "platform",
        "security",
        "authenticated_desktop_core_vertical_slice",
        4,
        "cf0d87d285007b906d5ec52621c4ed3f8653b63827b8baf4011c16579e8af533",
        (
            "windows-desktop-shell",
            "sidecar-lifecycle-and-session-security",
            "unauthorized-local-request",
        ),
        (
            "windows-desktop-shell",
            "core-capability-compatibility-baseline",
            "use-existing-core-capability-after-desktopization",
        ),
    ),
    "V11-R-015": _authority(
        "publication",
        "publication",
        "unsigned_prerelease_exact_proof_reuse",
        4,
        "0aac23d06ff6a1cbfa7f70e966bbf0e843fd922b0eb72a0cff73852e416c8e08",
        (
            "trusted-windows-distribution",
            "independent-candidate-build-and-no-duplicate-release",
            "release-consumes-main-proof",
        ),
        (
            "trusted-windows-distribution",
            "trusted-code-signing-and-smartscreen-gate",
            "publish-unsigned-test-package",
        ),
    ),
}

AUTHORITATIVE_BEHAVIOR_KEYS: dict[str, str] = {
    item_id: entry["behavior_key"] for item_id, entry in CANONICAL_REQUIREMENTS.items()
}
AUTHORITATIVE_ACCEPTANCE_SHA256: dict[str, str] = {
    item_id: entry["acceptance_sha256"]
    for item_id, entry in CANONICAL_REQUIREMENTS.items()
}
AUTHORITATIVE_NON_GOAL_BEHAVIOR_KEYS: dict[str, str] = {
    item_id: entry["behavior_key"] for item_id, entry in CANONICAL_NON_GOALS.items()
}
V11_AUTHORITATIVE_BEHAVIOR_KEYS: dict[str, str] = {
    item_id: entry["behavior_key"]
    for item_id, entry in V11_CANONICAL_REQUIREMENTS.items()
}
V11_AUTHORITATIVE_ACCEPTANCE_SHA256: dict[str, str] = {
    item_id: entry["acceptance_sha256"]
    for item_id, entry in V11_CANONICAL_REQUIREMENTS.items()
}


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
    chinese = (repo_root / "README.md").read_text(encoding="utf-8")
    english = (repo_root / "README.en.md").read_text(encoding="utf-8")
    if (
        not chinese.splitlines()
        or chinese.splitlines()[0] != "[English](README.en.md)"
        or not english.splitlines()
        or english.splitlines()[0] != "[简体中文](README.md)"
        or "README.en.md" not in _markdown_link_targets(chinese)
        or "README.md" not in _markdown_link_targets(english)
    ):
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


def _validate_canonical_requirement(
    item: Mapping[str, Any],
    item_id: str,
    canonical_requirements: Mapping[str, Mapping[str, Any]],
) -> None:
    canonical = canonical_requirements[item_id]
    for field in ("category", "kind", "behavior_key", "owning_stage"):
        if item[field] != canonical[field]:
            raise ValidationError(
                f"{item_id}.{field} does not match the canonical requirement registry"
            )
    acceptance_sha256 = hashlib.sha256(item["acceptance"].encode("utf-8")).hexdigest()
    if acceptance_sha256 != canonical["acceptance_sha256"]:
        raise ValidationError(
            f"{item_id}.acceptance does not match the authoritative meaning"
        )
    refs = [
        (ref["capability"], ref["requirement"], ref["scenario"])
        for ref in item["source_refs"]
    ]
    if len(refs) != len(set(refs)):
        raise ValidationError(
            f"{item_id}.source_refs contains a duplicate canonical scenario"
        )
    if frozenset(refs) != canonical["source_refs"]:
        raise ValidationError(
            f"{item_id}.source_refs must equal the exact authoritative reference set"
        )


def _validate_canonical_non_goal(item: Mapping[str, Any], item_id: str) -> None:
    canonical = CANONICAL_NON_GOALS[item_id]
    for field in ("category", "kind", "behavior_key", "owning_stage"):
        if item[field] != canonical[field]:
            raise ValidationError(
                f"{item_id}.{field} does not match the canonical non-goal registry"
            )
    acceptance_sha256 = hashlib.sha256(item["acceptance"].encode("utf-8")).hexdigest()
    refs = frozenset(
        (ref["capability"], ref["requirement"], ref["scenario"])
        for ref in item["source_refs"]
    )
    if (
        acceptance_sha256 != canonical["acceptance_sha256"]
        or refs != canonical["source_refs"]
        or len(refs) != len(item["source_refs"])
    ):
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
    if not evidence_list or len(evidence_list) > 24:
        raise ValidationError(f"{item_id}.evidence must contain 1..24 assertions")
    validated: list[dict[str, Any]] = []
    seen_records: set[tuple[str, ...]] = set()
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
        identity: tuple[str, ...]
        if state == "manual":
            identity = ("manual", str(evidence["procedure_id"]))
        elif runner == "gate":
            identity = ("gate", str(evidence["gate_id"]))
        else:
            identity = (
                "selector",
                runner,
                str(evidence["path"]),
                str(evidence["selector"]),
            )
        if identity in seen_records:
            raise ValidationError(
                f"{item_id}.evidence contains a duplicate evidence record"
            )
        seen_records.add(identity)
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
    *,
    canonical_requirements: Mapping[str, Mapping[str, Any]] = CANONICAL_REQUIREMENTS,
    canonical_non_goals: Mapping[str, Mapping[str, Any]] = CANONICAL_NON_GOALS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    item = _expect_mapping(raw, expected_id)
    _expect_exact_fields(item, ITEM_FIELDS, expected_id)
    item_id = _expect_text(
        item["id"],
        f"{expected_id}.id",
        minimum=len(expected_id),
        maximum=len(expected_id),
    )
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
    if expected_id in canonical_requirements:
        _validate_canonical_requirement(item, expected_id, canonical_requirements)
    else:
        if expected_id not in canonical_non_goals:
            raise ValidationError(f"{expected_id} is outside the frozen authority")
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
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    timeout_seconds = RELEASE_EVIDENCE_TIMEOUT_BUDGET.collection_timeout_seconds
    started_at = time.monotonic()
    try:
        return subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            env=None if environment is None else {**os.environ, **environment},
        )
    except subprocess.TimeoutExpired as exc:
        elapsed_seconds = max(0.0, time.monotonic() - started_at)
        raise ValidationError(
            f"{runner} selector collection timed out after {elapsed_seconds:.3f}s "
            f"(configured {timeout_seconds}s)"
        ) from exc


def _collect_existing_selectors(
    items: Iterable[Mapping[str, Any]],
    repo_root: Path,
    tracked_paths: frozenset[str] | None = None,
    *,
    selector_runners: frozenset[str] | None = None,
) -> None:
    pytest_selectors: list[str] = []
    frontend: dict[tuple[str, str], list[str]] = {}
    gates: set[str] = set()
    for item in items:
        for evidence in item["evidence"]:
            if evidence["state"] != "existing":
                continue
            runner = evidence["runner"]
            if (
                runner in {"pytest", "vitest", "playwright"}
                and selector_runners is not None
                and runner not in selector_runners
            ):
                continue
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
            environment=(
                {"STOCK_DESK_PERFORMANCE_MODE": "1"}
                if runner == "playwright" and path.endswith("/performance.spec.ts")
                else None
            ),
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


def _evidence_gate_errors(
    items: Sequence[Mapping[str, Any]], *, mode: str
) -> list[str]:
    if mode == "mapping":
        return []
    planned = sorted(
        str(item["id"])
        for item in items
        if any(evidence["state"] == "planned" for evidence in item["evidence"])
    )
    incomplete_manual = sorted(
        str(item["id"])
        for item in items
        if any(
            evidence["state"] == "manual"
            and not evidence["completed"]
            and (
                mode == "release"
                or evidence["required_by_gate"] == "release-acceptance"
            )
            for evidence in item["evidence"]
        )
    )
    errors: list[str] = []
    if planned:
        errors.append("planned evidence: " + ", ".join(planned))
    if incomplete_manual:
        label = (
            "incomplete manual evidence"
            if mode == "release"
            else "incomplete release-acceptance manual evidence"
        )
        errors.append(f"{label}: " + ", ".join(incomplete_manual))
    return errors


def validate_manifest(
    matrix: dict[str, Any],
    *,
    repo_root: Path,
    mode: str,
    verify_selectors: bool = True,
    selector_runners: frozenset[str] | None = None,
) -> dict[str, int]:
    if not isinstance(mode, str) or mode not in {
        "mapping",
        "pre-publish",
        "release",
    }:
        raise ValidationError("mode must be mapping, pre-publish, or release")
    if selector_runners is not None and (
        not selector_runners
        or not selector_runners <= {"pytest", "vitest", "playwright"}
    ):
        raise ValidationError(
            "selector_runners must be a non-empty subset of pytest, vitest, playwright"
        )
    _reject_publication_boundary(matrix)
    _expect_exact_fields(matrix, ROOT_FIELDS, "manifest")
    if type(matrix["schema_version"]) is not int or matrix["schema_version"] != 1:
        raise ValidationError("schema_version must be integer 1")
    requirements = _expect_list(matrix["requirements"], "requirements")
    non_goals = _expect_list(matrix["non_goals"], "non_goals")
    expected_requirements = [f"R-{number:03d}" for number in range(1, 83)]
    expected_non_goals = [f"N-{number:03d}" for number in range(1, 11)]
    if len(requirements) != len(expected_requirements):
        raise ValidationError("requirements must contain exactly R-001 through R-082")
    if len(non_goals) != len(expected_non_goals):
        raise ValidationError("non_goals must contain exactly N-001 through N-010")
    if list(CANONICAL_REQUIREMENTS) != expected_requirements:
        raise ValidationError(
            "canonical requirement registry must contain exactly R-001 through R-082"
        )
    if list(CANONICAL_NON_GOALS) != expected_non_goals:
        raise ValidationError(
            "canonical non-goal registry must contain exactly N-001 through N-010"
        )
    if (
        list(AUTHORITATIVE_BEHAVIOR_KEYS) != expected_requirements
        or list(AUTHORITATIVE_ACCEPTANCE_SHA256) != expected_requirements
    ):
        raise ValidationError(
            "authoritative requirement contract must contain exactly R-001 through R-082"
        )
    if list(AUTHORITATIVE_NON_GOAL_BEHAVIOR_KEYS) != expected_non_goals:
        raise ValidationError(
            "authoritative non-goal contract must contain exactly N-001 through N-010"
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
    release_errors = _evidence_gate_errors(validated, mode=mode)
    if release_errors:
        raise ValidationError("; ".join(release_errors))
    if verify_selectors:
        _collect_existing_selectors(
            validated,
            repo_root,
            tracked_paths,
            selector_runners=selector_runners,
        )
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


def validate_v11_manifest(
    matrix: dict[str, Any],
    *,
    repo_root: Path,
    mode: str,
    verify_selectors: bool = True,
    selector_runners: frozenset[str] | None = None,
) -> dict[str, int]:
    if not isinstance(mode, str) or mode not in {
        "mapping",
        "pre-publish",
        "release",
    }:
        raise ValidationError("mode must be mapping, pre-publish, or release")
    if selector_runners is not None and (
        not selector_runners
        or not selector_runners <= {"pytest", "vitest", "playwright"}
    ):
        raise ValidationError(
            "selector_runners must be a non-empty subset of pytest, vitest, playwright"
        )
    _reject_publication_boundary(matrix)
    _expect_exact_fields(matrix, ROOT_FIELDS, "v1.1 manifest")
    if type(matrix["schema_version"]) is not int or matrix["schema_version"] != 1:
        raise ValidationError("v1.1 schema_version must be integer 1")
    requirements = _expect_list(matrix["requirements"], "v1.1 requirements")
    non_goals = _expect_list(matrix["non_goals"], "v1.1 non_goals")
    expected = [f"V11-R-{index:03d}" for index in range(1, 16)]
    if [
        item.get("id") if isinstance(item, dict) else None for item in requirements
    ] != expected:
        raise ValidationError(
            "v1.1 requirements must contain exactly V11-R-001 through V11-R-015"
        )
    if non_goals:
        raise ValidationError("v1.1 non_goals must be empty for the frozen increment")
    if list(V11_CANONICAL_REQUIREMENTS) != expected:
        raise ValidationError(
            "v1.1 canonical registry must contain exactly V11-R-001 through V11-R-015"
        )
    if (
        list(V11_AUTHORITATIVE_BEHAVIOR_KEYS) != expected
        or list(V11_AUTHORITATIVE_ACCEPTANCE_SHA256) != expected
    ):
        raise ValidationError(
            "v1.1 authoritative contract must contain exactly V11-R-001 through V11-R-015"
        )
    tracked_paths = _tracked_paths(repo_root)
    behavior_keys: set[str] = set()
    validated: list[dict[str, Any]] = []
    for raw, expected_id in zip(requirements, expected, strict=True):
        item, _ = _validate_item(
            raw,
            expected_id,
            repo_root,
            mode,
            behavior_keys,
            tracked_paths,
            canonical_requirements=V11_CANONICAL_REQUIREMENTS,
            canonical_non_goals={},
        )
        if item["kind"] == "non_goal":
            raise ValidationError(f"{expected_id} cannot use non_goal kind")
        validated.append(item)
    release_errors = _evidence_gate_errors(validated, mode=mode)
    if release_errors:
        raise ValidationError("; ".join(release_errors))
    if verify_selectors:
        _collect_existing_selectors(
            validated,
            repo_root,
            tracked_paths,
            selector_runners=selector_runners,
        )
    return {
        "requirements": len(requirements),
        "non_goals": 0,
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


def validate_authority_manifest(
    matrix: dict[str, Any],
    *,
    manifest_path: Path,
    repo_root: Path,
    mode: str,
    verify_selectors: bool = True,
    selector_runners: frozenset[str] | None = None,
) -> dict[str, int]:
    if manifest_path.name == "requirements.yml":
        return validate_manifest(
            matrix,
            repo_root=repo_root,
            mode=mode,
            verify_selectors=verify_selectors,
            selector_runners=selector_runners,
        )
    if manifest_path.name == "v1_1_requirements.yml":
        return validate_v11_manifest(
            matrix,
            repo_root=repo_root,
            mode=mode,
            verify_selectors=verify_selectors,
            selector_runners=selector_runners,
        )
    raise ValidationError(f"unsupported requirement authority: {manifest_path.name}")


def _validate_cross_authority_uniqueness(
    manifests: Mapping[str, Mapping[str, Any]],
) -> None:
    ids: set[str] = set()
    behaviors: set[str] = set()
    for namespace, manifest in manifests.items():
        for item in [*manifest["requirements"], *manifest["non_goals"]]:
            item_id = str(item["id"])
            behavior = str(item["behavior_key"])
            if item_id in ids:
                raise ValidationError(
                    f"authorities contain a duplicate requirement id: {item_id}"
                )
            if behavior in behaviors:
                raise ValidationError(
                    f"authorities contain a duplicate behavior_key: {behavior}"
                )
            ids.add(item_id)
            behaviors.add(behavior)


def validate_all_manifests(
    *,
    repo_root: Path,
    mode: str,
    verify_selectors: bool = True,
    selector_runners: frozenset[str] | None = None,
    manifests: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, int]:
    authorities = (
        dict(manifests)
        if manifests is not None
        else {
            "v1": load_manifest(repo_root / "tests/acceptance/requirements.yml"),
            "v1.1": load_manifest(repo_root / "tests/acceptance/v1_1_requirements.yml"),
        }
    )
    if set(authorities) != {"v1", "v1.1"}:
        raise ValidationError("both v1 and v1.1 authorities are required")
    v1_counts = validate_manifest(
        authorities["v1"],
        repo_root=repo_root,
        mode=mode,
        verify_selectors=False,
        selector_runners=selector_runners,
    )
    v11_counts = validate_v11_manifest(
        authorities["v1.1"],
        repo_root=repo_root,
        mode=mode,
        verify_selectors=False,
        selector_runners=selector_runners,
    )
    _validate_cross_authority_uniqueness(authorities)
    if verify_selectors:
        items = [
            *authorities["v1"]["requirements"],
            *authorities["v1"]["non_goals"],
            *authorities["v1.1"]["requirements"],
        ]
        _collect_existing_selectors(
            items,
            repo_root,
            _tracked_paths(repo_root),
            selector_runners=selector_runners,
        )
    return {
        "v1_requirements": v1_counts["requirements"],
        "v1_non_goals": v1_counts["non_goals"],
        "v11_requirements": v11_counts["requirements"],
        "planned": v1_counts["planned"] + v11_counts["planned"],
        "manual": v1_counts["manual"] + v11_counts["manual"],
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
    parser.add_argument(
        "--mode", required=True, choices=("mapping", "pre-publish", "release")
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    manifest_path = repo_root / "tests" / "acceptance" / "requirements.yml"
    document_path = repo_root / "docs" / "acceptance.md"
    try:
        counts = validate_all_manifests(repo_root=repo_root, mode=args.mode)
        verify_document_digest(manifest_path, document_path)
    except (OSError, ValidationError) as exc:
        print(f"requirement coverage error: {exc}", file=sys.stderr)
        return 1
    print(
        f"{counts['v1_requirements']}/{len(CANONICAL_REQUIREMENTS)} requirements mapped; "
        f"{counts['v11_requirements']}/{len(V11_CANONICAL_REQUIREMENTS)} v1.1 requirements mapped; "
        f"{counts['v1_non_goals']}/10 non-goals mapped to absence checks; "
        "existing selectors collect successfully; "
        "planned/manual evidence explicitly enumerated "
        f"({counts['planned']} planned, {counts['manual']} manual)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
