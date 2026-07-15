"""Append-only deployment latency evidence and reproducible percentiles.

The ledger intentionally retains failed and invalidated observations.  A seal from
the previous append is the external continuity witness which makes tail deletion
detectable; the per-record hash chain detects edits and deletion inside a ledger.
A durable transaction journal makes the ledger/seal pair recoverable without ever
rolling back or replacing established history.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import cast


_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[1-9][0-9]*$")
_RUN_URL = re.compile(
    r"^https://github\.com/CongBao/stock-desk/actions/runs/([1-9][0-9]*)/"
    r"attempts/([1-9][0-9]*)$"
)
_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$"
)
_GENESIS_HASH = "0" * 64
_MINIMUM_SAMPLES = 5
_STRING_LIMITS = {
    "workflow": 256,
    "ref": 512,
    "environment_baseline.os": 256,
    "environment_baseline.architecture": 64,
    "environment_baseline.runner_image": 256,
    "environment_baseline.toolchain": 1024,
    "invalidation_reason": 1024,
}
_SAMPLE_FIELDS = {
    "schema_version",
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
}
_BASELINE_FIELDS = {"os", "architecture", "runner_image", "toolchain"}
_CACHE_STATES = {"hit", "miss", "partial", "disabled", "unknown"}
_OUTCOMES = {"success", "failure", "cancelled", "timed_out", "skipped"}
_CATEGORIES = {
    "typical-pr",
    "high-risk-pr",
    "main",
    "candidate",
    "signpath-queue",
    "proved-tag-to-release",
}


class DeploymentLatencyError(ValueError):
    """Raised when latency evidence is incomplete or its history is not intact."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _require_exact_fields(
    value: Mapping[str, object], expected: set[str], label: str
) -> None:
    missing = sorted(expected - set(value))
    if missing:
        raise DeploymentLatencyError(f"{label} missing required field: {missing[0]}")
    extra = sorted(set(value) - expected)
    if extra:
        raise DeploymentLatencyError(f"{label} has unknown field: {extra[0]}")


def _require_string(value: object, field: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeploymentLatencyError(f"{field} must be a non-empty string")
    if maximum is not None and len(value) > maximum:
        raise DeploymentLatencyError(
            f"{field} must contain at most {maximum} characters"
        )
    return value


def _duration(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DeploymentLatencyError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise DeploymentLatencyError(f"{field} must be a finite number")
    if result < 0:
        raise DeploymentLatencyError(f"{field} must be non-negative")
    return result


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise DeploymentLatencyError(
            f"{field} must be an unambiguous RFC 3339 UTC timestamp ending in Z"
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise DeploymentLatencyError(f"{field} is not a valid UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise DeploymentLatencyError(f"{field} must be UTC")
    return parsed


def validate_sample(sample: Mapping[str, object]) -> None:
    """Fail closed on missing raw values or internally inconsistent timing."""
    if not isinstance(sample, Mapping):
        raise DeploymentLatencyError("sample must be an object")
    _require_exact_fields(sample, _SAMPLE_FIELDS, "sample")
    if sample["schema_version"] != "stock-desk-deployment-latency-sample-v1":
        raise DeploymentLatencyError("unsupported sample schema_version")

    run_id = _require_string(sample["run_id"], "run_id")
    if _RUN_ID.fullmatch(run_id) is None:
        raise DeploymentLatencyError("run_id contains unsupported characters")
    attempt = sample["run_attempt"]
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise DeploymentLatencyError("run_attempt must be a positive integer")
    run_url = _require_string(sample["run_url"], "run_url")
    url_match = _RUN_URL.fullmatch(run_url)
    if url_match is None or url_match.groups() != (run_id, str(attempt)):
        raise DeploymentLatencyError(
            "run_url must bind the exact public Stock Desk run id and attempt"
        )
    for field in ("source_sha", "source_tree"):
        value = sample[field]
        if not isinstance(value, str) or _SHA1.fullmatch(value) is None:
            raise DeploymentLatencyError(f"{field} must be a lowercase 40-hex Git id")
    _require_string(sample["workflow"], "workflow", maximum=_STRING_LIMITS["workflow"])
    ref = _require_string(sample["ref"], "ref", maximum=_STRING_LIMITS["ref"])
    if not ref.startswith("refs/"):
        raise DeploymentLatencyError("ref must be a fully qualified refs/ value")
    category = _require_string(sample["category"], "category")
    if category not in _CATEGORIES:
        raise DeploymentLatencyError("category is not a supported deployment slice")

    queued_at = _timestamp(sample["queued_at"], "queued_at")
    started_at = _timestamp(sample["started_at"], "started_at")
    completed_at = _timestamp(sample["completed_at"], "completed_at")
    queue_seconds = _duration(sample["queue_seconds"], "queue_seconds")
    wall_seconds = _duration(sample["wall_seconds"], "wall_seconds")
    observed_queue = (started_at - queued_at).total_seconds()
    observed_wall = (completed_at - started_at).total_seconds()
    if observed_queue < 0 or not math.isclose(
        queue_seconds, observed_queue, abs_tol=0.001
    ):
        raise DeploymentLatencyError(
            "queue_seconds must equal started_at minus queued_at and be non-negative"
        )
    if observed_wall < 0 or not math.isclose(
        wall_seconds, observed_wall, abs_tol=0.001
    ):
        raise DeploymentLatencyError(
            "wall_seconds must equal completed_at minus started_at and be non-negative"
        )

    if sample["cache_status"] not in _CACHE_STATES:
        raise DeploymentLatencyError("cache_status is not recognized")
    if sample["outcome"] not in _OUTCOMES:
        raise DeploymentLatencyError("outcome is not recognized")
    baseline = sample["environment_baseline"]
    if not isinstance(baseline, Mapping):
        raise DeploymentLatencyError("environment_baseline must be an object")
    _require_exact_fields(baseline, _BASELINE_FIELDS, "environment_baseline")
    for field in sorted(_BASELINE_FIELDS):
        label = f"environment_baseline.{field}"
        _require_string(baseline[field], label, maximum=_STRING_LIMITS[label])

    invalidated = sample["invalidated"]
    if not isinstance(invalidated, bool):
        raise DeploymentLatencyError("invalidated must be a boolean")
    reason = sample["invalidation_reason"]
    if invalidated:
        _require_string(
            reason,
            "invalidation_reason",
            maximum=_STRING_LIMITS["invalidation_reason"],
        )
    elif reason is not None:
        raise DeploymentLatencyError(
            "invalidation_reason must be null when invalidated is false"
        )


def empty_ledger() -> dict[str, object]:
    return {
        "schema_version": "stock-desk-deployment-latency-ledger-v1",
        "records": [],
        "record_count": 0,
        "head_hash": _GENESIS_HASH,
    }


def _record_payload(record: Mapping[str, object]) -> dict[str, object]:
    return {
        "ordinal": record["ordinal"],
        "previous_hash": record["previous_hash"],
        "sample": record["sample"],
    }


def validate_ledger(ledger: Mapping[str, object]) -> None:
    """Verify ledger metadata, every raw sample, and the complete hash chain."""
    if not isinstance(ledger, Mapping):
        raise DeploymentLatencyError("ledger must be an object")
    expected_fields = {"schema_version", "records", "record_count", "head_hash"}
    _require_exact_fields(ledger, expected_fields, "ledger")
    if ledger["schema_version"] != "stock-desk-deployment-latency-ledger-v1":
        raise DeploymentLatencyError("unsupported ledger schema_version")
    records = ledger["records"]
    if not isinstance(records, list):
        raise DeploymentLatencyError("records must be an array")
    count = ledger["record_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise DeploymentLatencyError("record_count must be a non-negative integer")
    if count != len(records):
        raise DeploymentLatencyError("record_count does not match records")

    prior = _GENESIS_HASH
    identities: set[tuple[str, int]] = set()
    for index, record in enumerate(records, start=1):
        if not isinstance(record, Mapping):
            raise DeploymentLatencyError(f"record {index} must be an object")
        _require_exact_fields(
            record, {"ordinal", "previous_hash", "sample", "record_hash"}, "record"
        )
        if record["ordinal"] != index:
            raise DeploymentLatencyError(f"record {index} ordinal is not contiguous")
        if record["previous_hash"] != prior:
            raise DeploymentLatencyError(f"record {index} previous_hash mismatch")
        try:
            calculated = _digest(_record_payload(record))
        except (TypeError, ValueError) as exc:
            raise DeploymentLatencyError(
                f"record {index} cannot reproduce record_hash"
            ) from exc
        if record["record_hash"] != calculated:
            raise DeploymentLatencyError(f"record {index} record_hash mismatch")
        sample = record["sample"]
        if not isinstance(sample, Mapping):
            raise DeploymentLatencyError(f"record {index} sample must be an object")
        validate_sample(sample)
        identity = (str(sample["run_id"]), int(sample["run_attempt"]))
        if identity in identities:
            raise DeploymentLatencyError(
                f"duplicate run id/attempt: {identity[0]}/{identity[1]}"
            )
        identities.add(identity)
        prior = calculated

    head_hash = ledger["head_hash"]
    if not isinstance(head_hash, str) or _SHA256.fullmatch(head_hash) is None:
        raise DeploymentLatencyError("head_hash must be a SHA-256 digest")
    if head_hash != prior:
        raise DeploymentLatencyError("head_hash does not match the final record")


def ledger_seal(ledger: Mapping[str, object]) -> dict[str, object]:
    validate_ledger(ledger)
    return {
        "schema_version": "stock-desk-deployment-latency-seal-v1",
        "record_count": ledger["record_count"],
        "head_hash": ledger["head_hash"],
    }


def _validate_seal(seal: Mapping[str, object]) -> None:
    if not isinstance(seal, Mapping):
        raise DeploymentLatencyError("expected seal must be an object")
    _require_exact_fields(seal, {"schema_version", "record_count", "head_hash"}, "seal")
    if seal["schema_version"] != "stock-desk-deployment-latency-seal-v1":
        raise DeploymentLatencyError("unsupported seal schema_version")
    count = seal["record_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise DeploymentLatencyError("sealed record count must be non-negative")
    head = seal["head_hash"]
    if not isinstance(head, str) or _SHA256.fullmatch(head) is None:
        raise DeploymentLatencyError("sealed head hash must be a SHA-256 digest")


def _require_seal(ledger: Mapping[str, object], seal: Mapping[str, object]) -> None:
    _validate_seal(seal)
    if ledger["record_count"] != seal["record_count"]:
        raise DeploymentLatencyError("sealed record count does not match ledger")
    if ledger["head_hash"] != seal["head_hash"]:
        raise DeploymentLatencyError("sealed head hash does not match ledger")


def append_sample(
    ledger: Mapping[str, object],
    sample: Mapping[str, object],
    *,
    expected_seal: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return a new ledger; never mutate the caller's sample or history."""
    validate_ledger(ledger)
    prior_count = cast(int, ledger["record_count"])
    if prior_count > 0 and expected_seal is None:
        raise DeploymentLatencyError(
            "expected seal is required when appending to a non-empty ledger"
        )
    if expected_seal is not None:
        _require_seal(ledger, expected_seal)
    validate_sample(sample)
    identity = (sample["run_id"], sample["run_attempt"])
    existing_records = cast(list[dict[str, object]], ledger["records"])
    for existing_record in existing_records:
        recorded = cast(dict[str, object], existing_record["sample"])
        if (recorded["run_id"], recorded["run_attempt"]) == identity:
            raise DeploymentLatencyError(
                f"duplicate run id/attempt: {identity[0]}/{identity[1]}"
            )

    result = copy.deepcopy(dict(ledger))
    result_count = cast(int, result["record_count"])
    ordinal = result_count + 1
    new_record: dict[str, object] = {
        "ordinal": ordinal,
        "previous_hash": result["head_hash"],
        "sample": copy.deepcopy(dict(sample)),
    }
    new_record["record_hash"] = _digest(new_record)
    records = cast(list[dict[str, object]], result["records"])
    records.append(new_record)
    result["record_count"] = ordinal
    result["head_hash"] = new_record["record_hash"]
    validate_ledger(result)
    return result


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(1, math.ceil(percentile * len(ordered))) - 1
    return float(ordered[index])


def _comparison_identity(sample: Mapping[str, object]) -> dict[str, object]:
    """Return every field that must stay fixed across a comparable run streak."""
    return {
        "category": sample["category"],
        "workflow": sample["workflow"],
        "ref": sample["ref"],
        "environment_baseline": copy.deepcopy(sample["environment_baseline"]),
    }


def _comparison_segments(
    samples: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Split category history whenever its canonical comparison identity drifts."""
    segments: list[dict[str, object]] = []
    for sample in samples:
        identity = _comparison_identity(sample)
        identity_hash = _digest(identity)
        if not segments or segments[-1]["identity_hash"] != identity_hash:
            segments.append(
                {
                    "segment_index": len(segments) + 1,
                    "identity_hash": identity_hash,
                    "identity": identity,
                    "samples": [],
                }
            )
        cast(list[Mapping[str, object]], segments[-1]["samples"]).append(sample)
    return segments


def _segment_report(segment: Mapping[str, object]) -> dict[str, object]:
    samples = cast(list[Mapping[str, object]], segment["samples"])
    distinct_run_ids = list(dict.fromkeys(str(sample["run_id"]) for sample in samples))
    return {
        "segment_index": segment["segment_index"],
        "identity_hash": segment["identity_hash"],
        "identity": segment["identity"],
        "sample_count": len(samples),
        "distinct_run_count": len(distinct_run_ids),
        "run_ids": distinct_run_ids,
        "included_sample_ids": [
            f"{sample['run_id']}/{sample['run_attempt']}" for sample in samples
        ],
        "outcome_counts": dict(
            sorted(Counter(str(sample["outcome"]) for sample in samples).items())
        ),
        "cache_status_counts": dict(
            sorted(Counter(str(sample["cache_status"]) for sample in samples).items())
        ),
        "invalidated_count": sum(bool(sample["invalidated"]) for sample in samples),
    }


def _run_duration_representatives(
    samples: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Choose the slowest observed value per metric for each distinct run.

    Every retry remains in the raw ledger and representative's sample id list, but
    a fast retry can never lower the run's queue or wall duration.
    """
    representatives: dict[str, dict[str, object]] = {}
    for sample in samples:
        run_id = str(sample["run_id"])
        queue_seconds = _duration(sample["queue_seconds"], "queue_seconds")
        wall_seconds = _duration(sample["wall_seconds"], "wall_seconds")
        sample_id = f"{sample['run_id']}/{sample['run_attempt']}"
        if run_id not in representatives:
            representatives[run_id] = {
                "run_id": run_id,
                "included_sample_ids": [sample_id],
                "queue_seconds": queue_seconds,
                "wall_seconds": wall_seconds,
            }
            continue
        representative = representatives[run_id]
        cast(list[str], representative["included_sample_ids"]).append(sample_id)
        representative["queue_seconds"] = max(
            cast(float, representative["queue_seconds"]), queue_seconds
        )
        representative["wall_seconds"] = max(
            cast(float, representative["wall_seconds"]), wall_seconds
        )
    return list(representatives.values())


def _empty_category_report() -> dict[str, object]:
    return {
        "status": "incomplete",
        "minimum_sample_count": _MINIMUM_SAMPLES,
        "minimum_consecutive_run_count": _MINIMUM_SAMPLES,
        "sample_count": 0,
        "active_segment_sample_count": 0,
        "consecutive_run_count": 0,
        "included_sample_ids": [],
        "outcome_counts": {},
        "cache_status_counts": {},
        "invalidated_count": 0,
        "queue_seconds": {"p50": None, "p95": None},
        "wall_seconds": {"p50": None, "p95": None},
        "percentile_method": "nearest-rank-over-slowest-attempt-per-distinct-run",
        "run_duration_representatives": [],
        "active_identity_hash": None,
        "active_comparison_identity": None,
        "drift_detected": False,
        "comparison_groups": [],
    }


def aggregate_ledger(
    ledger: Mapping[str, object], *, expected_seal: Mapping[str, object]
) -> dict[str, object]:
    """Aggregate the latest consecutive comparable streak without hiding drift."""
    if not isinstance(ledger, Mapping):
        raise DeploymentLatencyError("aggregation requires a complete sealed ledger")
    validate_ledger(ledger)
    _require_seal(ledger, expected_seal)

    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    records = cast(list[dict[str, object]], ledger["records"])
    for record in records:
        sample = cast(dict[str, object], record["sample"])
        grouped[str(sample["category"])].append(sample)

    categories: dict[str, object] = {}
    for category in sorted(_CATEGORIES):
        if category not in grouped:
            categories[category] = _empty_category_report()
            continue
        all_samples = grouped[category]
        segments = _comparison_segments(all_samples)
        active_segment = segments[-1]
        samples = cast(list[Mapping[str, object]], active_segment["samples"])
        distinct_run_count = len({str(sample["run_id"]) for sample in samples})
        complete = distinct_run_count >= _MINIMUM_SAMPLES
        representatives = _run_duration_representatives(samples)
        queue_values = [
            cast(float, representative["queue_seconds"])
            for representative in representatives
        ]
        wall_values = [
            cast(float, representative["wall_seconds"])
            for representative in representatives
        ]

        def percentiles(values: list[float]) -> dict[str, float | None]:
            if not complete:
                return {"p50": None, "p95": None}
            return {
                "p50": _nearest_rank(values, 0.50),
                "p95": _nearest_rank(values, 0.95),
            }

        categories[category] = {
            "status": "complete" if complete else "incomplete",
            "minimum_sample_count": _MINIMUM_SAMPLES,
            "minimum_consecutive_run_count": _MINIMUM_SAMPLES,
            "sample_count": len(all_samples),
            "active_segment_sample_count": len(samples),
            "consecutive_run_count": distinct_run_count,
            "included_sample_ids": [
                f"{sample['run_id']}/{sample['run_attempt']}" for sample in samples
            ],
            "outcome_counts": dict(
                sorted(Counter(str(sample["outcome"]) for sample in samples).items())
            ),
            "cache_status_counts": dict(
                sorted(
                    Counter(str(sample["cache_status"]) for sample in samples).items()
                )
            ),
            "invalidated_count": sum(bool(sample["invalidated"]) for sample in samples),
            "queue_seconds": percentiles(queue_values),
            "wall_seconds": percentiles(wall_values),
            "percentile_method": "nearest-rank-over-slowest-attempt-per-distinct-run",
            "run_duration_representatives": representatives,
            "active_identity_hash": active_segment["identity_hash"],
            "active_comparison_identity": active_segment["identity"],
            "drift_detected": len(segments) > 1,
            "comparison_groups": [_segment_report(segment) for segment in segments],
        }

    return {
        "schema_version": "stock-desk-deployment-latency-report-v1",
        "source_record_count": ledger["record_count"],
        "source_head_hash": ledger["head_hash"],
        "categories": categories,
    }


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentLatencyError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise DeploymentLatencyError(f"{label} must contain a JSON object")
    return value


def _write_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _transaction_path(ledger_path: Path, seal_path: Path) -> Path:
    path_key = hashlib.sha256(str(seal_path.resolve()).encode("utf-8")).hexdigest()[:16]
    return ledger_path.parent / f".{ledger_path.name}.{path_key}.transaction.json"


def _write_exclusive(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise DeploymentLatencyError(
            f"unfinished latency transaction requires recovery: {path}"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    _fsync_directory(path.parent)


def _validate_transaction(
    transaction: Mapping[str, object], ledger_path: Path, seal_path: Path
) -> None:
    _require_exact_fields(
        transaction,
        {
            "schema_version",
            "ledger_path",
            "seal_path",
            "previous_ledger_existed",
            "previous_seal_existed",
            "previous_seal",
            "next_ledger",
            "next_seal",
        },
        "transaction",
    )
    if transaction["schema_version"] != "stock-desk-deployment-latency-transaction-v1":
        raise DeploymentLatencyError("unsupported transaction schema_version")
    if transaction["ledger_path"] != str(ledger_path.resolve()):
        raise DeploymentLatencyError("transaction ledger_path mismatch")
    if transaction["seal_path"] != str(seal_path.resolve()):
        raise DeploymentLatencyError("transaction seal_path mismatch")
    for field in ("previous_ledger_existed", "previous_seal_existed"):
        if not isinstance(transaction[field], bool):
            raise DeploymentLatencyError(f"transaction {field} must be a boolean")

    previous_seal = transaction["previous_seal"]
    next_ledger = transaction["next_ledger"]
    next_seal = transaction["next_seal"]
    if not isinstance(previous_seal, Mapping):
        raise DeploymentLatencyError("transaction previous_seal must be an object")
    if not isinstance(next_ledger, Mapping):
        raise DeploymentLatencyError("transaction next_ledger must be an object")
    if not isinstance(next_seal, Mapping):
        raise DeploymentLatencyError("transaction next_seal must be an object")
    _validate_seal(previous_seal)
    validate_ledger(next_ledger)
    _require_seal(next_ledger, next_seal)
    previous_count = cast(int, previous_seal["record_count"])
    next_count = cast(int, next_ledger["record_count"])
    if next_count != previous_count + 1:
        raise DeploymentLatencyError("transaction must contain exactly one append")
    records = cast(list[Mapping[str, object]], next_ledger["records"])
    if records[-1]["previous_hash"] != previous_seal["head_hash"]:
        raise DeploymentLatencyError(
            "transaction append does not continue the previous sealed head"
        )


def _same_seal(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    return dict(left) == dict(right)


def _ledger_matches_seal(
    ledger: Mapping[str, object], seal: Mapping[str, object]
) -> bool:
    validate_ledger(ledger)
    return (
        ledger["record_count"] == seal["record_count"]
        and ledger["head_hash"] == seal["head_hash"]
    )


def _finish_transaction(
    transaction_path: Path,
    transaction: Mapping[str, object],
    ledger_path: Path,
    seal_path: Path,
) -> None:
    previous_seal = cast(Mapping[str, object], transaction["previous_seal"])
    next_ledger = cast(Mapping[str, object], transaction["next_ledger"])
    next_seal = cast(Mapping[str, object], transaction["next_seal"])

    current_ledger = (
        _read_object(ledger_path, "ledger") if ledger_path.exists() else None
    )
    current_seal = _read_object(seal_path, "seal") if seal_path.exists() else None
    ledger_is_old = (
        current_ledger is None
        and not cast(bool, transaction["previous_ledger_existed"])
    ) or (
        current_ledger is not None
        and _ledger_matches_seal(current_ledger, previous_seal)
    )
    ledger_is_new = current_ledger is not None and dict(current_ledger) == dict(
        next_ledger
    )
    seal_is_old = (
        current_seal is None and not cast(bool, transaction["previous_seal_existed"])
    ) or (current_seal is not None and _same_seal(current_seal, previous_seal))
    seal_is_new = current_seal is not None and _same_seal(current_seal, next_seal)
    if not (ledger_is_old or ledger_is_new):
        raise DeploymentLatencyError(
            "ledger does not match either side of the unfinished transaction"
        )
    if not (seal_is_old or seal_is_new):
        raise DeploymentLatencyError(
            "seal does not match either side of the unfinished transaction"
        )
    if ledger_is_old:
        _write_atomic(ledger_path, next_ledger)
    if seal_is_old:
        _write_atomic(seal_path, next_seal)
    transaction_path.unlink()
    _fsync_directory(transaction_path.parent)


def _recover_transaction(ledger_path: Path, seal_path: Path) -> None:
    transaction_path = _transaction_path(ledger_path, seal_path)
    if not transaction_path.exists():
        return
    transaction = _read_object(transaction_path, "transaction")
    _validate_transaction(transaction, ledger_path, seal_path)
    _finish_transaction(transaction_path, transaction, ledger_path, seal_path)


def _commit_ledger_and_seal(
    ledger_path: Path,
    seal_path: Path,
    previous_ledger: Mapping[str, object],
    next_ledger: Mapping[str, object],
) -> None:
    if ledger_path.resolve() == seal_path.resolve():
        raise DeploymentLatencyError("ledger and seal output must be different files")
    previous_seal = ledger_seal(previous_ledger)
    next_seal = ledger_seal(next_ledger)
    if seal_path.exists():
        existing_seal = _read_object(seal_path, "seal")
        _validate_seal(existing_seal)
        if not _same_seal(existing_seal, previous_seal):
            raise DeploymentLatencyError(
                "existing seal output does not match the ledger being extended"
            )
    transaction_path = _transaction_path(ledger_path, seal_path)
    transaction: dict[str, object] = {
        "schema_version": "stock-desk-deployment-latency-transaction-v1",
        "ledger_path": str(ledger_path.resolve()),
        "seal_path": str(seal_path.resolve()),
        "previous_ledger_existed": ledger_path.exists(),
        "previous_seal_existed": seal_path.exists(),
        "previous_seal": previous_seal,
        "next_ledger": next_ledger,
        "next_seal": next_seal,
    }
    _validate_transaction(transaction, ledger_path, seal_path)
    _write_exclusive(transaction_path, transaction)
    _finish_transaction(transaction_path, transaction, ledger_path, seal_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="append one raw sample")
    collect.add_argument("--ledger", type=Path, required=True)
    collect.add_argument("--sample", type=Path, required=True)
    collect.add_argument(
        "--expected-seal",
        type=Path,
        help="required for an existing non-empty ledger to reject tail deletion",
    )
    collect.add_argument("--seal-output", type=Path, required=True)

    seal = commands.add_parser(
        "seal", help="create the genesis seal for an empty ledger"
    )
    seal.add_argument("--ledger", type=Path, required=True)
    seal.add_argument("--output", type=Path, required=True)

    aggregate = commands.add_parser("aggregate", help="build the P50/P95 report")
    aggregate.add_argument("--ledger", type=Path, required=True)
    aggregate.add_argument("--expected-seal", type=Path, required=True)
    aggregate.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "seal":
        ledger = _read_object(args.ledger, "ledger")
        validate_ledger(ledger)
        if ledger["record_count"] != 0:
            raise DeploymentLatencyError(
                "seal command only creates the genesis seal for an empty ledger"
            )
        _write_atomic(args.output, ledger_seal(ledger))
        return 0
    if args.command == "aggregate":
        _recover_transaction(args.ledger, args.expected_seal)
        report = aggregate_ledger(
            _read_object(args.ledger, "ledger"),
            expected_seal=_read_object(args.expected_seal, "seal"),
        )
        _write_atomic(args.output, report)
        return 0

    _recover_transaction(args.ledger, args.seal_output)
    ledger = (
        _read_object(args.ledger, "ledger") if args.ledger.exists() else empty_ledger()
    )
    validate_ledger(ledger)
    record_count = cast(int, ledger["record_count"])
    if record_count > 0 and args.expected_seal is None:
        raise DeploymentLatencyError(
            "--expected-seal is required when appending to a non-empty ledger"
        )
    expected = (
        _read_object(args.expected_seal, "seal")
        if args.expected_seal is not None
        else None
    )
    updated = append_sample(
        ledger, _read_object(args.sample, "sample"), expected_seal=expected
    )
    _commit_ledger_and_seal(args.ledger, args.seal_output, ledger, updated)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeploymentLatencyError as exc:
        raise SystemExit(f"deployment latency contract failed: {exc}") from exc
