# ruff: noqa: E402

"""Capture complete canonical semantics from the packaged sidecar database."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.v1_backtest_oracle import load_oracle, project_completed
from stock_desk.backtest.repository import BacktestRepository
from stock_desk.storage.database import create_engine_for_url


ORACLE_PATH = ROOT / "tests/fixtures/backtest/v1_0_oracle.json"
INPUTS_PATH = ROOT / "tests/fixtures/backtest/v1_0_oracle_inputs.json"
_SEMANTIC_KEYS = {
    "identity_graph",
    "run",
    "snapshot",
    "symbols",
    "report",
    "collections",
    "order_events",
}


class SemanticCaptureError(ValueError):
    """Packaged database does not contain the exact required semantics."""


def _load(path: Path, maximum: int = 8 * 1024 * 1024) -> dict[str, Any]:
    raw = path.read_bytes()
    if not raw or len(raw) > maximum:
        raise SemanticCaptureError("packaged evidence size is invalid")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise SemanticCaptureError("packaged evidence root must be an object")
    return cast(dict[str, Any], value)


def normalize_resumed_semantics(
    semantic: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized = deepcopy(dict(semantic))
    if set(normalized) != _SEMANTIC_KEYS:
        raise SemanticCaptureError("checkpoint semantic projection is not canonical")
    collections = normalized.get("collections")
    if not isinstance(collections, dict) or not isinstance(
        collections.get("logs"), list
    ):
        raise SemanticCaptureError("checkpoint logs are missing")
    logs = cast(list[object], collections["logs"])
    matches = [
        index
        for index, item in enumerate(logs)
        if isinstance(item, dict)
        and item
        == {
            "detail": {"attempt": 2},
            "level": "info",
            "message": "run_started",
            "ordinal": index,
        }
    ]
    if len(matches) != 1:
        raise SemanticCaptureError("checkpoint normalization allowlist mismatch")
    restart_index = matches[0]
    previous = logs[restart_index - 1] if restart_index > 0 else None
    if (
        restart_index not in {2, 3}
        or not isinstance(previous, dict)
        or previous.get("message") != "symbol_checkpointed"
        or previous.get("ordinal") != restart_index - 1
    ):
        raise SemanticCaptureError("checkpoint restart is not at a durable boundary")
    allowed = {
        "detail": {"attempt": 2},
        "level": "info",
        "message": "run_started",
        "ordinal": restart_index,
    }
    retained = [item for index, item in enumerate(logs) if index != restart_index]
    if not all(isinstance(item, dict) for item in retained):
        raise SemanticCaptureError("checkpoint log row is invalid")
    collections["logs"] = [
        {**cast(dict[str, Any], item), "ordinal": ordinal}
        for ordinal, item in enumerate(retained)
    ]
    return normalized, {
        "allowed_difference_id": "desktop-checkpoint-extension-v1.1",
        "removed_log": allowed,
        "renumbered_field": "collections.logs[].ordinal",
    }


def _projection(
    repository: BacktestRepository, engine: Any, run_id: str
) -> dict[str, Any]:
    run = repository.get_run(run_id)
    completed = SimpleNamespace(run=run, report=repository.report(run_id))
    harness = SimpleNamespace(engine=engine, repository=repository)
    value = project_completed(completed, harness)
    if set(value) != _SEMANTIC_KEYS:
        raise SemanticCaptureError("semantic projection fields are not canonical")
    return cast(dict[str, Any], value)


def capture(data_root: Path, evidence_path: Path) -> None:
    evidence = _load(evidence_path)
    oracle = load_oracle(ORACLE_PATH, inputs_path=INPUTS_PATH)
    database = data_root / "stock-desk.db"
    if not database.is_file():
        raise SemanticCaptureError("packaged sidecar database is missing")
    engine = create_engine_for_url(f"sqlite:///{database}")
    repository = BacktestRepository(engine)
    seen_runs: set[str] = set()
    try:
        for collection_name in ("cells", "special_cases"):
            records = evidence.get(collection_name)
            if not isinstance(records, list):
                raise SemanticCaptureError(f"{collection_name} is missing")
            for record in records:
                if not isinstance(record, dict):
                    raise SemanticCaptureError(f"{collection_name} row is invalid")
                case_id = str(record.get("case_id", ""))
                run_id = str(record.get("run_id", ""))
                if run_id in seen_runs:
                    raise SemanticCaptureError("packaged semantic run is duplicated")
                seen_runs.add(run_id)
                semantic = _projection(repository, engine, run_id)
                if semantic != oracle["cases"][case_id]["semantic"]:
                    raise SemanticCaptureError(
                        f"packaged semantic differs from v1 oracle: {case_id}"
                    )
                record["semantic_projection"] = semantic

        checkpoint = evidence.get("checkpoint")
        if not isinstance(checkpoint, dict):
            raise SemanticCaptureError("checkpoint is missing")
        baseline_run = str(checkpoint.get("baseline_run_id", ""))
        resumed_run = str(checkpoint.get("run_id", ""))
        if (
            baseline_run in seen_runs
            or resumed_run in seen_runs
            or baseline_run == resumed_run
        ):
            raise SemanticCaptureError("checkpoint run identity is duplicated")
        baseline = _projection(repository, engine, baseline_run)
        resumed = _projection(repository, engine, resumed_run)
        normalized, normalization = normalize_resumed_semantics(resumed)
        expected = oracle["cases"]["custom_pool_1d"]["semantic"]
        if baseline != expected or normalized != expected:
            raise SemanticCaptureError(
                "checkpoint baseline/resumed semantics differ from v1 oracle"
            )
        checkpoint["uninterrupted_semantic_projection"] = baseline
        checkpoint["resumed_semantic_projection"] = resumed
        checkpoint["resumed_normalized_projection"] = normalized
        checkpoint["normalization"] = normalization
    finally:
        engine.dispose()

    temporary = evidence_path.with_name(f".{evidence_path.name}.tmp")
    temporary.write_text(
        json.dumps(evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(evidence_path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    capture(args.data_root.resolve(), args.evidence.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
