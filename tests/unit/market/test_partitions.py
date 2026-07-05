from __future__ import annotations

from pathlib import PurePosixPath
import re

import pytest
from pydantic import ValidationError

from stock_desk.market.partitions import (
    PARTITION_LAYOUT_VERSION,
    PartitionKey,
    partition_manifest_id,
    partition_path,
)
from stock_desk.market.types import Adjustment, Period, ProviderId


def partition_key(**updates: object) -> PartitionKey:
    values: dict[str, object] = {
        "category": "bars",
        "source": ProviderId.AKSHARE,
        "symbol": "600000.SH",
        "period": Period.DAY,
        "adjustment": Adjustment.QFQ,
        "year": 2026,
    }
    values.update(updates)
    return PartitionKey.model_validate(values)


def test_partition_path_is_deterministic_relative_and_hive_style() -> None:
    key = partition_key()

    first = partition_path(key)
    second = partition_path(partition_key())

    assert first == second
    assert first == PurePosixPath(
        "layout=v1",
        "category=bars",
        "source=akshare",
        "symbol=600000.SH",
        "period=1d",
        "adjustment=qfq",
        "year=2026",
    )
    assert first.is_absolute() is False
    assert not {".", ".."}.intersection(first.parts)
    assert PARTITION_LAYOUT_VERSION == "v1"


def test_partition_manifest_id_is_stable_and_changes_with_identity() -> None:
    key = partition_key()

    manifest_id = partition_manifest_id(key)

    assert manifest_id == partition_manifest_id(partition_key())
    assert manifest_id == (
        "sha256:ee53c24547b284434b5e88ec9f5f7587cd650ad0f5d6c97a06d8d8ae91669bf4"
    )
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_id)
    assert manifest_id != partition_manifest_id(
        partition_key(adjustment=Adjustment.HFQ)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", "../bars"),
        ("category", "/bars"),
        ("category", "bars/2026"),
        ("category", "."),
        ("source", "../akshare"),
        ("source", "akshare/source"),
        ("source", "akshare source"),
        ("source", ""),
        ("layout_version", "../v1"),
        ("symbol", "600000.SH/../../secret"),
        ("symbol", "600000.sh"),
        ("year", 0),
        ("year", 10000),
        ("year", True),
    ],
)
def test_partition_key_rejects_traversal_and_noncanonical_input(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        partition_key(**{field: value})


def test_partition_key_rejects_unknown_enum_values() -> None:
    with pytest.raises(ValidationError):
        partition_key(period="daily")
    with pytest.raises(ValidationError):
        partition_key(adjustment="forward")


def test_partition_key_is_frozen() -> None:
    key = partition_key()

    with pytest.raises(ValidationError, match="frozen"):
        key.year = 2025


def test_manifest_id_documents_logical_partition_identity() -> None:
    assert partition_manifest_id.__doc__ is not None
    assert "logical partition manifest identity" in partition_manifest_id.__doc__
    assert "content identity" not in partition_manifest_id.__doc__
