from __future__ import annotations

from copy import deepcopy
import math

import pytest

from tests.performance.ten_year_a_share import (
    MINIMUM_SAMPLE_COUNT,
    PerformanceGateError,
    nearest_rank_p95,
    validate_performance_result,
)


def _sample(index: int) -> dict[str, object]:
    wall = 0.1 + index / 1_000
    return {
        "wall_seconds": wall,
        "local_seconds": wall,
        "external_wait_seconds": 0.0,
        "provider_span_count": 0,
        "provider_spans": [],
        "blocked_external_request_count": 0,
        "rss_start_bytes": 10_000,
        "rss_peak_bytes": 12_000 + index,
        "rss_delta_bytes": 2_000 + index,
        "rss_process_set_digest": "sha256:" + "3" * 64,
        "correctness_hash": "sha256:" + "1" * 64,
    }


def _valid_result() -> dict[str, object]:
    samples = [_sample(index) for index in range(MINIMUM_SAMPLE_COUNT)]
    walls = [float(item["wall_seconds"]) for item in samples]
    metric = {
        "samples": samples,
        "mean_seconds": sum(walls) / len(walls),
        "p95_seconds": nearest_rank_p95(walls),
        "budget_seconds": 2.0,
        "correctness_hash": "sha256:" + "1" * 64,
    }
    return {
        "schema_version": "stock-desk-performance-v1",
        "measured_at_utc": "2026-07-08T02:00:00Z",
        "git": {"sha": "a" * 40, "dirty": False},
        "fixture": {
            "fixture_id": "ten-year-a-share",
            "content_digest": "sha256:" + "a" * 64,
            "row_count": 2_609,
            "scoring_sessions": 2_609,
            "network_policy": "forbidden",
        },
        "environment": {
            "os": "TestOS",
            "arch": "test64",
            "cpu_model": "Test CPU",
            "logical_cpu_count": 4,
            "effective_cpu_count": 4.0,
            "memory_bytes": 16 * 1024**3,
            "effective_memory_bytes": 16 * 1024**3,
            "python_version": "3.12.0",
            "node_version": "v24.0.0",
            "browser_version": "Chromium 1",
            "tool_versions": {"duckdb": "1", "playwright": "1"},
        },
        "process_tree": {
            "declared_roots": [1, 2, 3, 4, 5],
            "declared_services": [
                {"pid": 2, "role": "api"},
                {"pid": 3, "role": "worker"},
                {"pid": 4, "role": "web"},
            ],
            "sampled_process_roles": [
                "api",
                "worker",
                "web",
                "browser",
                "playwright",
            ],
        },
        "definitions": {
            "chart_cold": "new browser context and empty HTTP/browser cache",
            "chart_warm": "same process after one unmeasured completed render",
            "formula_cache_cold": "new FormulaService result cache after untimed seed",
            "single_backtest_fresh": "new submitted task and persisted report per sample",
        },
        "metrics": {
            "chart_cold": deepcopy(metric),
            "chart_warm": deepcopy(metric),
            "formula_preview": {**deepcopy(metric), "budget_seconds": 3.0},
            "single_backtest": {**deepcopy(metric), "budget_seconds": 5.0},
            "pool_ui": {
                "samples": [
                    {
                        "long_task_count": 0,
                        "interaction_kind": (
                            "navigation"
                            if index == MINIMUM_SAMPLE_COUNT - 2
                            else "cancel"
                            if index == MINIMUM_SAMPLE_COUNT - 1
                            else "progress"
                        ),
                        "interactive": True,
                        "correctness_hash": "sha256:" + "2" * 64,
                    }
                    for index in range(MINIMUM_SAMPLE_COUNT)
                ],
                "long_task_count": 0,
                "observed_progress_states": ["queued:queued:0", "running:running:1"],
                "worker_claim_observed": True,
                "cancel_status": "cancelled",
                "correctness_hash": "sha256:" + "2" * 64,
            },
        },
    }


def test_gate_recomputes_nearest_rank_p95_and_accepts_complete_evidence() -> None:
    result = _valid_result()
    validate_performance_result(
        result,
        expected_fixture_digest="sha256:" + "a" * 64,
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["metrics"]["chart_cold"].update(p95_seconds=0.01), "p95"),
        (lambda value: value["metrics"]["chart_warm"]["samples"].pop(), "20 raw"),
        (
            lambda value: value["metrics"]["formula_preview"]["samples"][0].update(
                wall_seconds=math.nan
            ),
            "finite",
        ),
        (
            lambda value: value["metrics"]["single_backtest"]["samples"][0].update(
                local_seconds=-1.0
            ),
            "non-negative",
        ),
        (
            lambda value: value["metrics"]["chart_cold"]["samples"][0].pop(
                "rss_peak_bytes"
            ),
            "RSS",
        ),
        (
            lambda value: value["metrics"]["chart_cold"]["samples"][0].pop(
                "provider_span_count"
            ),
            "provider-span",
        ),
        (
            lambda value: value["metrics"]["chart_cold"]["samples"][0].update(
                provider_span_count=1
            ),
            "provider-span",
        ),
        (
            lambda value: value["metrics"]["chart_cold"]["samples"][0].update(
                correctness_hash="sha256:" + "9" * 64
            ),
            "correctness",
        ),
        (
            lambda value: value["metrics"]["pool_ui"]["samples"][0].update(
                long_task_count=1
            ),
            "Long Task",
        ),
        (
            lambda value: value["environment"].update(effective_cpu_count=3.0),
            "four effective",
        ),
    ],
)
def test_gate_rejects_untrustworthy_or_incomplete_evidence(
    mutation: object, message: str
) -> None:
    result = _valid_result()
    assert callable(mutation)
    mutation(result)

    with pytest.raises(PerformanceGateError, match=message):
        validate_performance_result(
            result,
            expected_fixture_digest="sha256:" + "a" * 64,
        )


def test_gate_rejects_a_stale_fixture_digest() -> None:
    with pytest.raises(PerformanceGateError, match="fixture digest"):
        validate_performance_result(
            _valid_result(),
            expected_fixture_digest="sha256:" + "b" * 64,
        )


def test_nominal_sixteen_gb_baseline_accepts_fifteen_gib_usable() -> None:
    result = _valid_result()
    result["environment"]["effective_memory_bytes"] = 15 * 1024**3

    validate_performance_result(
        result,
        expected_fixture_digest="sha256:" + "a" * 64,
    )


def test_nominal_sixteen_gb_baseline_rejects_below_fifteen_gib_usable() -> None:
    result = _valid_result()
    result["environment"]["effective_memory_bytes"] = 15 * 1024**3 - 1

    with pytest.raises(PerformanceGateError, match="nominal 16GB"):
        validate_performance_result(
            result,
            expected_fixture_digest="sha256:" + "a" * 64,
        )
