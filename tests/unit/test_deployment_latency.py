from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import scripts.deployment_latency as latency
from scripts.deployment_latency import (
    DeploymentLatencyError,
    aggregate_ledger,
    append_sample,
    empty_ledger,
    ledger_seal,
    main,
    validate_ledger,
    validate_sample,
)


SHA = "a" * 40
TREE = "b" * 40


def _sample(
    run_id: str,
    *,
    attempt: int = 1,
    category: str = "proved-tag-to-release",
    wall_seconds: float = 60.0,
    queue_seconds: float = 10.0,
    outcome: str = "success",
    invalidated: bool = False,
    invalidation_reason: str | None = None,
    workflow: str = "Formal stable release",
    ref: str = "refs/tags/v1.1.0",
    baseline_change: dict[str, str] | None = None,
) -> dict[str, object]:
    # The timestamps deliberately agree with the raw queue/wall observations.
    minute = int(wall_seconds // 60)
    second = int(wall_seconds % 60)
    sample: dict[str, object] = {
        "schema_version": "stock-desk-deployment-latency-sample-v1",
        "run_id": run_id,
        "run_attempt": attempt,
        "run_url": (
            "https://github.com/CongBao/stock-desk/actions/runs/"
            f"{run_id}/attempts/{attempt}"
        ),
        "source_sha": SHA,
        "source_tree": TREE,
        "workflow": workflow,
        "ref": ref,
        "category": category,
        "queued_at": "2026-07-15T00:00:00Z",
        "started_at": f"2026-07-15T00:00:{int(queue_seconds):02d}Z",
        "completed_at": f"2026-07-15T00:{minute:02d}:{second + int(queue_seconds):02d}Z",
        "queue_seconds": queue_seconds,
        "wall_seconds": wall_seconds,
        "cache_status": "hit",
        "outcome": outcome,
        "environment_baseline": {
            "os": "windows-2022",
            "architecture": "x86_64",
            "runner_image": "windows-2022@20260701.1",
            "toolchain": "python-3.12,node-22,rust-1.88",
        },
        "invalidated": invalidated,
        "invalidation_reason": invalidation_reason,
    }
    if baseline_change is not None:
        baseline = sample["environment_baseline"]
        assert isinstance(baseline, dict)
        baseline.update(baseline_change)
    return sample


def _ledger_with(samples: list[dict[str, object]]) -> dict[str, object]:
    ledger = empty_ledger()
    for sample in samples:
        expected_seal = ledger_seal(ledger) if ledger["record_count"] else None
        ledger = append_sample(ledger, sample, expected_seal=expected_seal)
    return ledger


def test_sample_requires_unambiguous_raw_identity_timing_and_baseline() -> None:
    validate_sample(_sample("100"))

    for missing in (
        "run_id",
        "run_attempt",
        "run_url",
        "source_sha",
        "source_tree",
        "workflow",
        "ref",
        "category",
        "queued_at",
        "started_at",
        "completed_at",
        "queue_seconds",
        "wall_seconds",
        "cache_status",
        "outcome",
        "environment_baseline",
        "invalidated",
        "invalidation_reason",
    ):
        sample = _sample("100")
        del sample[missing]
        with pytest.raises(DeploymentLatencyError, match=missing):
            validate_sample(sample)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("queue_seconds", -1, "non-negative"),
        ("wall_seconds", -1, "non-negative"),
        ("queued_at", "2026-07-15T00:00:00", "UTC"),
        ("started_at", "2026-07-15T08:00:10+08:00", "UTC"),
    ],
)
def test_sample_rejects_negative_duration_and_timezone_ambiguity(
    field: str, value: object, message: str
) -> None:
    sample = _sample("100")
    sample[field] = value
    with pytest.raises(DeploymentLatencyError, match=message):
        validate_sample(sample)


def test_sample_rejects_inconsistent_raw_timing_and_unexplained_invalidation() -> None:
    sample = _sample("100")
    sample["wall_seconds"] = 59
    with pytest.raises(DeploymentLatencyError, match="wall_seconds"):
        validate_sample(sample)

    invalid = _sample("101", invalidated=True)
    with pytest.raises(DeploymentLatencyError, match="invalidation_reason"):
        validate_sample(invalid)

    valid = _sample("102", invalidation_reason="operator did not like the result")
    with pytest.raises(DeploymentLatencyError, match="invalidation_reason"):
        validate_sample(valid)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema_version": "old"}, "schema_version"),
        ({"run_id": "bad id"}, "run_id"),
        (
            {"run_url": "https://github.com/CongBao/stock-desk/actions/runs/999"},
            "run_url",
        ),
        ({"run_attempt": 0}, "run_attempt"),
        ({"run_attempt": True}, "run_attempt"),
        ({"source_sha": "main"}, "source_sha"),
        ({"source_tree": "B" * 40}, "source_tree"),
        ({"workflow": ""}, "workflow"),
        ({"ref": "main"}, "ref"),
        ({"category": "main-to-stable"}, "category"),
        ({"cache_status": "maybe"}, "cache_status"),
        ({"outcome": "unknown"}, "outcome"),
        ({"invalidated": "no"}, "invalidated"),
    ],
)
def test_sample_rejects_invalid_identity_and_enum_values(
    change: dict[str, object], message: str
) -> None:
    sample = _sample("100")
    sample.update(change)
    with pytest.raises(DeploymentLatencyError, match=message):
        validate_sample(sample)


def test_sample_rejects_unknown_fields_and_incomplete_baseline() -> None:
    extra = _sample("100")
    extra["selected"] = True
    with pytest.raises(DeploymentLatencyError, match="unknown field"):
        validate_sample(extra)

    wrong_type = _sample("101")
    wrong_type["environment_baseline"] = "windows"
    with pytest.raises(DeploymentLatencyError, match="environment_baseline"):
        validate_sample(wrong_type)

    missing = _sample("102")
    del missing["environment_baseline"]["runner_image"]
    with pytest.raises(DeploymentLatencyError, match="runner_image"):
        validate_sample(missing)

    blank = _sample("103")
    blank["environment_baseline"]["toolchain"] = ""
    with pytest.raises(DeploymentLatencyError, match="toolchain"):
        validate_sample(blank)


@pytest.mark.parametrize(
    ("field", "maximum"),
    [
        ("workflow", 256),
        ("ref", 512),
        ("invalidation_reason", 1024),
    ],
)
def test_sample_runtime_string_limits_match_schema(field: str, maximum: int) -> None:
    sample = _sample(
        "100", invalidated=field == "invalidation_reason", invalidation_reason="valid"
    )
    sample[field] = "x" * (maximum + 1)
    with pytest.raises(DeploymentLatencyError, match=f"at most {maximum}"):
        validate_sample(sample)


@pytest.mark.parametrize(
    ("field", "maximum"),
    [("os", 256), ("architecture", 64), ("runner_image", 256), ("toolchain", 1024)],
)
def test_baseline_runtime_string_limits_match_schema(field: str, maximum: int) -> None:
    sample = _sample("100", baseline_change={field: "x" * (maximum + 1)})
    with pytest.raises(DeploymentLatencyError, match=f"at most {maximum}"):
        validate_sample(sample)


def test_runtime_string_limit_table_is_identical_to_sample_schema() -> None:
    schema = json.loads(
        Path("schemas/deployment-latency-sample-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    properties = schema["properties"]
    baseline = properties["environment_baseline"]["properties"]
    assert latency._STRING_LIMITS == {
        "workflow": properties["workflow"]["maxLength"],
        "ref": properties["ref"]["maxLength"],
        "environment_baseline.os": baseline["os"]["maxLength"],
        "environment_baseline.architecture": baseline["architecture"]["maxLength"],
        "environment_baseline.runner_image": baseline["runner_image"]["maxLength"],
        "environment_baseline.toolchain": baseline["toolchain"]["maxLength"],
        "invalidation_reason": properties["invalidation_reason"]["maxLength"],
    }


def test_append_rejects_duplicate_run_attempt() -> None:
    ledger = append_sample(empty_ledger(), _sample("100"))

    with pytest.raises(DeploymentLatencyError, match="expected seal"):
        append_sample(ledger, _sample("101"))

    with pytest.raises(DeploymentLatencyError, match="duplicate"):
        append_sample(ledger, _sample("100"), expected_seal=ledger_seal(ledger))


def test_hash_chain_rejects_historical_modification_and_middle_deletion() -> None:
    ledger = _ledger_with([_sample("100"), _sample("101", outcome="failure")])

    modified = copy.deepcopy(ledger)
    modified["records"][0]["sample"]["wall_seconds"] = 1
    with pytest.raises(DeploymentLatencyError, match="record_hash"):
        validate_ledger(modified)

    deleted = copy.deepcopy(ledger)
    del deleted["records"][0]
    deleted["record_count"] = 1
    with pytest.raises(DeploymentLatencyError, match="ordinal|previous_hash"):
        validate_ledger(deleted)


def test_seal_rejects_tail_deletion_and_selective_deletion() -> None:
    ledger = _ledger_with([_sample("100"), _sample("101", outcome="failure")])
    expected = ledger_seal(ledger)

    truncated = copy.deepcopy(ledger)
    truncated["records"].pop()
    truncated["record_count"] = 1
    truncated["head_hash"] = truncated["records"][-1]["record_hash"]
    validate_ledger(truncated)
    with pytest.raises(DeploymentLatencyError, match="sealed record count"):
        aggregate_ledger(truncated, expected_seal=expected)

    with pytest.raises(DeploymentLatencyError, match="complete sealed ledger"):
        aggregate_ledger(ledger["records"][:1], expected_seal=expected)


def test_fewer_than_five_samples_per_category_is_incomplete() -> None:
    ledger = _ledger_with([_sample(str(index)) for index in range(100, 104)])
    report = aggregate_ledger(ledger, expected_seal=ledger_seal(ledger))

    category = report["categories"]["proved-tag-to-release"]
    assert category["sample_count"] == 4
    assert category["status"] == "incomplete"
    assert category["minimum_sample_count"] == 5
    assert category["queue_seconds"] == {"p50": None, "p95": None}
    assert category["wall_seconds"] == {"p50": None, "p95": None}


def test_complete_report_uses_every_success_failure_and_invalidated_sample() -> None:
    samples = [
        _sample("100", wall_seconds=10, outcome="success"),
        _sample("101", wall_seconds=20, outcome="failure"),
        _sample(
            "102",
            wall_seconds=30,
            outcome="cancelled",
            invalidated=True,
            invalidation_reason="runner outage",
        ),
        _sample("103", wall_seconds=40, outcome="success"),
        _sample("104", wall_seconds=100, outcome="failure"),
    ]
    ledger = _ledger_with(samples)
    report = aggregate_ledger(ledger, expected_seal=ledger_seal(ledger))

    category = report["categories"]["proved-tag-to-release"]
    assert category["status"] == "complete"
    assert category["sample_count"] == 5
    assert category["included_sample_ids"] == [
        "100/1",
        "101/1",
        "102/1",
        "103/1",
        "104/1",
    ]
    assert category["outcome_counts"] == {
        "cancelled": 1,
        "failure": 2,
        "success": 2,
    }
    assert category["invalidated_count"] == 1
    # Nearest-rank over every raw sample: no success-only or fastest-only filter.
    assert category["wall_seconds"] == {"p50": 30.0, "p95": 100.0}
    assert report["source_record_count"] == 5
    assert report["source_head_hash"] == ledger["head_hash"]


def test_retries_cannot_inflate_five_run_completeness() -> None:
    ledger = _ledger_with(
        [
            _sample(
                "100", attempt=attempt, outcome="failure" if attempt < 5 else "success"
            )
            for attempt in range(1, 6)
        ]
    )
    category = aggregate_ledger(ledger, expected_seal=ledger_seal(ledger))[
        "categories"
    ]["proved-tag-to-release"]

    assert category["sample_count"] == 5
    assert category["active_segment_sample_count"] == 5
    assert category["consecutive_run_count"] == 1
    assert category["status"] == "incomplete"
    assert category["queue_seconds"] == {"p50": None, "p95": None}
    assert category["outcome_counts"] == {"failure": 4, "success": 1}


def test_fast_retries_cannot_lower_distinct_run_percentiles() -> None:
    samples = [_sample(str(run_id), wall_seconds=1000) for run_id in range(100, 105)]
    samples.extend(
        _sample("104", attempt=attempt, wall_seconds=1) for attempt in range(2, 102)
    )
    ledger = _ledger_with(samples)
    category = aggregate_ledger(ledger, expected_seal=ledger_seal(ledger))[
        "categories"
    ]["proved-tag-to-release"]

    assert category["status"] == "complete"
    assert category["sample_count"] == 105
    assert category["consecutive_run_count"] == 5
    assert category["wall_seconds"] == {"p50": 1000.0, "p95": 1000.0}
    assert category["percentile_method"] == (
        "nearest-rank-over-slowest-attempt-per-distinct-run"
    )
    representatives = category["run_duration_representatives"]
    assert len(representatives) == 5
    assert representatives[-1]["run_id"] == "104"
    assert representatives[-1]["wall_seconds"] == 1000.0
    assert len(representatives[-1]["included_sample_ids"]) == 101


def test_report_always_emits_all_six_categories_as_explicitly_incomplete() -> None:
    ledger = empty_ledger()
    report = aggregate_ledger(ledger, expected_seal=ledger_seal(ledger))

    assert set(report["categories"]) == {
        "typical-pr",
        "high-risk-pr",
        "main",
        "candidate",
        "signpath-queue",
        "proved-tag-to-release",
    }
    for category in report["categories"].values():
        assert category["status"] == "incomplete"
        assert category["sample_count"] == 0
        assert category["consecutive_run_count"] == 0
        assert category["queue_seconds"] == {"p50": None, "p95": None}
        assert category["wall_seconds"] == {"p50": None, "p95": None}
        assert category["active_identity_hash"] is None
        assert category["active_comparison_identity"] is None
        assert category["run_duration_representatives"] == []
        assert category["comparison_groups"] == []


def test_identity_drift_resets_streak_and_is_reported_transparently() -> None:
    samples = [_sample(str(run_id)) for run_id in range(100, 104)]
    samples.append(
        _sample(
            "104",
            workflow="Formal stable release v2",
            ref="refs/tags/v1.1.1",
            baseline_change={"runner_image": "windows-2022@20260715.1"},
            outcome="failure",
            invalidated=True,
            invalidation_reason="runner image drift",
        )
    )
    ledger = _ledger_with(samples)
    category = aggregate_ledger(ledger, expected_seal=ledger_seal(ledger))[
        "categories"
    ]["proved-tag-to-release"]

    assert category["sample_count"] == 5
    assert category["active_segment_sample_count"] == 1
    assert category["consecutive_run_count"] == 1
    assert category["status"] == "incomplete"
    assert category["drift_detected"] is True
    assert len(category["comparison_groups"]) == 2
    assert category["comparison_groups"][0]["distinct_run_count"] == 4
    assert category["comparison_groups"][1]["distinct_run_count"] == 1
    assert category["active_comparison_identity"] == {
        "category": "proved-tag-to-release",
        "workflow": "Formal stable release v2",
        "ref": "refs/tags/v1.1.1",
        "environment_baseline": samples[-1]["environment_baseline"],
    }
    # The active drift segment still includes failed and invalidated evidence.
    assert category["outcome_counts"] == {"failure": 1}
    assert category["invalidated_count"] == 1


def test_five_new_comparable_runs_complete_after_an_identity_drift() -> None:
    samples = [_sample("100")]
    samples.extend(
        _sample(
            str(run_id),
            baseline_change={"runner_image": "windows-2022@20260715.1"},
        )
        for run_id in range(101, 106)
    )
    ledger = _ledger_with(samples)
    category = aggregate_ledger(ledger, expected_seal=ledger_seal(ledger))[
        "categories"
    ]["proved-tag-to-release"]

    assert category["status"] == "complete"
    assert category["sample_count"] == 6
    assert category["active_segment_sample_count"] == 5
    assert category["consecutive_run_count"] == 5
    assert category["included_sample_ids"] == [
        "101/1",
        "102/1",
        "103/1",
        "104/1",
        "105/1",
    ]


def test_append_does_not_mutate_input_ledger_or_sample() -> None:
    ledger = empty_ledger()
    sample = _sample("100")
    original_ledger = copy.deepcopy(ledger)
    original_sample = copy.deepcopy(sample)

    append_sample(ledger, sample)

    assert ledger == original_ledger
    assert sample == original_sample


def test_ledger_and_seal_metadata_fail_closed() -> None:
    with pytest.raises(DeploymentLatencyError, match="ledger must be an object"):
        validate_ledger([])

    for change, message in (
        ({"schema_version": "old"}, "schema_version"),
        ({"records": "not-an-array"}, "records"),
        ({"record_count": -1}, "record_count"),
        ({"head_hash": "bad"}, "head_hash"),
    ):
        ledger = empty_ledger()
        ledger.update(change)
        with pytest.raises(DeploymentLatencyError, match=message):
            validate_ledger(ledger)

    mismatch = empty_ledger()
    mismatch["record_count"] = 1
    with pytest.raises(DeploymentLatencyError, match="record_count"):
        validate_ledger(mismatch)

    ledger = _ledger_with([_sample("100")])
    wrong_count = ledger_seal(ledger)
    wrong_count["record_count"] = 0
    with pytest.raises(DeploymentLatencyError, match="sealed record count"):
        aggregate_ledger(ledger, expected_seal=wrong_count)

    wrong_head = ledger_seal(ledger)
    wrong_head["head_hash"] = "c" * 64
    with pytest.raises(DeploymentLatencyError, match="sealed head hash"):
        aggregate_ledger(ledger, expected_seal=wrong_head)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_cli_collect_requires_continuity_seal_and_aggregates(tmp_path: Path) -> None:
    ledger_path = tmp_path / "nested" / "ledger.json"
    sample_path = tmp_path / "sample.json"
    seal_path = tmp_path / "seal.json"
    report_path = tmp_path / "report.json"
    _write_json(sample_path, _sample("100"))

    assert (
        main(
            [
                "collect",
                "--ledger",
                str(ledger_path),
                "--sample",
                str(sample_path),
                "--seal-output",
                str(seal_path),
            ]
        )
        == 0
    )
    first_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert first_ledger["record_count"] == 1

    _write_json(sample_path, _sample("101", outcome="failure"))
    with pytest.raises(DeploymentLatencyError, match="expected-seal"):
        main(
            [
                "collect",
                "--ledger",
                str(ledger_path),
                "--sample",
                str(sample_path),
                "--seal-output",
                str(seal_path),
            ]
        )

    assert (
        main(
            [
                "collect",
                "--ledger",
                str(ledger_path),
                "--sample",
                str(sample_path),
                "--expected-seal",
                str(seal_path),
                "--seal-output",
                str(seal_path),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "aggregate",
                "--ledger",
                str(ledger_path),
                "--expected-seal",
                str(seal_path),
                "--output",
                str(report_path),
            ]
        )
        == 0
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["source_record_count"] == 2
    assert report["categories"]["proved-tag-to-release"]["outcome_counts"] == {
        "failure": 1,
        "success": 1,
    }


def test_cli_can_emit_a_seal_for_an_empty_ledger(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.json"
    seal_path = tmp_path / "seal.json"
    _write_json(ledger_path, empty_ledger())

    assert main(["seal", "--ledger", str(ledger_path), "--output", str(seal_path)]) == 0
    assert json.loads(seal_path.read_text(encoding="utf-8")) == ledger_seal(
        empty_ledger()
    )

    _write_json(ledger_path, append_sample(empty_ledger(), _sample("100")))
    with pytest.raises(DeploymentLatencyError, match="only creates the genesis seal"):
        main(["seal", "--ledger", str(ledger_path), "--output", str(seal_path)])


@pytest.mark.parametrize("failure_target", ["ledger", "seal"])
def test_cli_recovers_an_interrupted_two_file_commit_without_losing_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_target: str
) -> None:
    ledger_path = tmp_path / "ledger.json"
    seal_path = tmp_path / "seal.json"
    sample_path = tmp_path / "sample.json"
    _write_json(sample_path, _sample("100"))
    original_write = latency._write_atomic
    failed = False

    def fail_once(path: Path, value: object) -> None:
        nonlocal failed
        target = ledger_path if failure_target == "ledger" else seal_path
        if path == target and not failed:
            failed = True
            raise OSError("injected commit interruption")
        original_write(path, value)

    monkeypatch.setattr(latency, "_write_atomic", fail_once)
    with pytest.raises(OSError, match="injected commit interruption"):
        main(
            [
                "collect",
                "--ledger",
                str(ledger_path),
                "--sample",
                str(sample_path),
                "--seal-output",
                str(seal_path),
            ]
        )
    assert list(tmp_path.glob(".*.transaction.json"))

    monkeypatch.setattr(latency, "_write_atomic", original_write)
    _write_json(sample_path, _sample("101", outcome="failure"))
    assert (
        main(
            [
                "collect",
                "--ledger",
                str(ledger_path),
                "--sample",
                str(sample_path),
                "--expected-seal",
                str(seal_path),
                "--seal-output",
                str(seal_path),
            ]
        )
        == 0
    )
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    assert ledger["record_count"] == 2
    assert [record["sample"]["run_id"] for record in ledger["records"]] == [
        "100",
        "101",
    ]
    assert seal == ledger_seal(ledger)
    assert not list(tmp_path.glob(".*.transaction.json"))


def test_recovery_refuses_to_rewrite_tampered_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_path = tmp_path / "ledger.json"
    seal_path = tmp_path / "seal.json"
    sample_path = tmp_path / "sample.json"
    _write_json(sample_path, _sample("100"))
    assert (
        main(
            [
                "collect",
                "--ledger",
                str(ledger_path),
                "--sample",
                str(sample_path),
                "--seal-output",
                str(seal_path),
            ]
        )
        == 0
    )

    _write_json(sample_path, _sample("101"))
    original_write = latency._write_atomic

    def interrupt_seal(path: Path, value: object) -> None:
        if path == seal_path:
            raise OSError("injected seal interruption")
        original_write(path, value)

    monkeypatch.setattr(latency, "_write_atomic", interrupt_seal)
    with pytest.raises(OSError, match="injected seal interruption"):
        main(
            [
                "collect",
                "--ledger",
                str(ledger_path),
                "--sample",
                str(sample_path),
                "--expected-seal",
                str(seal_path),
                "--seal-output",
                str(seal_path),
            ]
        )

    monkeypatch.setattr(latency, "_write_atomic", original_write)
    tampered = json.loads(ledger_path.read_text(encoding="utf-8"))
    tampered["records"][0]["sample"]["workflow"] = "tampered"
    _write_json(ledger_path, tampered)
    with pytest.raises(DeploymentLatencyError, match="record_hash"):
        main(
            [
                "aggregate",
                "--ledger",
                str(ledger_path),
                "--expected-seal",
                str(seal_path),
                "--output",
                str(tmp_path / "report.json"),
            ]
        )
