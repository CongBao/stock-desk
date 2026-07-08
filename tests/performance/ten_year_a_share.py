"""Deterministic CC0 synthetic data and the v1 aggregate performance gate."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
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
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "performance" / "ten-year-a-share.json"
MINIMUM_SAMPLE_COUNT = 20
MINIMUM_EFFECTIVE_MEMORY_BYTES = 15 * 1024**3
SHANGHAI = ZoneInfo("Asia/Shanghai")
_SHA256_PREFIX = "sha256:"


class PerformanceGateError(ValueError):
    """Raised when performance evidence cannot be trusted as a release gate."""


class FixtureMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["stock-desk-synthetic-performance-v1"]
    fixture_id: Literal["ten-year-a-share"]
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


def _finite_non_negative(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PerformanceGateError(f"{label} must be finite and non-negative")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise PerformanceGateError(f"{label} must be finite and non-negative")
    return result


def _validate_timed_metric(name: str, metric: dict[str, Any]) -> None:
    raw_samples = metric.get("samples")
    if not isinstance(raw_samples, list) or len(raw_samples) < MINIMUM_SAMPLE_COUNT:
        raise PerformanceGateError(f"{name} requires at least 20 raw samples")
    walls: list[float] = []
    correctness = metric.get("correctness_hash")
    if not _is_digest(correctness):
        raise PerformanceGateError(f"{name} correctness hash is invalid")
    for index, raw_sample in enumerate(raw_samples):
        sample = _mapping(raw_sample, f"{name} sample {index}")
        wall = _finite_non_negative(sample.get("wall_seconds"), f"{name} wall time")
        local = _finite_non_negative(sample.get("local_seconds"), f"{name} local time")
        external = _finite_non_negative(
            sample.get("external_wait_seconds"), f"{name} external wait"
        )
        spans = sample.get("provider_span_count")
        if not isinstance(spans, int) or isinstance(spans, bool) or spans < 0:
            raise PerformanceGateError(f"{name} provider-span count is missing")
        raw_spans = sample.get("provider_spans")
        if not isinstance(raw_spans, list):
            raise PerformanceGateError(f"{name} provider-span evidence is missing")
        measured_wait = 0.0
        for raw_span in raw_spans:
            span = _mapping(raw_span, f"{name} provider span")
            if not isinstance(span.get("source"), str) or not isinstance(
                span.get("decision"), str
            ):
                raise PerformanceGateError(f"{name} provider-span evidence is invalid")
            measured_wait += _finite_non_negative(
                span.get("elapsed_seconds"), f"{name} provider span wait"
            )
        if spans != len(raw_spans) or not math.isclose(
            external, measured_wait, rel_tol=0, abs_tol=1e-12
        ):
            raise PerformanceGateError(
                f"{name} provider-span summary does not match measured evidence"
            )
        if external > wall or local > wall + 1e-9:
            raise PerformanceGateError(f"{name} local/external time exceeds wall time")
        if external != 0 or spans != 0:
            raise PerformanceGateError(
                f"{name} network-forbidden cache observed provider-span external wait"
            )
        blocked = sample.get("blocked_external_request_count")
        if not isinstance(blocked, int) or isinstance(blocked, bool) or blocked != 0:
            raise PerformanceGateError(
                f"{name} browser attempted a forbidden external request"
            )
        start = sample.get("rss_start_bytes")
        peak = sample.get("rss_peak_bytes")
        delta = sample.get("rss_delta_bytes")
        if not all(
            isinstance(item, int) and not isinstance(item, bool)
            for item in (start, peak, delta)
        ):
            raise PerformanceGateError(f"{name} process-tree RSS fields are missing")
        assert (
            isinstance(start, int) and isinstance(peak, int) and isinstance(delta, int)
        )
        if start < 0 or peak < start or delta != peak - start:
            raise PerformanceGateError(
                f"{name} process-tree RSS values are inconsistent"
            )
        if not _is_digest(sample.get("rss_process_set_digest")):
            raise PerformanceGateError(f"{name} process-tree role digest is missing")
        if sample.get("correctness_hash") != correctness:
            raise PerformanceGateError(
                f"{name} correctness hash changed across samples"
            )
        walls.append(wall)
    expected_mean = sum(walls) / len(walls)
    expected_p95 = nearest_rank_p95(walls)
    supplied_mean = _finite_non_negative(metric.get("mean_seconds"), f"{name} mean")
    supplied_p95 = _finite_non_negative(metric.get("p95_seconds"), f"{name} p95")
    if not math.isclose(supplied_mean, expected_mean, rel_tol=0, abs_tol=1e-12):
        raise PerformanceGateError(f"{name} mean does not match raw samples")
    if not math.isclose(supplied_p95, expected_p95, rel_tol=0, abs_tol=1e-12):
        raise PerformanceGateError(f"{name} p95 does not match raw samples")
    budget = _finite_non_negative(metric.get("budget_seconds"), f"{name} budget")
    if supplied_p95 > budget:
        raise PerformanceGateError(f"{name} p95 exceeds its absolute budget")


def _validate_environment(environment: dict[str, Any]) -> None:
    required_text = (
        "os",
        "arch",
        "cpu_model",
        "python_version",
        "node_version",
        "browser_version",
    )
    if any(
        not isinstance(environment.get(key), str) or not environment[key]
        for key in required_text
    ):
        raise PerformanceGateError("environment metadata is incomplete")
    logical = environment.get("logical_cpu_count")
    effective = environment.get("effective_cpu_count")
    memory = environment.get("memory_bytes")
    effective_memory = environment.get("effective_memory_bytes")
    if (
        not isinstance(logical, int)
        or logical < 4
        or not isinstance(effective, (int, float))
        or effective < 4
    ):
        raise PerformanceGateError("baseline requires at least four effective CPUs")
    if (
        not isinstance(memory, int)
        or not isinstance(effective_memory, int)
        or min(memory, effective_memory) < MINIMUM_EFFECTIVE_MEMORY_BYTES
    ):
        raise PerformanceGateError(
            "baseline requires nominal 16GB memory (at least 15GiB usable)"
        )
    if (
        not isinstance(environment.get("tool_versions"), dict)
        or not environment["tool_versions"]
    ):
        raise PerformanceGateError("environment tool versions are incomplete")


def validate_performance_result(
    raw: object,
    *,
    expected_fixture_digest: str,
    baseline: object | None = None,
) -> None:
    result = _mapping(raw, "performance result")
    if result.get("schema_version") != "stock-desk-performance-v1":
        raise PerformanceGateError("unsupported performance result schema")
    measured_at = result.get("measured_at_utc")
    if not isinstance(measured_at, str) or not measured_at.endswith("Z"):
        raise PerformanceGateError("measurement UTC timestamp is missing")
    git = _mapping(result.get("git"), "git")
    if (
        not isinstance(git.get("sha"), str)
        or len(git["sha"]) != 40
        or any(character not in "0123456789abcdef" for character in git["sha"])
        or not isinstance(git.get("dirty"), bool)
    ):
        raise PerformanceGateError("git measurement metadata is invalid")
    fixture = _mapping(result.get("fixture"), "fixture")
    if fixture.get("content_digest") != expected_fixture_digest:
        raise PerformanceGateError("performance fixture digest is stale")
    if (
        fixture.get("network_policy") != "forbidden"
        or not isinstance(fixture.get("row_count"), int)
        or fixture.get("scoring_sessions", 0) < 2_400
    ):
        raise PerformanceGateError("performance fixture metadata is invalid")
    _validate_environment(_mapping(result.get("environment"), "environment"))
    process_tree = _mapping(result.get("process_tree"), "process_tree")
    roots = process_tree.get("declared_roots")
    services = process_tree.get("declared_services")
    roles = process_tree.get("sampled_process_roles")
    if (
        not isinstance(roots, list)
        or len(roots) < 5
        or not isinstance(services, list)
        or len(services) < 3
        or not isinstance(roles, list)
        or not {"api", "worker", "web", "browser", "playwright"}.issubset(set(roles))
    ):
        raise PerformanceGateError(
            "declared process tree does not cover all runtime roles"
        )
    definitions = _mapping(result.get("definitions"), "definitions")
    for key in (
        "chart_cold",
        "chart_warm",
        "formula_cache_cold",
        "single_backtest_fresh",
    ):
        if not isinstance(definitions.get(key), str) or not definitions[key]:
            raise PerformanceGateError("warm/cold definitions are incomplete")
    metrics = _mapping(result.get("metrics"), "metrics")
    for name in ("chart_cold", "chart_warm", "formula_preview", "single_backtest"):
        _validate_timed_metric(name, _mapping(metrics.get(name), name))
    pool = _mapping(metrics.get("pool_ui"), "pool_ui")
    pool_samples = pool.get("samples")
    if not isinstance(pool_samples, list) or len(pool_samples) < MINIMUM_SAMPLE_COUNT:
        raise PerformanceGateError("pool_ui requires at least 20 raw samples")
    correctness = pool.get("correctness_hash")
    progress_states = pool.get("observed_progress_states")
    if (
        not isinstance(progress_states, list)
        or len(set(progress_states)) < 2
        or pool.get("worker_claim_observed") is not True
        or pool.get("cancel_status") != "cancelled"
    ):
        raise PerformanceGateError(
            "pool_ui did not prove changing worker progress and real cancellation"
        )
    total_long_tasks = 0
    interaction_kinds: set[str] = set()
    for item in pool_samples:
        sample = _mapping(item, "pool_ui sample")
        count = sample.get("long_task_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise PerformanceGateError("pool_ui Long Task count is invalid")
        total_long_tasks += count
        if count != 0:
            raise PerformanceGateError("pool_ui Long Task count must be exactly zero")
        interaction = sample.get("interaction_kind")
        if interaction not in {"progress", "navigation", "cancel"}:
            raise PerformanceGateError("pool_ui interaction kind is invalid")
        interaction_kinds.add(interaction)
        if sample.get("interactive") is not True:
            raise PerformanceGateError(
                "pool_ui interactions did not remain interactive"
            )
        if sample.get("correctness_hash") != correctness:
            raise PerformanceGateError("pool_ui correctness hash changed")
    if pool.get("long_task_count") != total_long_tasks:
        raise PerformanceGateError("pool_ui aggregate Long Task count is mismatched")
    if interaction_kinds != {"progress", "navigation", "cancel"}:
        raise PerformanceGateError(
            "pool_ui must measure progress, navigation, and cancel windows"
        )
    if baseline is not None:
        baseline_result = _mapping(baseline, "baseline")
        validate_performance_result(
            baseline_result,
            expected_fixture_digest=expected_fixture_digest,
        )
        baseline_fixture = _mapping(baseline_result.get("fixture"), "baseline fixture")
        if baseline_fixture.get("content_digest") != expected_fixture_digest:
            raise PerformanceGateError("baseline fixture digest is stale")
        baseline_metrics = _mapping(baseline_result.get("metrics"), "baseline metrics")
        if baseline_result.get("definitions") != result.get("definitions"):
            raise PerformanceGateError("warm/cold definitions changed from baseline")
        for name in ("chart_cold", "chart_warm", "formula_preview", "single_backtest"):
            current_hash = _mapping(metrics.get(name), name).get("correctness_hash")
            baseline_hash = _mapping(baseline_metrics.get(name), name).get(
                "correctness_hash"
            )
            if current_hash != baseline_hash:
                raise PerformanceGateError(
                    f"{name} correctness hash changed from baseline"
                )
        if _mapping(metrics.get("pool_ui"), "pool_ui").get(
            "correctness_hash"
        ) != _mapping(baseline_metrics.get("pool_ui"), "baseline pool_ui").get(
            "correctness_hash"
        ):
            raise PerformanceGateError("pool_ui correctness hash changed from baseline")
