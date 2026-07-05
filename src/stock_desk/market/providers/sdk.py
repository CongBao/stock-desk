from __future__ import annotations

from collections.abc import Callable, Hashable
from datetime import date, datetime, timedelta
import importlib
from typing import ParamSpec, TypeVar, cast

from stock_desk.market.providers.base import (
    ProviderBarTable,
    ProviderInvalidResponse,
    ProviderMissingCoverage,
    ProviderTimeout,
    ProviderUnavailable,
)
from stock_desk.market.providers.normalization import (
    MARKET_TIMEZONE,
    records_from_table,
)
from stock_desk.market.types import Period


SDK_CHUNK_DAYS = 366
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _optional_timeout_types() -> tuple[type[BaseException], ...]:
    types: list[type[BaseException]] = []
    for module_name, attribute in (
        ("requests.exceptions", "Timeout"),
        ("urllib3.exceptions", "TimeoutError"),
        ("httpx", "TimeoutException"),
        ("httpcore", "TimeoutException"),
    ):
        try:
            module = importlib.import_module(module_name)
            value = getattr(module, attribute)
        except Exception:
            continue
        if isinstance(value, type) and issubclass(value, BaseException):
            types.append(value)
    return tuple(types)


def is_sdk_timeout(error: BaseException) -> bool:
    timeout_types: tuple[type[BaseException], ...] = (
        TimeoutError,
        ProviderTimeout,
        *_optional_timeout_types(),
    )
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, timeout_types):
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def call_sdk(
    operation: Callable[_P, _R],
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> _R:
    try:
        return operation(*args, **kwargs)
    except Exception as error:
        if is_sdk_timeout(error):
            raise ProviderTimeout() from None
        raise


def import_optional_sdk(name: str) -> object:
    try:
        return importlib.import_module(name)
    except Exception:
        raise ProviderUnavailable() from None


def required_sdk_callable(module: object, name: str) -> Callable[..., object]:
    try:
        value = getattr(module, name)
    except Exception:
        raise ProviderUnavailable() from None
    if not callable(value):
        raise ProviderUnavailable()
    return cast(Callable[..., object], value)


def inclusive_date_chunks(
    coverage_start: datetime,
    coverage_end: datetime,
) -> tuple[tuple[date, date], ...]:
    start = coverage_start.astimezone(MARKET_TIMEZONE).date()
    final = (
        coverage_end.astimezone(MARKET_TIMEZONE) - timedelta(microseconds=1)
    ).date()
    chunks: list[tuple[date, date]] = []
    current = start
    while current <= final:
        chunk_end = min(current + timedelta(days=SDK_CHUNK_DAYS - 1), final)
        chunks.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return tuple(chunks)


def _maximum_period_rows(
    chunk_start: date,
    chunk_end: date,
    period: Period,
) -> int:
    natural_days = (chunk_end - chunk_start).days + 1
    if period is Period.DAY:
        return natural_days
    if period is Period.MIN60:
        return natural_days * 4
    first_week = chunk_start - timedelta(days=chunk_start.weekday())
    final_week = chunk_end - timedelta(days=chunk_end.weekday())
    return (final_week - first_week).days // 7 + 1


def materialize_sdk_rows(
    table: object,
    *,
    chunk_start: date,
    chunk_end: date,
    period: Period,
    provider_row_limit: int | None,
) -> tuple[dict[str, object], ...]:
    rows = records_from_table(table, required=frozenset())
    if (provider_row_limit is not None and len(rows) >= provider_row_limit) or len(
        rows
    ) > _maximum_period_rows(chunk_start, chunk_end, period):
        raise ProviderMissingCoverage()
    return rows


def validate_sdk_chunk_rows(
    rows: tuple[dict[str, object], ...],
    *,
    chunk_start: date,
    chunk_end: date,
    temporal_identity: Callable[[dict[str, object]], tuple[date, Hashable]],
    seen_identities: set[Hashable],
) -> None:
    chunk_identities: set[Hashable] = set()
    for row in rows:
        row_day, identity = temporal_identity(row)
        if (
            not chunk_start <= row_day <= chunk_end
            or identity in chunk_identities
            or identity in seen_identities
        ):
            raise ProviderInvalidResponse()
        chunk_identities.add(identity)
    seen_identities.update(chunk_identities)


def combine_complete_chunks(
    chunks: tuple[tuple[dict[str, object], ...], ...],
    *,
    coverage_start: datetime,
    coverage_end: datetime,
) -> ProviderBarTable:
    nonempty = tuple(bool(chunk) for chunk in chunks)
    if any(nonempty) and not all(nonempty):
        raise ProviderMissingCoverage()
    rows = tuple(row for chunk in chunks for row in chunk)
    return complete_bar_table(
        rows,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )


def complete_bar_table(
    rows: tuple[dict[str, object], ...],
    *,
    coverage_start: datetime,
    coverage_end: datetime,
) -> ProviderBarTable:
    return ProviderBarTable(
        table=rows,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        complete=True,
        limit_reached=False,
    )
