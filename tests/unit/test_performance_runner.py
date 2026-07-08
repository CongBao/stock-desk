from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.run_performance_baseline as runner

from scripts.run_performance_baseline import (
    BaselineRecordingError,
    atomic_write_json,
    parse_args,
    require_recording_preconditions,
)


def test_cli_accepts_paths_but_no_user_supplied_timing_values(tmp_path: Path) -> None:
    args = parse_args(
        [
            "--fixture",
            "full-a-scope-bounded-ten-year",
            "--output",
            str(tmp_path / "current.json"),
        ]
    )
    assert args.fixture == "full-a-scope-bounded-ten-year"
    assert args.evidence_kind == "reference"
    assert parse_args(["--evidence-kind", "target_baseline"]).evidence_kind == (
        "target_baseline"
    )

    with pytest.raises(SystemExit):
        parse_args(["--chart-cold-seconds", "0.001"])
    with pytest.raises(SystemExit):
        parse_args(["--record-baseline", "--skip-browser-run"])
    with pytest.raises(SystemExit):
        parse_args(["--browser-output", str(tmp_path / "forged.json")])


def test_atomic_writer_never_leaves_a_partial_file(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "current.json"
    atomic_write_json(destination, {"schema_version": "test", "value": 1})

    assert json.loads(destination.read_text(encoding="utf-8"))["value"] == 1
    assert list(destination.parent.glob(".*.tmp")) == []


def test_browser_measurement_clears_stale_run_evidence_before_playwright(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_output = tmp_path / "browser-raw.json"
    process_output = tmp_path / "processes.json"
    raw_output.write_text("stale", encoding="utf-8")
    process_output.write_text("stale", encoding="utf-8")
    observed: dict[str, object] = {}

    def run(_command: object, **kwargs: object) -> None:
        observed["raw_exists"] = raw_output.exists()
        observed["process_exists"] = process_output.exists()
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        observed["process_file"] = environment["STOCK_DESK_PERFORMANCE_PROCESS_FILE"]

    monkeypatch.setattr(runner.subprocess, "run", run)

    runner._run_browser_measurement(raw_output)

    assert observed == {
        "raw_exists": False,
        "process_exists": False,
        "process_file": str(process_output.resolve()),
    }


@pytest.mark.parametrize(
    ("dirty", "digest_matches", "hardware_qualifies", "reason"),
    [
        (True, True, True, "dirty"),
        (False, False, True, "digest"),
        (False, True, False, "hardware"),
    ],
)
def test_baseline_recording_refuses_unqualified_evidence(
    dirty: bool,
    digest_matches: bool,
    hardware_qualifies: bool,
    reason: str,
) -> None:
    with pytest.raises(BaselineRecordingError, match=reason):
        require_recording_preconditions(
            dirty=dirty,
            digest_matches=digest_matches,
            hardware_qualifies=hardware_qualifies,
            gate_passed=True,
        )


def test_baseline_recording_refuses_a_failing_gate() -> None:
    with pytest.raises(BaselineRecordingError, match="gate"):
        require_recording_preconditions(
            dirty=False,
            digest_matches=True,
            hardware_qualifies=True,
            gate_passed=False,
        )


def test_performance_command_rejects_windows_before_browser_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_started = False

    def browser(_output: Path) -> None:
        nonlocal browser_started
        browser_started = True

    monkeypatch.setattr(runner.sys, "platform", "win32")
    monkeypatch.setattr(runner, "_run_browser_measurement", browser)

    with pytest.raises(SystemExit, match="unsupported.*Windows"):
        runner.main([])

    assert browser_started is False


def test_target_hardware_is_exact_four_cpu_nominal_sixteen_gb() -> None:
    environment = {
        "effective_cpu_count": 4.0,
        "memory_bytes": 16 * 1024**3,
        "effective_memory_bytes": 15 * 1024**3,
        "runner": {
            "provider": "github_actions",
            "os": "Linux",
            "arch": "X64",
            "repository": "CongBao/stock-desk",
            "image_os": "ubuntu24",
            "image_version": "20260701.1",
            "run_id": 1234,
            "run_attempt": 1,
        },
    }

    assert runner._qualifying_environment(environment, "target_baseline") is True
    environment["effective_cpu_count"] = 14.0
    assert runner._qualifying_environment(environment, "target_baseline") is False
    assert runner._qualifying_environment(environment, "reference") is True
    environment["effective_cpu_count"] = 4.0
    environment["memory_bytes"] = 24 * 1024**3
    assert runner._qualifying_environment(environment, "target_baseline") is False


def test_verified_git_sha_requires_the_current_commit_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(["a" * 40, "commit", "a" * 40])
    monkeypatch.setattr(
        runner,
        "_command_output",
        lambda _command, default="unavailable": next(responses),
    )
    assert runner._verified_git_sha() == "a" * 40

    mismatch = iter(["a" * 40, "commit", "b" * 40])
    monkeypatch.setattr(
        runner,
        "_command_output",
        lambda _command, default="unavailable": next(mismatch),
    )
    with pytest.raises(BaselineRecordingError, match="current checkout"):
        runner._verified_git_sha()
