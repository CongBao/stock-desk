"""Deterministic CC0 synthetic data and the v1 aggregate performance gate."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from stock_desk.market.provenance import (
    BarRoutingRequest,
    RoutedBarSuccess,
    make_routing_manifest,
)
from stock_desk.market.providers.normalization import dataset_version
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarQuery,
    BarResult,
    MarketCapability,
    Period,
    Provenance,
    ProviderId,
    TradingStatus,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = (
    ROOT / "tests" / "fixtures" / "performance" / "full-a-scope-bounded-ten-year.json"
)
MINIMUM_SAMPLE_COUNT = 20
MINIMUM_EFFECTIVE_MEMORY_BYTES = 15 * 1024**3
SHANGHAI = ZoneInfo("Asia/Shanghai")
_SHA256_PREFIX = "sha256:"
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_NODE_VERSION = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
_PLAYWRIGHT_VERSION = re.compile(r"^Version [0-9]+\.[0-9]+\.[0-9]+$")
_CHROMIUM_VERSION = re.compile(r"^Chromium [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
_UBUNTU_IMAGE_OS = re.compile(r"^ubuntu[0-9]{2}$")
_UBUNTU_IMAGE_VERSION = re.compile(r"^[0-9]{8}\.[0-9]+(?:\.[0-9]+)?$")


class PerformanceGateError(ValueError):
    """Raised when performance evidence cannot be trusted as a release gate."""


class FixtureMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["stock-desk-synthetic-performance-v1"]
    fixture_id: Literal["full-a-scope-bounded-ten-year"]
    label: Literal["SYNTHETIC PERFORMANCE FIXTURE — NOT VENDOR DATA"]
    license: Literal["CC0-1.0"]
    network_policy: Literal["forbidden"]
    source: Literal[ProviderId.STOCK_DESK_DEMO]
    generator: Literal["tests/performance/ten_year_a_share.py"]
    symbol: str
    period: Literal[Period.DAY]
    adjustment: Literal[Adjustment.QFQ]
    warmup_start: date
    scoring_start: date
    scoring_end: date
    scope_instrument_count: Literal[5000]
    runnable_symbol_count: Literal[40]
    row_count: int
    scoring_sessions: int
    wave_period: int
    content_digest: str

    @field_validator("content_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _is_digest(value):
            raise ValueError("fixture content digest must be sha256")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if not self.warmup_start < self.scoring_start < self.scoring_end:
            raise ValueError("fixture warmup and scoring windows are not ordered")
        if self.scoring_sessions < 2_400 or self.row_count < self.scoring_sessions:
            raise ValueError("fixture must contain at least 2,400 scoring sessions")
        if self.wave_period < 4 or self.wave_period % 2:
            raise ValueError("fixture wave period must be an even integer")
        return self


class GeneratedFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    routed: RoutedBarSuccess
    content_digest: str

    @property
    def bars(self) -> tuple[Bar, ...]:
        return self.routed.result.bars


def _is_digest(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith(_SHA256_PREFIX):
        return False
    tail = value.removeprefix(_SHA256_PREFIX)
    return len(tail) == 64 and all(
        character in "0123456789abcdef" for character in tail
    )


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return _SHA256_PREFIX + hashlib.sha256(encoded).hexdigest()


def load_fixture_metadata(path: Path = FIXTURE_PATH) -> FixtureMetadata:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("performance fixture metadata must be a regular file")
    return FixtureMetadata.model_validate_json(candidate.read_bytes())


def _weekdays(start: date, end: date) -> tuple[date, ...]:
    return tuple(
        day
        for offset in range((end - start).days)
        if (day := start + timedelta(days=offset)).weekday() < 5
    )


def generate_fixture_bars(metadata: FixtureMetadata) -> GeneratedFixture:
    days = _weekdays(metadata.warmup_start, metadata.scoring_end)
    timestamps = tuple(datetime.combine(day, time(), tzinfo=SHANGHAI) for day in days)
    closes: list[Decimal] = []
    for index in range(len(timestamps)):
        phase = index % metadata.wave_period
        height = (
            phase
            if phase <= metadata.wave_period // 2
            else metadata.wave_period - phase
        )
        closes.append(
            (Decimal("9") + Decimal(height) / Decimal("10")).quantize(Decimal("0.001"))
        )
    bars = tuple(
        Bar(
            symbol=metadata.symbol,
            timestamp=timestamp,
            period=Period.DAY,
            adjustment=Adjustment.QFQ,
            open=closes[index - 1] if index else close,
            high=max(closes[index - 1] if index else close, close) + Decimal("0.2"),
            low=min(closes[index - 1] if index else close, close) - Decimal("0.2"),
            close=close,
            volume=100_000 + index * 17,
            status=TradingStatus.NORMAL,
        )
        for index, (timestamp, close) in enumerate(zip(timestamps, closes, strict=True))
    )
    query = BarQuery(
        symbol=metadata.symbol,
        period=Period.DAY,
        adjustment=Adjustment.QFQ,
        start=timestamps[0],
        end=datetime.combine(metadata.scoring_end, time(), tzinfo=SHANGHAI),
    )
    cutoff = query.end
    version = dataset_version(
        source=ProviderId.STOCK_DESK_DEMO,
        operation="bars",
        request={"query": query},
        data_cutoff=cutoff,
        items=bars,
    )
    provenance = Provenance(
        source=ProviderId.STOCK_DESK_DEMO,
        fetched_at=cutoff + timedelta(minutes=1),
        data_cutoff=cutoff,
        adjustment=Adjustment.QFQ,
        dataset_version=version,
    )
    result = BarResult(
        query=query,
        bars=bars,
        coverage_start=query.start,
        coverage_end=query.end,
        provenance=provenance,
    )
    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=query),
        priority=(ProviderId.STOCK_DESK_DEMO,),
        attempts=(),
        selected_source=ProviderId.STOCK_DESK_DEMO,
        upstream_dataset_version=version,
        upstream_fetched_at=provenance.fetched_at,
        upstream_data_cutoff=cutoff,
        upstream_adjustment=Adjustment.QFQ,
    )
    digest = _canonical_digest(
        {
            "fixture": metadata.model_dump(
                mode="json", exclude={"content_digest", "row_count", "scoring_sessions"}
            ),
            "bars": [bar.model_dump(mode="json") for bar in bars],
        }
    )
    return GeneratedFixture(
        routed=RoutedBarSuccess(result=result, manifest=manifest),
        content_digest=digest,
    )


def nearest_rank_p95(values: list[float]) -> float:
    if not values:
        raise PerformanceGateError("p95 requires raw samples")
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PerformanceGateError(f"{label} must be an object")
    return value


def _exact_mapping(
    value: object, label: str, expected_keys: set[str]
) -> dict[str, Any]:
    result = _mapping(value, label)
    if set(result) != expected_keys:
        raise PerformanceGateError(f"{label} must have exact keys")
    return result


def _text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.lower() == "unavailable"
    ):
        raise PerformanceGateError(f"{label} must be a real nonempty value")
    return value


def _version(value: object, label: str, pattern: re.Pattern[str]) -> str:
    checked = _text(value, label)
    if pattern.fullmatch(checked) is None:
        raise PerformanceGateError(f"{label} has an invalid version format")
    return checked


def _require_local_commit_object(sha: str) -> None:
    try:
        completed = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PerformanceGateError(
            "git SHA could not be verified as a local commit object"
        ) from error
    if completed.returncode != 0:
        raise PerformanceGateError("git SHA is not a local commit object")


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PerformanceGateError(f"{label} must be an integer")
    if value < minimum:
        adjective = "positive" if minimum == 1 else "non-negative"
        raise PerformanceGateError(f"{label} must be {adjective}")
    return value


def _finite_non_negative(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PerformanceGateError(f"{label} must be finite and non-negative")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise PerformanceGateError(f"{label} must be finite and non-negative")
    return result


def _validate_progress_state(value: object, label: str) -> dict[str, Any]:
    state = _exact_mapping(
        value, label, {"status", "stage", "processed", "total", "failed"}
    )
    status = _text(state["status"], f"{label} status")
    stage = _text(state["stage"], f"{label} stage")
    processed = _integer(state["processed"], f"{label} processed")
    total = _integer(state["total"], f"{label} total", minimum=1)
    failed = _integer(state["failed"], f"{label} failed")
    if failed > processed or processed > total:
        raise PerformanceGateError(f"{label} progress counts are inconsistent")
    if status == "cancelled" and stage != "cancelled":
        raise PerformanceGateError(f"{label} cancelled stage is inconsistent")
    return state


def _validate_timed_metric(name: str, value: object) -> None:
    metric = _exact_mapping(
        value,
        name,
        {
            "samples",
            "mean_seconds",
            "p95_seconds",
            "budget_seconds",
            "correctness_hash",
        },
    )
    raw_samples = metric["samples"]
    if not isinstance(raw_samples, list) or len(raw_samples) != MINIMUM_SAMPLE_COUNT:
        raise PerformanceGateError(f"{name} requires exactly 20 raw samples")
    correctness = metric["correctness_hash"]
    if not _is_digest(correctness):
        raise PerformanceGateError(f"{name} correctness hash is invalid")
    walls: list[float] = []
    sample_keys = {
        "wall_seconds",
        "local_seconds",
        "external_wait_seconds",
        "provider_span_count",
        "provider_spans",
        "blocked_external_request_count",
        "rss_start_bytes",
        "rss_peak_bytes",
        "rss_delta_bytes",
        "correctness_hash",
    }
    for index, raw_sample in enumerate(raw_samples):
        sample = _exact_mapping(raw_sample, f"{name} sample {index}", sample_keys)
        wall = _finite_non_negative(sample["wall_seconds"], f"{name} wall time")
        local = _finite_non_negative(sample["local_seconds"], f"{name} local time")
        external = _finite_non_negative(
            sample["external_wait_seconds"], f"{name} external wait"
        )
        spans = _integer(sample["provider_span_count"], f"{name} provider-span count")
        raw_spans = sample["provider_spans"]
        if not isinstance(raw_spans, list):
            raise PerformanceGateError(f"{name} provider-span evidence must be a list")
        if raw_spans or spans:
            raise PerformanceGateError(
                f"{name} provider duration unavailable; cached gate requires zero attempts"
            )
        if external != 0:
            raise PerformanceGateError(f"{name} external wait must be exact zero")
        if external > wall or local > wall + 1e-9:
            raise PerformanceGateError(f"{name} local/external time exceeds wall time")
        if (
            _integer(
                sample["blocked_external_request_count"],
                f"{name} blocked external requests",
            )
            != 0
        ):
            raise PerformanceGateError(f"{name} attempted a forbidden external request")
        start = _integer(sample["rss_start_bytes"], f"{name} RSS start")
        peak = _integer(sample["rss_peak_bytes"], f"{name} RSS peak")
        delta = _integer(sample["rss_delta_bytes"], f"{name} RSS delta")
        if peak < start or delta != peak - start:
            raise PerformanceGateError(
                f"{name} process-tree RSS values are inconsistent"
            )
        if sample["correctness_hash"] != correctness:
            raise PerformanceGateError(
                f"{name} correctness hash changed across samples"
            )
        walls.append(wall)
    expected_mean = sum(walls) / len(walls)
    expected_p95 = nearest_rank_p95(walls)
    supplied_mean = _finite_non_negative(metric["mean_seconds"], f"{name} mean")
    supplied_p95 = _finite_non_negative(metric["p95_seconds"], f"{name} p95")
    if not math.isclose(supplied_mean, expected_mean, rel_tol=0, abs_tol=1e-12):
        raise PerformanceGateError(f"{name} mean does not match raw samples")
    if not math.isclose(supplied_p95, expected_p95, rel_tol=0, abs_tol=1e-12):
        raise PerformanceGateError(f"{name} p95 does not match raw samples")
    budget = _finite_non_negative(metric["budget_seconds"], f"{name} budget")
    if supplied_p95 > budget:
        raise PerformanceGateError(f"{name} p95 exceeds its absolute budget")


def _validate_environment(value: object, evidence_kind: str) -> None:
    environment = _exact_mapping(
        value,
        "environment",
        {
            "os",
            "arch",
            "cpu_model",
            "logical_cpu_count",
            "effective_cpu_count",
            "memory_bytes",
            "effective_memory_bytes",
            "python_version",
            "node_version",
            "browser_version",
            "tool_versions",
            "runner",
        },
    )
    for key in ("os", "arch", "cpu_model"):
        _text(environment[key], f"environment {key}")
    _version(environment["python_version"], "Python version", _SEMVER)
    _version(environment["node_version"], "Node version", _NODE_VERSION)
    _version(environment["browser_version"], "browser version", _CHROMIUM_VERSION)
    logical = _integer(environment["logical_cpu_count"], "logical CPU count", minimum=1)
    effective = _finite_non_negative(
        environment["effective_cpu_count"], "effective CPU count"
    )
    if logical < 4 or effective < 4:
        raise PerformanceGateError("baseline requires at least four effective CPUs")
    memory = _integer(environment["memory_bytes"], "physical memory bytes", minimum=1)
    effective_memory = _integer(
        environment["effective_memory_bytes"], "effective memory bytes", minimum=1
    )
    if min(memory, effective_memory) < MINIMUM_EFFECTIVE_MEMORY_BYTES:
        raise PerformanceGateError(
            "baseline requires nominal 16GB memory (at least 15GiB usable)"
        )
    versions = _exact_mapping(
        environment["tool_versions"],
        "tool versions",
        {"duckdb", "playwright", "pnpm"},
    )
    _version(versions["duckdb"], "DuckDB tool version", _SEMVER)
    _version(versions["playwright"], "Playwright tool version", _PLAYWRIGHT_VERSION)
    _version(versions["pnpm"], "pnpm tool version", _SEMVER)
    runner = _exact_mapping(
        environment["runner"],
        "runner",
        {
            "provider",
            "os",
            "arch",
            "name",
            "image_os",
            "image_version",
            "repository",
            "run_id",
            "run_attempt",
        },
    )
    for key in ("provider", "os", "arch", "name"):
        _text(runner[key], f"runner {key}")
    for key in ("image_os", "image_version", "repository"):
        if runner[key] is not None:
            _text(runner[key], f"runner {key}")
    for key in ("run_id", "run_attempt"):
        if runner[key] is not None:
            _integer(runner[key], f"runner {key}", minimum=1)
    if evidence_kind == "target_baseline":
        if effective != 4 or logical != 4:
            raise PerformanceGateError("target baseline requires exactly four CPUs")
        if memory > 17 * 1024**3:
            raise PerformanceGateError("target baseline requires nominal 16GB memory")
        if (
            runner["provider"] != "github_actions"
            or runner["os"] != "Linux"
            or runner["arch"] != "X64"
            or runner["repository"] != "CongBao/stock-desk"
            or not isinstance(runner["image_os"], str)
            or _UBUNTU_IMAGE_OS.fullmatch(runner["image_os"]) is None
            or not isinstance(runner["image_version"], str)
            or _UBUNTU_IMAGE_VERSION.fullmatch(runner["image_version"]) is None
            or not isinstance(runner["run_id"], int)
            or isinstance(runner["run_id"], bool)
            or runner["run_id"] < 1
            or not isinstance(runner["run_attempt"], int)
            or isinstance(runner["run_attempt"], bool)
            or runner["run_attempt"] < 1
            or any(
                runner[key] is None
                for key in (
                    "image_os",
                    "image_version",
                    "repository",
                    "run_id",
                    "run_attempt",
                )
            )
        ):
            raise PerformanceGateError(
                "target baseline requires GitHub Ubuntu x64 runner metadata"
            )


def _validate_pool(value: object) -> None:
    pool = _exact_mapping(
        value,
        "pool_ui",
        {
            "samples",
            "long_task_count",
            "observed_progress_states",
            "worker_claim_observed",
            "cancel_status",
            "semantic_evidence",
            "correctness_hash",
        },
    )
    semantic = _exact_mapping(
        pool["semantic_evidence"],
        "pool semantic evidence",
        {
            "formula_checksum",
            "pool_membership_digest",
            "pool_data_digest",
            "terminal_status",
        },
    )
    for key in ("formula_checksum", "pool_membership_digest", "pool_data_digest"):
        if not _is_digest(semantic[key]):
            raise PerformanceGateError(f"pool semantic {key} is invalid")
    if (
        semantic["terminal_status"] != "cancelled"
        or pool["cancel_status"] != "cancelled"
    ):
        raise PerformanceGateError("pool cancellation status is invalid")
    correctness = _canonical_digest(semantic)
    if pool["correctness_hash"] != correctness:
        raise PerformanceGateError("pool semantic correctness hash is mismatched")
    progress_states = pool["observed_progress_states"]
    if not isinstance(progress_states, list) or len(progress_states) != 18:
        raise PerformanceGateError(
            "pool_ui requires exactly 18 rendered progress states"
        )
    progress_keys: list[str] = []
    for index, state in enumerate(progress_states):
        checked = _validate_progress_state(state, f"pool progress state {index}")
        progress_keys.append(json.dumps(checked, sort_keys=True, separators=(",", ":")))
    if len(set(progress_keys)) < 2:
        raise PerformanceGateError(
            "pool rendered progress evidence requires at least two distinct states"
        )
    if pool["worker_claim_observed"] is not True:
        raise PerformanceGateError("pool worker claim was not observed")
    samples = pool["samples"]
    if not isinstance(samples, list) or len(samples) != MINIMUM_SAMPLE_COUNT:
        raise PerformanceGateError("pool_ui requires exactly 20 raw windows")
    kinds: list[str] = []
    total_long_tasks = 0
    for index, raw_sample in enumerate(samples):
        sample = _exact_mapping(
            raw_sample,
            f"pool_ui sample {index}",
            {
                "long_task_count",
                "interaction_kind",
                "interactive",
                "rendered_state",
                "api_state",
                "correctness_hash",
            },
        )
        count = _integer(sample["long_task_count"], "pool Long Task count")
        if count != 0:
            raise PerformanceGateError("pool Long Task count must be exactly zero")
        total_long_tasks += count
        kind = sample["interaction_kind"]
        if kind not in {"progress", "navigation", "cancel"}:
            raise PerformanceGateError("pool interaction kind is invalid")
        kinds.append(kind)
        if sample["interactive"] is not True:
            raise PerformanceGateError("pool interaction did not remain interactive")
        rendered = _validate_progress_state(
            sample["rendered_state"], "rendered progress"
        )
        api = _validate_progress_state(sample["api_state"], "API progress")
        if rendered != api:
            raise PerformanceGateError("pool rendered/API progress evidence mismatched")
        if index < 18 and rendered != progress_states[index]:
            raise PerformanceGateError(
                "pool rendered progress state ordering mismatched"
            )
        if sample["correctness_hash"] != correctness:
            raise PerformanceGateError("pool correctness hash changed")
    if kinds != ["progress"] * 18 + ["navigation", "cancel"]:
        raise PerformanceGateError(
            "pool must contain 18 progress, navigation, and cancel windows"
        )
    aggregate_long_tasks = _integer(
        pool["long_task_count"], "pool aggregate Long Task count"
    )
    if aggregate_long_tasks != total_long_tasks:
        raise PerformanceGateError("pool aggregate Long Task count is mismatched")
    final_state = _mapping(samples[-1], "pool cancellation sample")["rendered_state"]
    if _mapping(final_state, "pool final state").get("status") != "cancelled":
        raise PerformanceGateError("pool final rendered state is not cancelled")


def validate_performance_result(
    raw: object,
    *,
    expected_fixture_digest: str,
    baseline: object | None = None,
    expected_source_sha: str | None = None,
) -> None:
    result = _exact_mapping(
        raw,
        "performance result",
        {
            "schema_version",
            "evidence_kind",
            "measured_at_utc",
            "git",
            "fixture",
            "environment",
            "process_tree",
            "definitions",
            "metrics",
        },
    )
    if result["schema_version"] != "stock-desk-performance-v1":
        raise PerformanceGateError("unsupported performance result schema")
    evidence_kind = result["evidence_kind"]
    if evidence_kind not in {"reference", "target_baseline"}:
        raise PerformanceGateError("performance evidence kind is invalid")
    measured_at = result["measured_at_utc"]
    if (
        not isinstance(measured_at, str)
        or "T" not in measured_at
        or not measured_at.endswith("Z")
    ):
        raise PerformanceGateError("measurement must be a real UTC datetime")
    try:
        parsed_at = datetime.fromisoformat(measured_at.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise PerformanceGateError("measurement must be a real UTC datetime") from error
    if parsed_at.tzinfo is None or parsed_at.utcoffset() != timedelta(0):
        raise PerformanceGateError("measurement must be a real UTC datetime")
    git = _exact_mapping(result["git"], "git", {"sha", "dirty"})
    if (
        not isinstance(git["sha"], str)
        or len(git["sha"]) != 40
        or any(character not in "0123456789abcdef" for character in git["sha"])
    ):
        raise PerformanceGateError(
            "git SHA must be 40 lowercase hexadecimal characters"
        )
    if git["dirty"] is not False:
        raise PerformanceGateError("baseline evidence requires a clean git checkout")
    if expected_source_sha is not None:
        if git["sha"] != expected_source_sha:
            raise PerformanceGateError(
                "git SHA does not match the expected source commit"
            )
        _require_local_commit_object(expected_source_sha)
    fixture = _exact_mapping(
        result["fixture"],
        "fixture",
        {
            "fixture_id",
            "content_digest",
            "row_count",
            "scoring_sessions",
            "scope_instrument_count",
            "runnable_symbol_count",
            "network_policy",
        },
    )
    if fixture["content_digest"] != expected_fixture_digest:
        raise PerformanceGateError("performance fixture digest is stale")
    if (
        fixture["fixture_id"] != "full-a-scope-bounded-ten-year"
        or fixture["network_policy"] != "forbidden"
    ):
        raise PerformanceGateError("performance fixture identity is invalid")
    _integer(fixture["row_count"], "fixture row count", minimum=2400)
    _integer(fixture["scoring_sessions"], "fixture scoring sessions", minimum=2400)
    if (
        fixture["scope_instrument_count"] != 5000
        or fixture["runnable_symbol_count"] != 40
    ):
        raise PerformanceGateError("performance fixture scope is invalid")
    _validate_environment(result["environment"], evidence_kind)
    process_tree = _exact_mapping(
        result["process_tree"],
        "process tree",
        {
            "declared_roots",
            "declared_services",
            "sampled_process_roles",
            "role_set_digest",
        },
    )
    roots = process_tree["declared_roots"]
    if not isinstance(roots, list) or len(roots) < 5:
        raise PerformanceGateError("process roots are incomplete")
    checked_roots = [_integer(item, "process root", minimum=1) for item in roots]
    if checked_roots != sorted(set(checked_roots)):
        raise PerformanceGateError("process roots must be sorted and unique")
    services = process_tree["declared_services"]
    if not isinstance(services, list) or len(services) != 3:
        raise PerformanceGateError("declared services are incomplete")
    service_roles: set[str] = set()
    service_pids: set[int] = set()
    for index, raw_service in enumerate(services):
        service = _exact_mapping(
            raw_service, f"service {index}", {"pid", "role", "command"}
        )
        pid = _integer(service["pid"], "service PID", minimum=1)
        role = _text(service["role"], "service role")
        raw_command = service["command"]
        if not isinstance(raw_command, list) or not raw_command:
            raise PerformanceGateError("service command tokens are invalid")
        command = [
            _text(token, f"service {index} command token") for token in raw_command
        ]
        role_matches_command = (
            (role == "api" and len(command) >= 4 and command[1:3] == ["-m", "uvicorn"])
            or (
                role == "worker"
                and command[1:] == ["-m", "scripts.e2e_dev", "--worker"]
            )
            or (role == "web" and command == ["pnpm", "--dir", "web", "dev"])
        )
        if not role_matches_command:
            raise PerformanceGateError("service role/command relationship is invalid")
        if pid not in checked_roots:
            raise PerformanceGateError("service PID must be a declared root")
        service_pids.add(pid)
        service_roles.add(role)
    service_order = [
        _integer(
            _mapping(service, "declared service")["pid"],
            "service PID",
            minimum=1,
        )
        for service in services
    ]
    if service_order != sorted(service_order):
        raise PerformanceGateError("declared services must be sorted by PID")
    if len(service_pids) != 3 or service_roles != {"api", "worker", "web"}:
        raise PerformanceGateError("service PID/role relationships are invalid")
    roles = process_tree["sampled_process_roles"]
    if (
        not isinstance(roles, list)
        or roles != sorted(set(roles))
        or not {"api", "worker", "web", "browser", "playwright"}.issubset(set(roles))
        or not set(roles).issubset(
            {"api", "worker", "web", "browser", "playwright", "formula-child"}
        )
    ):
        raise PerformanceGateError("sampled process roles are invalid")
    if process_tree["role_set_digest"] != _canonical_digest(roles):
        raise PerformanceGateError("process role-set digest is mismatched")
    definitions = _exact_mapping(
        result["definitions"],
        "definitions",
        {
            "chart_cold",
            "chart_warm",
            "formula_cache_cold",
            "single_backtest_fresh",
            "pool_ui",
        },
    )
    for key, definition in definitions.items():
        _text(definition, f"definition {key}")
    metrics = _exact_mapping(
        result["metrics"],
        "metrics",
        {"chart_cold", "chart_warm", "formula_preview", "single_backtest", "pool_ui"},
    )
    for name in ("chart_cold", "chart_warm", "formula_preview", "single_backtest"):
        _validate_timed_metric(name, metrics[name])
    _validate_pool(metrics["pool_ui"])
    if baseline is not None:
        validate_performance_result(
            baseline, expected_fixture_digest=expected_fixture_digest
        )
        baseline_result = _mapping(baseline, "baseline")
        baseline_metrics = _mapping(baseline_result["metrics"], "baseline metrics")
        if baseline_result["definitions"] != result["definitions"]:
            raise PerformanceGateError("warm/cold definitions changed from baseline")
        for name in (
            "chart_cold",
            "chart_warm",
            "formula_preview",
            "single_backtest",
            "pool_ui",
        ):
            if (
                _mapping(metrics[name], name)["correctness_hash"]
                != _mapping(baseline_metrics[name], f"baseline {name}")[
                    "correctness_hash"
                ]
            ):
                raise PerformanceGateError(
                    f"{name} correctness hash changed from baseline"
                )
