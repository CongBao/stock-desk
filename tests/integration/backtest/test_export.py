from __future__ import annotations

import csv
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import insert, update

from stock_desk.backtest.export import stream_export
from stock_desk.backtest.models import (
    BacktestFailureRow,
    BacktestGroupMetricRow,
    BacktestLogRow,
    BacktestRunRow,
    BacktestSymbolRow,
    BacktestTradeRow,
)
from stock_desk.backtest.repository import BacktestRepository
from stock_desk.backtest.repository import (
    BacktestConflict,
    BacktestRepositoryError,
    _encode_cursor,
)
from stock_desk.backtest.service import BacktestService
from stock_desk.api.backtests import BacktestServices
from stock_desk.api.market import MarketServices
from stock_desk.config import Settings
from stock_desk.backtest.snapshot import freeze_request
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import FormulaPreviewValidationError, FormulaService
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.instruments import InstrumentRepository
from stock_desk.market.lake import MarketLake
from stock_desk.market.pools import PoolRepository
from stock_desk.market.types import Adjustment
from stock_desk.main import create_app
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository
from tests.integration.backtest.test_single_run import MACD, _intent, _status
from tests.integration.market.lake_test_helpers import routed_daily_bars
from tests.integration.market.task6_test_helpers import instrument, routed_instruments
from tests.unit.backtest.test_config import _pinned, _request


RUN_ID = "11111111-1111-1111-1111-111111111111"
TASK_ID = "22222222-2222-2222-2222-222222222222"
FINISHED = datetime(2024, 2, 3, 4, 5, 6, 123456, tzinfo=timezone.utc)


def _completed_repository(
    tmp_path: Path, *, complete: bool = True
) -> BacktestRepository:
    url = f"sqlite:///{tmp_path / 'export.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    repository = BacktestRepository(engine)
    tasks = TaskRepository(engine)
    snapshot = freeze_request(_request())
    with engine.begin() as connection:
        tasks.enqueue_in_transaction(
            connection,
            "backtest.run",
            {"run_id": RUN_ID, "snapshot_id": snapshot.snapshot_id},
            task_id=TASK_ID,
            now=FINISHED,
        )
        repository.create_in_transaction(
            connection,
            run_id=RUN_ID,
            task_id=TASK_ID,
            snapshot=snapshot,
            now=FINISHED,
        )
        connection.execute(
            insert(BacktestTradeRow),
            [
                {
                    "run_id": RUN_ID,
                    "symbol": "600000.SH",
                    "ordinal": ordinal,
                    "realized": realized,
                    "payload_json": {
                        "symbol": "600000.SH",
                        "net_pnl": "-10.25",
                        "net_return": None,
                        "entry_fill_at": "2024-01-04T01:30:00Z",
                        "price_basis_convention": r"C:\private\data.csv",
                        "open_pnl_convention": r"\\server\share\secret.csv",
                        "unicode_path": "/用户/私密.txt",
                        "embedded_path": "open(/Users/Bao/a.txt)",
                        "file_uri": "file:///Users/Bao/a.txt",
                        "device_path": r"\\?\C:\private\device.txt",
                        "sizing_version": "ordinary C: relative text",
                        "unsafe_token": "TOP-SECRET-token=/private/data",
                    },
                }
                for ordinal, realized in ((0, True), (1, False))
            ],
        )
        connection.execute(
            insert(BacktestGroupMetricRow),
            [
                {
                    "run_id": RUN_ID,
                    "dimension": "symbol",
                    "group_key": key,
                    "payload_json": {
                        "win_rate": "0.5",
                        "unsafe_token": "TOP-SECRET",
                        "error": "open(/用户/私密.txt)",
                    },
                }
                for key in (
                    '  =HYPERLINK("https://evil")',
                    " +cmd",
                    " -cmd",
                    " @cmd",
                    "\tcmd",
                    "\rcmd",
                    "\n=cmd",
                    "\v=cmd",
                    "\f=cmd",
                )
            ],
        )
        connection.execute(
            insert(BacktestFailureRow),
            {
                "run_id": RUN_ID,
                "symbol": "600000.SH",
                "ordinal": 0,
                "reason": "missing_signal_data",
                "detail_json": {"raw_error": "TOP-SECRET"},
            },
        )
        connection.execute(
            insert(BacktestLogRow),
            {
                "run_id": RUN_ID,
                "ordinal": 0,
                "level": "info",
                "message": "run_completed",
                "detail_json": {"status": "succeeded", "token": "TOP-SECRET"},
            },
        )
        if complete:
            connection.execute(
                update(BacktestSymbolRow)
                .where(BacktestSymbolRow.run_id == RUN_ID)
                .values(status="succeeded", updated_at=FINISHED)
            )
            connection.execute(
                update(BacktestRunRow)
                .where(BacktestRunRow.id == RUN_ID)
                .values(
                    status="succeeded",
                    stage="completed",
                    processed=1,
                    finished_at=FINISHED,
                    updated_at=FINISHED,
                )
            )
    return repository


def _bytes(repository: BacktestRepository, section: str, format_: str) -> bytes:
    return b"".join(stream_export(repository, RUN_ID, section=section, format=format_))


def test_json_export_is_byte_stable_exact_and_secret_safe(tmp_path: Path) -> None:
    repository = _completed_repository(tmp_path)

    first = _bytes(repository, "trades", "json")
    second = _bytes(repository, "trades", "json")

    assert first == second
    assert b'"generated_at":"2024-02-03T04:05:06.123456Z"' in first
    assert b'"net_pnl":"-10.25"' in first
    assert b'"net_return":null' in first
    assert b"TOP-SECRET" not in first
    assert b"/private/data" not in first
    assert b"C:\\\\private" not in first
    assert b"server\\\\share" not in first
    assert b"ordinary C: relative text" in first
    assert "/用户/私密.txt" not in first.decode("utf-8")
    assert b"/Users/Bao" not in first
    assert b"file:///" not in first
    assert b"device.txt" not in first
    assert b"NaN" not in first
    metadata = json.loads(first)["metadata"]
    assert metadata["formula_version_id"] == "macd-v1"
    assert metadata["formula_checksum"] == "sha256:" + "b" * 64
    assert metadata["formula_engine_version"] == "formula-engine-v1"
    assert metadata["compatibility_version"] == "tdx-v1"
    assert metadata["period"] == "1d"
    assert metadata["adjustment"] == "qfq"
    assert metadata["instrument_dataset_version"] == "sha256:" + "a" * 64
    assert metadata["symbol_count"] == metadata["runnable_count"] == 1
    assert metadata["gap_count"] == 0
    assert metadata["signal_source_ids"] == ["tushare"]
    assert metadata["execution_source_ids"] == ["akshare"]
    assert metadata["status_source_ids"] == ["tdx_local"]
    assert metadata["provenance_digest"].startswith("sha256:")
    assert metadata["backtest_engine_version"] == "backtest-engine-v1"
    assert metadata["execution_rules_version"] == "a-share-v1"
    assert metadata["cost_model_version"] == "a-share-cost-v1"
    assert metadata["sizing_version"] == "fixed-lot-v1"
    assert metadata["warmup_policy_version"] == "formula-warmup-v1"
    assert metadata["quantity_shares"] == 1000
    assert metadata["commission_bps"] == "2.5"
    assert metadata["minimum_commission"] == "5"
    assert metadata["sell_tax_bps"] == "5"
    assert metadata["slippage_bps"] == "3"
    assert "formula_source" not in metadata


def test_csv_export_has_metadata_null_and_spreadsheet_safe_text(tmp_path: Path) -> None:
    repository = _completed_repository(tmp_path)

    payload = _bytes(repository, "groups", "csv")
    text = payload.decode("utf-8")
    rows = list(csv.DictReader(StringIO(text)))

    assert not payload.startswith(b"\xef\xbb\xbf")
    assert "\r\n" not in text
    assert rows[0]["record_type"] == "metadata"
    assert rows[0]["snapshot_id"].startswith("sha256:")
    assert rows[0]["formula_version_id"] == "macd-v1"
    assert rows[0]["formula_checksum"] == "sha256:" + "b" * 64
    assert rows[0]["signal_source_ids"] == json.dumps(
        ["tushare"], separators=(",", ":")
    )
    assert rows[0]["quantity_shares"] == "1000"
    assert rows[0]["commission_bps"] == "2.5"
    assert rows[0]["key"] == r"\N"
    assert all(row["record_type"] == "data" for row in rows[1:])
    assert all(row["key"].startswith("'") for row in rows[1:])
    assert "independent trade samples, not portfolio return" in text


def test_every_export_section_drops_private_failure_and_log_detail(
    tmp_path: Path,
) -> None:
    repository = _completed_repository(tmp_path)

    payloads = [
        _bytes(repository, section, format_)
        for section in ("trades", "open", "groups", "failures", "logs")
        for format_ in ("json", "csv")
    ]

    assert all(b"TOP-SECRET" not in payload for payload in payloads)
    assert all(b"raw_error" not in payload for payload in payloads)


def test_repository_report_pages_and_cursors_use_public_snapshots(
    tmp_path: Path,
) -> None:
    repository = _completed_repository(tmp_path)

    overview = repository.get_overview(RUN_ID)
    listed = repository.list_runs_page(limit=1, cursor=None)
    report = repository.report(RUN_ID)
    groups = repository.page(RUN_ID, collection="groups", limit=100, cursor=None)
    trades = repository.page(RUN_ID, collection="trades", limit=100, cursor=None)
    opened = repository.page(RUN_ID, collection="open", limit=100, cursor=None)
    failures = repository.page(RUN_ID, collection="failures", limit=100, cursor=None)
    logs = repository.page(RUN_ID, collection="logs", limit=100, cursor=None)
    symbols = repository.page(RUN_ID, collection="symbols", limit=100, cursor=None)

    assert overview.status == "succeeded"
    assert listed.items == (overview,)
    assert listed.next_cursor is None
    assert report.overview == overview
    assert report.formula_version_id == "macd-v1"
    assert report.instrument_dataset_version.startswith("sha256:")
    assert report.symbol_count == report.runnable_count == 1
    assert report.gap_count == 0
    assert len(groups.items) == 9
    assert len(trades.items) == 1
    assert len(opened.items) == 1
    assert len(failures.items) == len(logs.items) == 1
    assert logs.after_cursor
    assert len(symbols.items) == 1
    assert symbols.items[0].symbol == "600000.SH"

    with pytest.raises(BacktestConflict, match="cursor"):
        repository.page(
            RUN_ID,
            collection="trades",
            limit=100,
            cursor=logs.after_cursor,
        )
    with pytest.raises(BacktestConflict, match="cursor"):
        repository.page(
            RUN_ID,
            collection="symbols",
            limit=100,
            cursor=logs.after_cursor,
        )
    with pytest.raises(BacktestConflict, match="limit"):
        repository.list_runs_page(limit=101, cursor=None)


def test_group_pages_filter_in_sql_and_reject_cross_dimension_cursor(
    tmp_path: Path,
) -> None:
    repository = _completed_repository(tmp_path)

    symbol_page = repository.page(
        RUN_ID,
        collection="groups",
        limit=1,
        cursor=None,
        dimension="symbol",
    )
    month_page = repository.page(
        RUN_ID,
        collection="groups",
        limit=1,
        cursor=None,
        dimension="entry_month",
    )

    assert symbol_page.items
    assert all(item.dimension == "symbol" for item in symbol_page.items)
    assert month_page.items == ()
    assert symbol_page.next_cursor is not None
    with pytest.raises(BacktestConflict, match="cursor"):
        repository.page(
            RUN_ID,
            collection="groups",
            limit=1,
            cursor=symbol_page.next_cursor,
            dimension="entry_month",
        )


def test_cursor_rejects_oversized_integer_before_sql_execution(tmp_path: Path) -> None:
    repository = _completed_repository(tmp_path)
    forged = _encode_cursor(
        "trades",
        RUN_ID,
        [10**100, 0, "600000.SH"],
    )

    with pytest.raises(BacktestConflict, match="cursor"):
        repository.page(
            RUN_ID,
            collection="trades",
            limit=1,
            cursor=forged,
        )


def test_report_rejects_impossible_succeeded_gap_outcome(tmp_path: Path) -> None:
    repository = _completed_repository(tmp_path)
    with repository._engine.begin() as connection:  # noqa: SLF001 - corruption regression
        connection.exec_driver_sql("DROP TRIGGER trg_backtest_symbol_terminal_update")
        connection.execute(
            update(BacktestSymbolRow)
            .where(BacktestSymbolRow.run_id == RUN_ID)
            .values(input_kind="gap")
        )

    with pytest.raises(BacktestRepositoryError, match="outcome counts"):
        repository.report(RUN_ID)


def test_filtered_group_cursor_key_must_match_requested_dimension(
    tmp_path: Path,
) -> None:
    repository = _completed_repository(tmp_path)
    forged = _encode_cursor(
        "groups:symbol",
        RUN_ID,
        ["entry_month", ""],
    )

    with pytest.raises(BacktestConflict, match="cursor"):
        repository.page(
            RUN_ID,
            collection="groups",
            limit=1,
            cursor=forged,
            dimension="symbol",
        )


def test_paginated_failure_and_log_details_are_allowlisted_and_secret_safe(
    tmp_path: Path,
) -> None:
    repository = _completed_repository(tmp_path)
    services = SimpleNamespace(
        page=lambda run_id, *, collection, limit, cursor: repository.page(
            run_id, collection=collection, limit=limit, cursor=cursor
        )
    )

    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        failures = client.get(f"/api/backtests/{RUN_ID}/failures")
        logs = client.get(f"/api/backtests/{RUN_ID}/logs")

    assert failures.status_code == logs.status_code == 200
    rendered = failures.text + logs.text
    assert "TOP-SECRET" not in rendered
    assert "raw_error" not in rendered
    assert '"token"' not in rendered
    assert failures.json()["items"][0]["detail"] == {}
    assert logs.json()["items"][0]["detail"] == {"status": "succeeded"}


def test_paginated_group_trade_and_open_payloads_share_secret_safe_policy(
    tmp_path: Path,
) -> None:
    repository = _completed_repository(tmp_path)
    services = SimpleNamespace(
        page=lambda run_id, *, collection, limit, cursor: repository.page(
            run_id, collection=collection, limit=limit, cursor=cursor
        )
    )

    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        responses = [
            client.get(f"/api/backtests/{RUN_ID}/{collection}")
            for collection in ("groups", "trades", "open")
        ]

    assert all(response.status_code == 200 for response in responses)
    encoded = json.dumps(
        [response.json() for response in responses], ensure_ascii=False
    )
    assert "TOP-SECRET" not in encoded
    assert "unsafe_token" not in encoded
    assert "/用户/私密.txt" not in encoded
    assert "C:\\private" not in encoded
    assert "server\\share" not in encoded
    assert "device.txt" not in encoded
    assert "ordinary C: relative text" in encoded


def test_symbols_page_reads_strict_persisted_json_and_corruption_is_storage_error(
    tmp_path: Path,
) -> None:
    repository = _completed_repository(tmp_path)
    services = SimpleNamespace(
        page=lambda run_id, *, collection, limit, cursor: repository.page(
            run_id, collection=collection, limit=limit, cursor=cursor
        )
    )

    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        healthy = client.get(f"/api/backtests/{RUN_ID}/symbols")

    assert healthy.status_code == 200
    item = healthy.json()["items"][0]
    assert item["symbol"] == "600000.SH"
    assert item["input_kind"] == "runnable"
    assert item["provenance"]["signal_source"] == "tushare"

    bad_root = tmp_path / "bad"
    bad_root.mkdir()
    bad_repository = _completed_repository(bad_root, complete=False)
    with bad_repository._engine.begin() as connection:  # noqa: SLF001 - corrupt-storage regression
        connection.execute(
            update(BacktestSymbolRow)
            .where(BacktestSymbolRow.run_id == RUN_ID)
            .values(reference_json={"symbol": "600000.SH"})
        )
    bad_services = SimpleNamespace(
        page=lambda run_id, *, collection, limit, cursor: bad_repository.page(
            run_id, collection=collection, limit=limit, cursor=cursor
        )
    )
    with TestClient(create_app(backtest_services=bad_services)) as client:  # type: ignore[arg-type]
        corrupt = client.get(f"/api/backtests/{RUN_ID}/symbols")

    assert corrupt.status_code == 503
    assert corrupt.json() == {"code": "storage_unavailable"}


def test_running_trade_cursor_advances_by_frozen_symbol_ordinal(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'append.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    repository = BacktestRepository(engine)
    tasks = TaskRepository(engine)
    snapshot = freeze_request(
        _request(
            scope_kind="preset",
            scope_id="preset:test",
            scope_revision_or_snapshot_id="sha256:" + "9" * 64,
            symbols=("600000.SH", "000001.SZ"),
            symbol_inputs=(_pinned("600000.SH"), _pinned("000001.SZ")),
        )
    )
    now = datetime(2024, 2, 1, tzinfo=timezone.utc)
    with engine.begin() as connection:
        tasks.enqueue_in_transaction(
            connection,
            "backtest.run",
            {"run_id": RUN_ID, "snapshot_id": snapshot.snapshot_id},
            task_id=TASK_ID,
            now=now,
        )
        repository.create_in_transaction(
            connection,
            run_id=RUN_ID,
            task_id=TASK_ID,
            snapshot=snapshot,
            now=now,
        )
        connection.execute(
            insert(BacktestTradeRow),
            {
                "run_id": RUN_ID,
                "symbol": "600000.SH",
                "ordinal": 0,
                "realized": True,
                "payload_json": {"net_pnl": "1"},
            },
        )

    first = repository.page(RUN_ID, collection="trades", limit=1, cursor=None)
    assert tuple(item.symbol for item in first.items) == ("600000.SH",)
    assert first.after_cursor is not None

    with engine.begin() as connection:
        connection.execute(
            insert(BacktestTradeRow),
            {
                "run_id": RUN_ID,
                "symbol": "000001.SZ",
                "ordinal": 0,
                "realized": True,
                "payload_json": {"net_pnl": "2"},
            },
        )
    continuation = repository.page(
        RUN_ID, collection="trades", limit=100, cursor=first.after_cursor
    )

    assert tuple(item.symbol for item in continuation.items) == ("000001.SZ",)
    assert continuation.next_cursor is None
    engine.dispose()


def test_injected_backtest_storage_mismatch_fails_before_route_execution(
    tmp_path: Path,
) -> None:
    app_url = f"sqlite:///{tmp_path / 'app.db'}"
    backtest_url = f"sqlite:///{tmp_path / 'backtest.db'}"
    migrate(app_url)
    migrate(backtest_url)
    app_market = MarketServices(
        engine=create_engine_for_url(app_url),
        lake_root=(tmp_path / "app-market").resolve(),
    )
    app_tasks = TaskRepository(app_market.engine)
    app_formula = FormulaService(
        repository=FormulaRepository(app_market.engine), lake=app_market.lake
    )
    backtest_engine = create_engine_for_url(backtest_url)
    backtest_market = MarketLake(
        engine=backtest_engine, root=(tmp_path / "backtest-market").resolve()
    )
    backtest_tasks = TaskRepository(backtest_engine)
    backtest_repository = BacktestRepository(backtest_engine)
    backtest_formula = FormulaService(
        repository=FormulaRepository(backtest_engine), lake=backtest_market
    )
    backtest_service = BacktestService(
        engine=backtest_engine,
        tasks=backtest_tasks,
        repository=backtest_repository,
        market_lake=backtest_market,
        status_lake=ExecutionStatusLake(backtest_engine),
        instruments=InstrumentRepository(backtest_engine),
        pools=PoolRepository(backtest_engine),
        formulas=backtest_formula,
    )
    injected = BacktestServices(
        service=backtest_service,
        repository=backtest_repository,
        tasks=backtest_tasks,
    )
    try:
        with TestClient(
            create_app(
                Settings(database_url=app_url, data_dir=tmp_path),
                task_repository=app_tasks,
                market_services=app_market,
                formula_service=app_formula,
                backtest_services=injected,
            )
        ) as client:
            response = client.get("/api/backtests")
    finally:
        app_market.close()
        backtest_engine.dispose()

    assert response.status_code == 503
    assert response.json() == {"code": "storage_unavailable"}
    assert str(tmp_path) not in response.text


def test_lazy_backtest_dependency_storage_mismatch_is_fixed_503(tmp_path: Path) -> None:
    app_url = f"sqlite:///{tmp_path / 'shared-app.db'}"
    task_url = f"sqlite:///{tmp_path / 'foreign-tasks.db'}"
    migrate(app_url)
    migrate(task_url)
    market = MarketServices(
        engine=create_engine_for_url(app_url),
        lake_root=(tmp_path / "shared-market").resolve(),
    )
    formula = FormulaService(
        repository=FormulaRepository(market.engine), lake=market.lake
    )
    foreign_engine = create_engine_for_url(task_url)
    foreign_tasks = TaskRepository(foreign_engine)
    try:
        with TestClient(
            create_app(
                Settings(database_url=app_url, data_dir=tmp_path),
                task_repository=foreign_tasks,
                market_services=market,
                formula_service=formula,
            ),
            raise_server_exceptions=False,
        ) as client:
            response = client.get("/api/backtests")
    finally:
        market.close()
        foreign_engine.dispose()

    assert response.status_code == 503
    assert response.json() == {"code": "storage_unavailable"}
    assert str(tmp_path) not in response.text


def test_preflight_is_read_only_and_matches_immediate_atomic_submit(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'preflight.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    market = MarketLake(engine=engine, root=(tmp_path / "market").resolve())
    statuses = ExecutionStatusLake(engine)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    tasks = TaskRepository(engine)
    formulas = FormulaRepository(engine)
    repository = BacktestRepository(engine)
    service = BacktestService(
        engine=engine,
        tasks=tasks,
        repository=repository,
        market_lake=market,
        status_lake=statuses,
        instruments=instruments,
        pools=pools,
        formulas=FormulaService(repository=formulas, lake=market),
    )
    try:
        instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
        market.write(
            routed_daily_bars(
                tuple(
                    datetime(2024, 1, day, tzinfo=timezone.utc).date()
                    for day in range(2, 7)
                ),
                adjustment=Adjustment.NONE,
            )
        )
        statuses.write(
            _status(
                "600000.SH",
                datetime(2024, 1, 2).date(),
                datetime(2024, 1, 7).date(),
            )
        )
        valid = formulas.create("MACD", "trading", MACD, {}, placement="subchart")
        invalid = formulas.create(
            "Indicator", "indicator", "X:MA(C,5);", {}, placement="subchart"
        )

        with pytest.raises(FormulaPreviewValidationError):
            service.preflight(_intent(invalid.id))
        assert repository.list_run_ids() == ()
        assert tasks.list_recent() == []

        preview = service.preflight(_intent(valid.id))
        assert repository.list_run_ids() == ()
        assert tasks.list_recent() == []
        submitted = service.submit(_intent(valid.id))

        assert preview.reservation is False
        assert preview.preview_snapshot_id == submitted.snapshot_id
        assert preview.total == preview.runnable == 1
        assert preview.gap_count == 0
        assert preview.gap_sample == ()
        assert preview.pinned_signal_count == 1
        assert preview.pinned_execution_count == 1
        assert preview.pinned_status_count == 1
        assert preview.estimated_formula_rows == 5
        assert repository.list_run_ids() == (submitted.run_id,)
        assert tasks.get(submitted.task_id).payload == {
            "run_id": submitted.run_id,
            "snapshot_id": submitted.snapshot_id,
        }

        exact = service.copy(submitted.run_id, mode="exact")
        latest = service.copy(submitted.run_id, mode="latest")
        assert exact.snapshot_id == submitted.snapshot_id
        assert latest.run_id not in {submitted.run_id, exact.run_id}
        assert len(repository.list_run_ids()) == 3

        cancelled = service.cancel(exact.run_id)
        assert cancelled.run_id == exact.run_id
        assert tasks.get(exact.task_id).status == "cancelled"
        assert repository.get_overview(exact.run_id).status == "cancelled"
    finally:
        engine.dispose()


def test_log_after_cursor_observes_new_rows_without_rescanning_or_duplicates(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'log-tail.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    repository = BacktestRepository(engine)
    tasks = TaskRepository(engine)
    snapshot = freeze_request(_request())
    with engine.begin() as connection:
        tasks.enqueue_in_transaction(
            connection,
            "backtest.run",
            {"run_id": RUN_ID, "snapshot_id": snapshot.snapshot_id},
            task_id=TASK_ID,
            now=FINISHED,
        )
        repository.create_in_transaction(
            connection,
            run_id=RUN_ID,
            task_id=TASK_ID,
            snapshot=snapshot,
            now=FINISHED,
        )
        connection.execute(
            insert(BacktestLogRow),
            [
                {
                    "run_id": RUN_ID,
                    "ordinal": ordinal,
                    "level": "info",
                    "message": "symbol_checkpointed",
                    "detail_json": {"symbol": "600000.SH"},
                }
                for ordinal in range(60)
            ],
        )

    services = SimpleNamespace(
        page=lambda run_id, *, collection, limit, cursor: repository.page(
            run_id, collection=collection, limit=limit, cursor=cursor
        )
    )
    try:
        with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
            first = client.get(f"/api/backtests/{RUN_ID}/logs", params={"limit": 50})
            second = client.get(
                f"/api/backtests/{RUN_ID}/logs",
                params={"limit": 50, "cursor": first.json()["next_cursor"]},
            )
            tail = second.json()["after_cursor"]
            with engine.begin() as connection:
                connection.execute(
                    insert(BacktestLogRow),
                    {
                        "run_id": RUN_ID,
                        "ordinal": 60,
                        "level": "info",
                        "message": "symbol_checkpointed",
                        "detail_json": {"symbol": "600000.SH"},
                    },
                )
            appended = client.get(
                f"/api/backtests/{RUN_ID}/logs",
                params={"limit": 50, "after_cursor": tail},
            )

        assert [item["ordinal"] for item in first.json()["items"]] == list(range(50))
        assert [item["ordinal"] for item in second.json()["items"]] == list(
            range(50, 60)
        )
        assert second.json()["next_cursor"] is None
        assert tail
        assert [item["ordinal"] for item in appended.json()["items"]] == [60]
        assert appended.json()["after_cursor"] != tail
    finally:
        engine.dispose()
