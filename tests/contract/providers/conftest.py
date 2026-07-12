from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import importlib
import json
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import pytest

from stock_desk.market.providers.base import ProviderBarTable
from stock_desk.market.types import ProviderId


FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "providers"
FETCHED_AT = datetime(2024, 7, 8, 16, tzinfo=ZoneInfo("Asia/Shanghai"))
SECRET_SENTINEL = "token=TOP-SECRET-DO-NOT-LEAK"


class FakeFrame:
    def __init__(
        self,
        rows: list[dict[str, object]],
        *,
        columns: list[str] | None = None,
    ) -> None:
        self._rows = rows
        self.columns = columns or (list(rows[0]) if rows else [])

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return [
            {column: row.get(column) for column in self.columns} for row in self._rows
        ]


class FakeCursor:
    def __init__(
        self,
        rows: list[dict[str, object]],
        *,
        error_code: str = "0",
        fields: list[str] | None = None,
        fail_after: int | None = None,
        row_width_delta: int = 0,
    ) -> None:
        self.error_code = error_code
        self.error_msg = SECRET_SENTINEL
        self.fields = fields or (list(rows[0]) if rows else [])
        self._rows = rows
        self._index = -1
        self._fail_after = fail_after
        self._row_width_delta = row_width_delta

    def next(self) -> bool:
        if self._fail_after is not None and self._index + 1 >= self._fail_after:
            self.error_code = "999"
            return False
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self) -> list[object]:
        values = [self._rows[self._index].get(field) for field in self.fields]
        if self._row_width_delta < 0:
            return values[: self._row_width_delta]
        return [*values, *([None] * self._row_width_delta)]


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


class FixtureClient:
    def __init__(
        self,
        fixture: dict[str, object],
        *,
        table_style: str = "list",
    ) -> None:
        self.fixture = fixture
        self.table_style = table_style
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.bar_exception: Exception | None = None
        self.instrument_exception: Exception | None = None
        self.calendar_exception: Exception | None = None
        self.bar_mutation: str | None = None
        self.bar_coverage_mutation: str | None = None
        self.return_raw_bar_table = False
        self.frame_columns: list[str] | None = None
        self.cursor_error_code = "0"
        self.cursor_fields: list[str] | None = None
        self.cursor_fail_after: int | None = None
        self.cursor_row_width_delta = 0

    def _table(self, rows: list[dict[str, object]]) -> object:
        copied = [row.copy() for row in rows]
        if self.table_style == "frame":
            return FakeFrame(copied, columns=self.frame_columns)
        if self.table_style == "cursor":
            return FakeCursor(
                copied,
                error_code=self.cursor_error_code,
                fields=self.cursor_fields,
                fail_after=self.cursor_fail_after,
                row_width_delta=self.cursor_row_width_delta,
            )
        return copied

    def _bars(self, key: str, *, market_key: str | None = None) -> object:
        if self.bar_exception is not None:
            raise self.bar_exception
        bars = self.fixture["bars"]
        assert isinstance(bars, dict)
        rows = [dict(row) for row in bars[key]]
        market_bars = self.fixture.get("market_bars")
        if (
            key == "1d"
            and market_key is not None
            and isinstance(market_bars, dict)
            and market_key in market_bars
        ):
            rows = [dict(market_bars[market_key])]
        if self.bar_mutation == "empty":
            rows = []
        elif self.bar_mutation == "duplicate":
            rows.append(rows[0].copy())
        elif self.bar_mutation == "corrupt":
            rows[0][next(key for key in rows[0] if key in {"open", "开盘"})] = True
        elif self.bar_mutation == "mismatch":
            for row in rows:
                if "ts_code" in row:
                    row["ts_code"] = "000001.SZ"
                elif "股票代码" in row:
                    row["股票代码"] = "000001"
                elif "code" in row:
                    row["code"] = "sz.000001"
        elif self.bar_mutation == "long-cell":
            rows[0][next(iter(rows[0]))] = "x" * 5000
        elif self.bar_mutation == "too-many":
            rows = [rows[0].copy() for _ in range(10_001)]
        elif self.bar_mutation == "fractional-lot":
            rows[0][next(key for key in rows[0] if key in {"vol", "成交量"})] = "0.015"
        elif self.bar_mutation == "tiny-lot":
            rows[0][next(key for key in rows[0] if key in {"vol", "成交量"})] = "0.01"
        elif self.bar_mutation in {
            "bool-volume",
            "negative-volume",
            "overflow-volume",
        }:
            volume_key = next(
                key for key in rows[0] if key in {"vol", "成交量", "volume"}
            )
            rows[0][volume_key] = {
                "bool-volume": True,
                "negative-volume": "-1",
                "overflow-volume": str(2**63),
            }[self.bar_mutation]
        elif self.bar_mutation == "nan-price":
            rows[0][next(key for key in rows[0] if key in {"open", "开盘"})] = "NaN"
        elif self.bar_mutation == "missing-suspended-prices":
            suspended = next(row for row in rows if row.get("tradestatus") == "0")
            for key in ("open", "high", "low", "close"):
                suspended[key] = ""
        return self._table(rows)

    def _bar_response(
        self,
        table: object,
        *,
        coverage_start: datetime,
        coverage_end: datetime,
    ) -> object:
        if self.return_raw_bar_table:
            return table
        complete = True
        limit_reached = False
        if self.bar_coverage_mutation == "partial":
            complete = False
            if isinstance(table, list):
                table = table[:1]
        elif self.bar_coverage_mutation == "limit-reached":
            limit_reached = True
        elif self.bar_coverage_mutation == "start-mismatch":
            coverage_start += timedelta(minutes=1)
        elif self.bar_coverage_mutation == "end-mismatch":
            coverage_end -= timedelta(minutes=1)
        return ProviderBarTable(
            table=table,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            complete=complete,
            limit_reached=limit_reached,
        )


class FakeTushareClient(FixtureClient):
    def pro_bar(self, **kwargs: object) -> object:
        coverage_start = kwargs.pop("_coverage_start")
        coverage_end = kwargs.pop("_coverage_end")
        assert isinstance(coverage_start, datetime)
        assert isinstance(coverage_end, datetime)
        self.calls.append(("pro_bar", kwargs))
        key = {"D": "1d", "W": "1w", "60min": "60m"}[str(kwargs["freq"])]
        return self._bar_response(
            self._bars(key, market_key=str(kwargs["ts_code"])),
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )

    def stock_basic(self, **kwargs: object) -> object:
        self.calls.append(("stock_basic", kwargs))
        if self.instrument_exception is not None:
            raise self.instrument_exception
        return self._table(list(self.fixture["instruments"]))

    def trade_cal(self, **kwargs: object) -> object:
        self.calls.append(("trade_cal", kwargs))
        if self.calendar_exception is not None:
            raise self.calendar_exception
        calendars = self.fixture["calendar"]
        assert isinstance(calendars, dict)
        return self._table(list(calendars[str(kwargs["exchange"])]))


class FakeAkShareClient(FixtureClient):
    def stock_zh_a_hist(self, **kwargs: object) -> object:
        coverage_start = kwargs.pop("_coverage_start")
        coverage_end = kwargs.pop("_coverage_end")
        assert isinstance(coverage_start, datetime)
        assert isinstance(coverage_end, datetime)
        self.calls.append(("stock_zh_a_hist", kwargs))
        key = {"daily": "1d", "weekly": "1w"}[str(kwargs["period"])]
        return self._bar_response(
            self._bars(key, market_key=str(kwargs["symbol"])),
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )

    def stock_zh_a_hist_min_em(self, **kwargs: object) -> object:
        coverage_start = kwargs.pop("_coverage_start")
        coverage_end = kwargs.pop("_coverage_end")
        assert isinstance(coverage_start, datetime)
        assert isinstance(coverage_end, datetime)
        self.calls.append(("stock_zh_a_hist_min_em", kwargs))
        return self._bar_response(
            self._bars("60m"),
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )

    def stock_info_a_code_name(self) -> object:
        self.calls.append(("stock_info_a_code_name", {}))
        if self.instrument_exception is not None:
            raise self.instrument_exception
        return self._table(list(self.fixture["instruments"]))

    def stock_zh_index_spot_sina(self) -> object:
        self.calls.append(("stock_zh_index_spot_sina", {}))
        if self.instrument_exception is not None:
            raise self.instrument_exception
        return self._table(list(self.fixture["indices"]))

    def stock_zh_index_daily(self, **kwargs: object) -> object:
        coverage_start = kwargs.pop("_coverage_start")
        coverage_end = kwargs.pop("_coverage_end")
        assert isinstance(coverage_start, datetime)
        assert isinstance(coverage_end, datetime)
        self.calls.append(("stock_zh_index_daily", kwargs))
        return self._bar_response(
            self._table(list(self.fixture["index_bars"])),
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )

    def tool_trade_date_hist_sina(self, **kwargs: object) -> object:
        self.calls.append(("tool_trade_date_hist_sina", kwargs))
        if self.calendar_exception is not None:
            raise self.calendar_exception
        return self._table(list(self.fixture["calendar"]))


class FakeBaoStockClient(FixtureClient):
    def query_history_k_data_plus(self, **kwargs: object) -> object:
        coverage_start = kwargs.pop("_coverage_start")
        coverage_end = kwargs.pop("_coverage_end")
        assert isinstance(coverage_start, datetime)
        assert isinstance(coverage_end, datetime)
        self.calls.append(("query_history_k_data_plus", kwargs))
        key = {"d": "1d", "w": "1w", "60": "60m"}[str(kwargs["frequency"])]
        return self._bar_response(
            self._bars(key, market_key=str(kwargs["code"])),
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )

    def query_stock_basic(self, **kwargs: object) -> object:
        self.calls.append(("query_stock_basic", kwargs))
        if self.instrument_exception is not None:
            raise self.instrument_exception
        return self._table(list(self.fixture["instruments"]))

    def query_trade_dates(self, **kwargs: object) -> object:
        self.calls.append(("query_trade_dates", kwargs))
        if self.calendar_exception is not None:
            raise self.calendar_exception
        return self._table(list(self.fixture["calendar"]))


@dataclass(frozen=True)
class ProviderCase:
    source: ProviderId
    provider_module: str
    provider_class: str
    client_type: type[FixtureClient]
    fixture_name: str

    @property
    def provider_type(self) -> type[object]:
        module = importlib.import_module(self.provider_module)
        value = getattr(module, self.provider_class)
        assert isinstance(value, type)
        return value

    def build(
        self,
        *,
        table_style: str = "list",
        clock: Callable[[], datetime] = lambda: FETCHED_AT,
    ) -> tuple[object, FixtureClient]:
        client = self.client_type(
            load_fixture(self.fixture_name), table_style=table_style
        )
        return self.provider_type(client=client, clock=clock), client


PROVIDER_CASES = (
    ProviderCase(
        ProviderId.TUSHARE,
        "stock_desk.market.providers.tushare",
        "TushareProvider",
        FakeTushareClient,
        "tushare",
    ),
    ProviderCase(
        ProviderId.AKSHARE,
        "stock_desk.market.providers.akshare",
        "AkShareProvider",
        FakeAkShareClient,
        "akshare",
    ),
    ProviderCase(
        ProviderId.BAOSTOCK,
        "stock_desk.market.providers.baostock",
        "BaoStockProvider",
        FakeBaoStockClient,
        "baostock",
    ),
)


@pytest.fixture(params=PROVIDER_CASES, ids=lambda case: case.source.value)
def provider_case(request: pytest.FixtureRequest) -> ProviderCase:
    assert isinstance(request.param, ProviderCase)
    return request.param
