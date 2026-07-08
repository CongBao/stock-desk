from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

import pytest

from tests.acceptance.failure_matrix_harness import (
    SECRET,
    FailureHarness,
    FailureResult,
)


FAILURES = (
    "data_permission",
    "data_timeout",
    "corrupt_tdx",
    "missing_60m",
    "formula_syntax",
    "future_formula",
    "pool_symbol_failure",
    "model_auth",
    "model_rate_limit",
    "critical_evidence_gap",
)

EXPECTED_FAILURES = {
    "data_permission": FailureResult(
        user_message="provider permission was denied",
        recovery_action="check provider token and data permissions",
        typed_reason="permission_denied",
        scope="600000.SH",
        source="tushare",
    ),
    "data_timeout": FailureResult(
        user_message="provider request timed out",
        recovery_action="retry the provider connection",
        typed_reason="timeout",
        scope="600000.SH",
        source="tushare",
    ),
    "corrupt_tdx": FailureResult(
        user_message="TDX vipdoc contents are corrupt",
        recovery_action="repair the TDX day file or select another data source",
        typed_reason="corrupt",
        scope="TDX vipdoc",
        source="tdx_local",
    ),
    "missing_60m": FailureResult(
        user_message="provider does not support this request",
        recovery_action="configure a provider with 60-minute coverage",
        typed_reason="unsupported",
        scope="600000.SH/60m",
        source="tdx_local",
    ),
    "formula_syntax": FailureResult(
        user_message="Formula is outside the supported TDX-compatible grammar.",
        recovery_action="edit formula at line 1, column 13",
        typed_reason="formula_syntax_error",
        scope="line:1:column:13",
        source="formula_engine",
    ),
    "future_formula": FailureResult(
        user_message="argument N is below its minimum",
        recovery_action="remove future reference at line 1",
        typed_reason="future_data",
        scope="line:1:column:15",
        source="formula_engine",
    ),
    "pool_symbol_failure": FailureResult(
        user_message="000001.SZ: missing_signal_data",
        recovery_action="refresh the failed symbol data and rerun the pool",
        typed_reason="missing_signal_data",
        scope="000001.SZ",
        source="backtest_pool",
        safe_status="partial",
        partial_preserved=True,
    ),
    "model_auth": FailureResult(
        user_message="model authentication failed",
        recovery_action="update the model credentials and test the connection",
        typed_reason="model_authentication",
        scope="analysis_model",
        source="model_provider",
    ),
    "model_rate_limit": FailureResult(
        user_message="model request was rate limited",
        recovery_action="retry after the provider rate-limit window",
        typed_reason="model_rate_limit",
        scope="analysis_model",
        source="model_provider",
    ),
    "critical_evidence_gap": FailureResult(
        user_message=(
            "critical evidence is missing: market,fundamentals,announcements,news"
        ),
        recovery_action=(
            "refresh_market_evidence,refresh_fundamentals_evidence,"
            "refresh_announcements_evidence,refresh_news_evidence"
        ),
        typed_reason="insufficient_evidence",
        scope="600000.SH",
        source="research_data",
        safe_status="insufficient_evidence",
    ),
}


@pytest.fixture
def failure_harness(tmp_path: Path) -> FailureHarness:
    return FailureHarness(tmp_path)


@pytest.mark.parametrize("failure", FAILURES)
def test_cross_domain_errors_are_actionable_and_redacted(
    failure: str,
    failure_harness: FailureHarness,
) -> None:
    result = failure_harness.trigger(failure)

    assert result == EXPECTED_FAILURES[failure]
    assert SECRET not in result.user_message
    assert SECRET not in result.recovery_action
    assert SECRET not in result.typed_reason
    assert SECRET not in result.scope
    assert SECRET not in result.source
    assert SECRET not in json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    assert not result.contains_secret


def test_pool_symbol_failure_preserves_valid_partial_work(
    failure_harness: FailureHarness,
) -> None:
    result = failure_harness.trigger("pool_symbol_failure")

    assert result.safe_status == "partial"
    assert result.partial_preserved is True
    assert result.typed_reason == "missing_signal_data"


def test_critical_evidence_gap_suppresses_rating_and_lists_recovery(
    failure_harness: FailureHarness,
) -> None:
    result = failure_harness.trigger("critical_evidence_gap")

    assert result.safe_status == "insufficient_evidence"
    assert result.rating is None
    assert "refresh_market_evidence" in result.recovery_action
    assert "refresh_fundamentals_evidence" in result.recovery_action
