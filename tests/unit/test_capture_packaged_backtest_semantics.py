from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.capture_packaged_backtest_semantics as capture_module
from scripts.capture_packaged_backtest_semantics import SemanticCaptureError


SEMANTIC_KEYS = {
    "identity_graph",
    "run",
    "snapshot",
    "symbols",
    "report",
    "collections",
    "order_events",
}


def _semantic(*, resumed: bool = False, marker: str = "expected") -> dict[str, Any]:
    logs: list[dict[str, Any]] = [
        {
            "detail": {"attempt": 1},
            "level": "info",
            "message": "run_started",
            "ordinal": 0,
        },
        {
            "detail": {"symbol": "000001.SS"},
            "level": "info",
            "message": "symbol_checkpointed",
            "ordinal": 1,
        },
        {
            "detail": {},
            "level": "info",
            "message": "run_completed",
            "ordinal": 2,
        },
    ]
    if resumed:
        logs[2]["ordinal"] = 3
        logs.insert(
            2,
            {
                "detail": {"attempt": 2},
                "level": "info",
                "message": "run_started",
                "ordinal": 2,
            },
        )
    return {
        "identity_graph": {"marker": marker},
        "run": {"marker": marker},
        "snapshot": {"marker": marker},
        "symbols": [],
        "report": {"marker": marker},
        "collections": {"logs": logs},
        "order_events": [],
    }


def _capture_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, dict[str, Any], dict[str, Any], dict[str, Any], SimpleNamespace]:
    expected = _semantic()
    resumed = _semantic(resumed=True)
    evidence = {
        "cells": [{"case_id": "cell", "run_id": "cell-run"}],
        "special_cases": [{"case_id": "special", "run_id": "special-run"}],
        "checkpoint": {
            "baseline_run_id": "baseline-run",
            "run_id": "resumed-run",
        },
    }
    oracle = {
        "cases": {
            "cell": {"semantic": deepcopy(expected)},
            "special": {"semantic": deepcopy(expected)},
            "custom_pool_1d": {"semantic": deepcopy(expected)},
        }
    }
    projections = {
        "cell-run": deepcopy(expected),
        "special-run": deepcopy(expected),
        "baseline-run": deepcopy(expected),
        "resumed-run": resumed,
    }
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "stock-desk.db").write_bytes(b"sqlite fixture")
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text("{}", encoding="utf-8")
    engine = SimpleNamespace(disposed=False)
    engine.dispose = lambda: setattr(engine, "disposed", True)
    monkeypatch.setattr(capture_module, "_load", lambda _path: evidence)
    monkeypatch.setattr(
        capture_module,
        "load_oracle",
        lambda _path, *, inputs_path: oracle,
    )
    monkeypatch.setattr(capture_module, "create_engine_for_url", lambda _url: engine)
    monkeypatch.setattr(capture_module, "BacktestRepository", lambda _engine: object())
    monkeypatch.setattr(
        capture_module,
        "_projection",
        lambda _repository, _engine, run_id: deepcopy(projections[run_id]),
    )
    return data_root, evidence_path, evidence, oracle, projections, engine


def test_load_rejects_empty_oversized_and_non_object_payloads(tmp_path: Path) -> None:
    path = tmp_path / "payload.json"
    path.write_bytes(b"")
    with pytest.raises(SemanticCaptureError, match="size is invalid"):
        capture_module._load(path)

    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SemanticCaptureError, match="size is invalid"):
        capture_module._load(path, maximum=1)

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(SemanticCaptureError, match="root must be an object"):
        capture_module._load(path)

    path.write_text('{"ok": true}', encoding="utf-8")
    assert capture_module._load(path) == {"ok": True}


def test_normalize_resumed_semantics_rejects_noncanonical_shapes() -> None:
    with pytest.raises(SemanticCaptureError, match="projection is not canonical"):
        capture_module.normalize_resumed_semantics({})

    missing_logs = _semantic()
    missing_logs["collections"] = {}
    with pytest.raises(SemanticCaptureError, match="logs are missing"):
        capture_module.normalize_resumed_semantics(missing_logs)

    no_restart = _semantic()
    with pytest.raises(SemanticCaptureError, match="allowlist mismatch"):
        capture_module.normalize_resumed_semantics(no_restart)

    duplicate_restart = _semantic(resumed=True)
    duplicate_restart["collections"]["logs"].append(
        {
            "detail": {"attempt": 2},
            "level": "info",
            "message": "run_started",
            "ordinal": 4,
        }
    )
    with pytest.raises(SemanticCaptureError, match="allowlist mismatch"):
        capture_module.normalize_resumed_semantics(duplicate_restart)


def test_normalize_resumed_semantics_rejects_unsafe_restart_boundary_and_rows() -> None:
    wrong_boundary = _semantic(resumed=True)
    wrong_boundary["collections"]["logs"][1]["message"] = "not_checkpointed"
    with pytest.raises(SemanticCaptureError, match="not at a durable boundary"):
        capture_module.normalize_resumed_semantics(wrong_boundary)

    invalid_row = _semantic(resumed=True)
    invalid_row["collections"]["logs"].append("invalid")
    with pytest.raises(SemanticCaptureError, match="log row is invalid"):
        capture_module.normalize_resumed_semantics(invalid_row)


def test_normalize_resumed_semantics_returns_canonical_projection_and_allowlist() -> (
    None
):
    normalized, allowlist = capture_module.normalize_resumed_semantics(
        _semantic(resumed=True)
    )
    assert normalized == _semantic()
    assert allowlist == {
        "allowed_difference_id": "desktop-checkpoint-extension-v1.1",
        "removed_log": {
            "detail": {"attempt": 2},
            "level": "info",
            "message": "run_started",
            "ordinal": 2,
        },
        "renumbered_field": "collections.logs[].ordinal",
    }


def test_projection_uses_repository_and_rejects_noncanonical_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SimpleNamespace(
        get_run=lambda run_id: {"run_id": run_id},
        report=lambda run_id: {"report_id": run_id},
    )
    observed: dict[str, Any] = {}

    def project(completed: Any, harness: Any) -> dict[str, Any]:
        observed["completed"] = completed
        observed["harness"] = harness
        return _semantic()

    monkeypatch.setattr(capture_module, "project_completed", project)
    engine = object()
    assert capture_module._projection(repository, engine, "run-1") == _semantic()
    assert observed["completed"].run == {"run_id": "run-1"}
    assert observed["completed"].report == {"report_id": "run-1"}
    assert observed["harness"].engine is engine
    assert observed["harness"].repository is repository

    monkeypatch.setattr(capture_module, "project_completed", lambda *_args: {})
    with pytest.raises(SemanticCaptureError, match="fields are not canonical"):
        capture_module._projection(repository, engine, "run-1")


def test_capture_writes_all_semantic_projections_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root, evidence_path, _evidence, _oracle, _projections, engine = (
        _capture_fixture(tmp_path, monkeypatch)
    )

    capture_module.capture(data_root, evidence_path)

    written = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert written["cells"][0]["semantic_projection"] == _semantic()
    assert written["special_cases"][0]["semantic_projection"] == _semantic()
    checkpoint = written["checkpoint"]
    assert checkpoint["uninterrupted_semantic_projection"] == _semantic()
    assert checkpoint["resumed_semantic_projection"] == _semantic(resumed=True)
    assert checkpoint["resumed_normalized_projection"] == _semantic()
    assert checkpoint["normalization"]["allowed_difference_id"] == (
        "desktop-checkpoint-extension-v1.1"
    )
    assert engine.disposed is True
    assert not evidence_path.with_name(f".{evidence_path.name}.tmp").exists()


def test_capture_rejects_missing_database_before_opening_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(capture_module, "_load", lambda _path: {})
    monkeypatch.setattr(capture_module, "load_oracle", lambda *_args, **_kwargs: {})
    with pytest.raises(SemanticCaptureError, match="database is missing"):
        capture_module.capture(tmp_path / "missing", evidence_path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_cells", "cells is missing"),
        ("invalid_cell", "cells row is invalid"),
        ("missing_special_cases", "special_cases is missing"),
        ("duplicate_run", "semantic run is duplicated"),
        ("semantic_mismatch", "differs from v1 oracle"),
        ("missing_checkpoint", "checkpoint is missing"),
        ("duplicate_checkpoint", "checkpoint run identity is duplicated"),
        ("checkpoint_mismatch", "baseline/resumed semantics differ"),
    ],
)
def test_capture_fails_closed_for_incomplete_or_inconsistent_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    data_root, evidence_path, evidence, _oracle, projections, engine = _capture_fixture(
        tmp_path, monkeypatch
    )
    if mutation == "missing_cells":
        evidence["cells"] = None
    elif mutation == "invalid_cell":
        evidence["cells"] = ["invalid"]
    elif mutation == "missing_special_cases":
        evidence["special_cases"] = None
    elif mutation == "duplicate_run":
        evidence["special_cases"][0]["run_id"] = "cell-run"
    elif mutation == "semantic_mismatch":
        projections["cell-run"] = _semantic(marker="different")
    elif mutation == "missing_checkpoint":
        evidence["checkpoint"] = None
    elif mutation == "duplicate_checkpoint":
        evidence["checkpoint"]["baseline_run_id"] = "cell-run"
    elif mutation == "checkpoint_mismatch":
        projections["baseline-run"] = _semantic(marker="different")
    else:  # pragma: no cover - exhaustive parameter guard
        raise AssertionError(mutation)

    with pytest.raises(SemanticCaptureError, match=message):
        capture_module.capture(data_root, evidence_path)
    assert engine.disposed is True


def test_main_resolves_paths_and_invokes_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        capture_module,
        "capture",
        lambda data_root, evidence: observed.append((data_root, evidence)),
    )
    data_root = tmp_path / "relative-data"
    evidence = tmp_path / "relative-evidence.json"

    assert (
        capture_module.main(
            ["--data-root", str(data_root), "--evidence", str(evidence)]
        )
        == 0
    )
    assert observed == [(data_root.resolve(), evidence.resolve())]
    assert set(capture_module._SEMANTIC_KEYS) == SEMANTIC_KEYS
