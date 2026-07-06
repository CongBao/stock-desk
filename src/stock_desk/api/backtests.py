from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import re
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Path, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from stock_desk.api.market import MarketServices
from stock_desk.backtest.repository import (
    BacktestConflict,
    BacktestFailureSnapshot,
    BacktestGroupSnapshot,
    BacktestLogSnapshot,
    BacktestNotFound,
    BacktestOutcomeSnapshot,
    BacktestOverviewSnapshot,
    BacktestPage,
    BacktestReportSnapshot,
    BacktestRepository,
    BacktestRepositoryError,
    BacktestTradeSnapshot,
    BacktestSymbolSnapshot,
)
from stock_desk.backtest.types import MAX_QUANTITY_SHARES, PinnedMarketRef
from stock_desk.backtest.export import stream_export
from stock_desk.backtest.service import (
    BacktestIntent,
    BacktestPreflight,
    BacktestService,
    BacktestSubmissionError,
    SubmittedBacktest,
)
from stock_desk.formula.service import FormulaService
from stock_desk.formula.signal_series import MAX_PUBLIC_OUTPUTS
from stock_desk.formula.repository import (
    FormulaConflict,
    FormulaNotFound,
    FormulaValidationError,
)
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.instruments import (
    InstrumentConflict,
    InstrumentNotFound,
    InstrumentValidationError,
)
from stock_desk.market.pools import (
    PoolConflict,
    PoolNotFound,
    PoolRevisionConflict,
    PoolValidationError,
)
from stock_desk.market.types import Adjustment, CanonicalSymbol, Period
from stock_desk.tasks.repository import TaskRepository
from stock_desk.tasks.repository import TaskConflict


class _BacktestDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SingleScopeRequest(_BacktestDTO):
    kind: Literal["single"]
    symbol: CanonicalSymbol


UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
UUIDIdentity = Annotated[str, Field(min_length=36, max_length=36, pattern=UUID_PATTERN)]
RunIdPath = Annotated[str, Path(pattern=UUID_PATTERN)]
PresetPoolId = Annotated[
    str,
    Field(
        min_length=8,
        max_length=71,
        pattern=r"^preset:[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$",
    ),
]
SnapshotId = Annotated[
    str, Field(pattern=r"^sha256:[0-9a-f]{64}$", min_length=71, max_length=71)
]


class PresetScopeRequest(_BacktestDTO):
    kind: Literal["preset"]
    pool_id: PresetPoolId
    snapshot_id: SnapshotId


class CustomScopeRequest(_BacktestDTO):
    kind: Literal["custom"]
    pool_id: UUIDIdentity
    revision: Annotated[StrictInt, Field(gt=0)]


ScopeRequest = Annotated[
    SingleScopeRequest | PresetScopeRequest | CustomScopeRequest,
    Field(discriminator="kind"),
]
MAX_SAFE_INTEGER = 2**53 - 1
ParameterValue = (
    Annotated[StrictInt, Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)]
    | Annotated[StrictFloat, Field(allow_inf_nan=False)]
)


def _exact_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("exact decimal must be encoded as a string")
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("exact decimal must be encoded as a string")
    if re.fullmatch(r"(?:0|[1-9][0-9]*|(?:0|[1-9][0-9]*)\.[0-9]*[1-9])", value) is None:
        raise ValueError("exact decimal is not canonical")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("exact decimal is invalid") from error
    if not parsed.is_finite():
        raise ValueError("exact decimal must be finite")
    return parsed


class BacktestCreateRequest(_BacktestDTO):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    scope: ScopeRequest
    formula_version_id: UUIDIdentity
    formula_parameters: Annotated[
        dict[Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")], ParameterValue],
        Field(max_length=64),
    ] = Field(default_factory=dict)
    period: Period
    adjustment: Adjustment
    scoring_start: AwareDatetime
    scoring_end: AwareDatetime
    quantity_shares: Annotated[
        StrictInt, Field(gt=0, le=MAX_QUANTITY_SHARES, multiple_of=100)
    ] = 1_000
    commission_bps: Decimal
    minimum_commission: Decimal
    sell_tax_bps: Decimal
    slippage_bps: Decimal

    @field_validator("scoring_start", "scoring_end", mode="before")
    @classmethod
    def validate_rfc3339_timestamp(cls, value: object) -> object:
        if (
            not isinstance(value, str)
            or re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})",
                value,
            )
            is None
        ):
            raise ValueError("timestamp must be RFC3339 with a timezone")
        return value

    @field_validator("scoring_start", "scoring_end", mode="after")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_range_and_costs(self) -> BacktestCreateRequest:
        if self.scoring_start >= self.scoring_end:
            raise ValueError("scoring_start must be before scoring_end")
        if any(
            value < 0
            for value in (
                self.commission_bps,
                self.minimum_commission,
                self.sell_tax_bps,
                self.slippage_bps,
            )
        ):
            raise ValueError("cost must not be negative")
        if any(
            value > 10_000
            for value in (
                self.commission_bps,
                self.sell_tax_bps,
                self.slippage_bps,
            )
        ):
            raise ValueError("basis points exceed maximum")
        return self

    @field_validator(
        "commission_bps",
        "minimum_commission",
        "sell_tax_bps",
        "slippage_bps",
        mode="before",
    )
    @classmethod
    def validate_exact_decimal(cls, value: object) -> Decimal:
        return _exact_decimal(value)

    @field_validator("formula_parameters", mode="before")
    @classmethod
    def validate_parameter_values(cls, value: object) -> object:
        if isinstance(value, Mapping):
            for item in value.values():
                if isinstance(item, bool) or type(item) not in {int, float}:
                    raise ValueError("formula parameter must be numeric")
                if type(item) is int:
                    if abs(item) > MAX_SAFE_INTEGER:
                        raise ValueError("integer parameter is out of range")
                    continue
                if not math.isfinite(item):
                    raise ValueError("formula parameter must be finite")
        return value

    def to_intent(self) -> BacktestIntent:
        scope = self.scope
        scope_kind: Literal["single", "preset", "custom"]
        if isinstance(scope, SingleScopeRequest):
            scope_kind = "single"
            symbol = str(scope.symbol)
            scope_id = None
            revision = None
        elif isinstance(scope, PresetScopeRequest):
            scope_kind = "preset"
            symbol = None
            scope_id = scope.pool_id
            revision = scope.snapshot_id
        else:
            scope_kind = "custom"
            symbol = None
            scope_id = scope.pool_id
            revision = str(scope.revision)
        return BacktestIntent(
            scope_kind=scope_kind,
            symbol=symbol,
            scope_id=scope_id,
            scope_revision_or_snapshot_id=revision,
            formula_version_id=self.formula_version_id,
            formula_parameters=self.formula_parameters,
            period=self.period,
            adjustment=self.adjustment,
            scoring_start=self.scoring_start,
            scoring_end=self.scoring_end,
            quantity_shares=self.quantity_shares,
            commission_bps=self.commission_bps,
            minimum_commission=self.minimum_commission,
            sell_tax_bps=self.sell_tax_bps,
            slippage_bps=self.slippage_bps,
        )


class BacktestSubmissionResponse(_BacktestDTO):
    run_id: str
    task_id: str
    snapshot_id: str
    warnings: tuple[str, ...]

    @classmethod
    def from_submission(
        cls, submitted: SubmittedBacktest
    ) -> BacktestSubmissionResponse:
        return cls(
            run_id=submitted.run_id,
            task_id=submitted.task_id,
            snapshot_id=submitted.snapshot_id,
            warnings=submitted.warnings,
        )


class BacktestCopyRequest(_BacktestDTO):
    mode: Literal["exact", "latest"]


class BacktestOverviewResponse(_BacktestDTO):
    run_id: str
    task_id: str
    snapshot_id: str
    status: str
    stage: str
    total: int
    processed: int
    failed: int
    progress: float
    result_hash: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_snapshot(cls, item: BacktestOverviewSnapshot) -> BacktestOverviewResponse:
        return cls(
            run_id=item.run_id,
            task_id=item.task_id,
            snapshot_id=item.snapshot_id,
            status=item.status,
            stage=item.stage,
            total=item.total,
            processed=item.processed,
            failed=item.failed,
            progress=item.processed / item.total,
            result_hash=item.result_hash,
            created_at=item.created_at,
            updated_at=item.updated_at,
            started_at=item.started_at,
            finished_at=item.finished_at,
        )


class BacktestListResponse(_BacktestDTO):
    items: Annotated[tuple[BacktestOverviewResponse, ...], Field(max_length=100)]
    next_cursor: Annotated[str | None, Field(max_length=512)]


class BacktestSourceSummaryResponse(_BacktestDTO):
    signal: Annotated[tuple[str, ...], Field(max_length=5)]
    execution: Annotated[tuple[str, ...], Field(max_length=5)]
    status: Annotated[tuple[str, ...], Field(max_length=5)]


class BacktestProvenanceSummaryResponse(_BacktestDTO):
    instrument_dataset_version: str
    symbol_count: int
    runnable_count: int
    gap_count: int
    source_ids: BacktestSourceSummaryResponse
    digest: str


class BacktestOutcomeResponse(_BacktestDTO):
    total: Annotated[StrictInt, Field(ge=1, le=10_000)]
    succeeded: Annotated[StrictInt, Field(ge=0, le=10_000)]
    failed: Annotated[StrictInt, Field(ge=0, le=10_000)]
    data_insufficient: Annotated[StrictInt, Field(ge=0, le=10_000)]
    unprocessed: Annotated[StrictInt, Field(ge=0, le=10_000)]

    @model_validator(mode="after")
    def validate_total(self) -> BacktestOutcomeResponse:
        if (
            self.succeeded + self.failed + self.data_insufficient + self.unprocessed
            != self.total
        ):
            raise ValueError("backtest outcomes do not reconcile")
        return self

    @classmethod
    def from_snapshot(cls, item: BacktestOutcomeSnapshot) -> BacktestOutcomeResponse:
        return cls(
            total=item.total,
            succeeded=item.succeeded,
            failed=item.failed,
            data_insufficient=item.data_insufficient,
            unprocessed=item.unprocessed,
        )


class BacktestReportResponse(_BacktestDTO):
    overview: BacktestOverviewResponse
    formula_version_id: str
    formula_checksum: str
    formula_engine_version: str
    compatibility_version: str
    backtest_engine_version: str
    formula_parameters: tuple[dict[str, object], ...]
    provenance: BacktestProvenanceSummaryResponse
    period: str
    adjustment: str
    quantity_shares: int
    costs: BacktestPreflightCostsResponse
    execution_rules_version: str
    cost_model_version: str
    sizing_version: str
    warmup_policy_version: str
    metrics: dict[str, object]
    disclaimer: str
    outcomes: BacktestOutcomeResponse

    @model_validator(mode="after")
    def validate_outcomes(self) -> BacktestReportResponse:
        if (
            self.outcomes.total != self.overview.total
            or self.outcomes.succeeded
            + self.outcomes.failed
            + self.outcomes.data_insufficient
            != self.overview.processed
            or self.outcomes.failed + self.outcomes.data_insufficient
            != self.overview.failed
        ):
            raise ValueError("backtest report outcomes do not match overview")
        return self

    @classmethod
    def from_snapshot(cls, item: BacktestReportSnapshot) -> BacktestReportResponse:
        return cls(
            overview=BacktestOverviewResponse.from_snapshot(item.overview),
            formula_version_id=item.formula_version_id,
            formula_checksum=item.formula_checksum,
            formula_engine_version=item.formula_engine_version,
            compatibility_version=item.compatibility_version,
            backtest_engine_version=item.backtest_engine_version,
            formula_parameters=tuple(dict(value) for value in item.formula_parameters),
            provenance=BacktestProvenanceSummaryResponse(
                instrument_dataset_version=item.instrument_dataset_version,
                symbol_count=item.symbol_count,
                runnable_count=item.runnable_count,
                gap_count=item.gap_count,
                source_ids=BacktestSourceSummaryResponse(
                    signal=item.signal_source_ids,
                    execution=item.execution_source_ids,
                    status=item.status_source_ids,
                ),
                digest=item.provenance_digest,
            ),
            period=item.period,
            adjustment=item.adjustment,
            quantity_shares=item.quantity_shares,
            costs=BacktestPreflightCostsResponse(
                commission_bps=item.commission_bps,
                minimum_commission=item.minimum_commission,
                sell_tax_bps=item.sell_tax_bps,
                slippage_bps=item.slippage_bps,
            ),
            execution_rules_version=item.execution_rules_version,
            cost_model_version=item.cost_model_version,
            sizing_version=item.sizing_version,
            warmup_policy_version=item.warmup_policy_version,
            metrics=dict(item.metrics),
            disclaimer=item.disclaimer,
            outcomes=BacktestOutcomeResponse.from_snapshot(item.outcomes),
        )


class BacktestGroupResponse(_BacktestDTO):
    dimension: str
    key: str
    payload: dict[str, object]


class BacktestTradeResponse(_BacktestDTO):
    symbol: str
    ordinal: int
    payload: dict[str, object]


class BacktestFailureResponse(_BacktestDTO):
    symbol: str
    ordinal: int
    reason: str
    detail: dict[str, object]


class BacktestLogResponse(_BacktestDTO):
    ordinal: int
    level: str
    message: str
    detail: dict[str, object]


class BacktestSymbolResponse(_BacktestDTO):
    symbol: str
    ordinal: int
    input_kind: Literal["runnable", "gap"]
    status: str
    signal_series_id: str | None
    provenance: dict[str, object]


ReplayTimestamp = Annotated[
    str,
    Field(
        min_length=20,
        max_length=40,
        pattern=(
            r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
            r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
        ),
    ),
]
ReplayDecimal = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$",
    ),
]


class BacktestReplayBarResponse(_BacktestDTO):
    symbol: CanonicalSymbol
    timestamp: ReplayTimestamp
    period: Literal["1d", "1w", "60m"]
    adjustment: Literal["none", "qfq", "hfq"]
    open: ReplayDecimal
    high: ReplayDecimal
    low: ReplayDecimal
    close: ReplayDecimal
    volume: Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]
    status: Literal["unknown", "normal", "suspended", "limit_up", "limit_down"]


class BacktestReplayNumericOutputResponse(_BacktestDTO):
    name: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
    values: Annotated[tuple[float | None, ...], Field(max_length=500)]


class BacktestReplaySignalResponse(_BacktestDTO):
    name: Literal["BUY", "SELL"]
    values: Annotated[tuple[bool | None, ...], Field(max_length=500)]


class BacktestReplayFormulaResponse(_BacktestDTO):
    signal_series_id: SnapshotId
    formula_version_id: UUIDIdentity
    formula_checksum: SnapshotId
    engine_version: Annotated[str, Field(min_length=1, max_length=64)]
    compatibility_version: Annotated[str, Field(min_length=1, max_length=64)]
    numeric_outputs: Annotated[
        tuple[BacktestReplayNumericOutputResponse, ...],
        Field(max_length=MAX_PUBLIC_OUTPUTS),
    ]
    signals: Annotated[tuple[BacktestReplaySignalResponse, ...], Field(max_length=2)]


class BacktestReplayFillMarkerResponse(_BacktestDTO):
    side: Literal["buy", "sell"]
    signal_at: ReplayTimestamp
    filled_at: ReplayTimestamp
    anchor_ordinal: Annotated[StrictInt, Field(ge=0)]
    reference_open: ReplayDecimal
    fill_price: ReplayDecimal
    quantity: Annotated[StrictInt, Field(gt=0)]


class BacktestReplayExecutionEvidenceResponse(_BacktestDTO):
    side: Literal["buy", "sell"]
    filled_at: ReplayTimestamp
    bar: BacktestReplayBarResponse


class BacktestReplayPinResponse(_BacktestDTO):
    manifest_record_id: SnapshotId
    dataset_version: SnapshotId
    route_version: SnapshotId
    source: Annotated[str, Field(min_length=1, max_length=32)]
    data_cutoff: ReplayTimestamp


class BacktestReplayProvenanceResponse(_BacktestDTO):
    signal: BacktestReplayPinResponse
    execution: BacktestReplayPinResponse
    status: BacktestReplayPinResponse


class BacktestReplayResponse(_BacktestDTO):
    run_id: UUIDIdentity
    snapshot_id: SnapshotId
    result_hash: SnapshotId | None
    symbol: CanonicalSymbol
    trade_ordinal: Annotated[StrictInt, Field(ge=0)]
    period: Literal["1d", "1w", "60m"]
    adjustment: Literal["none", "qfq", "hfq"]
    bars: Annotated[tuple[BacktestReplayBarResponse, ...], Field(max_length=500)]
    formula: BacktestReplayFormulaResponse
    trade: Annotated[dict[str, object], Field(max_length=64)]
    fill_markers: Annotated[
        tuple[BacktestReplayFillMarkerResponse, ...], Field(max_length=2)
    ]
    execution_evidence: Annotated[
        tuple[BacktestReplayExecutionEvidenceResponse, ...], Field(max_length=2)
    ]
    provenance: BacktestReplayProvenanceResponse
    next_cursor: Annotated[str | None, Field(max_length=512)]

    @model_validator(mode="after")
    def validate_alignment(self) -> BacktestReplayResponse:
        count = len(self.bars)
        if tuple(item.name for item in self.formula.signals) != ("BUY", "SELL"):
            raise ValueError("backtest replay signals are invalid")
        if any(len(item.values) != count for item in self.formula.numeric_outputs):
            raise ValueError("backtest replay numeric outputs are not aligned")
        if any(len(item.values) != count for item in self.formula.signals):
            raise ValueError("backtest replay signals are not aligned")
        if len(self.fill_markers) != len(self.execution_evidence):
            raise ValueError("backtest replay fills are not aligned")
        marker_pairs = tuple((item.side, item.filled_at) for item in self.fill_markers)
        evidence_pairs = tuple(
            (item.side, item.filled_at) for item in self.execution_evidence
        )
        if marker_pairs != evidence_pairs or len(
            {item.side for item in self.fill_markers}
        ) != len(self.fill_markers):
            raise ValueError("backtest replay fills are mismatched")
        realized = self.trade.get("realized")
        if type(realized) is not bool or len(self.fill_markers) != (
            2 if realized else 1
        ):
            raise ValueError("backtest replay fills do not match trade state")
        expected_execution_period = "1d" if self.period == "1w" else self.period
        if any(
            item.bar.period != expected_execution_period
            or item.bar.symbol != self.symbol
            or item.bar.adjustment != self.adjustment
            for item in self.execution_evidence
        ):
            raise ValueError("backtest replay execution evidence is invalid")
        return self


class BacktestGroupPageResponse(_BacktestDTO):
    items: Annotated[tuple[BacktestGroupResponse, ...], Field(max_length=100)]
    next_cursor: str | None


class BacktestTradePageResponse(_BacktestDTO):
    items: Annotated[tuple[BacktestTradeResponse, ...], Field(max_length=100)]
    next_cursor: str | None


class BacktestFailurePageResponse(_BacktestDTO):
    items: Annotated[tuple[BacktestFailureResponse, ...], Field(max_length=100)]
    next_cursor: str | None


class BacktestLogPageResponse(_BacktestDTO):
    items: Annotated[tuple[BacktestLogResponse, ...], Field(max_length=100)]
    next_cursor: str | None
    after_cursor: str | None


class BacktestSymbolPageResponse(_BacktestDTO):
    items: Annotated[tuple[BacktestSymbolResponse, ...], Field(max_length=100)]
    next_cursor: str | None


class BacktestErrorResponse(_BacktestDTO):
    code: Annotated[str, Field(min_length=1, max_length=64)]


class BacktestPreflightFormulaResponse(_BacktestDTO):
    formula_id: str
    formula_version_id: str
    formula_checksum: str
    engine_version: str
    compatibility_version: str
    normalized_parameters: tuple[dict[str, object], ...]


class BacktestPreflightGapResponse(_BacktestDTO):
    symbol: str
    reason: str


class BacktestPreflightScopeResponse(_BacktestDTO):
    kind: str
    symbol: str | None
    pool_id: str | None
    revision_or_snapshot_id: str | None
    total: int
    runnable: int
    gap_count: int
    gap_sample: Annotated[
        tuple[BacktestPreflightGapResponse, ...], Field(max_length=100)
    ]
    gaps_truncated: bool
    warnings: tuple[str, ...]


class BacktestPreflightWarmupResponse(_BacktestDTO):
    policy_version: str
    lookback_bars: int | None
    unbounded_dependency: bool


class BacktestPreflightCoverageResponse(_BacktestDTO):
    signal: int
    execution: int
    status: int


class BacktestPreflightWorkloadResponse(_BacktestDTO):
    symbols: int
    runnable_symbols: int
    formula_rows: int


class BacktestPreflightRulesResponse(_BacktestDTO):
    execution_rules_version: str
    cost_model_version: str
    sizing_version: str


class BacktestPreflightCostsResponse(_BacktestDTO):
    commission_bps: str
    minimum_commission: str
    sell_tax_bps: str
    slippage_bps: str


def _decimal_response(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


class BacktestPreflightResponse(_BacktestDTO):
    preview_snapshot_id: str
    reservation: Literal[False]
    formula: BacktestPreflightFormulaResponse
    scope: BacktestPreflightScopeResponse
    period: str
    adjustment: str
    scoring_start: datetime
    scoring_end: datetime
    warmup: BacktestPreflightWarmupResponse
    coverage: BacktestPreflightCoverageResponse
    rules: BacktestPreflightRulesResponse
    quantity_shares: int
    costs: BacktestPreflightCostsResponse
    estimated_workload: BacktestPreflightWorkloadResponse
    disclaimer: str

    @classmethod
    def from_snapshot(cls, item: BacktestPreflight) -> BacktestPreflightResponse:
        return cls(
            preview_snapshot_id=item.preview_snapshot_id,
            reservation=False,
            formula=BacktestPreflightFormulaResponse(
                formula_id=item.formula_id,
                formula_version_id=item.formula_version_id,
                formula_checksum=item.formula_checksum,
                engine_version=item.engine_version,
                compatibility_version=item.compatibility_version,
                normalized_parameters=tuple(
                    dict(value) for value in item.normalized_parameters
                ),
            ),
            scope=BacktestPreflightScopeResponse(
                kind=item.scope_kind,
                symbol=item.symbol,
                pool_id=item.scope_id,
                revision_or_snapshot_id=item.scope_revision_or_snapshot_id,
                total=item.total,
                runnable=item.runnable,
                gap_count=item.gap_count,
                gap_sample=tuple(
                    BacktestPreflightGapResponse(symbol=symbol, reason=reason)
                    for symbol, reason in item.gap_sample
                ),
                gaps_truncated=item.gap_count > len(item.gap_sample),
                warnings=item.warnings,
            ),
            period=(
                item.period.value if isinstance(item.period, Period) else item.period
            ),
            adjustment=(
                item.adjustment.value
                if isinstance(item.adjustment, Adjustment)
                else item.adjustment
            ),
            scoring_start=item.scoring_start,
            scoring_end=item.scoring_end,
            warmup=BacktestPreflightWarmupResponse(
                policy_version=item.warmup_policy_version,
                lookback_bars=item.lookback_bars,
                unbounded_dependency=item.unbounded_dependency,
            ),
            coverage=BacktestPreflightCoverageResponse(
                signal=item.pinned_signal_count,
                execution=item.pinned_execution_count,
                status=item.pinned_status_count,
            ),
            rules=BacktestPreflightRulesResponse(
                execution_rules_version=item.execution_rules_version,
                cost_model_version=item.cost_model_version,
                sizing_version=item.sizing_version,
            ),
            quantity_shares=item.quantity_shares,
            costs=BacktestPreflightCostsResponse(
                commission_bps=_decimal_response(item.commission_bps),
                minimum_commission=_decimal_response(item.minimum_commission),
                sell_tax_bps=_decimal_response(item.sell_tax_bps),
                slippage_bps=_decimal_response(item.slippage_bps),
            ),
            estimated_workload=BacktestPreflightWorkloadResponse(
                symbols=item.total,
                runnable_symbols=item.runnable,
                formula_rows=item.estimated_formula_rows,
            ),
            disclaimer=item.disclaimer,
        )


class BacktestServices:
    def __init__(
        self,
        *,
        service: BacktestService,
        repository: BacktestRepository,
        tasks: TaskRepository,
    ) -> None:
        identities = (
            service.database_identity,
            repository.database_identity,
            tasks.database_identity,
        )
        if identities[1:] != identities[:-1]:
            raise ValueError("backtest services database identities do not match")
        self.service = service
        self.repository = repository
        self.tasks = tasks
        self.database_identity = identities[0]

    @classmethod
    def from_shared(
        cls,
        *,
        market_services: MarketServices,
        formula_service: FormulaService,
        tasks: TaskRepository,
    ) -> BacktestServices:
        engine = market_services.engine
        repository = BacktestRepository(engine)
        service = BacktestService(
            engine=engine,
            tasks=tasks,
            repository=repository,
            market_lake=market_services.lake,
            status_lake=ExecutionStatusLake(engine),
            instruments=market_services.instruments,
            pools=market_services.pools,
            formulas=formula_service,
        )
        return cls(service=service, repository=repository, tasks=tasks)

    def submit(self, intent: BacktestIntent) -> SubmittedBacktest:
        return self.service.submit(intent)

    def preflight(self, intent: BacktestIntent) -> BacktestPreflight:
        return self.service.preflight(intent)

    def list_runs(
        self, *, limit: int, cursor: str | None
    ) -> BacktestPage[BacktestOverviewSnapshot]:
        return self.service.list_runs(limit=limit, cursor=cursor)

    def get_overview(self, run_id: str) -> BacktestOverviewSnapshot:
        return self.service.get_overview(run_id)

    def cancel(self, run_id: str) -> SubmittedBacktest:
        return self.service.cancel(run_id)

    def copy(
        self, run_id: str, *, mode: Literal["exact", "latest"]
    ) -> SubmittedBacktest:
        return self.service.copy(run_id, mode=mode)

    def report(self, run_id: str) -> BacktestReportSnapshot:
        return self.service.report(run_id)

    def page(
        self,
        run_id: str,
        *,
        collection: str,
        limit: int,
        cursor: str | None,
        dimension: str | None = None,
    ) -> BacktestPage[object]:
        return self.service.page(
            run_id,
            collection=collection,
            limit=limit,
            cursor=cursor,
            dimension=dimension,
        )

    def export(self, run_id: str, *, section: str, format: str) -> Iterator[bytes]:
        return stream_export(self.repository, run_id, section=section, format=format)

    def replay(
        self,
        run_id: str,
        symbol: str,
        trade_ordinal: int,
        *,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        return self.service.replay(
            run_id,
            symbol,
            trade_ordinal,
            limit=limit,
            cursor=cursor,
        )


class BacktestServiceDatabaseMismatch(RuntimeError):
    pass


def _error(code: str, status_code: int) -> JSONResponse:
    response = BacktestErrorResponse(code=code)
    return JSONResponse(status_code=status_code, content=response.model_dump())


def _exception(error: Exception) -> JSONResponse:
    if isinstance(
        error, (BacktestNotFound, FormulaNotFound, PoolNotFound, InstrumentNotFound)
    ):
        return _error("not_found", status.HTTP_404_NOT_FOUND)
    if isinstance(
        error,
        (
            BacktestConflict,
            TaskConflict,
            FormulaConflict,
            PoolConflict,
            PoolRevisionConflict,
            InstrumentConflict,
        ),
    ):
        code = "invalid_cursor" if "cursor" in str(error) else "state_conflict"
        return _error(
            code,
            status.HTTP_409_CONFLICT
            if code != "invalid_cursor"
            else status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if isinstance(
        error,
        (
            BacktestSubmissionError,
            FormulaValidationError,
            PoolValidationError,
            InstrumentValidationError,
            ValueError,
        ),
    ):
        return _error("invalid_request", status.HTTP_422_UNPROCESSABLE_CONTENT)
    if isinstance(error, BacktestRepositoryError):
        return _error("storage_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE)
    return _error("service_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE)


async def backtest_request_validation_handler(
    _request: Request, _error_value: Exception
) -> JSONResponse:
    return _error("invalid_request", status.HTTP_422_UNPROCESSABLE_CONTENT)


async def backtest_service_database_mismatch_handler(
    _request: Request, _error_value: Exception
) -> JSONResponse:
    return _error("storage_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE)


def get_backtest_services(request: Request) -> BacktestServices:
    provider = cast(
        Callable[[], BacktestServices], request.app.state.backtest_services_provider
    )
    services = provider()
    if not isinstance(services, BacktestServices):
        return services
    expected = (
        request.app.state.market_services_provider().database_identity,
        request.app.state.formula_service_provider().database_identity,
        request.app.state.task_repository_provider().database_identity,
    )
    if any(identity != services.database_identity for identity in expected):
        raise BacktestServiceDatabaseMismatch(
            "backtest service storage does not match application storage"
        )
    return services


BacktestServicesDependency = Annotated[BacktestServices, Depends(get_backtest_services)]
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": BacktestErrorResponse},
    status.HTTP_409_CONFLICT: {"model": BacktestErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": BacktestErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": BacktestErrorResponse},
}
router = APIRouter(prefix="/backtests", tags=["backtests"], responses=_ERROR_RESPONSES)


@router.post(
    "",
    response_model=BacktestSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": BacktestErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": BacktestErrorResponse},
    },
)
def create_backtest(
    body: BacktestCreateRequest,
    services: BacktestServicesDependency,
) -> BacktestSubmissionResponse | JSONResponse:
    try:
        return BacktestSubmissionResponse.from_submission(
            services.submit(body.to_intent())
        )
    except Exception as error:
        return _exception(error)


@router.post("/preflight", response_model=BacktestPreflightResponse)
def preflight_backtest(
    body: BacktestCreateRequest,
    services: BacktestServicesDependency,
) -> BacktestPreflightResponse | JSONResponse:
    try:
        return BacktestPreflightResponse.from_snapshot(
            services.preflight(body.to_intent())
        )
    except Exception as error:
        return _exception(error)


@router.get("", response_model=BacktestListResponse)
def list_backtests(
    services: BacktestServicesDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
) -> BacktestListResponse | JSONResponse:
    try:
        page = services.list_runs(limit=limit, cursor=cursor)
        return BacktestListResponse(
            items=tuple(
                BacktestOverviewResponse.from_snapshot(item) for item in page.items
            ),
            next_cursor=page.next_cursor,
        )
    except Exception as error:
        return _exception(error)


@router.get("/{run_id}", response_model=BacktestOverviewResponse)
def get_backtest(
    run_id: RunIdPath,
    services: BacktestServicesDependency,
) -> BacktestOverviewResponse | JSONResponse:
    try:
        return BacktestOverviewResponse.from_snapshot(services.get_overview(run_id))
    except Exception as error:
        return _exception(error)


@router.post(
    "/{run_id}/cancel",
    response_model=BacktestSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def cancel_backtest(
    run_id: RunIdPath,
    services: BacktestServicesDependency,
) -> BacktestSubmissionResponse | JSONResponse:
    try:
        return BacktestSubmissionResponse.from_submission(services.cancel(run_id))
    except Exception as error:
        return _exception(error)


@router.post(
    "/{run_id}/copy",
    response_model=BacktestSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def copy_backtest(
    run_id: RunIdPath,
    body: BacktestCopyRequest,
    services: BacktestServicesDependency,
) -> BacktestSubmissionResponse | JSONResponse:
    try:
        return BacktestSubmissionResponse.from_submission(
            services.copy(run_id, mode=body.mode)
        )
    except Exception as error:
        return _exception(error)


@router.get("/{run_id}/report", response_model=BacktestReportResponse)
def get_backtest_report(
    run_id: RunIdPath,
    services: BacktestServicesDependency,
) -> BacktestReportResponse | JSONResponse:
    try:
        return BacktestReportResponse.from_snapshot(services.report(run_id))
    except Exception as error:
        return _exception(error)


def _page_parameters(
    services: BacktestServices,
    run_id: str,
    collection: str,
    limit: int,
    cursor: str | None,
    dimension: str | None = None,
) -> BacktestPage[object] | JSONResponse:
    try:
        if dimension is None:
            return services.page(
                run_id, collection=collection, limit=limit, cursor=cursor
            )
        return services.page(
            run_id,
            collection=collection,
            limit=limit,
            cursor=cursor,
            dimension=dimension,
        )
    except Exception as error:
        return _exception(error)


@router.get("/{run_id}/groups", response_model=BacktestGroupPageResponse)
def list_backtest_groups(
    run_id: RunIdPath,
    services: BacktestServicesDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    dimension: Annotated[
        Literal["symbol", "entry_month", "entry_year"] | None, Query()
    ] = None,
) -> BacktestGroupPageResponse | JSONResponse:
    page = _page_parameters(
        services, run_id, "groups", limit, cursor, dimension=dimension
    )
    if isinstance(page, JSONResponse):
        return page
    return BacktestGroupPageResponse(
        items=tuple(
            BacktestGroupResponse(
                dimension=item.dimension, key=item.key, payload=dict(item.payload)
            )
            for item in cast(tuple[BacktestGroupSnapshot, ...], page.items)
        ),
        next_cursor=page.next_cursor,
    )


def _trade_page_response(
    page: BacktestPage[object],
) -> BacktestTradePageResponse:
    return BacktestTradePageResponse(
        items=tuple(
            BacktestTradeResponse(
                symbol=item.symbol, ordinal=item.ordinal, payload=dict(item.payload)
            )
            for item in cast(tuple[BacktestTradeSnapshot, ...], page.items)
        ),
        next_cursor=page.next_cursor,
    )


@router.get("/{run_id}/trades", response_model=BacktestTradePageResponse)
def list_backtest_trades(
    run_id: RunIdPath,
    services: BacktestServicesDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
) -> BacktestTradePageResponse | JSONResponse:
    page = _page_parameters(services, run_id, "trades", limit, cursor)
    return page if isinstance(page, JSONResponse) else _trade_page_response(page)


@router.get(
    "/{run_id}/trades/{symbol}/{trade_ordinal}/replay",
    response_model=BacktestReplayResponse,
)
def get_backtest_trade_replay(
    run_id: RunIdPath,
    symbol: Annotated[
        str, Path(pattern=r"^[0-9]{6}\.(?:SH|SZ|BJ)$", min_length=9, max_length=9)
    ],
    trade_ordinal: Annotated[int, Path(ge=0, le=2**63 - 1)],
    services: BacktestServicesDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
) -> BacktestReplayResponse | JSONResponse:
    try:
        raw = services.replay(
            run_id,
            symbol,
            trade_ordinal,
            limit=limit,
            cursor=cursor,
        )
        try:
            response = BacktestReplayResponse.model_validate_json(
                json.dumps(
                    raw,
                    allow_nan=False,
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
            )
        except (RecursionError, TypeError, ValueError, OverflowError) as error:
            raise BacktestRepositoryError(
                "backtest replay response is invalid"
            ) from error
        if (
            response.run_id != run_id
            or response.symbol != symbol
            or response.trade_ordinal != trade_ordinal
        ):
            raise BacktestRepositoryError("backtest replay identity is invalid")
        return response
    except Exception as error:
        return _exception(error)


@router.get("/{run_id}/open", response_model=BacktestTradePageResponse)
def list_backtest_open_trades(
    run_id: RunIdPath,
    services: BacktestServicesDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
) -> BacktestTradePageResponse | JSONResponse:
    page = _page_parameters(services, run_id, "open", limit, cursor)
    return page if isinstance(page, JSONResponse) else _trade_page_response(page)


@router.get("/{run_id}/failures", response_model=BacktestFailurePageResponse)
def list_backtest_failures(
    run_id: RunIdPath,
    services: BacktestServicesDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
) -> BacktestFailurePageResponse | JSONResponse:
    page = _page_parameters(services, run_id, "failures", limit, cursor)
    if isinstance(page, JSONResponse):
        return page
    return BacktestFailurePageResponse(
        items=tuple(
            BacktestFailureResponse(
                symbol=item.symbol,
                ordinal=item.ordinal,
                reason=item.reason,
                detail=dict(item.detail),
            )
            for item in cast(tuple[BacktestFailureSnapshot, ...], page.items)
        ),
        next_cursor=page.next_cursor,
    )


@router.get("/{run_id}/logs", response_model=BacktestLogPageResponse)
def list_backtest_logs(
    run_id: RunIdPath,
    services: BacktestServicesDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    after_cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
) -> BacktestLogPageResponse | JSONResponse:
    if cursor is not None and after_cursor is not None:
        return _error("invalid_request", status.HTTP_422_UNPROCESSABLE_CONTENT)
    page = _page_parameters(services, run_id, "logs", limit, cursor or after_cursor)
    if isinstance(page, JSONResponse):
        return page
    return BacktestLogPageResponse(
        items=tuple(
            BacktestLogResponse(
                ordinal=item.ordinal,
                level=item.level,
                message=item.message,
                detail=dict(item.detail),
            )
            for item in cast(tuple[BacktestLogSnapshot, ...], page.items)
        ),
        next_cursor=page.next_cursor,
        after_cursor=page.after_cursor,
    )


@router.get("/{run_id}/symbols", response_model=BacktestSymbolPageResponse)
def list_backtest_symbols(
    run_id: RunIdPath,
    services: BacktestServicesDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
) -> BacktestSymbolPageResponse | JSONResponse:
    page = _page_parameters(services, run_id, "symbols", limit, cursor)
    if isinstance(page, JSONResponse):
        return page
    return BacktestSymbolPageResponse(
        items=tuple(
            BacktestSymbolResponse(
                symbol=item.symbol,
                ordinal=item.ordinal,
                input_kind=(
                    "runnable" if isinstance(item.reference, PinnedMarketRef) else "gap"
                ),
                status=item.status,
                signal_series_id=item.signal_series_id,
                provenance=dict(item.reference.model_dump(mode="json")),
            )
            for item in cast(tuple[BacktestSymbolSnapshot, ...], page.items)
        ),
        next_cursor=page.next_cursor,
    )


def _primed_stream(first: bytes, remainder: Iterator[bytes]) -> Iterator[bytes]:
    try:
        yield first
        yield from remainder
    finally:
        close = getattr(remainder, "close", None)
        if callable(close):
            close()


@router.get("/{run_id}/export/{section}.{format}", response_model=None)
def export_backtest(
    run_id: Annotated[
        str,
        Path(
            pattern=(
                r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                r"[0-9a-f]{4}-[0-9a-f]{12}$"
            )
        ),
    ],
    section: Literal["groups", "trades", "open", "failures", "logs"],
    format: Literal["json", "csv"],
    services: BacktestServicesDependency,
) -> StreamingResponse | JSONResponse:
    try:
        stream = services.export(run_id, section=section, format=format)
        first = next(stream)
    except StopIteration:
        return _error("storage_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception as error:
        return _exception(error)
    media_type = "application/json" if format == "json" else "text/csv; charset=utf-8"
    filename = f"stock-desk-backtest-{run_id}-{section}.{format}"
    return StreamingResponse(
        _primed_stream(first, stream),
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


__all__ = ["BacktestServices", "router"]
