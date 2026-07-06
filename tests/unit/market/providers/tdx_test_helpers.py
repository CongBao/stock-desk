from __future__ import annotations

from datetime import datetime
import importlib
from pathlib import Path
from struct import Struct
from typing import Any
from zoneinfo import ZoneInfo

from stock_desk.market.types import Adjustment, BarQuery, Period


VALID_SH_RECORDS = bytes.fromhex(
    "3dd93401e80300001a040000de030000fc03000000e64046e803000000000000"
    "3ed93401fc03000038040000f20300002e0400000040b7460000000000000000"
)
MAX_VOLUME_RECORD = bytes.fromhex(
    "3dd93401f401000026020000ea0100000802000000045845ffffffff00000000"
)
FLOAT32 = Struct("<f")
SHANGHAI = ZoneInfo("Asia/Shanghai")
FETCHED_AT = datetime(2024, 7, 8, 16, tzinfo=SHANGHAI)
TDX_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "tdx"
PROJECT_ROOT = Path(__file__).resolve().parents[4]


def tdx_binary() -> Any:
    return importlib.import_module("stock_desk.market.providers.tdx_binary")


def provider_corrupt() -> type[Exception]:
    base = importlib.import_module("stock_desk.market.providers.base")
    return base.ProviderCorrupt


def tdx_local() -> Any:
    return importlib.import_module("stock_desk.market.providers.tdx_local")


def tdx_windows() -> Any:
    return importlib.import_module("stock_desk.market.providers.tdx_windows")


def make_vipdoc_root(tmp_path: Path) -> Path:
    root = tmp_path / "vipdoc"
    for market in ("sh", "sz"):
        (root / market / "lday").mkdir(parents=True)
    return root


def write_tdx_file(root: Path, symbol: str, payload: bytes) -> Path:
    code, suffix = symbol.split(".")
    market = suffix.lower()
    target = root / market / "lday" / f"{market}{code}.day"
    target.write_bytes(payload)
    return target


def golden_payload(symbol: str) -> bytes:
    code, suffix = symbol.split(".")
    fixture = TDX_FIXTURES / f"{suffix.lower()}{code}.day.hex"
    return bytes.fromhex(fixture.read_text(encoding="ascii"))


def bar_query(
    *,
    symbol: str = "600000.SH",
    period: Period = Period.DAY,
    adjustment: Adjustment = Adjustment.NONE,
    start: datetime | None = None,
    end: datetime | None = None,
) -> BarQuery:
    return BarQuery(
        symbol=symbol,
        period=period,
        adjustment=adjustment,
        start=start or datetime(2024, 7, 1, tzinfo=SHANGHAI),
        end=end or datetime(2024, 7, 3, tzinfo=SHANGHAI),
    )


def raw_record(
    *,
    raw_date: int = 20240701,
    open_price: int = 1000,
    high: int = 1050,
    low: int = 990,
    close: int = 1020,
    amount: float = 12345.5,
    amount_bytes: bytes | None = None,
    volume: int = 1000,
    reserved: int = 0,
) -> bytes:
    return b"".join(
        (
            raw_date.to_bytes(4, "little"),
            open_price.to_bytes(4, "little"),
            high.to_bytes(4, "little"),
            low.to_bytes(4, "little"),
            close.to_bytes(4, "little"),
            FLOAT32.pack(amount) if amount_bytes is None else amount_bytes,
            volume.to_bytes(4, "little"),
            reserved.to_bytes(4, "little"),
        )
    )
