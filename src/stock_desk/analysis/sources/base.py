from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
import json
import math
from typing import cast, Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from stock_desk.analysis.snapshot import (
    ResearchSection,
    ResearchSectionKind,
)
from stock_desk.market.providers.base import (
    ProviderClientError,
    ProviderCorrupt,
    ProviderInvalidResponse,
    ProviderMissingCoverage,
    ProviderNoData,
    ProviderPermissionDenied,
    ProviderTimeout,
    ProviderTransientFailure,
    ProviderUnavailable,
    ProviderUnsupported,
)
from stock_desk.market.providers.normalization import records_from_table
from stock_desk.market.providers.normalization import MARKET_TIMEZONE
from stock_desk.market.types import CanonicalSymbol, FailureReason, ProviderId


MAX_RESEARCH_ITEMS: Final = 512
MAX_RESEARCH_ITEM_BYTES: Final = 32_768
MAX_RESEARCH_TOTAL_BYTES: Final = 224_000
MAX_RESEARCH_DEPTH: Final = 16
MAX_RESEARCH_NODES: Final = 16_000

RESEARCH_SOURCE_CATEGORIES: Final = frozenset(
    {
        ResearchSectionKind.FUNDAMENTALS,
        ResearchSectionKind.ANNOUNCEMENTS,
        ResearchSectionKind.NEWS,
    }
)


class ResearchSourceCapability(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    source: ProviderId
    categories: frozenset[ResearchSectionKind]

    @field_validator("categories")
    @classmethod
    def validate_categories(
        cls, value: frozenset[ResearchSectionKind]
    ) -> frozenset[ResearchSectionKind]:
        if not value.issubset(RESEARCH_SOURCE_CATEGORIES):
            raise ValueError("research capability contains an invalid category")
        return value

    @field_serializer("categories", when_used="json")
    def serialize_categories(
        self, value: frozenset[ResearchSectionKind]
    ) -> tuple[str, ...]:
        return tuple(sorted(item.value for item in value))

    def supports(self, kind: ResearchSectionKind) -> bool:
        return kind in self.categories


@runtime_checkable
class ResearchSourceAdapter(Protocol):
    name: ProviderId

    def fetch(
        self,
        symbol: CanonicalSymbol,
        kind: ResearchSectionKind,
    ) -> ResearchSection: ...


_SAFE_PROVIDER_ERRORS: Final[dict[FailureReason, type[ProviderClientError]]] = {
    FailureReason.PERMISSION_DENIED: ProviderPermissionDenied,
    FailureReason.UNSUPPORTED: ProviderUnsupported,
    FailureReason.TRANSIENT_FAILURE: ProviderTransientFailure,
    FailureReason.TIMEOUT: ProviderTimeout,
    FailureReason.PROVIDER_UNAVAILABLE: ProviderUnavailable,
    FailureReason.INVALID_RESPONSE: ProviderInvalidResponse,
    FailureReason.CORRUPT: ProviderCorrupt,
    FailureReason.NO_DATA: ProviderNoData,
    FailureReason.MISSING: ProviderMissingCoverage,
}


def clean_provider_error(error: ProviderClientError) -> ProviderClientError:
    """Create an equivalent public error without retaining an exception chain."""
    error_type = _SAFE_PROVIDER_ERRORS.get(error.reason, ProviderInvalidResponse)
    return error_type()


def _normalize_value(
    value: object,
    *,
    depth: int,
    nodes: list[int],
) -> object:
    if depth > MAX_RESEARCH_DEPTH:
        raise ProviderInvalidResponse()
    nodes[0] += 1
    if nodes[0] > MAX_RESEARCH_NODES:
        raise ProviderInvalidResponse()
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float:
        if math.isnan(value):
            return None
        if not math.isfinite(value):
            raise ProviderInvalidResponse()
        return value
    if type(value) is Decimal:
        if not value.is_finite():
            raise ProviderInvalidResponse()
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ProviderInvalidResponse()
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, child in value.items():
            if type(key) is not str:
                raise ProviderInvalidResponse()
            normalized[key] = _normalize_value(
                child,
                depth=depth + 1,
                nodes=nodes,
            )
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _normalize_value(child, depth=depth + 1, nodes=nodes)
            for child in value
        ]
    if type(value).__module__.startswith("numpy"):
        item = getattr(value, "item", None)
        if callable(item):
            try:
                scalar = item()
            except Exception:
                raise ProviderInvalidResponse() from None
            if scalar is value:
                raise ProviderInvalidResponse()
            return _normalize_value(scalar, depth=depth, nodes=nodes)
    raise ProviderInvalidResponse()


def _normalize_research_table(table: object) -> tuple[dict[str, object], ...]:
    try:
        raw_rows = records_from_table(table, required=frozenset())
    except Exception as error:
        if isinstance(error, (ProviderInvalidResponse, ProviderNoData)):
            raise
        raise ProviderInvalidResponse() from None
    if not raw_rows:
        raise ProviderNoData()
    if len(raw_rows) > MAX_RESEARCH_ITEMS:
        raise ProviderInvalidResponse()
    nodes = [0]
    normalized_rows: list[dict[str, object]] = []
    total_bytes = 0
    for row in raw_rows:
        normalized = _normalize_value(row, depth=1, nodes=nodes)
        if not isinstance(normalized, dict) or not normalized:
            raise ProviderInvalidResponse()
        try:
            encoded = json.dumps(
                normalized,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise ProviderInvalidResponse() from None
        if len(encoded) > MAX_RESEARCH_ITEM_BYTES:
            raise ProviderInvalidResponse()
        total_bytes += len(encoded)
        if total_bytes > MAX_RESEARCH_TOTAL_BYTES:
            raise ProviderInvalidResponse()
        normalized_rows.append(cast(dict[str, object], normalized))
    return tuple(normalized_rows)


def normalize_research_table(table: object) -> tuple[dict[str, object], ...]:
    safe_error: ProviderClientError | None = None
    try:
        return _normalize_research_table(table)
    except ProviderClientError as error:
        safe_error = clean_provider_error(error)
    except Exception:
        safe_error = ProviderInvalidResponse()
    raise safe_error


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=MARKET_TIMEZONE).astimezone(timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=MARKET_TIMEZONE).astimezone(
            timezone.utc
        )
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    for pattern in (
        "%Y%m%d",
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(value, pattern).replace(
                tzinfo=MARKET_TIMEZONE
            ).astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=MARKET_TIMEZONE).astimezone(timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest_timestamp(
    rows: tuple[dict[str, object], ...],
    field_names: tuple[str, ...],
) -> datetime | None:
    parsed = tuple(
        timestamp
        for row in rows
        for field in field_names
        if (timestamp := _parse_datetime(row.get(field))) is not None
    )
    return max(parsed, default=None)


def _first_url(
    rows: tuple[dict[str, object], ...],
    field_names: tuple[str, ...],
) -> str | None:
    for row in rows:
        for field in field_names:
            value = row.get(field)
            if isinstance(value, str) and value.startswith(("https://", "http://")):
                return value
    return None


def _content_digest(
    *,
    source: ProviderId,
    kind: ResearchSectionKind,
    symbol: str,
    rows: tuple[dict[str, object], ...],
) -> str:
    encoded = json.dumps(
        {
            "source": source.value,
            "kind": kind.value,
            "symbol": symbol,
            "items": rows,
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _research_section_from_table(
    *,
    source: ProviderId,
    kind: ResearchSectionKind,
    symbol: CanonicalSymbol,
    table: object,
    fetched_at: datetime,
    cutoff_fields: tuple[str, ...],
    published_fields: tuple[str, ...] = (),
    url_fields: tuple[str, ...] = (),
    default_source_url: str | None = None,
) -> ResearchSection:
    if kind not in RESEARCH_SOURCE_CATEGORIES:
        raise ProviderInvalidResponse()
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ProviderInvalidResponse()
    canonical_fetched_at = fetched_at.astimezone(timezone.utc)
    rows = normalize_research_table(table)
    cutoff = _latest_timestamp(rows, cutoff_fields)
    if cutoff is None:
        cutoff = canonical_fetched_at
    published_at = _latest_timestamp(rows, published_fields)
    if kind in {ResearchSectionKind.ANNOUNCEMENTS, ResearchSectionKind.NEWS}:
        if published_at is None:
            raise ProviderInvalidResponse()
    else:
        published_at = None
    if cutoff > canonical_fetched_at or (
        published_at is not None and published_at > canonical_fetched_at
    ):
        raise ProviderInvalidResponse()
    digest = _content_digest(
        source=source,
        kind=kind,
        symbol=symbol,
        rows=rows,
    )
    try:
        return ResearchSection.model_validate(
            {
                "kind": kind,
                "canonical_source": source.value,
                "source_record": f"{source.value}:{kind.value}:{digest}",
                "source_url": _first_url(rows, url_fields) or default_source_url,
                "published_at": published_at,
                "data_cutoff": cutoff,
                "fetched_at": canonical_fetched_at,
                "dataset_version": digest,
                "content": {"symbol": symbol, "items": rows},
            }
        )
    except Exception:
        raise ProviderInvalidResponse() from None


def research_section_from_table(
    *,
    source: ProviderId,
    kind: ResearchSectionKind,
    symbol: CanonicalSymbol,
    table: object,
    fetched_at: datetime,
    cutoff_fields: tuple[str, ...],
    published_fields: tuple[str, ...] = (),
    url_fields: tuple[str, ...] = (),
    default_source_url: str | None = None,
) -> ResearchSection:
    safe_error: ProviderClientError | None = None
    try:
        return _research_section_from_table(
            source=source,
            kind=kind,
            symbol=symbol,
            table=table,
            fetched_at=fetched_at,
            cutoff_fields=cutoff_fields,
            published_fields=published_fields,
            url_fields=url_fields,
            default_source_url=default_source_url,
        )
    except ProviderClientError as error:
        safe_error = clean_provider_error(error)
    except Exception:
        safe_error = ProviderInvalidResponse()
    raise safe_error


Clock = Callable[[], datetime]


__all__ = [
    "Clock",
    "MAX_RESEARCH_DEPTH",
    "MAX_RESEARCH_ITEM_BYTES",
    "MAX_RESEARCH_ITEMS",
    "MAX_RESEARCH_NODES",
    "MAX_RESEARCH_TOTAL_BYTES",
    "RESEARCH_SOURCE_CATEGORIES",
    "ResearchSourceAdapter",
    "ResearchSourceCapability",
    "clean_provider_error",
    "normalize_research_table",
    "research_section_from_table",
]
