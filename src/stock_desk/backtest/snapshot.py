from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from typing import Protocol

from pydantic import BaseModel

from stock_desk.backtest.config import BacktestRequest
from stock_desk.backtest.types import (
    SNAPSHOT_SCHEMA_VERSION,
    BacktestSnapshot,
    FrozenSymbolGap,
    PinnedMarketRef,
)
from stock_desk.market.calendar import MARKET_TIMEZONE
from stock_desk.market.execution_status_lake import (
    execution_status_manifest_record_id,
)
from stock_desk.market.provenance import RoutedBarSuccess
from stock_desk.market.provenance import RoutedExecutionStatusSuccess
from stock_desk.market.lake import manifest_record_id


class MarketLakeReader(Protocol):
    def read(self, manifest_record_id: str) -> RoutedBarSuccess: ...


class StatusLakeReader(Protocol):
    def read(self, manifest_record_id: str) -> RoutedExecutionStatusSuccess: ...


@dataclass(frozen=True, slots=True)
class ReopenedSymbolInput:
    reference: PinnedMarketRef | FrozenSymbolGap
    signal: RoutedBarSuccess | None
    execution: RoutedBarSuccess | None
    execution_status: RoutedExecutionStatusSuccess | None


@dataclass(frozen=True, slots=True)
class ReopenedSnapshot:
    snapshot: BacktestSnapshot
    symbols: tuple[ReopenedSymbolInput, ...]

    def canonical_bytes(self) -> bytes:
        payload = {
            "snapshot": self.snapshot.model_dump(mode="json"),
            "symbols": tuple(
                {
                    "reference": item.reference.model_dump(mode="json"),
                    "signal": _json_value(item.signal),
                    "execution": _json_value(item.execution),
                    "execution_status": _json_value(item.execution_status),
                }
                for item in self.symbols
            ),
        }
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")


def freeze_request(request: BacktestRequest) -> BacktestSnapshot:
    if not isinstance(request, BacktestRequest):
        raise TypeError("freeze_request requires a BacktestRequest")
    canonical = BacktestRequest.model_validate(request.model_dump(mode="python"))
    fields = canonical.model_dump(mode="python")
    identity_payload = {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        **BacktestRequest.model_validate(fields).model_dump(mode="json"),
    }
    encoded = json.dumps(
        identity_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return BacktestSnapshot(
        snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        **fields,
    )


def reopen_snapshot(
    snapshot: BacktestSnapshot,
    *,
    market_lake: MarketLakeReader,
    status_lake: StatusLakeReader,
) -> ReopenedSnapshot:
    canonical = BacktestSnapshot.model_validate(snapshot.model_dump(mode="python"))
    symbols: list[ReopenedSymbolInput] = []
    for reference in canonical.symbol_inputs:
        if isinstance(reference, FrozenSymbolGap):
            symbols.append(
                ReopenedSymbolInput(
                    reference=reference,
                    signal=None,
                    execution=None,
                    execution_status=None,
                )
            )
            continue
        signal = market_lake.read(reference.signal_manifest_record_id)
        _validate_signal_identity(reference, signal)
        execution = market_lake.read(reference.execution_manifest_record_id)
        _validate_execution_identity(reference, execution)
        status = status_lake.read(reference.execution_status_manifest_record_id)
        _validate_status_identity(reference, status)
        symbols.append(
            ReopenedSymbolInput(
                reference=reference,
                signal=signal,
                execution=execution,
                execution_status=status,
            )
        )
    return ReopenedSnapshot(snapshot=canonical, symbols=tuple(symbols))


def reopen_symbol_input(
    reference: PinnedMarketRef,
    *,
    market_lake: MarketLakeReader,
    status_lake: StatusLakeReader,
) -> ReopenedSymbolInput:
    """Reopen and fully validate one pinned symbol without loading a whole pool."""

    canonical = PinnedMarketRef.model_validate(reference.model_dump(mode="python"))
    signal = market_lake.read(canonical.signal_manifest_record_id)
    _validate_signal_identity(canonical, signal)
    execution = market_lake.read(canonical.execution_manifest_record_id)
    _validate_execution_identity(canonical, execution)
    status = status_lake.read(canonical.execution_status_manifest_record_id)
    _validate_status_identity(canonical, status)
    return ReopenedSymbolInput(
        reference=canonical,
        signal=signal,
        execution=execution,
        execution_status=status,
    )


def _validate_signal_identity(
    reference: PinnedMarketRef,
    routed: RoutedBarSuccess,
) -> None:
    if manifest_record_id(routed.manifest) != reference.signal_manifest_record_id:
        raise ValueError("reopened signal manifest identity does not match snapshot")
    if routed.result.query != reference.signal_query:
        raise ValueError("reopened signal query does not match snapshot")
    if routed.result.provenance.dataset_version != reference.signal_dataset_version:
        raise ValueError("reopened signal dataset version does not match snapshot")
    if routed.manifest.route_version != reference.signal_route_version:
        raise ValueError("reopened signal route version does not match snapshot")
    if routed.result.provenance.source is not reference.signal_source:
        raise ValueError("reopened signal source does not match snapshot")
    if routed.result.provenance.data_cutoff != reference.signal_data_cutoff:
        raise ValueError("reopened signal cutoff does not match snapshot")


def _validate_execution_identity(
    reference: PinnedMarketRef,
    routed: RoutedBarSuccess,
) -> None:
    if manifest_record_id(routed.manifest) != reference.execution_manifest_record_id:
        raise ValueError("reopened execution manifest identity does not match snapshot")
    if routed.result.query != reference.execution_query:
        raise ValueError("reopened execution query does not match snapshot")
    if routed.result.provenance.dataset_version != reference.execution_dataset_version:
        raise ValueError("reopened execution dataset version does not match snapshot")
    if routed.manifest.route_version != reference.execution_route_version:
        raise ValueError("reopened execution route version does not match snapshot")
    if routed.result.provenance.source is not reference.execution_source:
        raise ValueError("reopened execution source does not match snapshot")
    if routed.result.provenance.data_cutoff != reference.execution_data_cutoff:
        raise ValueError("reopened execution cutoff does not match snapshot")


def _validate_status_identity(
    reference: PinnedMarketRef,
    routed: RoutedExecutionStatusSuccess,
) -> None:
    if (
        execution_status_manifest_record_id(routed.manifest)
        != reference.execution_status_manifest_record_id
    ):
        raise ValueError("reopened execution status manifest does not match snapshot")
    if routed.result.dataset_version != reference.execution_status_dataset_version:
        raise ValueError(
            "reopened execution status dataset version does not match snapshot"
        )
    if routed.manifest.route_version != reference.execution_status_route_version:
        raise ValueError("reopened execution status route does not match snapshot")
    if routed.manifest.selected_source is not reference.execution_status_source:
        raise ValueError("reopened execution status source does not match snapshot")
    if routed.manifest.upstream_data_cutoff != reference.execution_status_data_cutoff:
        raise ValueError("reopened execution status cutoff does not match snapshot")
    if routed.result.query != reference.execution_status_query:
        raise ValueError("reopened execution status query does not match snapshot")
    local_start = reference.execution_query.start.astimezone(MARKET_TIMEZONE)
    local_end = reference.execution_query.end.astimezone(MARKET_TIMEZONE)
    required_start = local_start.date()
    required_end = local_end.date()
    if (local_end.hour, local_end.minute, local_end.second, local_end.microsecond) != (
        0,
        0,
        0,
        0,
    ):
        required_end += timedelta(days=1)
    if (
        routed.result.query.start > required_start
        or routed.result.query.end < required_end
    ):
        raise ValueError("reopened execution status coverage does not match snapshot")


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not (value == value and abs(value) != float("inf")):
            raise ValueError("reopened payload contains a non-finite float")
        return value
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, datetime):
        normalized = value.astimezone(timezone.utc)
        return normalized.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_json_value(item) for item in value)
    raise TypeError(f"reopened payload contains unsupported {type(value).__name__}")
