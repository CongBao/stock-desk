from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.performance.ten_year_a_share import (
    load_fixture_metadata,
    validate_performance_result,
)


CURRENT_PATH = Path(
    os.environ.get(
        "STOCK_DESK_PERFORMANCE_RESULT",
        "test-results/performance/current.json",
    )
)


@pytest.fixture(scope="module")
def performance_results() -> dict[str, object]:
    if not CURRENT_PATH.is_file():
        pytest.fail(
            "performance evidence is missing; run scripts/run_performance_baseline.py"
        )
    result = json.loads(CURRENT_PATH.read_text(encoding="utf-8"))
    fixture = load_fixture_metadata()
    validate_performance_result(result, expected_fixture_digest=fixture.content_digest)
    assert isinstance(result, dict)
    return result


def test_v1_cached_budgets(performance_results: dict[str, object]) -> None:
    metrics = performance_results["metrics"]
    assert isinstance(metrics, dict)
    for name, budget in (
        ("chart_cold", 2.0),
        ("chart_warm", 2.0),
        ("formula_preview", 3.0),
        ("single_backtest", 5.0),
    ):
        metric = metrics[name]
        assert isinstance(metric, dict)
        assert metric["p95_seconds"] <= budget
    pool = metrics["pool_ui"]
    assert isinstance(pool, dict)
    assert pool["long_task_count"] <= 1


def test_v1_correctness_and_measurement_evidence_is_complete(
    performance_results: dict[str, object],
) -> None:
    metrics = performance_results["metrics"]
    assert isinstance(metrics, dict)
    for name in ("chart_cold", "chart_warm", "formula_preview", "single_backtest"):
        metric = metrics[name]
        assert isinstance(metric, dict)
        samples = metric["samples"]
        assert isinstance(samples, list)
        assert len(samples) >= 20
        assert all(sample["external_wait_seconds"] == 0 for sample in samples)
        assert all(sample["provider_span_count"] == 0 for sample in samples)
