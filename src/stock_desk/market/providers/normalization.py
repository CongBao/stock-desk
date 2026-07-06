from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import math
from typing import cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from stock_desk.market.providers.base import (
    BatchItem,
    Clock,
    DatasetProvenance,
    ProviderBatch,
    ProviderBatchFailure,
    ProviderBarTable,
    ProviderClientError,
    ProviderInvalidResponse,
    ProviderMissingCoverage,
    ProviderNoData,
    ProviderOperation,
)
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarFailure,
    BarQuery,
    BarResult,
    Exchange,
    FailureReason,
    Period,
    Provenance,
    ProviderId,
    TradingDay,
)


MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")
MAX_ROWS = 10_000
MAX_COLUMNS = 128
MAX_CELL_LENGTH = 4_096
MAX_VOLUME = 2**63 - 1
_MINUTE_END_TIMES = frozenset({time(10, 30), time(11, 30), time(14), time(15)})


def aware_now(clock: Clock) -> datetime:
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ProviderInvalidResponse()
    return value.astimezone(timezone.utc)


def _valid_error_code(value: object) -> bool:
    return value == "0" or value == 0


def _validate_columns(columns: object, required: frozenset[str]) -> tuple[str, ...]:
    if isinstance(columns, (str, bytes)):
        raise ProviderInvalidResponse()
    try:
        values: tuple[object, ...] = tuple(cast(Iterable[object], columns))
    except (TypeError, ValueError):
        raise ProviderInvalidResponse() from None
    if not values or len(values) > MAX_COLUMNS:
        raise ProviderInvalidResponse()
    if any(not isinstance(value, str) or not value for value in values):
        raise ProviderInvalidResponse()
    if len(values) != len(frozenset(values)) or not required.issubset(values):
        raise ProviderInvalidResponse()
    return cast(tuple[str, ...], values)


def _validate_cell(value: object) -> None:
    if isinstance(value, (str, bytes)) and len(value) > MAX_CELL_LENGTH:
        raise ProviderInvalidResponse()


def _validate_record(
    row: object,
    required: frozenset[str],
    declared_columns: frozenset[str] | None = None,
) -> dict[str, object]:
    if not isinstance(row, Mapping):
        raise ProviderInvalidResponse()
    if len(row) > MAX_COLUMNS or any(not isinstance(key, str) for key in row):
        raise ProviderInvalidResponse()
    keys = frozenset(row)
    if not required.issubset(keys):
        raise ProviderInvalidResponse()
    if declared_columns is not None and keys != declared_columns:
        raise ProviderInvalidResponse()
    copied = dict(row)
    for value in copied.values():
        _validate_cell(value)
    return copied


def _records_from_cursor(
    table: object,
    required: frozenset[str],
) -> tuple[dict[str, object], ...]:
    if not _valid_error_code(getattr(table, "error_code")):
        raise ProviderInvalidResponse()
    fields = _validate_columns(getattr(table, "fields"), required)
    next_row = getattr(table, "next")
    row_data = getattr(table, "get_row_data")
    if not callable(next_row) or not callable(row_data):
        raise ProviderInvalidResponse()
    records: list[dict[str, object]] = []
    while True:
        has_next = next_row()
        if type(has_next) is not bool:
            raise ProviderInvalidResponse()
        if not has_next:
            break
        if len(records) >= MAX_ROWS:
            raise ProviderInvalidResponse()
        values = row_data()
        if not isinstance(values, (list, tuple)) or len(values) != len(fields):
            raise ProviderInvalidResponse()
        row = dict(zip(fields, values, strict=True))
        records.append(_validate_record(row, required, frozenset(fields)))
    if not _valid_error_code(getattr(table, "error_code")):
        raise ProviderInvalidResponse()
    return tuple(records)


def records_from_table(
    table: object,
    *,
    required: frozenset[str],
) -> tuple[dict[str, object], ...]:
    if all(
        hasattr(table, attribute)
        for attribute in ("error_code", "fields", "next", "get_row_data")
    ):
        return _records_from_cursor(table, required)

    to_dict = getattr(table, "to_dict", None)
    if callable(to_dict):
        raw_columns = getattr(table, "columns", None)
        if isinstance(raw_columns, (str, bytes)):
            raise ProviderInvalidResponse()
        try:
            materialized_columns = tuple(cast(Iterable[object], raw_columns))
        except (TypeError, ValueError):
            raise ProviderInvalidResponse() from None
        if not materialized_columns:
            rows = to_dict(orient="records")
            if not isinstance(rows, (list, tuple)) or len(rows) > MAX_ROWS:
                raise ProviderInvalidResponse()
            if rows:
                raise ProviderInvalidResponse()
            return ()
        columns = _validate_columns(materialized_columns, required)
        rows = to_dict(orient="records")
        if not isinstance(rows, (list, tuple)) or len(rows) > MAX_ROWS:
            raise ProviderInvalidResponse()
        declared = frozenset(columns)
        return tuple(_validate_record(row, required, declared) for row in rows)

    if isinstance(table, Mapping):
        return (_validate_record(table, required),)
    if isinstance(table, (str, bytes)) or not isinstance(table, Sequence):
        raise ProviderInvalidResponse()
    if len(table) > MAX_ROWS:
        raise ProviderInvalidResponse()
    return tuple(_validate_record(row, required) for row in table)


def validated_bar_table(value: object, query: BarQuery) -> object:
    if not isinstance(value, ProviderBarTable):
        raise ProviderInvalidResponse()
    if (
        value.coverage_start != query.start
        or value.coverage_end != query.end
        or not value.complete
        or value.limit_reached
    ):
        raise ProviderMissingCoverage()
    return value.table


def decimal_price(raw: object, adjustment: Adjustment) -> Decimal:
    if type(raw) not in (str, int, float, Decimal):
        raise ProviderInvalidResponse()
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ProviderInvalidResponse() from None
    if not value.is_finite():
        raise ProviderInvalidResponse()
    if adjustment is Adjustment.NONE and value <= 0:
        raise ProviderInvalidResponse()
    return value


def required_text(raw: object) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > MAX_CELL_LENGTH:
        raise ProviderInvalidResponse()
    return raw


def require_unique(values: Sequence[object]) -> None:
    try:
        if len(values) != len(frozenset(values)):
            raise ProviderInvalidResponse()
    except TypeError:
        raise ProviderInvalidResponse() from None


def complete_explicit_calendar(
    items: Sequence[TradingDay],
    *,
    exchange: Exchange,
    start: date,
    end: date,
) -> tuple[TradingDay, ...]:
    expected_days = tuple(
        start + timedelta(days=offset) for offset in range((end - start).days)
    )
    identities = tuple((item.exchange, item.day) for item in items)
    require_unique(identities)
    if (
        len(items) != len(expected_days)
        or {item.day for item in items} != set(expected_days)
        or any(item.exchange is not exchange for item in items)
    ):
        raise ProviderInvalidResponse()
    return tuple(sorted(items, key=lambda item: item.day))


def binary_flag(raw: object) -> bool:
    if type(raw) is int and raw in {0, 1}:
        return raw == 1
    if isinstance(raw, str) and raw in {"0", "1"}:
        return raw == "1"
    raise ProviderInvalidResponse()


def share_volume(raw: object, *, lot_size: int) -> int:
    if type(raw) not in (str, int, float, Decimal):
        raise ProviderInvalidResponse()
    try:
        value = Decimal(str(raw)) * lot_size
    except (InvalidOperation, ValueError):
        raise ProviderInvalidResponse() from None
    if not value.is_finite() or value < 0 or value != value.to_integral_value():
        raise ProviderInvalidResponse()
    normalized = int(value)
    if normalized > MAX_VOLUME:
        raise ProviderInvalidResponse()
    return normalized


def parse_date(raw: object, *, compact: bool = False) -> date:
    if isinstance(raw, datetime):
        if raw.tzinfo is not None and raw.utcoffset() is not None:
            return raw.astimezone(MARKET_TIMEZONE).date()
        return raw.date()
    if isinstance(raw, date):
        return raw
    to_pydatetime = getattr(raw, "to_pydatetime", None)
    if callable(to_pydatetime):
        converted = to_pydatetime()
        if not isinstance(converted, datetime):
            raise ProviderInvalidResponse()
        return parse_date(converted)
    if not isinstance(raw, str) or len(raw) > MAX_CELL_LENGTH:
        raise ProviderInvalidResponse()
    try:
        return datetime.strptime(raw, "%Y%m%d" if compact else "%Y-%m-%d").date()
    except ValueError:
        raise ProviderInvalidResponse() from None


def parse_datetime(raw: object, *, compact_baostock: bool = False) -> datetime:
    if not isinstance(raw, datetime):
        to_pydatetime = getattr(raw, "to_pydatetime", None)
        if callable(to_pydatetime):
            raw = to_pydatetime()
    if isinstance(raw, datetime):
        if raw.tzinfo is not None and raw.utcoffset() is not None:
            return raw.astimezone(MARKET_TIMEZONE)
        return raw.replace(tzinfo=MARKET_TIMEZONE)
    if not isinstance(raw, str) or len(raw) > MAX_CELL_LENGTH:
        raise ProviderInvalidResponse()
    text = raw[:14] if compact_baostock and len(raw) == 17 else raw
    pattern = "%Y%m%d%H%M%S" if compact_baostock else "%Y-%m-%d %H:%M:%S"
    try:
        value = datetime.strptime(text, pattern)
    except ValueError:
        raise ProviderInvalidResponse() from None
    return value.replace(tzinfo=MARKET_TIMEZONE)


def parse_optional_date(raw: object, *, compact: bool = False) -> date | None:
    if raw is None or (isinstance(raw, str) and raw == ""):
        return None
    if type(raw) is float and math.isnan(raw):
        return None
    if isinstance(raw, Decimal) and raw.is_nan():
        return None
    return parse_date(raw, compact=compact)


def period_bounds(
    raw: object,
    period: Period,
    *,
    compact_date: bool = False,
    compact_minute: bool = False,
) -> tuple[datetime, datetime]:
    if period is Period.MIN60:
        endpoint = parse_datetime(raw, compact_baostock=compact_minute)
        if endpoint.time() not in _MINUTE_END_TIMES:
            raise ProviderInvalidResponse()
        start = endpoint - timedelta(hours=1)
        return start.astimezone(timezone.utc), endpoint.astimezone(timezone.utc)

    day = parse_date(raw, compact=compact_date)
    local = datetime.combine(day, time(), tzinfo=MARKET_TIMEZONE)
    if period is Period.WEEK:
        local -= timedelta(days=local.weekday())
    cutoff = datetime.combine(day, time(15), tzinfo=MARKET_TIMEZONE)
    return local.astimezone(timezone.utc), cutoff.astimezone(timezone.utc)


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(nested) for nested in value]
    if value is None or type(value) in (str, int, bool):
        return value
    raise ProviderInvalidResponse()


def dataset_version(
    *,
    source: ProviderId,
    operation: str,
    request: Mapping[str, object],
    data_cutoff: datetime,
    items: Sequence[BaseModel],
) -> str:
    payload = {
        "schema": "stock-desk-provider-dataset-v1",
        "source": source.value,
        "operation": operation,
        "request": _jsonable(request),
        "data_cutoff": _jsonable(data_cutoff),
        "items": [_jsonable(item) for item in items],
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def make_bar_result(
    *,
    source: ProviderId,
    query: BarQuery,
    normalized: Sequence[tuple[Bar, datetime]],
    clock: Clock,
) -> BarResult:
    if not normalized:
        raise ProviderNoData()
    selected = sorted(
        (
            (bar, endpoint)
            for bar, endpoint in normalized
            if query.start <= bar.timestamp < query.end
        ),
        key=lambda pair: pair[0].timestamp,
    )
    if not selected:
        raise ProviderNoData()
    timestamps = tuple(bar.timestamp for bar, _endpoint in selected)
    if len(timestamps) != len(frozenset(timestamps)):
        raise ProviderInvalidResponse()
    bars = tuple(bar for bar, _endpoint in selected)
    cutoff = max(endpoint for _bar, endpoint in selected)
    fetched_at = aware_now(clock)
    version = dataset_version(
        source=source,
        operation="bars",
        request={"query": query},
        data_cutoff=cutoff,
        items=bars,
    )
    return BarResult(
        query=query,
        bars=bars,
        coverage_start=query.start,
        coverage_end=query.end,
        provenance=Provenance(
            source=source,
            fetched_at=fetched_at,
            data_cutoff=cutoff,
            adjustment=query.adjustment,
            dataset_version=version,
        ),
    )


def make_batch(
    *,
    source: ProviderId,
    operation: ProviderOperation,
    request: Mapping[str, object],
    items: tuple[BatchItem, ...],
    data_cutoff: datetime,
    observed_at: datetime,
) -> ProviderBatch[BatchItem]:
    if not items:
        raise ProviderNoData()
    version = dataset_version(
        source=source,
        operation=operation.value,
        request=request,
        data_cutoff=data_cutoff,
        items=items,
    )
    return ProviderBatch[BatchItem](
        items=items,
        provenance=DatasetProvenance(
            source=source,
            fetched_at=observed_at,
            data_cutoff=data_cutoff,
            dataset_version=version,
        ),
    )


def failure_metadata(error: Exception) -> tuple[FailureReason, str]:
    if isinstance(error, ProviderClientError):
        return error.reason, error.safe_detail
    if isinstance(error, TimeoutError):
        return FailureReason.TIMEOUT, "provider request timed out"
    return FailureReason.INVALID_RESPONSE, ProviderInvalidResponse.safe_detail


def bar_failure(
    *,
    source: ProviderId,
    query: BarQuery,
    error: Exception,
    detail: str | None = None,
) -> BarFailure:
    reason, safe_detail = failure_metadata(error)
    return BarFailure(
        query=query,
        source=source,
        reason=reason,
        failed_start=query.start,
        failed_end=query.end,
        detail=detail or safe_detail,
    )


def batch_failure(
    *,
    source: ProviderId,
    operation: ProviderOperation,
    error: Exception,
    exchange: Exchange | None = None,
    start: date | None = None,
    end: date | None = None,
) -> ProviderBatchFailure:
    reason, detail = failure_metadata(error)
    return ProviderBatchFailure(
        source=source,
        operation=operation,
        exchange=exchange,
        start=start,
        end=end,
        reason=reason,
        detail=detail,
    )


def dated_cutoff(value: date) -> datetime:
    return datetime.combine(value + timedelta(days=1), time(), tzinfo=MARKET_TIMEZONE)
