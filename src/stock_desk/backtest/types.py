from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

from stock_desk.formula.context import MAX_PARAMETERS
from stock_desk.formula.signal_series import NormalizedParameter
from stock_desk.market.provenance import Sha256Digest
from stock_desk.market.execution_status import (
    ExecutionStatusEvidenceLevel,
    ExecutionStatusQuery,
)
from stock_desk.market.types import (
    Adjustment,
    BarQuery,
    CanonicalSymbol,
    Period,
    ProviderId,
    UtcDatetime,
)


SNAPSHOT_SCHEMA_VERSION: Literal["backtest-snapshot-v1"] = "backtest-snapshot-v1"
WARMUP_POLICY_VERSION: Literal["formula-warmup-v1"] = "formula-warmup-v1"
COST_MODEL_VERSION: Literal["a-share-cost-v1"] = "a-share-cost-v1"
EXECUTION_RULES_VERSION: Literal["a-share-v1"] = "a-share-v1"
BASIC_EXECUTION_RULES_VERSION: Literal["a-share-v2"] = "a-share-v2"
MAX_BACKTEST_SYMBOLS = 10_000
MAX_QUANTITY_SHARES = 100_000_000

BoundedIdentity = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=256, pattern=r"^\S+$"),
]
GapReason = Literal[
    "missing_signal_data",
    "missing_execution_data",
    "missing_execution_status",
    "corrupt_data",
]
ExecutionStatusEvidenceSummary = Literal[
    "authoritative", "basic_no_price_limits", "mixed"
]


def _canonical_decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("backtest cost must be finite")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _normalize_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("backtest cost must use an exact decimal value")
    if not isinstance(value, (Decimal, int, str)):
        raise ValueError("backtest cost must use an exact decimal value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("backtest cost must be a valid decimal") from error
    text = _canonical_decimal_text(result)
    if len(text) > 64:
        raise ValueError("backtest cost exceeds canonical precision bounds")
    return Decimal(text)


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


class _FrozenBacktestContract(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if update is not None:
            raise TypeError("frozen contract model_copy does not accept update")
        return super().model_copy(deep=deep)


class PinnedMarketRef(_FrozenBacktestContract):
    symbol: CanonicalSymbol
    signal_manifest_record_id: Sha256Digest
    signal_dataset_version: Sha256Digest
    signal_route_version: Sha256Digest
    signal_source: ProviderId
    signal_data_cutoff: UtcDatetime
    signal_query: BarQuery
    execution_manifest_record_id: Sha256Digest
    execution_dataset_version: Sha256Digest
    execution_route_version: Sha256Digest
    execution_source: ProviderId
    execution_data_cutoff: UtcDatetime
    execution_query: BarQuery
    execution_status_manifest_record_id: Sha256Digest
    execution_status_dataset_version: Sha256Digest
    execution_status_route_version: Sha256Digest
    execution_status_source: ProviderId
    execution_status_data_cutoff: UtcDatetime
    execution_status_query: ExecutionStatusQuery
    execution_status_evidence_level: ExecutionStatusEvidenceLevel = Field(
        default=ExecutionStatusEvidenceLevel.AUTHORITATIVE,
        exclude_if=lambda value: value is ExecutionStatusEvidenceLevel.AUTHORITATIVE,
    )

    @model_validator(mode="after")
    def validate_queries(self) -> Self:
        if (
            self.signal_query.symbol != self.symbol
            or self.execution_query.symbol != self.symbol
            or self.execution_status_query.symbol != self.symbol
        ):
            raise ValueError("pinned market reference queries must match its symbol")
        return self


class FrozenSymbolGap(_FrozenBacktestContract):
    symbol: CanonicalSymbol
    reason: GapReason
    signal_query: BarQuery
    execution_query: BarQuery
    checked_instrument_dataset_version: Sha256Digest
    checked_signal_catalog_version: Sha256Digest
    checked_execution_catalog_version: Sha256Digest
    checked_status_catalog_version: Sha256Digest

    @model_validator(mode="after")
    def validate_queries(self) -> Self:
        if (
            self.signal_query.symbol != self.symbol
            or self.execution_query.symbol != self.symbol
        ):
            raise ValueError("frozen symbol gap queries must match its symbol")
        return self


def execution_status_evidence_summary(
    inputs: tuple[PinnedMarketRef | FrozenSymbolGap, ...],
) -> tuple[ExecutionStatusEvidenceSummary, tuple[str, ...]]:
    runnable = tuple(item for item in inputs if isinstance(item, PinnedMarketRef))
    if not runnable:
        raise ValueError("runnable execution-status evidence is required")
    levels = {item.execution_status_evidence_level for item in runnable}
    level: ExecutionStatusEvidenceSummary
    if len(levels) > 1:
        level = "mixed"
    else:
        level = next(iter(levels)).value
    warnings: list[str] = []
    if any(isinstance(item, FrozenSymbolGap) for item in inputs):
        warnings.append("partial_pool_gaps")
    if ExecutionStatusEvidenceLevel.BASIC_NO_PRICE_LIMITS in levels:
        warnings.append("basic_execution_status")
    return level, tuple(warnings)


class _BacktestInputs(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    scope_kind: Literal["single", "preset", "custom"]
    scope_id: BoundedIdentity | None = None
    scope_revision_or_snapshot_id: BoundedIdentity | None = None
    instrument_dataset_version: Sha256Digest
    symbols: Annotated[
        tuple[CanonicalSymbol, ...], Field(max_length=MAX_BACKTEST_SYMBOLS)
    ]
    formula_version_id: BoundedIdentity
    formula_checksum: Sha256Digest
    formula_engine_version: BoundedIdentity
    compatibility_version: BoundedIdentity
    formula_parameters: Annotated[
        tuple[NormalizedParameter, ...], Field(max_length=MAX_PARAMETERS)
    ] = ()
    warmup_policy_version: Literal["formula-warmup-v1"] = WARMUP_POLICY_VERSION
    symbol_inputs: Annotated[
        tuple[PinnedMarketRef | FrozenSymbolGap, ...],
        Field(max_length=MAX_BACKTEST_SYMBOLS),
    ]
    period: Period
    adjustment: Adjustment
    scoring_start: UtcDatetime
    scoring_end: UtcDatetime
    quantity_shares: int = 1_000
    commission_bps: Decimal
    minimum_commission: Decimal
    sell_tax_bps: Decimal
    slippage_bps: Decimal
    cost_model_version: Literal["a-share-cost-v1"] = COST_MODEL_VERSION
    backtest_engine_version: BoundedIdentity
    execution_rules_version: Literal["a-share-v1", "a-share-v2"] = (
        EXECUTION_RULES_VERSION
    )

    @field_validator(
        "commission_bps",
        "minimum_commission",
        "sell_tax_bps",
        "slippage_bps",
        mode="before",
    )
    @classmethod
    def normalize_cost(cls, value: object) -> Decimal:
        return _normalize_decimal(value)

    @field_serializer(
        "commission_bps",
        "minimum_commission",
        "sell_tax_bps",
        "slippage_bps",
        when_used="json",
    )
    def serialize_cost(self, value: Decimal) -> str:
        return _canonical_decimal_text(value)

    @model_validator(mode="after")
    def validate_execution_inputs(self) -> Self:
        if not self.symbols:
            raise ValueError("symbols must contain at least one symbol")
        if len(self.symbols) != len(set(self.symbols)):
            raise ValueError("symbols must be unique")
        if self.scope_kind == "single":
            if len(self.symbols) != 1:
                raise ValueError("single scope must contain exactly one symbol")
            if (
                self.scope_id is not None
                or self.scope_revision_or_snapshot_id is not None
            ):
                raise ValueError("single scope cannot include pool identity")
        elif self.scope_id is None or self.scope_revision_or_snapshot_id is None:
            raise ValueError("pool scope requires scope identity and revision")

        if tuple(item.symbol for item in self.symbol_inputs) != self.symbols:
            raise ValueError(
                "ordered symbol_inputs must contain exactly one entry per symbol"
            )
        parameter_names = tuple(item.name for item in self.formula_parameters)
        if parameter_names != tuple(sorted(parameter_names)) or len(
            parameter_names
        ) != len(set(parameter_names)):
            raise ValueError("formula parameters must be uniquely sorted by name")

        if self.scoring_start >= self.scoring_end:
            raise ValueError("scoring start must be before scoring end")
        if self.quantity_shares <= 0:
            raise ValueError("quantity_shares must be positive")
        if self.quantity_shares % 100 != 0:
            raise ValueError("quantity_shares must use a 100-share lot")
        if self.quantity_shares > MAX_QUANTITY_SHARES:
            raise ValueError("quantity_shares exceeds the supported maximum")

        costs = (
            self.commission_bps,
            self.minimum_commission,
            self.sell_tax_bps,
            self.slippage_bps,
        )
        if any(not value.is_finite() for value in costs):
            raise ValueError("backtest cost must be finite")
        if any(value < 0 for value in costs):
            raise ValueError("backtest cost cannot be negative")
        if any(
            value > 10_000
            for value in (
                self.commission_bps,
                self.sell_tax_bps,
                self.slippage_bps,
            )
        ):
            raise ValueError("basis points cannot exceed 10000")

        runnable = tuple(
            item for item in self.symbol_inputs if isinstance(item, PinnedMarketRef)
        )
        if runnable:
            evidence_level = execution_status_evidence_summary(self.symbol_inputs)[0]
            expected_rules = (
                EXECUTION_RULES_VERSION
                if evidence_level == "authoritative"
                else BASIC_EXECUTION_RULES_VERSION
            )
            if self.execution_rules_version != expected_rules:
                raise ValueError(
                    "execution rules version must match status evidence level"
                )

        for item in self.symbol_inputs:
            if (
                isinstance(item, FrozenSymbolGap)
                and item.checked_instrument_dataset_version
                != self.instrument_dataset_version
            ):
                raise ValueError(
                    "gap instrument dataset version must match snapshot catalog"
                )
            if item.signal_query.period is not self.period:
                raise ValueError("signal query period must match backtest period")
            if (
                item.signal_query.adjustment is not self.adjustment
                or item.execution_query.adjustment is not self.adjustment
            ):
                raise ValueError(
                    "market query adjustment must match backtest adjustment"
                )
            expected_execution_period = (
                Period.DAY if self.period is Period.WEEK else self.period
            )
            if item.execution_query.period is not expected_execution_period:
                raise ValueError(
                    "execution query period must match the execution fill series"
                )
            if not (
                item.signal_query.start <= self.scoring_start
                and item.signal_query.end >= self.scoring_end
                and item.execution_query.start <= self.scoring_start
                and item.execution_query.end >= self.scoring_end
            ):
                raise ValueError("market queries must cover the scoring range")
        return self


class BacktestSnapshot(_BacktestInputs):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    snapshot_schema_version: Literal["backtest-snapshot-v1"] = SNAPSHOT_SCHEMA_VERSION
    snapshot_id: Sha256Digest

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if update is not None:
            raise TypeError("frozen contract model_copy does not accept update")
        return super().model_copy(deep=deep)

    @model_validator(mode="after")
    def validate_snapshot_identity(self) -> Self:
        expected = (
            "sha256:"
            + hashlib.sha256(
                _canonical_bytes(
                    self.model_dump(
                        mode="json",
                        exclude={"snapshot_id"},
                    )
                )
            ).hexdigest()
        )
        if self.snapshot_id != expected:
            raise ValueError("snapshot_id does not match canonical payload")
        return self

    def canonical_identity_bytes(self) -> bytes:
        validated = BacktestSnapshot.model_validate(self.model_dump(mode="python"))
        return _canonical_bytes(
            validated.model_dump(mode="json", exclude={"snapshot_id"})
        )

    def canonical_bytes(self) -> bytes:
        validated = BacktestSnapshot.model_validate(self.model_dump(mode="python"))
        return _canonical_bytes(validated.model_dump(mode="json"))

    @classmethod
    def from_canonical_bytes(cls, payload: bytes) -> Self:
        if type(payload) is not bytes:
            raise TypeError("canonical backtest snapshot payload must be bytes")
        value = cls.model_validate_json(payload, strict=False)
        if value.canonical_bytes() != payload:
            raise ValueError("backtest snapshot payload is not canonical JSON")
        return value
