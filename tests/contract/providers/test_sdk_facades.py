from __future__ import annotations

from datetime import date, datetime
import importlib
from pathlib import Path
from types import SimpleNamespace
import tomllib
from typing import Callable
from zoneinfo import ZoneInfo

import pytest
from requests.exceptions import Timeout as RequestsTimeout
from urllib3.exceptions import TimeoutError as Urllib3Timeout

from stock_desk.market.execution_status import (
    ExecutionStatusEvidenceLevel,
    ExecutionStatusQuery,
    ExecutionStatusSnapshot,
    SuspensionState,
)
from stock_desk.market.providers.akshare import AkShareProvider
from stock_desk.market.providers.baostock import BaoStockProvider
from stock_desk.market.providers.base import (
    ProviderBatch,
    ProviderBatchFailure,
    ProviderPermissionDenied,
    ProviderTimeout,
    ProviderUnavailable,
)
from stock_desk.market.providers.sdk import inclusive_date_chunks
from stock_desk.market.providers.execution_status import ExecutionStatusFailure
from stock_desk.market.providers.tushare import TushareProvider
from stock_desk.market.types import (
    Adjustment,
    BarFailure,
    BarQuery,
    BarResult,
    Exchange,
    FailureReason,
    InstrumentKind,
    Period,
    ProviderId,
)
from tests.contract.providers.conftest import (
    FETCHED_AT,
    SECRET_SENTINEL,
    FakeBaoStockClient,
    FakeCursor,
    load_fixture,
)


ROOT = Path(__file__).resolve().parents[3]
SHANGHAI = ZoneInfo("Asia/Shanghai")


def bar_query(
    *,
    period: Period = Period.DAY,
    start: datetime | None = None,
    end: datetime | None = None,
) -> BarQuery:
    if period is Period.WEEK:
        default_start = datetime(2024, 7, 1, tzinfo=SHANGHAI)
        default_end = datetime(2024, 7, 8, tzinfo=SHANGHAI)
    elif period is Period.MIN60:
        default_start = datetime(2024, 7, 1, 9, 30, tzinfo=SHANGHAI)
        default_end = datetime(2024, 7, 1, 15, tzinfo=SHANGHAI)
    else:
        default_start = datetime(2024, 7, 1, tzinfo=SHANGHAI)
        default_end = datetime(2024, 7, 3, tzinfo=SHANGHAI)
    return BarQuery(
        symbol="600000.SH",
        period=period,
        adjustment=Adjustment.NONE,
        start=start or default_start,
        end=end or default_end,
    )


class FakeTusharePro:
    def __init__(self, fixture: dict[str, object]) -> None:
        self.fixture = fixture
        self.stock_basic_calls: list[dict[str, object]] = []
        self.trade_cal_calls: list[dict[str, object]] = []

    def stock_basic(self, **kwargs: object) -> object:
        self.stock_basic_calls.append(kwargs)
        if kwargs["list_status"] == "L":
            return self.fixture["instruments"]
        return []

    def trade_cal(self, **kwargs: object) -> object:
        self.trade_cal_calls.append(kwargs)
        rows = [
            *self.fixture["calendar"][str(kwargs["exchange"])],
            {
                "exchange": kwargs["exchange"],
                "cal_date": "20240707",
                "is_open": "0",
            },
        ]
        return [
            row
            for row in rows
            if str(kwargs["start_date"])
            <= str(row["cal_date"])
            <= str(kwargs["end_date"])
        ]


class FakeTushareModule:
    def __init__(
        self,
        *,
        row_count: int | None = None,
        chunk_rows: list[list[dict[str, object]]] | None = None,
    ) -> None:
        self.fixture = load_fixture("tushare")
        self.pro = FakeTusharePro(self.fixture)
        self.row_count = row_count
        self.chunk_rows = chunk_rows
        self.pro_api_tokens: list[str] = []
        self.pro_bar_calls: list[dict[str, object]] = []
        self.set_token_calls: list[str] = []

    def set_token(self, token: str) -> None:
        self.set_token_calls.append(token)

    def pro_api(self, token: str) -> FakeTusharePro:
        self.pro_api_tokens.append(token)
        return self.pro

    def pro_bar(self, **kwargs: object) -> object:
        self.pro_bar_calls.append(kwargs)
        if self.chunk_rows is not None:
            return self.chunk_rows[len(self.pro_bar_calls) - 1]
        key = {"D": "1d", "W": "1w", "60min": "60m"}[str(kwargs["freq"])]
        rows = self.fixture["bars"][key]
        if self.row_count is None:
            return rows
        return [rows[0].copy() for _ in range(self.row_count)]


class FakeAkShareModule:
    def __init__(
        self,
        *,
        chunk_rows: list[list[dict[str, object]]] | None = None,
        bar_error: Exception | None = None,
        daily_rows: list[dict[str, object]] | None = None,
        trade_dates: list[dict[str, object]] | None = None,
    ) -> None:
        self.fixture = load_fixture("akshare")
        self.bar_calls: list[dict[str, object]] = []
        self.daily_calls: list[dict[str, object]] = []
        self.index_bar_calls: list[dict[str, object]] = []
        self.trade_date_calls = 0
        self.chunk_rows = chunk_rows
        self.bar_error = bar_error
        self.daily_rows = daily_rows
        self.trade_dates = trade_dates

    def stock_zh_a_hist(self, **kwargs: object) -> object:
        self.bar_calls.append(kwargs)
        if self.bar_error is not None:
            raise self.bar_error
        if self.chunk_rows is not None:
            return self.chunk_rows[len(self.bar_calls) - 1]
        key = {"daily": "1d", "weekly": "1w"}[str(kwargs["period"])]
        return self.fixture["bars"][key]

    def stock_zh_a_daily(self, **kwargs: object) -> object:
        self.daily_calls.append(kwargs)
        if self.daily_rows is not None:
            return self.daily_rows
        return [
            {
                "date": row["日期"],
                "open": row["开盘"],
                "high": row["最高"],
                "low": row["最低"],
                "close": row["收盘"],
                "volume": int(str(row["成交量"])) * 100,
            }
            for row in self.fixture["bars"]["1d"]
        ]

    def stock_info_a_code_name(self) -> object:
        return self.fixture["instruments"]

    def stock_zh_index_spot_sina(self) -> object:
        return self.fixture["indices"]

    def stock_zh_index_daily(self, **kwargs: object) -> object:
        self.index_bar_calls.append(kwargs)
        return self.fixture["index_bars"]

    def tool_trade_date_hist_sina(self) -> object:
        self.trade_date_calls += 1
        return self.trade_dates or self.fixture["calendar"]


class FakeBaoStockModule:
    def __init__(
        self,
        *,
        login_code: str = "0",
        chunk_rows: list[list[dict[str, object]]] | None = None,
    ) -> None:
        self.fixture = load_fixture("baostock")
        self.login_code = login_code
        self.login_calls = 0
        self.logout_calls = 0
        self.bar_calls: list[dict[str, object]] = []
        self.calendar_calls: list[dict[str, object]] = []
        self.chunk_rows = chunk_rows

    def login(self) -> object:
        self.login_calls += 1
        return SimpleNamespace(error_code=self.login_code, error_msg=SECRET_SENTINEL)

    def logout(self) -> object:
        self.logout_calls += 1
        return SimpleNamespace(error_code="0", error_msg="")

    def query_history_k_data_plus(self, **kwargs: object) -> object:
        self.bar_calls.append(kwargs)
        if self.chunk_rows is not None:
            return self.chunk_rows[len(self.bar_calls) - 1]
        key = {"d": "1d", "w": "1w", "60": "60m"}[str(kwargs["frequency"])]
        return self.fixture["bars"][key]

    def query_stock_basic(self, **kwargs: object) -> object:
        return self.fixture["instruments"]

    def query_trade_dates(self, **kwargs: object) -> object:
        self.calendar_calls.append(kwargs)
        rows = [
            *self.fixture["calendar"],
            {"calendar_date": "2024-07-07", "is_trading_day": "0"},
        ]
        return [
            row
            for row in rows
            if str(kwargs["start_date"])
            <= str(row["calendar_date"])
            <= str(kwargs["end_date"])
        ]


def install_fake_module(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    module: object,
) -> None:
    original = importlib.import_module

    def fake_import(requested: str) -> object:
        if requested == name:
            return module
        return original(requested)

    monkeypatch.setattr(importlib, "import_module", fake_import)


def raising(error: Exception) -> Callable[..., object]:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise error

    return fail


def install_tushare_catch_all_retry_wrapper(
    module: FakeTushareModule,
    attempts: tuple[object, ...],
) -> None:
    remaining = list(attempts)

    def endpoint(**_kwargs: object) -> object:
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    setattr(module.pro, "daily", endpoint)

    def pro_bar(**kwargs: object) -> object:
        retry_count = kwargs.get("retry_count")
        assert retry_count == 1
        for _ in range(retry_count):
            try:
                return getattr(kwargs["api"], "daily")()
            except Exception:
                continue
        raise OSError("ERROR.")

    module.pro_bar = pro_bar


class NativeTimeoutCursor(FakeCursor):
    def next(self) -> bool:
        if self._index >= 0:
            raise RequestsTimeout(SECRET_SENTINEL)
        return super().next()


def test_tushare_from_sdk_uses_token_scoped_api_and_merges_all_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeTushareModule()
    install_fake_module(monkeypatch, "tushare", module)

    provider = TushareProvider.from_sdk(
        token="scoped-token",
        clock=lambda: FETCHED_AT,
    )
    bars = provider.fetch_bars(bar_query())
    instruments = provider.fetch_instruments()

    assert isinstance(bars, BarResult)
    assert module.pro_api_tokens == ["scoped-token"]
    assert module.set_token_calls == []
    tracked_api = module.pro_bar_calls[0]["api"]
    assert tracked_api is not module.pro
    assert getattr(tracked_api, "fixture") is module.pro.fixture
    assert module.pro_bar_calls[0].get("retry_count") == 1
    assert [call["list_status"] for call in module.pro.stock_basic_calls] == [
        "L",
        "D",
        "P",
    ]
    assert instruments.provenance.source.value == "tushare"


def test_akshare_from_sdk_uses_module_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeAkShareModule()
    install_fake_module(monkeypatch, "akshare", module)

    provider = AkShareProvider.from_sdk(clock=lambda: FETCHED_AT)
    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarResult)
    assert len(module.bar_calls) == 1


def test_akshare_sdk_facade_uses_explicit_index_endpoint_and_provider_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeAkShareModule()
    install_fake_module(monkeypatch, "akshare", module)
    provider = AkShareProvider.from_sdk(clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(
        BarQuery(
            symbol="000001.SS",
            instrument_kind=InstrumentKind.INDEX,
            period=Period.DAY,
            adjustment=Adjustment.NONE,
            start=datetime(2024, 7, 1, tzinfo=SHANGHAI),
            end=datetime(2024, 7, 3, tzinfo=SHANGHAI),
        )
    )

    assert isinstance(outcome, BarResult)
    assert module.index_bar_calls == [{"symbol": "sh000001"}]
    assert module.bar_calls == []


def test_akshare_daily_bars_fall_back_to_sina_with_share_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeAkShareModule(
        bar_error=ConnectionError("eastmoney unavailable"),
        daily_rows=[
            {
                "date": "2024-07-01",
                "open": "10.00",
                "high": "10.50",
                "low": "9.90",
                "close": "10.20",
                "volume": "1234",
            },
            {
                "date": "2024-07-02",
                "open": "10.20",
                "high": "10.80",
                "low": "10.10",
                "close": "10.60",
                "volume": "5678",
            },
        ],
    )
    install_fake_module(monkeypatch, "akshare", module)
    provider = AkShareProvider.from_sdk(clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarResult)
    assert tuple(item.volume for item in outcome.bars) == (1234, 5678)
    assert module.daily_calls == [
        {
            "symbol": "sh600000",
            "start_date": "20240701",
            "end_date": "20240702",
            "adjust": "",
        }
    ]


def test_akshare_weekly_bars_do_not_use_daily_sina_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeAkShareModule(bar_error=ConnectionError("eastmoney unavailable"))
    install_fake_module(monkeypatch, "akshare", module)
    provider = AkShareProvider.from_sdk(clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(bar_query(period=Period.WEEK))

    assert isinstance(outcome, BarFailure)
    assert module.daily_calls == []


def test_akshare_materializes_basic_status_only_from_complete_sina_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeAkShareModule(
        daily_rows=[
            {
                "date": "2024-07-01",
                "open": "10.00",
                "high": "10.50",
                "low": "9.90",
                "close": "10.20",
                "volume": "1234",
            },
            {
                "date": "2024-07-02",
                "open": "10.20",
                "high": "10.80",
                "low": "10.10",
                "close": "10.60",
                "volume": "5678",
            },
        ],
        trade_dates=[
            {"trade_date": "2024-07-01"},
            {"trade_date": "2024-07-02"},
        ],
    )
    install_fake_module(monkeypatch, "akshare", module)
    provider = AkShareProvider.from_sdk(clock=lambda: FETCHED_AT)

    outcome = provider.fetch_execution_status(
        ExecutionStatusQuery(
            symbol="600000.SH",
            exchange=Exchange.SH,
            start=date(2024, 7, 1),
            end=date(2024, 7, 3),
        )
    )

    assert isinstance(outcome, ExecutionStatusSnapshot)
    assert outcome.source is ProviderId.AKSHARE
    assert outcome.evidence_level is ExecutionStatusEvidenceLevel.BASIC_NO_PRICE_LIMITS
    assert tuple(item.suspension_state for item in outcome.days) == (
        SuspensionState.NORMAL,
        SuspensionState.NORMAL,
    )
    assert len(outcome.eligibility) == 2
    assert all(item.evidence_complete for item in outcome.eligibility)
    assert module.trade_date_calls == 1


def test_akshare_basic_status_fails_closed_when_open_day_has_no_stock_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeAkShareModule(
        daily_rows=[
            {
                "date": "2024-07-01",
                "open": "10.00",
                "high": "10.50",
                "low": "9.90",
                "close": "10.20",
                "volume": "1234",
            }
        ],
        trade_dates=[
            {"trade_date": "2024-07-01"},
            {"trade_date": "2024-07-02"},
        ],
    )
    install_fake_module(monkeypatch, "akshare", module)
    provider = AkShareProvider.from_sdk(clock=lambda: FETCHED_AT)

    outcome = provider.fetch_execution_status(
        ExecutionStatusQuery(
            symbol="600000.SH",
            exchange=Exchange.SH,
            start=date(2024, 7, 1),
            end=date(2024, 7, 3),
        )
    )

    assert isinstance(outcome, ExecutionStatusFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("provider_name", "period"),
    [
        ("tushare", Period.DAY),
        ("tushare", Period.WEEK),
        ("tushare", Period.MIN60),
        ("akshare", Period.DAY),
        ("akshare", Period.WEEK),
        ("baostock", Period.DAY),
        ("baostock", Period.WEEK),
        ("baostock", Period.MIN60),
    ],
)
def test_sdk_facade_accepts_real_period_timestamp_schema(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    period: Period,
) -> None:
    if provider_name == "tushare":
        module = FakeTushareModule()
        install_fake_module(monkeypatch, "tushare", module)
        provider = TushareProvider.from_sdk(
            token="scoped-token", clock=lambda: FETCHED_AT
        )
    elif provider_name == "akshare":
        module = FakeAkShareModule()
        install_fake_module(monkeypatch, "akshare", module)
        provider = AkShareProvider.from_sdk(clock=lambda: FETCHED_AT)
    else:
        module = FakeBaoStockModule()
        install_fake_module(monkeypatch, "baostock", module)
        provider = BaoStockProvider.from_sdk(clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(bar_query(period=period))

    assert isinstance(outcome, BarResult)


@pytest.mark.parametrize("provider_name", ["tushare", "baostock"])
def test_sdk_minute_facade_rejects_real_schema_weekend_row(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    fixture = load_fixture(provider_name)
    row = fixture["bars"]["60m"][-1].copy()
    if provider_name == "tushare":
        row["trade_time"] = "2024-07-06 10:30:00"
        module = FakeTushareModule(chunk_rows=[[row]])
        install_fake_module(monkeypatch, "tushare", module)
        provider = TushareProvider.from_sdk(
            token="scoped-token", clock=lambda: FETCHED_AT
        )
    else:
        row["date"] = "2024-07-06"
        row["time"] = "20240706103000000"
        module = FakeBaoStockModule(chunk_rows=[[row]])
        install_fake_module(monkeypatch, "baostock", module)
        provider = BaoStockProvider.from_sdk(clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(
        bar_query(
            period=Period.MIN60,
            start=datetime(2024, 7, 6, 9, 30, tzinfo=SHANGHAI),
            end=datetime(2024, 7, 6, 15, tzinfo=SHANGHAI),
        )
    )

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            datetime(2024, 7, 1, tzinfo=SHANGHAI),
            datetime(2024, 7, 3, tzinfo=SHANGHAI),
            ((date(2024, 7, 1), date(2024, 7, 2)),),
        ),
        (
            datetime(2024, 7, 1, 9, 30, tzinfo=SHANGHAI),
            datetime(2024, 7, 1, 15, tzinfo=SHANGHAI),
            ((date(2024, 7, 1), date(2024, 7, 1)),),
        ),
        (
            datetime(2024, 7, 1, tzinfo=SHANGHAI),
            datetime(2024, 7, 2, 16, tzinfo=ZoneInfo("UTC")),
            ((date(2024, 7, 1), date(2024, 7, 2)),),
        ),
        (
            datetime(2020, 1, 1, tzinfo=SHANGHAI),
            datetime(2022, 1, 1, tzinfo=SHANGHAI),
            (
                (date(2020, 1, 1), date(2020, 12, 31)),
                (date(2021, 1, 1), date(2021, 12, 31)),
            ),
        ),
    ],
)
def test_sdk_chunks_derive_final_inclusive_date_from_local_exclusive_end(
    start: datetime,
    end: datetime,
    expected: tuple[tuple[date, date], ...],
) -> None:
    assert inclusive_date_chunks(start, end) == expected


@pytest.mark.parametrize("provider_name", ["tushare", "akshare", "baostock"])
def test_sdk_daily_facade_excludes_local_midnight_end_date(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    if provider_name == "tushare":
        module = FakeTushareModule()
        install_fake_module(monkeypatch, "tushare", module)
        provider = TushareProvider.from_sdk(
            token="scoped-token", clock=lambda: FETCHED_AT
        )
        calls = module.pro_bar_calls
        expected_end = "20240702"
    elif provider_name == "akshare":
        module = FakeAkShareModule()
        install_fake_module(monkeypatch, "akshare", module)
        provider = AkShareProvider.from_sdk(clock=lambda: FETCHED_AT)
        calls = module.bar_calls
        expected_end = "20240702"
    else:
        module = FakeBaoStockModule()
        install_fake_module(monkeypatch, "baostock", module)
        provider = BaoStockProvider.from_sdk(clock=lambda: FETCHED_AT)
        calls = module.bar_calls
        expected_end = "2024-07-02"

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarResult)
    assert calls[-1]["end_date"] == expected_end


def sdk_day_row(
    provider_name: str,
    raw_day: date,
) -> dict[str, object]:
    fixture = load_fixture(provider_name)
    row = fixture["bars"]["1d"][0].copy()
    if provider_name == "tushare":
        row["trade_date"] = raw_day.strftime("%Y%m%d")
    elif provider_name == "akshare":
        row["日期"] = raw_day.isoformat()
    else:
        row["date"] = raw_day.isoformat()
    return row


@pytest.mark.parametrize("provider_name", ["tushare", "akshare", "baostock"])
@pytest.mark.parametrize(
    ("scenario", "raw_days", "expected_type"),
    [
        (
            "wrong-chunks",
            (date(2021, 7, 1), date(2021, 7, 2), date(2021, 7, 5)),
            BarFailure,
        ),
        (
            "cross-chunk-duplicate",
            (date(2021, 7, 1), date(2021, 7, 1), date(2021, 7, 1)),
            BarFailure,
        ),
        (
            "valid-chunks",
            (date(2020, 7, 1), date(2021, 7, 1), date(2022, 7, 1)),
            BarResult,
        ),
    ],
)
def test_sdk_facade_validates_raw_rows_against_exact_chunk(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    scenario: str,
    raw_days: tuple[date, date, date],
    expected_type: type[BarFailure] | type[BarResult],
) -> None:
    chunks = [[sdk_day_row(provider_name, raw_day)] for raw_day in raw_days]
    if provider_name == "tushare":
        module = FakeTushareModule(chunk_rows=chunks)
        install_fake_module(monkeypatch, "tushare", module)
        provider = TushareProvider.from_sdk(
            token="scoped-token", clock=lambda: FETCHED_AT
        )
    elif provider_name == "akshare":
        module = FakeAkShareModule(chunk_rows=chunks)
        install_fake_module(monkeypatch, "akshare", module)
        provider = AkShareProvider.from_sdk(clock=lambda: FETCHED_AT)
    else:
        module = FakeBaoStockModule(chunk_rows=chunks)
        install_fake_module(monkeypatch, "baostock", module)
        provider = BaoStockProvider.from_sdk(clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(
        bar_query(
            start=datetime(2020, 1, 1, tzinfo=SHANGHAI),
            end=datetime(2023, 1, 1, tzinfo=SHANGHAI),
        )
    )

    assert isinstance(outcome, expected_type), scenario
    if isinstance(outcome, BarFailure):
        assert outcome.reason is FailureReason.INVALID_RESPONSE
    else:
        assert len(outcome.bars) == 3


def test_baostock_factory_owns_explicit_session_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeBaoStockModule()
    install_fake_module(monkeypatch, "baostock", module)

    provider = BaoStockProvider.from_sdk(clock=lambda: FETCHED_AT)
    outcome = provider.fetch_bars(bar_query())
    provider.close()
    provider.close()

    assert isinstance(outcome, BarResult)
    assert module.login_calls == 1
    assert module.logout_calls == 1


def test_baostock_regular_injected_construction_does_not_login() -> None:
    client = FakeBaoStockClient(load_fixture("baostock"))

    BaoStockProvider(client=client, clock=lambda: FETCHED_AT)

    assert client.calls == []


@pytest.mark.parametrize("provider_name", ["tushare", "baostock"])
def test_sdk_calendar_translates_exclusive_end_to_inclusive_upstream(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    if provider_name == "tushare":
        module = FakeTushareModule()
        install_fake_module(monkeypatch, "tushare", module)
        provider = TushareProvider.from_sdk(
            token="scoped-token",
            clock=lambda: FETCHED_AT,
        )
        calls = module.pro.trade_cal_calls
        expected_end = "20240706"
    else:
        module = FakeBaoStockModule()
        install_fake_module(monkeypatch, "baostock", module)
        provider = BaoStockProvider.from_sdk(clock=lambda: FETCHED_AT)
        calls = module.calendar_calls
        expected_end = "2024-07-06"

    outcome = provider.fetch_calendar(
        Exchange.SH,
        date(2024, 7, 1),
        date(2024, 7, 7),
    )

    assert isinstance(outcome, ProviderBatch)
    assert calls[0]["end_date"] == expected_end
    assert tuple(item.day for item in outcome.items) == tuple(
        date(2024, 7, day) for day in range(1, 7)
    )


@pytest.mark.parametrize(
    ("factory", "module_name"),
    [
        (
            lambda: TushareProvider.from_sdk(
                token="scoped-token",
                clock=lambda: FETCHED_AT,
            ),
            "tushare",
        ),
        (lambda: AkShareProvider.from_sdk(clock=lambda: FETCHED_AT), "akshare"),
        (lambda: BaoStockProvider.from_sdk(clock=lambda: FETCHED_AT), "baostock"),
    ],
)
def test_missing_sdk_is_a_safe_typed_unavailable_failure(
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[], object],
    module_name: str,
) -> None:
    original = importlib.import_module

    def missing(requested: str) -> object:
        if requested == module_name:
            raise ModuleNotFoundError(SECRET_SENTINEL)
        return original(requested)

    monkeypatch.setattr(importlib, "import_module", missing)

    with pytest.raises(ProviderUnavailable) as captured:
        factory()

    assert SECRET_SENTINEL not in str(captured.value)


def test_baostock_login_failure_is_safe_and_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeBaoStockModule(login_code="1")
    install_fake_module(monkeypatch, "baostock", module)

    with pytest.raises(ProviderPermissionDenied) as captured:
        BaoStockProvider.from_sdk(clock=lambda: FETCHED_AT)

    assert SECRET_SENTINEL not in str(captured.value)
    assert module.logout_calls == 0


@pytest.mark.parametrize(
    ("provider_name", "operation", "expected_reason"),
    [
        ("tushare", "bars", FailureReason.INVALID_RESPONSE),
        ("tushare", "instruments", FailureReason.TIMEOUT),
        ("tushare", "calendar", FailureReason.TIMEOUT),
        ("akshare", "bars", FailureReason.TIMEOUT),
        ("akshare", "instruments", FailureReason.TIMEOUT),
        ("baostock", "bars", FailureReason.TIMEOUT),
        ("baostock", "instruments", FailureReason.TIMEOUT),
        ("baostock", "calendar", FailureReason.TIMEOUT),
    ],
)
def test_real_requests_timeout_maps_to_typed_provider_outcome(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    operation: str,
    expected_reason: FailureReason,
) -> None:
    error = RequestsTimeout(SECRET_SENTINEL)
    if provider_name == "tushare":
        module = FakeTushareModule()
        install_fake_module(monkeypatch, "tushare", module)
        provider = TushareProvider.from_sdk(
            token="scoped-token",
            clock=lambda: FETCHED_AT,
        )
        target = module if operation == "bars" else module.pro
        method = {
            "bars": "pro_bar",
            "instruments": "stock_basic",
            "calendar": "trade_cal",
        }[operation]
    elif provider_name == "akshare":
        module = FakeAkShareModule()
        install_fake_module(monkeypatch, "akshare", module)
        provider = AkShareProvider.from_sdk(clock=lambda: FETCHED_AT)
        target = module
        method = {
            "bars": "stock_zh_a_hist",
            "instruments": "stock_info_a_code_name",
        }[operation]
        if operation == "bars":
            monkeypatch.setattr(module, "stock_zh_a_daily", raising(error))
    else:
        module = FakeBaoStockModule()
        install_fake_module(monkeypatch, "baostock", module)
        provider = BaoStockProvider.from_sdk(clock=lambda: FETCHED_AT)
        target = module
        method = {
            "bars": "query_history_k_data_plus",
            "instruments": "query_stock_basic",
            "calendar": "query_trade_dates",
        }[operation]
    monkeypatch.setattr(target, method, raising(error))

    if operation == "bars":
        outcome = provider.fetch_bars(bar_query())
        assert isinstance(outcome, BarFailure)
    elif operation == "instruments":
        outcome = provider.fetch_instruments()
        assert isinstance(outcome, ProviderBatchFailure)
    else:
        outcome = provider.fetch_calendar(
            Exchange.SH,
            date(2024, 7, 1),
            date(2024, 7, 7),
        )
        assert isinstance(outcome, ProviderBatchFailure)
    assert outcome.reason is expected_reason
    assert SECRET_SENTINEL not in outcome.detail


@pytest.mark.parametrize("provider_name", ["tushare", "baostock"])
def test_factory_native_timeout_is_not_misclassified_as_permission_failure(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    if provider_name == "tushare":
        module = FakeTushareModule()
        module.pro_api = raising(RequestsTimeout(SECRET_SENTINEL))
        install_fake_module(monkeypatch, "tushare", module)
    else:
        module = FakeBaoStockModule()
        module.login = raising(RequestsTimeout(SECRET_SENTINEL))
        install_fake_module(monkeypatch, "baostock", module)

    with pytest.raises(ProviderTimeout) as captured:
        if provider_name == "tushare":
            TushareProvider.from_sdk(
                token="scoped-token",
                clock=lambda: FETCHED_AT,
            )
        else:
            BaoStockProvider.from_sdk(clock=lambda: FETCHED_AT)

    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__


def test_baostock_logout_native_timeout_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeBaoStockModule()
    install_fake_module(monkeypatch, "baostock", module)
    provider = BaoStockProvider.from_sdk(clock=lambda: FETCHED_AT)
    module.logout = raising(Urllib3Timeout(SECRET_SENTINEL))

    with pytest.raises(ProviderTimeout):
        provider.close()


@pytest.mark.parametrize("operation", ["bars", "instruments", "calendar"])
def test_baostock_cursor_pagination_native_timeout_is_typed(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    module = FakeBaoStockModule()
    install_fake_module(monkeypatch, "baostock", module)
    if operation == "bars":
        rows = module.fixture["bars"]["1d"]
        method = "query_history_k_data_plus"
    elif operation == "instruments":
        rows = module.fixture["instruments"]
        method = "query_stock_basic"
    else:
        rows = module.fixture["calendar"]
        method = "query_trade_dates"
    monkeypatch.setattr(module, method, lambda **_kwargs: NativeTimeoutCursor(rows))
    provider = BaoStockProvider.from_sdk(clock=lambda: FETCHED_AT)

    if operation == "bars":
        outcome = provider.fetch_bars(bar_query())
        assert isinstance(outcome, BarFailure)
    elif operation == "instruments":
        outcome = provider.fetch_instruments()
        assert isinstance(outcome, ProviderBatchFailure)
    else:
        outcome = provider.fetch_calendar(
            Exchange.SH,
            date(2024, 7, 1),
            date(2024, 7, 7),
        )
        assert isinstance(outcome, ProviderBatchFailure)
    assert outcome.reason is FailureReason.TIMEOUT


def test_tushare_wrapper_timeout_cause_without_proxy_evidence_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeTushareModule()

    def wrapped_timeout(**_kwargs: object) -> object:
        try:
            raise RequestsTimeout(SECRET_SENTINEL)
        except RequestsTimeout as error:
            raise RuntimeError("SDK wrapper") from error

    module.pro_bar = wrapped_timeout
    install_fake_module(monkeypatch, "tushare", module)
    provider = TushareProvider.from_sdk(
        token="scoped-token",
        clock=lambda: FETCHED_AT,
    )

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


@pytest.mark.parametrize("error_type", [TimeoutError, ProviderTimeout])
def test_tushare_wrapper_timeout_type_without_proxy_evidence_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    module = FakeTushareModule()
    module.pro_bar = raising(error_type(SECRET_SENTINEL))
    install_fake_module(monkeypatch, "tushare", module)
    provider = TushareProvider.from_sdk(
        token="scoped-token",
        clock=lambda: FETCHED_AT,
    )

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE
    assert SECRET_SENTINEL not in outcome.detail


@pytest.mark.parametrize(
    ("attempt_types", "expected_reason"),
    [
        ((RequestsTimeout,), FailureReason.TIMEOUT),
        ((RuntimeError,), FailureReason.INVALID_RESPONSE),
    ],
    ids=["timeout", "ordinary-error"],
)
def test_tushare_catch_all_retry_wrapper_uses_underlying_failure_types(
    monkeypatch: pytest.MonkeyPatch,
    attempt_types: tuple[type[Exception], ...],
    expected_reason: FailureReason,
) -> None:
    module = FakeTushareModule()
    install_tushare_catch_all_retry_wrapper(
        module,
        tuple(error_type(SECRET_SENTINEL) for error_type in attempt_types),
    )
    install_fake_module(monkeypatch, "tushare", module)
    provider = TushareProvider.from_sdk(
        token="scoped-token",
        clock=lambda: FETCHED_AT,
    )

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is expected_reason


def test_tushare_tracking_proxy_redacts_failure_before_wrapper_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = FakeTushareModule()
    original = RequestsTimeout(SECRET_SENTINEL)
    original.__cause__ = RuntimeError(SECRET_SENTINEL)
    original.__context__ = RuntimeError(SECRET_SENTINEL)
    setattr(module.pro, "daily", raising(original))
    caught: list[Exception] = []

    def printing_wrapper(**kwargs: object) -> object:
        assert kwargs.get("retry_count") == 1
        try:
            getattr(kwargs["api"], "daily")()
        except Exception as error:
            caught.append(error)
            print(error)
        raise OSError("ERROR.")

    module.pro_bar = printing_wrapper
    install_fake_module(monkeypatch, "tushare", module)
    provider = TushareProvider.from_sdk(
        token="scoped-token",
        clock=lambda: FETCHED_AT,
    )

    outcome = provider.fetch_bars(bar_query())
    captured = capsys.readouterr()

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.TIMEOUT
    assert len(caught) == 1
    assert str(caught[0]) == ""
    assert caught[0].__cause__ is None
    assert caught[0].__context__ is None
    assert SECRET_SENTINEL not in captured.out
    assert SECRET_SENTINEL not in captured.err
    assert SECRET_SENTINEL not in repr(caught[0])
    assert SECRET_SENTINEL not in outcome.detail
    assert SECRET_SENTINEL not in repr(outcome)


@pytest.mark.parametrize(
    "attempt_types",
    [
        (RequestsTimeout, RequestsTimeout),
        (RequestsTimeout, RuntimeError),
    ],
    ids=["multiple-timeouts", "mixed-errors"],
)
def test_tushare_wrapper_requires_exactly_one_tracked_timeout(
    monkeypatch: pytest.MonkeyPatch,
    attempt_types: tuple[type[Exception], ...],
) -> None:
    module = FakeTushareModule()
    remaining = [error_type(SECRET_SENTINEL) for error_type in attempt_types]

    def endpoint(**_kwargs: object) -> object:
        raise remaining.pop(0)

    setattr(module.pro, "daily", endpoint)

    def wrapper_with_extra_calls(**kwargs: object) -> object:
        assert kwargs.get("retry_count") == 1
        for _ in attempt_types:
            try:
                getattr(kwargs["api"], "daily")()
            except Exception:
                continue
        raise OSError("ERROR.")

    module.pro_bar = wrapper_with_extra_calls
    install_fake_module(monkeypatch, "tushare", module)
    provider = TushareProvider.from_sdk(
        token="scoped-token",
        clock=lambda: FETCHED_AT,
    )

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


def test_tushare_wrapper_internal_failure_without_tracked_call_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeTushareModule()

    def wrapper_with_malformed_internal_response(**kwargs: object) -> object:
        assert kwargs.get("retry_count") == 1
        malformed: dict[str, object] = {}
        return malformed["bars"]

    module.pro_bar = wrapper_with_malformed_internal_response
    install_fake_module(monkeypatch, "tushare", module)
    provider = TushareProvider.from_sdk(
        token="scoped-token",
        clock=lambda: FETCHED_AT,
    )

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


def test_tushare_catch_all_retry_wrapper_ignores_failures_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeTushareModule()
    setattr(module.pro, "daily", raising(RequestsTimeout(SECRET_SENTINEL)))

    def successful_wrapper(**kwargs: object) -> object:
        assert kwargs.get("retry_count") == 1
        try:
            getattr(kwargs["api"], "daily")()
        except Exception:
            return module.fixture["bars"]["1d"]
        raise AssertionError("the underlying timeout was not raised")

    module.pro_bar = successful_wrapper
    install_fake_module(monkeypatch, "tushare", module)
    provider = TushareProvider.from_sdk(
        token="scoped-token",
        clock=lambda: FETCHED_AT,
    )

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarResult)


def test_timeout_named_runtime_error_is_still_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Timeout(RuntimeError):
        pass

    module = FakeTushareModule()
    module.pro_bar = raising(Timeout(SECRET_SENTINEL))
    install_fake_module(monkeypatch, "tushare", module)
    provider = TushareProvider.from_sdk(
        token="scoped-token",
        clock=lambda: FETCHED_AT,
    )

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


def test_sdk_facade_chunks_long_ranges_before_signing_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeTushareModule(row_count=0)
    install_fake_module(monkeypatch, "tushare", module)
    provider = TushareProvider.from_sdk(
        token="scoped-token",
        clock=lambda: FETCHED_AT,
    )

    outcome = provider.fetch_bars(
        bar_query(
            start=datetime(2020, 1, 1, tzinfo=SHANGHAI),
            end=datetime(2022, 1, 1, tzinfo=SHANGHAI),
        )
    )

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.NO_DATA
    assert len(module.pro_bar_calls) >= 2


def test_sdk_facade_rejects_nonempty_middle_with_empty_boundary_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = sdk_day_row("tushare", date(2021, 7, 1))
    module = FakeTushareModule(chunk_rows=[[], [row], []])
    install_fake_module(monkeypatch, "tushare", module)
    provider = TushareProvider.from_sdk(
        token="scoped-token",
        clock=lambda: FETCHED_AT,
    )

    outcome = provider.fetch_bars(
        bar_query(
            start=datetime(2020, 1, 1, tzinfo=SHANGHAI),
            end=datetime(2023, 1, 1, tzinfo=SHANGHAI),
        )
    )

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.MISSING
    assert len(module.pro_bar_calls) == 3


@pytest.mark.parametrize("row_count", [6000, 6001])
def test_tushare_sdk_facade_rejects_exact_or_over_provider_limit(
    monkeypatch: pytest.MonkeyPatch,
    row_count: int,
) -> None:
    module = FakeTushareModule(row_count=row_count)
    install_fake_module(monkeypatch, "tushare", module)
    provider = TushareProvider.from_sdk(
        token="scoped-token",
        clock=lambda: FETCHED_AT,
    )

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.MISSING


def test_sdk_facade_rejects_frequency_impossible_rows_below_provider_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeTushareModule(row_count=999)
    install_fake_module(monkeypatch, "tushare", module)
    provider = TushareProvider.from_sdk(
        token="scoped-token",
        clock=lambda: FETCHED_AT,
    )

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.MISSING


def test_nonempty_chunk_with_ordinary_session_gaps_is_not_pagination_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = FakeTushareModule()
    install_fake_module(monkeypatch, "tushare", module)
    provider = TushareProvider.from_sdk(
        token="scoped-token",
        clock=lambda: FETCHED_AT,
    )

    outcome = provider.fetch_bars(
        bar_query(
            start=datetime(2024, 7, 1, tzinfo=SHANGHAI),
            end=datetime(2024, 7, 8, tzinfo=SHANGHAI),
        )
    )

    assert isinstance(outcome, BarResult)
    assert len(outcome.bars) == 2


def test_provider_optional_extra_and_lock_have_exact_constraints() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["optional-dependencies"]["providers"] == [
        "tushare>=1.4.29,<2",
        "akshare>=1.18.64,<2",
        "baostock>=0.9.2,<1",
    ]
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "tushare"' in lock
    assert 'name = "akshare"' in lock
    assert 'name = "baostock"' in lock
