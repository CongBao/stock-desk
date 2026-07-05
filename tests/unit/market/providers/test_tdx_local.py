from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import errno
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from stock_desk.market.providers.base import (
    MarketDataProvider,
    ProviderBatchFailure,
)
from stock_desk.market.providers.normalization import dataset_version
from stock_desk.market.types import (
    Adjustment,
    BarFailure,
    BarQuery,
    BarResult,
    CapabilityState,
    Exchange,
    FailureReason,
    MarketCapability,
    Period,
    ProviderId,
    TradingStatus,
)

from tests.unit.market.providers.tdx_test_helpers import (
    FETCHED_AT,
    PROJECT_ROOT,
    SHANGHAI,
    bar_query,
    golden_payload,
    make_vipdoc_root,
    raw_record,
    tdx_local,
    write_tdx_file,
)


def test_tdx_provider_static_capabilities_and_unsupported_operations_do_not_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tdx_local()

    def unexpected_io(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(
            "static provider operations must not access the filesystem"
        )

    monkeypatch.setattr(module.os, "open", unexpected_io)
    monkeypatch.setattr(module.os, "scandir", unexpected_io)
    provider = module.TdxLocalProvider(
        root=tmp_path / "missing-vipdoc",
        clock=lambda: FETCHED_AT,
    )

    assert isinstance(provider, MarketDataProvider)
    assert provider.name is ProviderId.TDX_LOCAL
    report = provider.capabilities()
    assert report.source is ProviderId.TDX_LOCAL
    assert report.state is CapabilityState.AVAILABLE
    assert report.capabilities == frozenset({MarketCapability.BARS})
    assert report.available_periods == frozenset({Period.DAY})
    assert report.available_adjustments == frozenset({Adjustment.NONE})
    assert report.markets == frozenset({Exchange.SH, Exchange.SZ})
    assert report.data_cutoff is None
    assert {(gap.capability, gap.reason) for gap in report.gaps} == {
        (MarketCapability.INSTRUMENTS, FailureReason.UNSUPPORTED),
        (MarketCapability.TRADING_CALENDAR, FailureReason.UNSUPPORTED),
    }

    instruments = provider.fetch_instruments()
    calendar = provider.fetch_calendar(
        Exchange.SH,
        date(2024, 7, 1),
        date(2024, 7, 2),
    )

    assert isinstance(instruments, ProviderBatchFailure)
    assert instruments.reason is FailureReason.UNSUPPORTED
    assert isinstance(calendar, ProviderBatchFailure)
    assert calendar.reason is FailureReason.UNSUPPORTED


def test_tdx_preflight_reports_validated_markets_and_bounded_counts(
    tmp_path: Path,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    (root / "sh" / "lday" / "sh600000.day").write_bytes(raw_record())
    (root / "sz" / "lday" / "sz000001.day").write_bytes(raw_record())
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.preflight()

    assert isinstance(outcome, module.TdxInspectionSuccess)
    assert outcome.markets == frozenset({Exchange.SH, Exchange.SZ})
    assert outcome.file_counts == (
        module.TdxMarketFileCount(exchange=Exchange.SH, count=1),
        module.TdxMarketFileCount(exchange=Exchange.SZ, count=1),
    )
    assert outcome.detail == "TDX vipdoc layout validated"
    assert not hasattr(outcome, "data_cutoff")


@pytest.mark.parametrize(
    ("setup", "expected_reason"),
    [
        ("relative", FailureReason.INVALID_RESPONSE),
        ("missing", FailureReason.MISSING),
        ("non-directory", FailureReason.INVALID_RESPONSE),
        ("market-mismatch", FailureReason.CORRUPT),
    ],
)
def test_tdx_preflight_returns_safe_typed_layout_failures(
    tmp_path: Path,
    setup: str,
    expected_reason: FailureReason,
) -> None:
    module = tdx_local()
    if setup == "relative":
        root = Path("relative-vipdoc")
    elif setup == "missing":
        root = tmp_path / "missing-vipdoc"
    elif setup == "non-directory":
        root = tmp_path / "vipdoc"
        root.mkdir()
        (root / "sh").write_bytes(b"not a directory")
        (root / "sz" / "lday").mkdir(parents=True)
    else:
        root = make_vipdoc_root(tmp_path)
        (root / "sh" / "lday" / "sz000001.day").write_bytes(b"mismatch")
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.preflight()

    assert isinstance(outcome, module.TdxInspectionFailure)
    assert outcome.reason is expected_reason
    assert str(root) not in outcome.detail
    assert len(outcome.detail) <= 128


def test_tdx_preflight_rejects_directory_entry_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    (root / "sh" / "lday" / "sh600000.day").write_bytes(b"one")
    (root / "sh" / "lday" / "sh600001.day").write_bytes(b"two")
    monkeypatch.setattr(module, "MAX_DIRECTORY_ENTRIES", 1)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.preflight()

    assert isinstance(outcome, module.TdxInspectionFailure)
    assert outcome.reason is FailureReason.CORRUPT


def test_tdx_preflight_requires_at_least_one_valid_market_file(
    tmp_path: Path,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.preflight()

    assert isinstance(outcome, module.TdxInspectionFailure)
    assert outcome.reason is FailureReason.MISSING


def test_tdx_preflight_derives_markets_from_nonempty_file_counts(
    tmp_path: Path,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    (root / "sh" / "lday" / "sh600000.day").write_bytes(raw_record())
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.preflight()

    assert isinstance(outcome, module.TdxInspectionSuccess)
    assert outcome.markets == frozenset({Exchange.SH})
    assert outcome.file_counts == (
        module.TdxMarketFileCount(exchange=Exchange.SH, count=1),
        module.TdxMarketFileCount(exchange=Exchange.SZ, count=0),
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor race probe")
def test_tdx_preflight_never_follows_lday_replaced_by_external_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (external / "sh600000.day").write_bytes(raw_record())
    (root / "sz" / "lday" / "sz000001.day").write_bytes(raw_record())
    lday = root / "sh" / "lday"
    original_scandir = module.os.scandir
    replaced = False

    def replace_before_scan(path: object) -> object:
        nonlocal replaced
        if not replaced:
            replaced = True
            lday.rmdir()
            lday.symlink_to(external, target_is_directory=True)
        return original_scandir(path)

    monkeypatch.setattr(module.os, "scandir", replace_before_scan)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.preflight()

    assert replaced
    assert isinstance(outcome, module.TdxInspectionFailure)
    assert outcome.reason is FailureReason.TRANSIENT_FAILURE
    assert str(external) not in outcome.detail


@pytest.mark.parametrize(
    ("symbol", "expected_prices", "expected_volumes"),
    [
        (
            "600000.SH",
            (
                (Decimal("10"), Decimal("10.5"), Decimal("9.9"), Decimal("10.2")),
                (
                    Decimal("10.2"),
                    Decimal("10.8"),
                    Decimal("10.1"),
                    Decimal("10.7"),
                ),
            ),
            (1000, 0),
        ),
        (
            "000001.SZ",
            (
                (Decimal("5"), Decimal("5.5"), Decimal("4.9"), Decimal("5.2")),
                (Decimal("5.2"), Decimal("5.6"), Decimal("5.1"), Decimal("5.5")),
            ),
            (2**32 - 1, 200),
        ),
    ],
)
def test_tdx_fetches_exact_sh_sz_golden_bars(
    tmp_path: Path,
    symbol: str,
    expected_prices: tuple[tuple[Decimal, Decimal, Decimal, Decimal], ...],
    expected_volumes: tuple[int, ...],
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    write_tdx_file(root, symbol, golden_payload(symbol))
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)
    query = bar_query(symbol=symbol)

    outcome = provider.fetch_bars(query)

    assert isinstance(outcome, BarResult)
    assert outcome.query == query
    assert outcome.coverage_start == query.start
    assert outcome.coverage_end == query.end
    assert tuple(bar.timestamp for bar in outcome.bars) == (
        datetime(2024, 7, 1, tzinfo=SHANGHAI).astimezone(timezone.utc),
        datetime(2024, 7, 2, tzinfo=SHANGHAI).astimezone(timezone.utc),
    )
    assert (
        tuple((bar.open, bar.high, bar.low, bar.close) for bar in outcome.bars)
        == expected_prices
    )
    assert tuple(bar.volume for bar in outcome.bars) == expected_volumes
    assert {bar.status for bar in outcome.bars} == {TradingStatus.UNKNOWN}
    assert outcome.provenance.source is ProviderId.TDX_LOCAL
    assert outcome.provenance.fetched_at == FETCHED_AT
    assert outcome.provenance.data_cutoff == datetime(2024, 7, 2, 15, tzinfo=SHANGHAI)
    assert outcome.provenance.adjustment is Adjustment.NONE
    assert outcome.provenance.dataset_version == dataset_version(
        source=ProviderId.TDX_LOCAL,
        operation="bars",
        request={"query": query},
        data_cutoff=outcome.provenance.data_cutoff,
        items=outcome.bars,
    )


@pytest.mark.parametrize(
    ("query", "expected_reason"),
    [
        (bar_query(period=Period.WEEK), FailureReason.UNSUPPORTED),
        (bar_query(period=Period.MIN60), FailureReason.UNSUPPORTED),
        (
            bar_query(adjustment=Adjustment.QFQ),
            FailureReason.UNSUPPORTED,
        ),
        (
            bar_query(adjustment=Adjustment.HFQ),
            FailureReason.UNSUPPORTED,
        ),
        (bar_query(symbol="430001.BJ"), FailureReason.UNSUPPORTED),
    ],
)
def test_tdx_rejects_unsupported_queries_before_filesystem_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: BarQuery,
    expected_reason: FailureReason,
) -> None:
    module = tdx_local()

    def unexpected_io(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unsupported query must not access the filesystem")

    monkeypatch.setattr(module.os, "open", unexpected_io)
    provider = module.TdxLocalProvider(
        root=tmp_path / "missing-vipdoc",
        clock=lambda: FETCHED_AT,
    )

    outcome = provider.fetch_bars(query)

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is expected_reason


@pytest.mark.parametrize(
    ("start", "end", "expected_reason"),
    [
        (
            datetime(2024, 6, 30, 23, 59, 59, 999999, tzinfo=SHANGHAI),
            datetime(2024, 7, 3, tzinfo=SHANGHAI),
            FailureReason.MISSING,
        ),
        (
            datetime(2024, 7, 1, tzinfo=SHANGHAI),
            datetime(2024, 7, 3, 0, 0, 0, 1, tzinfo=SHANGHAI),
            FailureReason.MISSING,
        ),
        (
            datetime(2024, 7, 1, 12, tzinfo=SHANGHAI),
            datetime(2024, 7, 2, tzinfo=SHANGHAI),
            FailureReason.NO_DATA,
        ),
    ],
)
def test_tdx_fetch_enforces_coverage_and_covered_no_data(
    tmp_path: Path,
    start: datetime,
    end: datetime,
    expected_reason: FailureReason,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    write_tdx_file(root, "600000.SH", golden_payload("600000.SH"))
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(bar_query(start=start, end=end))

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is expected_reason


def test_tdx_fetch_accepts_exact_last_record_following_midnight(
    tmp_path: Path,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    write_tdx_file(root, "600000.SH", golden_payload("600000.SH"))
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(bar_query(end=datetime(2024, 7, 3, tzinfo=SHANGHAI)))

    assert isinstance(outcome, BarResult)
    assert len(outcome.bars) == 2


def test_tdx_fetch_preserves_interior_gaps_without_synthesis(
    tmp_path: Path,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    payload = raw_record(raw_date=20240701) + raw_record(raw_date=20240703)
    write_tdx_file(root, "600000.SH", payload)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(bar_query(end=datetime(2024, 7, 4, tzinfo=SHANGHAI)))

    assert isinstance(outcome, BarResult)
    assert tuple(bar.timestamp.astimezone(SHANGHAI).day for bar in outcome.bars) == (
        1,
        3,
    )


@pytest.mark.parametrize(
    ("payload", "expected_reason"),
    [
        (None, FailureReason.MISSING),
        (b"", FailureReason.NO_DATA),
    ],
)
def test_tdx_fetch_distinguishes_missing_and_empty_files(
    tmp_path: Path,
    payload: bytes | None,
    expected_reason: FailureReason,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    if payload is not None:
        write_tdx_file(root, "600000.SH", payload)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is expected_reason


def test_tdx_fetch_samples_clock_once_after_read_and_rejects_future_cutoff(
    tmp_path: Path,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    write_tdx_file(root, "600000.SH", golden_payload("600000.SH"))
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return datetime(2024, 7, 2, 14, tzinfo=SHANGHAI)

    provider = module.TdxLocalProvider(root=root, clock=clock)

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.MISSING
    assert calls == 1


def test_tdx_dataset_version_excludes_root_metadata_and_fetched_at(
    tmp_path: Path,
) -> None:
    module = tdx_local()
    roots = [make_vipdoc_root(tmp_path / name) for name in ("a", "b")]
    targets = [
        write_tdx_file(root, "600000.SH", golden_payload("600000.SH")) for root in roots
    ]
    targets[1].touch()
    providers = [
        module.TdxLocalProvider(root=roots[0], clock=lambda: FETCHED_AT),
        module.TdxLocalProvider(
            root=roots[1],
            clock=lambda: FETCHED_AT + timedelta(days=1),
        ),
    ]

    outcomes = [provider.fetch_bars(bar_query()) for provider in providers]

    assert all(isinstance(outcome, BarResult) for outcome in outcomes)
    assert (
        outcomes[0].provenance.dataset_version == outcomes[1].provenance.dataset_version
    )


def make_symlinked_vipdoc(
    tmp_path: Path,
    *,
    component: str,
    target_scope: str,
) -> Path:
    container = tmp_path / "container"
    container.mkdir()
    outside = tmp_path / "outside"
    if component == "root":
        target_base = (
            container / "inside-targets" if target_scope == "inside" else outside
        )
        actual = make_vipdoc_root(target_base)
        write_tdx_file(actual, "600000.SH", golden_payload("600000.SH"))
        configured = container / "vipdoc"
        configured.symlink_to(actual, target_is_directory=True)
        return configured

    root = make_vipdoc_root(container)
    target_base = root / "targets" if target_scope == "inside" else outside
    target_base.mkdir(parents=True)
    if component == "market":
        target = target_base / "market"
        (target / "lday").mkdir(parents=True)
        (target / "lday" / "sh600000.day").write_bytes(golden_payload("600000.SH"))
        (root / "sh" / "lday").rmdir()
        (root / "sh").rmdir()
        (root / "sh").symlink_to(target, target_is_directory=True)
    elif component == "lday":
        target = target_base / "lday"
        target.mkdir(parents=True)
        (target / "sh600000.day").write_bytes(golden_payload("600000.SH"))
        (root / "sh" / "lday").rmdir()
        (root / "sh" / "lday").symlink_to(target, target_is_directory=True)
    else:
        target = target_base / "sh600000.day"
        target.write_bytes(golden_payload("600000.SH"))
        (root / "sh" / "lday" / "sh600000.day").symlink_to(target)
    return root


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink security matrix")
@pytest.mark.parametrize("component", ["root", "market", "lday", "leaf"])
@pytest.mark.parametrize("target_scope", ["inside", "outside"])
def test_tdx_preflight_and_fetch_reject_every_symlink_component(
    tmp_path: Path,
    component: str,
    target_scope: str,
) -> None:
    module = tdx_local()
    root = make_symlinked_vipdoc(
        tmp_path,
        component=component,
        target_scope=target_scope,
    )
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    inspection = provider.preflight()
    outcome = provider.fetch_bars(bar_query())

    assert isinstance(inspection, module.TdxInspectionFailure)
    assert inspection.reason is FailureReason.INVALID_RESPONSE
    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE
    assert str(root) not in inspection.detail
    assert str(root) not in outcome.detail


@pytest.mark.skipif(os.name != "posix", reason="POSIX nonregular-file probe")
@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_tdx_fetch_rejects_nonregular_leaf_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    target = root / "sh" / "lday" / "sh600000.day"
    sockets: tuple[socket.socket, socket.socket] | None = None
    if kind == "fifo":
        os.mkfifo(target)
    else:
        sockets = socket.socketpair()
        original_open = module.os.open

        def open_socket(
            path: object,
            flags: int,
            *args: object,
            **kwargs: object,
        ) -> int:
            if path == "sh600000.day":
                assert sockets is not None
                return os.dup(sockets[0].fileno())
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(module.os, "open", open_socket)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)
    try:
        outcome = provider.fetch_bars(bar_query())
    finally:
        if sockets is not None:
            sockets[0].close()
            sockets[1].close()

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


def test_tdx_fetch_translates_permission_failure_without_path_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    write_tdx_file(root, "600000.SH", golden_payload("600000.SH"))
    original_open = module.os.open

    def denied(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == "sh600000.day":
            raise PermissionError(f"denied {root} token=TOP-SECRET")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", denied)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.PERMISSION_DENIED
    assert str(root) not in outcome.detail
    assert "TOP-SECRET" not in outcome.detail


@pytest.mark.parametrize("mutation", ["append", "truncate", "replace"])
def test_tdx_snapshot_persistent_mutation_retries_once_then_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    original_payload = golden_payload("600000.SH")
    target = write_tdx_file(root, "600000.SH", original_payload)
    original_read = module._read_exact
    calls = 0

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        payload = original_read(descriptor, size)
        if mutation == "append":
            with target.open("ab") as stream:
                stream.write(raw_record(raw_date=20240703 + calls))
        elif mutation == "truncate":
            target.write_bytes(
                original_payload[:32]
                if target.stat().st_size != 32
                else original_payload
            )
        else:
            replacement = target.with_suffix(f".replacement-{calls}")
            replacement.write_bytes(original_payload)
            os.replace(replacement, target)
        return payload

    monkeypatch.setattr(module, "_read_exact", mutate_after_read)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.TRANSIENT_FAILURE
    assert calls == 2


def test_tdx_snapshot_recovers_after_one_transient_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    target = write_tdx_file(root, "600000.SH", golden_payload("600000.SH"))
    original_read = module._read_exact
    calls = 0

    def append_once(descriptor: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        payload = original_read(descriptor, size)
        if calls == 1:
            with target.open("ab") as stream:
                stream.write(raw_record(raw_date=20240703))
        return payload

    monkeypatch.setattr(module, "_read_exact", append_once)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarResult)
    assert calls == 2


def test_tdx_snapshot_short_read_is_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    write_tdx_file(root, "600000.SH", golden_payload("600000.SH"))
    original_read = module._read_exact
    calls = 0

    def short_read(descriptor: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        return original_read(descriptor, size)[:-1]

    monkeypatch.setattr(module, "_read_exact", short_read)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.TRANSIENT_FAILURE
    assert calls == 2


def test_tdx_platform_without_secure_backend_fails_closed_without_path_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tdx_local()
    calls: list[str] = []

    def unexpected_open(*args: object, **kwargs: object) -> int:
        calls.append("open")
        raise AssertionError("unsupported platform must not open paths")

    def unexpected_scan(*args: object, **kwargs: object) -> object:
        calls.append("scandir")
        raise AssertionError("unsupported platform must not scan paths")

    monkeypatch.setattr(module, "_USE_POSIX_DESCRIPTOR_IO", False)
    monkeypatch.setattr(module, "_PLATFORM", "unsupported")
    monkeypatch.setattr(module.os, "open", unexpected_open)
    monkeypatch.setattr(module.os, "scandir", unexpected_scan)
    provider = module.TdxLocalProvider(
        root=tmp_path / "vipdoc",
        clock=lambda: FETCHED_AT,
    )

    inspection = provider.preflight()
    outcome = provider.fetch_bars(bar_query())

    assert isinstance(inspection, module.TdxInspectionFailure)
    assert inspection.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert calls == []


@pytest.mark.parametrize(
    "size",
    [0, 7, 33, 320_001],
    ids=["empty", "seven-bytes", "misaligned", "oversized"],
)
def test_tdx_preflight_rejects_structurally_invalid_day_file_sizes(
    tmp_path: Path,
    size: int,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    target = root / "sh" / "lday" / "sh600000.day"
    with target.open("wb") as stream:
        stream.truncate(size)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.preflight()

    assert isinstance(outcome, module.TdxInspectionFailure)
    assert outcome.reason is FailureReason.CORRUPT


@pytest.mark.skipif(os.name != "posix", reason="POSIX open policy probe")
def test_tdx_posix_open_calls_use_nofollow_directory_and_nonblock_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tdx_local()
    if not module._USE_POSIX_DESCRIPTOR_IO:
        pytest.skip("descriptor flags require POSIX openat mode")
    root = make_vipdoc_root(tmp_path)
    write_tdx_file(root, "600000.SH", golden_payload("600000.SH"))
    original_open = module.os.open
    calls: list[tuple[object, int, int | None]] = []

    def tracking_open(
        path: object,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        calls.append((path, flags, kwargs.get("dir_fd")))
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", tracking_open)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarResult)
    directory_calls = [call for call in calls if call[0] != "sh600000.day"]
    leaf_calls = [call for call in calls if call[0] == "sh600000.day"]
    assert directory_calls
    assert leaf_calls
    required_directory = os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    required_leaf = os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    assert all(
        flags & required_directory == required_directory
        for _, flags, _ in directory_calls
    )
    assert all(flags & required_leaf == required_leaf for _, flags, _ in leaf_calls)
    assert all(
        parent is not None
        for path, _, parent in directory_calls
        if path != os.fspath(root)
    )
    assert all(parent is not None for _, _, parent in leaf_calls)


@pytest.mark.skipif(os.name != "posix", reason="POSIX device probe")
def test_tdx_fetch_rejects_device_leaf_and_closes_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    original_open = module.os.open
    device = original_open("/dev/null", os.O_RDONLY)
    returned: list[int] = []

    def open_device(
        path: object,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        if path == "sh600000.day":
            descriptor = os.dup(device)
            returned.append(descriptor)
            return descriptor
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", open_device)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)
    try:
        outcome = provider.fetch_bars(bar_query())
    finally:
        os.close(device)

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE
    assert len(returned) == 1
    with pytest.raises(OSError):
        os.fstat(returned[0])


def test_tdx_preflight_maps_unknown_os_error_to_safe_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    original_open = module.os.open

    def unavailable(
        path: object,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        if path == os.fspath(root):
            raise OSError(errno.ENOSYS, f"unsupported {root} token=TOP-SECRET")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", unavailable)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.preflight()

    assert isinstance(outcome, module.TdxInspectionFailure)
    assert outcome.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert str(root) not in outcome.detail
    assert "TOP-SECRET" not in outcome.detail


@pytest.mark.parametrize("missing_leaf", [False, True])
def test_tdx_posix_descriptor_paths_close_every_open_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_leaf: bool,
) -> None:
    module = tdx_local()
    if not module._USE_POSIX_DESCRIPTOR_IO:
        pytest.skip("descriptor accounting requires POSIX openat mode")
    root = make_vipdoc_root(tmp_path)
    if not missing_leaf:
        write_tdx_file(root, "600000.SH", golden_payload("600000.SH"))
    original_open = module.os.open
    original_close = module.os.close
    opened: list[int] = []
    closed: list[int] = []

    def tracking_open(
        path: object,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def tracking_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(module.os, "open", tracking_open)
    monkeypatch.setattr(module.os, "close", tracking_close)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure if missing_leaf else BarResult)
    assert sorted(opened) == sorted(closed)


@pytest.mark.skipif(os.name != "posix", reason="POSIX fstat cleanup probe")
@pytest.mark.parametrize("target_kind", ["directory", "leaf"])
def test_tdx_posix_open_closes_descriptor_when_fstat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_kind: str,
) -> None:
    module = tdx_local()
    if not module._USE_POSIX_DESCRIPTOR_IO:
        pytest.skip("descriptor cleanup requires POSIX openat mode")
    root = make_vipdoc_root(tmp_path)
    write_tdx_file(root, "600000.SH", golden_payload("600000.SH"))
    original_open = module.os.open
    original_fstat = module.os.fstat
    target_descriptors: list[int] = []

    def tracking_open(
        path: object,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        if (target_kind == "directory" and path == os.fspath(root)) or (
            target_kind == "leaf" and path == "sh600000.day"
        ):
            target_descriptors.append(descriptor)
        return descriptor

    def failing_fstat(descriptor: int) -> os.stat_result:
        if descriptor in target_descriptors:
            raise OSError(errno.EIO, "injected fstat failure token=TOP-SECRET")
        return original_fstat(descriptor)

    monkeypatch.setattr(module.os, "open", tracking_open)
    monkeypatch.setattr(module.os, "fstat", failing_fstat)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = (
        provider.preflight()
        if target_kind == "directory"
        else provider.fetch_bars(bar_query())
    )

    assert target_descriptors
    if target_kind == "directory":
        assert isinstance(outcome, module.TdxInspectionFailure)
        assert outcome.reason is FailureReason.PROVIDER_UNAVAILABLE
    else:
        assert isinstance(outcome, BarFailure)
        assert outcome.reason is FailureReason.PROVIDER_UNAVAILABLE
    for descriptor in target_descriptors:
        with pytest.raises(OSError):
            original_fstat(descriptor)


@pytest.mark.skipif(os.name != "posix", reason="POSIX close cleanup probe")
@pytest.mark.parametrize("with_primary", [False, True])
def test_tdx_posix_close_attempts_every_descriptor_without_masking_primary(
    monkeypatch: pytest.MonkeyPatch,
    with_primary: bool,
) -> None:
    module = tdx_local()
    descriptors = os.pipe()
    original_close = module.os.close
    original_fstat = module.os.fstat
    close_calls: list[int] = []

    def flaky_close(descriptor: int) -> None:
        close_calls.append(descriptor)
        original_close(descriptor)
        if descriptor == descriptors[1]:
            raise OSError(errno.EIO, "injected close failure")

    def operation() -> None:
        if with_primary:
            try:
                raise RuntimeError("primary failure")
            finally:
                module._close_descriptors(descriptors)
        module._close_descriptors(descriptors)

    monkeypatch.setattr(module.os, "close", flaky_close)

    with pytest.raises(RuntimeError if with_primary else OSError):
        operation()

    assert close_calls == [descriptors[1], descriptors[0]]
    for descriptor in descriptors:
        with pytest.raises(OSError):
            original_fstat(descriptor)


def test_tdx_snapshot_leaf_replaced_by_symlink_remains_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    target = write_tdx_file(root, "600000.SH", golden_payload("600000.SH"))
    external = tmp_path / "external.day"
    external.write_bytes(golden_payload("600000.SH"))
    original_read = module._read_exact
    calls = 0

    def replace_with_symlink(descriptor: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        payload = original_read(descriptor, size)
        if calls == 1:
            target.unlink()
            target.symlink_to(external)
        return payload

    monkeypatch.setattr(module, "_read_exact", replace_with_symlink)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.TRANSIENT_FAILURE
    assert calls == 1


def test_tdx_preflight_translates_permission_without_os_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    original_open = module.os.open

    def denied(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == "sh":
            raise PermissionError(f"denied {root} token=TOP-SECRET")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", denied)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.preflight()

    assert isinstance(outcome, module.TdxInspectionFailure)
    assert outcome.reason is FailureReason.PERMISSION_DENIED
    assert str(root) not in outcome.detail
    assert "TOP-SECRET" not in outcome.detail


def test_tdx_success_samples_clock_exactly_once(tmp_path: Path) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    write_tdx_file(root, "600000.SH", golden_payload("600000.SH"))
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return FETCHED_AT

    provider = module.TdxLocalProvider(root=root, clock=clock)

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarResult)
    assert calls == 1


def test_tdx_corrupt_failure_detail_never_contains_path_os_text_or_bytes(
    tmp_path: Path,
) -> None:
    module = tdx_local()
    root = make_vipdoc_root(tmp_path)
    payload = b"token=TOP-SECRET raw bytes"
    write_tdx_file(root, "600000.SH", payload)
    provider = module.TdxLocalProvider(root=root, clock=lambda: FETCHED_AT)

    outcome = provider.fetch_bars(bar_query())

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.CORRUPT
    assert str(root) not in outcome.detail
    assert "TOP-SECRET" not in outcome.detail
    assert payload.hex() not in outcome.detail


def test_tdx_provider_import_has_no_scan_network_pandas_or_sdk_side_effects() -> None:
    script = """
import os
import socket
import sys

def fail(*args, **kwargs):
    raise AssertionError("import side effect")

os.open = fail
os.scandir = fail
socket.socket = fail
for name in ("pandas", "tushare", "akshare", "baostock"):
    sys.modules[name] = None
import stock_desk.market.providers.tdx_local
"""

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
