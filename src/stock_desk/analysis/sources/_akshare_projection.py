"""Versioned, bounded projections for AKShare research responses."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from itertools import islice
import json
import math
from typing import cast, Final

from stock_desk.analysis.sources.base import (
    _canonical_source_url,
    _parse_datetime,
    clean_provider_error,
    normalize_research_table,
)
from stock_desk.market.providers.base import (
    ProviderClientError,
    ProviderInvalidResponse,
)


AKSHARE_RESEARCH_PROJECTION_VERSION: Final = "akshare-research-projection-v1"
AKSHARE_ANNOUNCEMENT_WINDOW_DAYS: Final = 366

_MAX_RAW_ROWS: Final = 2_048
_MAX_RAW_COLUMNS: Final = 256
_MAX_RAW_CELLS: Final = 262_144
_MAX_RAW_CELL_BYTES: Final = 65_536
_MAX_SELECTED_RAW_BYTES: Final = 8_388_608
_MAX_COLUMN_BYTES: Final = 256


@dataclass(frozen=True)
class _Projection:
    fields: tuple[str, ...]
    required_fields: frozenset[str]
    identity_field: str
    sort_field: str
    optional_date_fields: tuple[str, ...]
    url_fields: tuple[str, ...]
    analyzable_fields: frozenset[str]
    requires_text_content: bool
    maximum_items: int
    window_days: int | None = None


_FUNDAMENTAL_METRICS: Final = (
    "EPSJB",
    "EPSKCJB",
    "BPS",
    "MGJYXJJE",
    "TOTALOPERATEREVE",
    "PARENTNETPROFIT",
    "KCFJCXSYJLR",
    "TOTALOPERATEREVETZ",
    "PARENTNETPROFITTZ",
    "KCFJCXSYJLRTZ",
    "ROEJQ",
    "ROEKCJQ",
    "ROIC",
    "XSMLL",
    "XSJLL",
    "JYXJLYYSR",
    "ZCFZL",
    "LD",
    "SD",
    # Bank indicators exposed by the current Eastmoney response.
    "TOTALDEPOSITS",
    "GROSSLOANS",
    "LTDRR",
    "NONPERLOAN",
    "NEWCAPITALADER",
    "HXYJBCZL",
    "BLDKBBL",
    "FIRST_ADEQUACY_RATIO",
    "LOAN_ADVANCES",
    "NON_PERFORMING_LOAN",
    "NET_INTEREST_SPREAD",
    "NET_INTEREST_MARGIN",
    "LOAN_PROVISION_RATIO",
    "CAPITAL_LEVERAGE_RATIO",
    "LIQUIDITY_COVERAGE_RATIO",
    # Insurer indicators exposed by the current Eastmoney response.
    "EARNED_PREMIUM",
    "COMPENSATE_EXPENSE",
    "SURRENDER_RATE_LIFE",
    "SOLVENCY_AR",
    "NBV_LIFE",
    "NBV_RATE",
    # Compatibility names returned by earlier AKShare fixtures.
    "BASIC_EPS",
    "TOTAL_OPERATE_INCOME",
    "PARENT_NETPROFIT",
)

_PROJECTIONS: Final[dict[str, _Projection]] = {
    "stock_financial_analysis_indicator_em": _Projection(
        fields=(
            "SECUCODE",
            "SECURITY_CODE",
            "SECURITY_NAME_ABBR",
            "REPORT_DATE",
            "REPORT_TYPE",
            "REPORT_DATE_NAME",
            "NOTICE_DATE",
            "UPDATE_DATE",
            "CURRENCY",
            *_FUNDAMENTAL_METRICS,
        ),
        required_fields=frozenset({"SECUCODE", "REPORT_DATE"}),
        identity_field="SECUCODE",
        sort_field="REPORT_DATE",
        optional_date_fields=("NOTICE_DATE", "UPDATE_DATE"),
        url_fields=(),
        analyzable_fields=frozenset(_FUNDAMENTAL_METRICS),
        requires_text_content=False,
        maximum_items=24,
    ),
    "stock_individual_notice_report": _Projection(
        fields=("代码", "名称", "公告标题", "公告类型", "公告日期", "网址", "公告链接"),
        required_fields=frozenset({"代码", "公告日期", "网址"}),
        identity_field="代码",
        sort_field="公告日期",
        optional_date_fields=(),
        url_fields=("网址", "公告链接"),
        analyzable_fields=frozenset({"公告标题"}),
        requires_text_content=True,
        maximum_items=256,
        window_days=AKSHARE_ANNOUNCEMENT_WINDOW_DAYS,
    ),
    "stock_news_em": _Projection(
        fields=("关键词", "新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"),
        required_fields=frozenset({"关键词", "发布时间", "新闻链接"}),
        identity_field="关键词",
        sort_field="发布时间",
        optional_date_fields=(),
        url_fields=("新闻链接",),
        analyzable_fields=frozenset({"新闻标题", "新闻内容"}),
        requires_text_content=True,
        maximum_items=100,
    ),
}


def _validated_fetched_at(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ProviderInvalidResponse()
    return value.astimezone(timezone.utc)


def _validated_columns(value: object, *, expected_count: int) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ProviderInvalidResponse()
    try:
        iterator = iter(cast(Iterable[object], value))
        columns = tuple(islice(iterator, expected_count + 1))
    except Exception:
        raise ProviderInvalidResponse() from None
    if (
        expected_count <= 0
        or expected_count > _MAX_RAW_COLUMNS
        or len(columns) != expected_count
        or len(columns) != len(frozenset(columns))
        or any(
            type(column) is not str
            or not column
            or len(column) > _MAX_COLUMN_BYTES
            or len(column.encode("utf-8")) > _MAX_COLUMN_BYTES
            for column in columns
        )
    ):
        raise ProviderInvalidResponse()
    return cast(tuple[str, ...], columns)


def _validated_shape(value: object) -> tuple[int, int]:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or type(value[0]) is not int
        or type(value[1]) is not int
        or value[0] < 0
        or value[1] < 0
        or value[0] > _MAX_RAW_ROWS
        or value[1] > _MAX_RAW_COLUMNS
        or value[0] * value[1] > _MAX_RAW_CELLS
    ):
        raise ProviderInvalidResponse()
    return cast(tuple[int, int], value)


def _dataframe_rows(table: object, projection: _Projection) -> tuple[object, ...]:
    row_count, column_count = _validated_shape(getattr(table, "shape", None))
    columns = _validated_columns(
        getattr(table, "columns", None), expected_count=column_count
    )
    selected_columns = tuple(field for field in projection.fields if field in columns)
    if not projection.required_fields.issubset(selected_columns):
        raise ProviderInvalidResponse()
    projected_table = table
    if selected_columns != columns:
        locator = getattr(table, "loc", None)
        if locator is None:
            raise ProviderInvalidResponse()
        try:
            projected_table = locator[:, list(selected_columns)]
        except Exception:
            raise ProviderInvalidResponse() from None
    projected_shape = getattr(projected_table, "shape", None)
    projected_columns = _validated_columns(
        getattr(projected_table, "columns", None),
        expected_count=len(selected_columns),
    )
    if (
        projected_shape != (row_count, len(selected_columns))
        or projected_columns != selected_columns
    ):
        raise ProviderInvalidResponse()
    to_dict = getattr(projected_table, "to_dict", None)
    if not callable(to_dict):
        raise ProviderInvalidResponse()
    try:
        rows = to_dict(orient="records")
    except Exception:
        raise ProviderInvalidResponse() from None
    if not isinstance(rows, (list, tuple)) or len(rows) != row_count:
        raise ProviderInvalidResponse()
    return tuple(rows)


def _raw_rows(table: object, projection: _Projection) -> tuple[object, ...]:
    if isinstance(table, Mapping):
        return (table,)
    if callable(getattr(table, "to_dict", None)):
        return _dataframe_rows(table, projection)
    if isinstance(table, (str, bytes)) or not isinstance(table, Sequence):
        raise ProviderInvalidResponse()
    if len(table) > _MAX_RAW_ROWS:
        raise ProviderInvalidResponse()
    return tuple(table)


def _raw_cell_bytes(value: object) -> int:
    if isinstance(value, str):
        if len(value) > _MAX_RAW_CELL_BYTES:
            raise ProviderInvalidResponse()
        size = len(value.encode("utf-8"))
    elif isinstance(value, (bytes, bytearray)):
        size = len(value)
    else:
        size = 16
    if size > _MAX_RAW_CELL_BYTES:
        raise ProviderInvalidResponse()
    return size


def _optional_date(value: object, *, fetched_at: datetime) -> None:
    if value is None or value == "" or (type(value) is float and math.isnan(value)):
        return
    _required_date(value, fetched_at=fetched_at)


def _required_date(value: object, *, fetched_at: datetime) -> datetime:
    if isinstance(value, datetime) and (
        value.tzinfo is None or value.utcoffset() is None
    ):
        raise ProviderInvalidResponse()
    parsed = _parse_datetime(value)
    if parsed is None or parsed > fetched_at:
        raise ProviderInvalidResponse()
    return parsed


def _is_meaningful_metric(value: object) -> bool:
    if value is None:
        return False
    if type(value) is str:
        return bool(value.strip())
    if type(value) is float:
        return math.isfinite(value)
    return type(value) is int


def _has_analyzable_content(
    row: Mapping[str, object],
    projection: _Projection,
) -> bool:
    values = (row.get(field) for field in projection.analyzable_fields)
    if projection.requires_text_content:
        return any(type(value) is str and bool(value.strip()) for value in values)
    return any(_is_meaningful_metric(value) for value in values)


def _project(
    operation: str,
    table: object,
    *,
    expected_identity: str,
    fetched_at: datetime,
) -> list[dict[str, object]]:
    projection = _PROJECTIONS.get(operation)
    if projection is None:
        raise ProviderInvalidResponse()
    raw_rows = _raw_rows(table, projection)
    if not raw_rows:
        return list(normalize_research_table(()))
    projected: list[tuple[datetime, dict[str, object]]] = []
    selected_raw_bytes = 0
    total_cells = 0
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping) or not raw_row:
            raise ProviderInvalidResponse()
        if len(raw_row) > _MAX_RAW_COLUMNS or any(
            type(key) is not str
            or not key
            or len(key) > _MAX_COLUMN_BYTES
            or len(key.encode("utf-8")) > _MAX_COLUMN_BYTES
            for key in raw_row
        ):
            raise ProviderInvalidResponse()
        total_cells += len(raw_row)
        if total_cells > _MAX_RAW_CELLS:
            raise ProviderInvalidResponse()
        if not projection.required_fields.issubset(raw_row):
            raise ProviderInvalidResponse()
        identity = raw_row[projection.identity_field]
        if type(identity) is not str or identity != expected_identity:
            raise ProviderInvalidResponse()
        selected: dict[str, object] = {}
        for field in projection.fields:
            if field not in raw_row:
                continue
            value = raw_row[field]
            selected_raw_bytes += len(field.encode("utf-8")) + _raw_cell_bytes(value)
            if selected_raw_bytes > _MAX_SELECTED_RAW_BYTES:
                raise ProviderInvalidResponse()
            selected[field] = value
        sort_date = _required_date(
            selected[projection.sort_field],
            fetched_at=fetched_at,
        )
        for field in projection.optional_date_fields:
            if field in selected:
                _optional_date(selected[field], fetched_at=fetched_at)
        for field in projection.url_fields:
            if field not in selected:
                continue
            canonical = _canonical_source_url(selected[field])
            if canonical is None:
                raise ProviderInvalidResponse()
            selected[field] = canonical
        normalized = normalize_research_table((selected,))[0]
        if not _has_analyzable_content(normalized, projection):
            raise ProviderInvalidResponse()
        projected.append((sort_date, normalized))
    projected.sort(key=lambda item: item[0], reverse=True)
    bounded = [row for _date, row in projected[: projection.maximum_items]]
    return list(normalize_research_table(bounded))


def project_akshare_research_table(
    operation: str,
    table: object,
    *,
    expected_identity: str,
    fetched_at: datetime,
) -> list[dict[str, object]]:
    """Project one AKShare response before the global table safety boundary."""
    safe_error: ProviderClientError | None = None
    try:
        if (
            type(expected_identity) is not str
            or not expected_identity
            or len(expected_identity) > 32
            or expected_identity != expected_identity.strip()
        ):
            raise ProviderInvalidResponse()
        return _project(
            operation,
            table,
            expected_identity=expected_identity,
            fetched_at=_validated_fetched_at(fetched_at),
        )
    except ProviderClientError as error:
        safe_error = clean_provider_error(error)
    except Exception:
        safe_error = ProviderInvalidResponse()
    raise safe_error


def akshare_projection_fields(operation: str) -> tuple[str, ...]:
    projection = _PROJECTIONS.get(operation)
    if projection is None:
        raise ProviderInvalidResponse()
    return projection.fields


def akshare_projection_contract(operation: str) -> dict[str, object]:
    projection = _PROJECTIONS.get(operation)
    if projection is None:
        raise ProviderInvalidResponse()
    encoded_fields = json.dumps(
        projection.fields,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    contract: dict[str, object] = {
        "fields_sha256": f"sha256:{hashlib.sha256(encoded_fields).hexdigest()}",
        "maximum_items": projection.maximum_items,
        "projection_version": AKSHARE_RESEARCH_PROJECTION_VERSION,
    }
    if projection.window_days is not None:
        contract["window_days"] = projection.window_days
    return contract


def akshare_expected_identity(
    operation: str,
    kwargs: Mapping[str, object],
) -> str:
    if operation == "stock_individual_notice_report":
        value = kwargs.get("security")
    elif operation in {
        "stock_financial_analysis_indicator_em",
        "stock_news_em",
    }:
        value = kwargs.get("symbol")
    else:
        raise ProviderInvalidResponse()
    if type(value) is not str or not value or len(value) > 32 or value != value.strip():
        raise ProviderInvalidResponse()
    return value


__all__ = [
    "AKSHARE_ANNOUNCEMENT_WINDOW_DAYS",
    "AKSHARE_RESEARCH_PROJECTION_VERSION",
    "akshare_expected_identity",
    "akshare_projection_contract",
    "akshare_projection_fields",
    "project_akshare_research_table",
]
