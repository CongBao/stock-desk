from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from stock_desk.market.types import Adjustment, CanonicalSymbol, Period, ProviderId


SafePartitionSegment = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
    ),
]
PartitionYear = Annotated[int, Field(strict=True, ge=1900, le=9999)]
PARTITION_LAYOUT_VERSION: Final[Literal["v1"]] = "v1"


class PartitionKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    layout_version: Literal["v1"] = PARTITION_LAYOUT_VERSION
    category: SafePartitionSegment
    source: ProviderId
    symbol: CanonicalSymbol
    period: Period
    adjustment: Adjustment
    year: PartitionYear


def partition_path(key: PartitionKey) -> PurePosixPath:
    """Map a validated partition identity to a relative Hive-style path."""
    return PurePosixPath(
        f"layout={key.layout_version}",
        f"category={key.category}",
        f"source={key.source.value}",
        f"symbol={key.symbol}",
        f"period={key.period.value}",
        f"adjustment={key.adjustment.value}",
        f"year={key.year:04d}",
    )


def partition_manifest_id(key: PartitionKey) -> str:
    """Return the stable logical partition manifest identity for this key."""
    canonical_bytes = json.dumps(
        {
            "layout_version": key.layout_version,
            "category": key.category,
            "source": key.source.value,
            "symbol": key.symbol,
            "period": key.period.value,
            "adjustment": key.adjustment.value,
            "year": key.year,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(canonical_bytes).hexdigest()}"
