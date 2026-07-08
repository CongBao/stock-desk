from __future__ import annotations

import json
from pathlib import Path

import pytest

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
            "ten-year-a-share",
            "--output",
            str(tmp_path / "current.json"),
        ]
    )
    assert args.fixture == "ten-year-a-share"

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
