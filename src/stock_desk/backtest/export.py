from __future__ import annotations

from collections.abc import Iterator, Mapping
import csv
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
import json

from stock_desk.backtest.repository import (
    BacktestExportMetadata,
    BacktestExportRecord,
    BacktestRepository,
)
from stock_desk.backtest.public_data import is_dangerous_key, public_text


EXPORT_SCHEMA_VERSION = "stock-desk-backtest-export-v1"
CSV_NULL = r"\N"
CSV_COLUMNS = (
    "record_type",
    "export_schema_version",
    "run_id",
    "snapshot_id",
    "generated_at",
    "section",
    "disclaimer",
    "formula_version_id",
    "formula_checksum",
    "formula_engine_version",
    "compatibility_version",
    "backtest_engine_version",
    "instrument_dataset_version",
    "symbol_count",
    "runnable_count",
    "gap_count",
    "signal_source_ids",
    "execution_source_ids",
    "status_source_ids",
    "provenance_digest",
    "period",
    "adjustment",
    "quantity_shares",
    "commission_bps",
    "minimum_commission",
    "sell_tax_bps",
    "slippage_bps",
    "execution_rules_version",
    "cost_model_version",
    "sizing_version",
    "warmup_policy_version",
    "symbol",
    "ordinal",
    "dimension",
    "key",
    "reason",
    "level",
    "message",
    "payload",
)
_SAFE_LOG_KEYS = frozenset({"attempt", "reason", "status", "symbol"})


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("export timestamp must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("export Decimal must be finite")
    normalized = value.normalize()
    return format(normalized.copy_abs() if normalized.is_zero() else normalized, "f")


def _safe_json(value: object, *, section: str, key: str | None = None) -> object:
    if key is not None and is_dangerous_key(key):
        return None
    if value is None or type(value) in {bool, int, str}:
        return public_text(value) if isinstance(value, str) else value
    if isinstance(value, float):
        raise ValueError("persisted export values must not contain floats")
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key in sorted(value):
            if type(raw_key) is not str or is_dangerous_key(raw_key):
                continue
            if section == "logs" and key == "detail" and raw_key not in _SAFE_LOG_KEYS:
                continue
            result[raw_key] = _safe_json(value[raw_key], section=section, key=raw_key)
        return result
    if isinstance(value, (tuple, list)):
        return [_safe_json(item, section=section) for item in value]
    raise ValueError("persisted export value is unsupported")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _metadata_dict(metadata: BacktestExportMetadata) -> dict[str, object]:
    return {
        "disclaimer": metadata.disclaimer,
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "generated_at": _timestamp(metadata.generated_at),
        "run_id": metadata.run_id,
        "section": metadata.section,
        "snapshot_id": metadata.snapshot_id,
        "formula_version_id": metadata.formula_version_id,
        "formula_checksum": metadata.formula_checksum,
        "formula_engine_version": metadata.formula_engine_version,
        "compatibility_version": metadata.compatibility_version,
        "backtest_engine_version": metadata.backtest_engine_version,
        "instrument_dataset_version": metadata.instrument_dataset_version,
        "symbol_count": metadata.symbol_count,
        "runnable_count": metadata.runnable_count,
        "gap_count": metadata.gap_count,
        "signal_source_ids": list(metadata.signal_source_ids),
        "execution_source_ids": list(metadata.execution_source_ids),
        "status_source_ids": list(metadata.status_source_ids),
        "provenance_digest": metadata.provenance_digest,
        "period": metadata.period,
        "adjustment": metadata.adjustment,
        "quantity_shares": metadata.quantity_shares,
        "commission_bps": metadata.commission_bps,
        "minimum_commission": metadata.minimum_commission,
        "sell_tax_bps": metadata.sell_tax_bps,
        "slippage_bps": metadata.slippage_bps,
        "execution_rules_version": metadata.execution_rules_version,
        "cost_model_version": metadata.cost_model_version,
        "sizing_version": metadata.sizing_version,
        "warmup_policy_version": metadata.warmup_policy_version,
    }


def _json_stream(
    records: Iterator[BacktestExportMetadata | BacktestExportRecord],
) -> Iterator[bytes]:
    try:
        metadata = next(records)
    except StopIteration:
        raise ValueError("export metadata is missing") from None
    if not isinstance(metadata, BacktestExportMetadata):
        raise ValueError("export metadata is invalid")
    yield b'{"metadata":'
    yield _canonical_json(_metadata_dict(metadata))
    yield b',"rows":['
    first = True
    for record in records:
        if not isinstance(record, BacktestExportRecord):
            raise ValueError("export row is invalid")
        if not first:
            yield b","
        first = False
        yield _canonical_json(_safe_json(record.data, section=record.section))
    yield b"]}"


def _csv_text(value: object, *, trusted_numeric: bool = False) -> str:
    if value is None:
        return CSV_NULL
    if isinstance(value, Decimal):
        return _decimal(value)
    if type(value) is int:
        return str(value)
    if isinstance(value, (tuple, list, dict)):
        value = _canonical_json(value).decode("utf-8")
    if not isinstance(value, str):
        raise ValueError("CSV scalar is invalid")
    if trusted_numeric:
        return value
    stripped = value.lstrip(" \t\r\n\v\f")
    if value.startswith(("\t", "\r", "\n", "\v", "\f")) or stripped.startswith(
        ("=", "+", "-", "@")
    ):
        return "'" + value
    return value


def _csv_line(values: list[str]) -> bytes:
    output = StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerow(values)
    return output.getvalue().encode("utf-8")


def _csv_row(
    metadata: BacktestExportMetadata,
    *,
    record_type: str,
    data: Mapping[str, object] | None,
) -> list[str]:
    row: dict[str, object] = {
        "record_type": record_type,
        **_metadata_dict(metadata),
        "symbol": None,
        "ordinal": None,
        "dimension": None,
        "key": None,
        "reason": None,
        "level": None,
        "message": None,
        "payload": None,
    }
    if data is not None:
        safe = _safe_json(data, section=metadata.section)
        if not isinstance(safe, Mapping):
            raise ValueError("export row is invalid")
        for key in (
            "symbol",
            "ordinal",
            "dimension",
            "key",
            "reason",
            "level",
            "message",
        ):
            if key in safe:
                row[key] = safe[key]
        payload_key = "payload" if "payload" in safe else "detail"
        if payload_key in safe:
            row["payload"] = _canonical_json(safe[payload_key]).decode("utf-8")
    return [
        _csv_text(
            row[column],
            trusted_numeric=column
            in {
                "ordinal",
                "symbol_count",
                "runnable_count",
                "gap_count",
                "quantity_shares",
            },
        )
        for column in CSV_COLUMNS
    ]


def _csv_stream(
    records: Iterator[BacktestExportMetadata | BacktestExportRecord],
) -> Iterator[bytes]:
    try:
        metadata = next(records)
    except StopIteration:
        raise ValueError("export metadata is missing") from None
    if not isinstance(metadata, BacktestExportMetadata):
        raise ValueError("export metadata is invalid")
    yield _csv_line(list(CSV_COLUMNS))
    yield _csv_line(_csv_row(metadata, record_type="metadata", data=None))
    for record in records:
        if not isinstance(record, BacktestExportRecord):
            raise ValueError("export row is invalid")
        yield _csv_line(_csv_row(metadata, record_type="data", data=record.data))


def stream_export(
    repository: BacktestRepository,
    run_id: str,
    *,
    section: str,
    format: str,
) -> Iterator[bytes]:
    records = repository.iter_export_records(run_id, section=section)
    if format == "json":
        return _json_stream(records)
    if format == "csv":
        return _csv_stream(records)
    raise ValueError("export format is invalid")


__all__ = ["stream_export"]
