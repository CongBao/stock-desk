from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math

import pytest

from tests.performance.ten_year_a_share import (
    MINIMUM_SAMPLE_COUNT,
    PerformanceGateError,
    nearest_rank_p95,
    validate_performance_result,
)


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


ROLES = ["api", "browser", "playwright", "web", "worker"]


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
        "correctness_hash": "sha256:" + "1" * 64,
    }


def _progress(
    *,
    status: str = "running",
    stage: str = "executing",
    processed: int,
) -> dict[str, object]:
    return {
        "status": status,
        "stage": stage,
        "processed": processed,
        "total": 40,
        "failed": 0,
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
    semantic = {
        "formula_checksum": "sha256:" + "4" * 64,
        "pool_membership_digest": "sha256:" + "5" * 64,
        "pool_data_digest": "sha256:" + "6" * 64,
        "terminal_status": "cancelled",
    }
    pool_correctness = _digest(semantic)
    progress_states = [_progress(processed=index + 1) for index in range(18)]
    pool_samples = []
    for index in range(MINIMUM_SAMPLE_COUNT):
        if index < 18:
            interaction = "progress"
            state = progress_states[index]
        elif index == 18:
            interaction = "navigation"
            state = _progress(processed=19)
        else:
            interaction = "cancel"
            state = _progress(status="cancelled", stage="cancelled", processed=19)
        pool_samples.append(
            {
                "long_task_count": 0,
                "interaction_kind": interaction,
                "interactive": True,
                "rendered_state": deepcopy(state),
                "api_state": deepcopy(state),
                "correctness_hash": pool_correctness,
            }
        )
    return {
        "schema_version": "stock-desk-performance-v1",
        "evidence_kind": "reference",
        "measured_at_utc": "2026-07-08T02:00:00Z",
        "git": {"sha": "a" * 40, "dirty": False},
        "fixture": {
            "fixture_id": "ten-year-a-share",
            "content_digest": "sha256:" + "a" * 64,
            "row_count": 2_609,
            "scoring_sessions": 2_609,
            "scope_instrument_count": 5_000,
            "runnable_symbol_count": 40,
            "network_policy": "forbidden",
        },
        "environment": {
            "os": "Darwin 25.5.0",
            "arch": "arm64",
            "cpu_model": "Test CPU",
            "logical_cpu_count": 14,
            "effective_cpu_count": 14.0,
            "memory_bytes": 24 * 1024**3,
            "effective_memory_bytes": 24 * 1024**3,
            "python_version": "3.12.0",
            "node_version": "v24.0.0",
            "browser_version": "Chromium 1",
            "tool_versions": {
                "duckdb": "1.4.5",
                "playwright": "Version 1.61.1",
                "pnpm": "11.7.0",
            },
            "runner": {
                "provider": "local",
                "os": "Darwin",
                "arch": "arm64",
                "name": "test-host",
                "image_os": None,
                "image_version": None,
                "repository": None,
                "run_id": None,
                "run_attempt": None,
            },
        },
        "process_tree": {
            "declared_roots": [1, 2, 3, 4, 5],
            "declared_services": [
                {"pid": 2, "role": "api"},
                {"pid": 3, "role": "worker"},
                {"pid": 4, "role": "web"},
            ],
            "sampled_process_roles": ROLES,
            "role_set_digest": _digest(ROLES),
        },
        "definitions": {
            "chart_cold": "20 raw cold contexts; timer includes finished and interaction handshake",
            "chart_warm": "20 raw windows on one shared warm page; same timer boundary",
            "formula_cache_cold": "20 raw immutable formula-version windows",
            "single_backtest_fresh": "20 raw fresh worker-persisted report windows",
            "pool_ui": "20 windows from one pool task: 18 progress, navigation, cancel",
        },
        "metrics": {
            "chart_cold": deepcopy(metric),
            "chart_warm": deepcopy(metric),
            "formula_preview": {**deepcopy(metric), "budget_seconds": 3.0},
            "single_backtest": {**deepcopy(metric), "budget_seconds": 5.0},
            "pool_ui": {
                "samples": pool_samples,
                "long_task_count": 0,
                "observed_progress_states": progress_states,
                "worker_claim_observed": True,
                "cancel_status": "cancelled",
                "semantic_evidence": semantic,
                "correctness_hash": pool_correctness,
            },
        },
    }


def test_gate_recomputes_nearest_rank_p95_and_accepts_complete_reference() -> None:
    validate_performance_result(
        _valid_result(), expected_fixture_digest="sha256:" + "a" * 64
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(extra=True), "exact keys"),
        (lambda value: value["git"].update(extra=True), "exact keys"),
        (lambda value: value.update(measured_at_utc="2026-07-08 02:00:00Z"), "UTC"),
        (
            lambda value: value.update(measured_at_utc="2026-07-08T10:00:00+08:00"),
            "UTC",
        ),
        (lambda value: value["git"].update(dirty=True), "clean"),
        (
            lambda value: value["environment"].update(effective_cpu_count=math.nan),
            "finite",
        ),
        (lambda value: value["environment"].update(logical_cpu_count=True), "integer"),
        (
            lambda value: value["environment"]["tool_versions"].update(
                playwright="unavailable"
            ),
            "tool version",
        ),
        (
            lambda value: value["process_tree"].update(declared_roots=[1, 2, 2, 4, 5]),
            "unique",
        ),
        (
            lambda value: value["process_tree"].update(declared_roots=[0, 2, 3, 4, 5]),
            "positive",
        ),
        (
            lambda value: value["process_tree"]["declared_services"][0].update(pid=99),
            "root",
        ),
        (
            lambda value: value["process_tree"].update(
                role_set_digest="sha256:" + "9" * 64
            ),
            "role-set digest",
        ),
        (
            lambda value: value["metrics"]["chart_cold"].update(p95_seconds=0.01),
            "p95",
        ),
        (lambda value: value["metrics"]["chart_warm"]["samples"].pop(), "20 raw"),
        (
            lambda value: value["metrics"]["formula_preview"]["samples"][0].update(
                wall_seconds=math.nan
            ),
            "finite",
        ),
        (
            lambda value: value["metrics"]["single_backtest"]["samples"][0].update(
                unexpected=True
            ),
            "exact keys",
        ),
        (
            lambda value: value["metrics"]["chart_cold"]["samples"][0].update(
                provider_span_count=1,
                provider_spans=[{"source": "tushare", "decision": "unavailable"}],
            ),
            "duration unavailable",
        ),
        (
            lambda value: value["metrics"]["pool_ui"]["samples"][0]["api_state"].update(
                processed=2
            ),
            "rendered/API",
        ),
        (
            lambda value: value["metrics"]["pool_ui"].update(
                correctness_hash="sha256:" + "9" * 64
            ),
            "semantic",
        ),
    ],
)
def test_gate_rejects_forged_or_incomplete_evidence(
    mutation: object, message: str
) -> None:
    result = _valid_result()
    assert callable(mutation)
    mutation(result)

    with pytest.raises(PerformanceGateError, match=message):
        validate_performance_result(
            result, expected_fixture_digest="sha256:" + "a" * 64
        )


def test_target_baseline_requires_github_ubuntu_x64_exact_four_cpu() -> None:
    result = _valid_result()
    result["evidence_kind"] = "target_baseline"
    result["environment"].update(
        logical_cpu_count=4,
        effective_cpu_count=4.0,
        memory_bytes=16 * 1024**3,
        effective_memory_bytes=15 * 1024**3,
    )
    result["environment"]["runner"].update(
        provider="github_actions",
        os="Linux",
        arch="X64",
        name="GitHub Actions 1",
        image_os="ubuntu24",
        image_version="20260701.1",
        repository="owner/repository",
        run_id="1234",
        run_attempt="1",
    )
    validate_performance_result(result, expected_fixture_digest="sha256:" + "a" * 64)

    result["environment"]["effective_cpu_count"] = 14.0
    with pytest.raises(PerformanceGateError, match="exactly four"):
        validate_performance_result(
            result, expected_fixture_digest="sha256:" + "a" * 64
        )


def test_gate_rejects_a_stale_fixture_digest() -> None:
    with pytest.raises(PerformanceGateError, match="fixture digest"):
        validate_performance_result(
            _valid_result(), expected_fixture_digest="sha256:" + "b" * 64
        )


def test_reference_accepts_fifteen_gib_usable() -> None:
    result = _valid_result()
    result["environment"]["effective_memory_bytes"] = 15 * 1024**3
    validate_performance_result(result, expected_fixture_digest="sha256:" + "a" * 64)


def test_reference_rejects_below_fifteen_gib_usable() -> None:
    result = _valid_result()
    result["environment"]["effective_memory_bytes"] = 15 * 1024**3 - 1
    with pytest.raises(PerformanceGateError, match="nominal 16GB"):
        validate_performance_result(
            result, expected_fixture_digest="sha256:" + "a" * 64
        )
