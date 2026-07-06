from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import threading
from typing import Iterator
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import event, text

from stock_desk.api.market import MarketServices
from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.market.instruments import InstrumentRepository
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository
from tests.integration.market.lake_read_test_helpers import corrupt_catalog
from tests.integration.market.lake_test_helpers import routed_daily_bars
from tests.integration.market.task6_test_helpers import instrument, routed_instruments


@dataclass(frozen=True)
class ApiContext:
    client: TestClient
    services: MarketServices
    tasks: TaskRepository


@contextmanager
def market_api(tmp_path: Path) -> Iterator[ApiContext]:
    database_url = f"sqlite:///{tmp_path / 'market-api.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    services = MarketServices(engine=engine, lake_root=(tmp_path / "market").resolve())
    tasks = TaskRepository(engine)
    settings = Settings(database_url=database_url, data_dir=tmp_path)
    try:
        with TestClient(
            create_app(
                settings,
                task_repository=tasks,
                market_services=services,
            )
        ) as client:
            yield ApiContext(client, services, tasks)
    finally:
        services.close()


def seed_instruments(repository: InstrumentRepository):
    return repository.ingest(
        routed_instruments(
            (
                instrument("000001.SZ", "平安银行"),
                instrument("600000.SH", "浦发银行"),
                instrument("600036.SH", "招商银行"),
            )
        )
    )


def test_instrument_search_and_detail_include_pinned_provenance(
    tmp_path: Path,
) -> None:
    with market_api(tmp_path) as context:
        manifest = seed_instruments(context.services.instruments)

        by_code = context.client.get(
            "/api/market/instruments",
            params={"q": "600000", "limit": 10},
        )
        by_name = context.client.get(
            "/api/market/instruments",
            params={"q": "浦发", "limit": 10},
        )
        detail = context.client.get("/api/market/instruments/600000.SH")
        missing = context.client.get("/api/market/instruments/999999.SH")

    assert by_code.status_code == 200
    assert [item["symbol"] for item in by_code.json()] == ["600000.SH"]
    assert [item["name"] for item in by_name.json()] == ["浦发银行"]
    body = detail.json()
    assert body["symbol"] == "600000.SH"
    assert body["name"] == "浦发银行"
    assert body["provenance"]["manifest_record_id"] == manifest.manifest_record_id
    assert body["provenance"]["dataset_version"] == manifest.dataset_version
    assert body["provenance"]["route_version"] == manifest.route_version
    assert body["provenance"]["routing_manifest"]["category"] == "instruments"
    assert missing.status_code == 404
    assert missing.json() == {"code": "not_found"}


def test_pool_list_detail_and_custom_crud_have_stable_errors(tmp_path: Path) -> None:
    with market_api(tmp_path) as context:
        seed_instruments(context.services.instruments)
        preset = context.services.pools.publish_full_a()
        existing = context.services.pools.create_custom(
            name="existing",
            symbols=("600036.SH", "600000.SH"),
        )

        listed = context.client.get("/api/market/pools")
        preset_detail = context.client.get(f"/api/market/pools/{preset.pool_id}")
        custom_detail = context.client.get(f"/api/market/pools/{existing.pool_id}")
        created_response = context.client.post(
            "/api/market/pools",
            json={"name": "created", "symbols": ["000001.SZ", "600000.SH"]},
        )
        invalid = context.client.post(
            "/api/market/pools",
            json={"name": "invalid", "symbols": ["bad", "999999.SH"]},
        )
        extra = context.client.post(
            "/api/market/pools",
            json={"name": "invalid", "symbols": ["600000.SH"], "extra": True},
        )
        created = created_response.json()
        updated = context.client.put(
            f"/api/market/pools/{created['pool_id']}",
            json={
                "expected_revision": 1,
                "name": "updated",
                "symbols": ["600036.SH"],
            },
        )
        stale = context.client.put(
            f"/api/market/pools/{created['pool_id']}",
            json={
                "expected_revision": 1,
                "name": "stale",
                "symbols": ["600000.SH"],
            },
        )
        preset_delete = context.client.delete(
            f"/api/market/pools/{preset.pool_id}",
            params={"expected_revision": 1},
        )
        deleted = context.client.delete(
            f"/api/market/pools/{created['pool_id']}",
            params={"expected_revision": 2},
        )

    assert listed.status_code == 200
    assert listed.json()["next_cursor"] is None
    assert {item["pool_id"] for item in listed.json()["items"]} == {
        preset.pool_id,
        existing.pool_id,
    }
    assert preset_detail.json()["kind"] == "preset"
    assert preset_detail.json()["members"][0]["symbol"] == "000001.SZ"
    assert custom_detail.json()["kind"] == "custom"
    assert [item["symbol"] for item in custom_detail.json()["members"]] == [
        "600036.SH",
        "600000.SH",
    ]
    assert custom_detail.json()["provenance"]["routing_manifest"]["category"] == (
        "instruments"
    )
    assert created_response.status_code == 201
    assert created["revision"] == 1
    assert invalid.status_code == 422
    assert invalid.json() == {
        "code": "invalid_pool_members",
        "issues": [
            {"ordinal": 0, "code": "invalid"},
            {"ordinal": 1, "code": "not_found"},
        ],
    }
    assert extra.status_code == 422
    assert extra.json() == {"code": "invalid_request", "issues": []}
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["name"] == "updated"
    assert stale.status_code == 409
    assert stale.json() == {"code": "revision_conflict"}
    assert preset_delete.status_code == 422
    assert preset_delete.json() == {"code": "preset_read_only", "issues": []}
    assert deleted.status_code == 204
    assert deleted.content == b""


def test_bars_are_cache_only_with_full_series_exact_and_stable_misses(
    tmp_path: Path,
) -> None:
    older = routed_daily_bars((date(2024, 1, 2),))
    newer = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    with market_api(tmp_path) as context:
        context.services.lake.write(older)
        context.services.lake.write(newer)

        latest = context.client.get(
            "/api/market/bars",
            params={"symbol": "600000.SH", "period": "1d", "adjustment": "qfq"},
        )
        exact = context.client.get(
            "/api/market/bars",
            params={
                "symbol": "600000.SH",
                "period": "1d",
                "adjustment": "qfq",
                "start": older.result.query.start.isoformat(),
                "end": older.result.query.end.isoformat(),
            },
        )
        one_bound = context.client.get(
            "/api/market/bars",
            params={
                "symbol": "600000.SH",
                "period": "1d",
                "adjustment": "qfq",
                "start": older.result.query.start.isoformat(),
            },
        )
        missing = context.client.get(
            "/api/market/bars",
            params={"symbol": "000001.SZ", "period": "1d", "adjustment": "qfq"},
        )

    assert latest.status_code == 200
    latest_body = latest.json()
    assert len(latest_body["bars"]) == 2
    assert latest_body["bars"][0]["open"] == "-2.125"
    assert latest_body["bars"][0]["timestamp"].endswith("Z")
    assert latest_body["coverage"]["start"].endswith("Z")
    assert latest_body["manifest_record_id"].startswith("sha256:")
    assert latest_body["dataset_version"] == newer.result.provenance.dataset_version
    assert latest_body["route_version"] == newer.manifest.route_version
    assert latest_body["routing_manifest"] == newer.manifest.model_dump(mode="json")
    assert latest_body["provenance"]["source"] == "tushare"
    assert latest_body["provenance"]["adjustment"] == "qfq"
    assert exact.status_code == 200
    assert len(exact.json()["bars"]) == 1
    assert one_bound.status_code == 422
    assert one_bound.json() == {"code": "invalid_request", "issues": []}
    assert missing.status_code == 404
    assert missing.json() == {"code": "not_found"}


def test_bars_map_newest_cache_corruption_to_generic_500(tmp_path: Path) -> None:
    routed = routed_daily_bars((date(2024, 1, 2),))
    with market_api(tmp_path) as context:
        stored = context.services.lake.write(routed)
        corrupt_catalog(
            context.services.engine,
            table="market_routing_manifest",
            sql=(
                "UPDATE market_routing_manifest SET route_version = ? "
                "WHERE manifest_record_id = ?"
            ),
            parameters=(f"sha256:{'0' * 64}", stored.manifest_record_id),
        )

        response = context.client.get(
            "/api/market/bars",
            params={"symbol": "600000.SH", "period": "1d", "adjustment": "qfq"},
        )

    assert response.status_code == 500
    assert response.json() == {"code": "internal_error"}
    assert stored.manifest_record_id not in response.text


def test_market_update_endpoint_creates_durable_task_and_existing_cancel_works(
    tmp_path: Path,
) -> None:
    payload = {
        "symbols": ["600000.SH", "000001.SZ"],
        "period": "1d",
        "adjustment": "qfq",
        "start": "2024-01-01T16:00:00Z",
        "end": "2024-01-03T16:00:00Z",
    }
    with market_api(tmp_path) as context:
        created = context.client.post("/api/market/updates", json=payload)
        body = created.json()
        cancelled = context.client.post(f"/api/tasks/{body['id']}/cancel")

    assert created.status_code == 201
    assert body["kind"] == "market.update"
    assert body["payload"] == payload
    assert body["status"] == "queued"
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_market_update_endpoint_rejects_over_two_million_bucket_work(
    tmp_path: Path,
) -> None:
    payload = {
        "symbols": [f"{index:06d}.SZ" for index in range(10_000)],
        "period": "1d",
        "adjustment": "qfq",
        "start": "2024-01-01T00:00:00Z",
        "end": "2024-07-20T00:00:00Z",
    }
    with market_api(tmp_path) as context:
        response = context.client.post("/api/market/updates", json=payload)

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request", "issues": []}


def test_market_api_requires_timezone_qualified_rfc3339_bounds(
    tmp_path: Path,
) -> None:
    base_update = {
        "symbols": ["600000.SH"],
        "period": "1d",
        "adjustment": "qfq",
        "start": "2024-01-01T00:00:00Z",
        "end": "2024-01-02T00:00:00Z",
    }
    invalid_update_bounds = (
        (1704067200, 1704153600),
        ("1704067200", "1704153600"),
        ("2024-01-01T00:00:00", "2024-01-02T00:00:00"),
        ("2024-01-01", "2024-01-02"),
    )
    invalid_bar_bounds = invalid_update_bounds[1:]
    routed = routed_daily_bars((date(2024, 1, 2),))
    shanghai = ZoneInfo("Asia/Shanghai")
    with market_api(tmp_path) as context:
        invalid_updates = [
            context.client.post(
                "/api/market/updates",
                json={**base_update, "start": start, "end": end},
            )
            for start, end in invalid_update_bounds
        ]
        invalid_bars = [
            context.client.get(
                "/api/market/bars",
                params={
                    "symbol": "600000.SH",
                    "period": "1d",
                    "adjustment": "qfq",
                    "start": start,
                    "end": end,
                },
            )
            for start, end in invalid_bar_bounds
        ]
        accepted_update = context.client.post(
            "/api/market/updates",
            json={
                **base_update,
                "start": "2024-01-01T08:00:00+08:00",
                "end": "2024-01-02T08:00:00+08:00",
            },
        )
        context.services.lake.write(routed)
        accepted_bars = context.client.get(
            "/api/market/bars",
            params={
                "symbol": routed.result.query.symbol,
                "period": routed.result.query.period.value,
                "adjustment": routed.result.query.adjustment.value,
                "start": routed.result.query.start.astimezone(shanghai).isoformat(),
                "end": routed.result.query.end.astimezone(shanghai).isoformat(),
            },
        )

    assert all(response.status_code == 422 for response in invalid_updates)
    assert all(response.status_code == 422 for response in invalid_bars)
    assert all(
        response.json() == {"code": "invalid_request", "issues": []}
        for response in (*invalid_updates, *invalid_bars)
    )
    assert accepted_update.status_code == 201
    assert accepted_update.json()["payload"]["start"] == "2024-01-01T00:00:00Z"
    assert accepted_bars.status_code == 200
    assert accepted_bars.json()["query"]["start"].endswith("Z")


def test_market_catalog_miss_and_invalid_query_use_stable_errors(
    tmp_path: Path,
) -> None:
    with market_api(tmp_path) as context:
        empty = context.client.get(
            "/api/market/instruments",
            params={"q": "浦发", "limit": 10},
        )
        invalid = context.client.get(
            "/api/market/instruments",
            params={"q": "", "limit": 101},
        )

    assert empty.status_code == 404
    assert empty.json() == {"code": "not_found"}
    assert invalid.status_code == 422
    assert invalid.json() == {"code": "invalid_request", "issues": []}


def test_pool_list_is_bounded_cursor_paged_and_shallow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with market_api(tmp_path) as context:
        seed_instruments(context.services.instruments)
        created_pools = []
        for index in range(120):
            created_pools.append(
                context.services.pools.create_custom(
                    name=f"pool-{index:03d}",
                    symbols=("600000.SH",),
                )
            )

        def reject_detail(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("pool list loaded detail data")

        monkeypatch.setattr(context.services.pools, "_load_custom", reject_detail)
        monkeypatch.setattr(
            context.services.instruments,
            "pinned_catalog",
            reject_detail,
        )
        first = context.client.get("/api/market/pools", params={"limit": 100})
        first_body = first.json()
        second = context.client.get(
            "/api/market/pools",
            params={"limit": 100, "cursor": first_body["next_cursor"]},
        )
        for pool in created_pools[:20]:
            context.services.pools.delete_custom(pool.pool_id, expected_revision=1)
        exact_page = context.client.get("/api/market/pools", params={"limit": 100})
        invalid = context.client.get(
            "/api/market/pools",
            params={"limit": 101, "cursor": "not-a-pool-id"},
        )

    assert first.status_code == 200
    assert len(first_body["items"]) == 100
    assert first_body["next_cursor"] == first_body["items"][-1]["pool_id"]
    assert second.status_code == 200
    assert len(second.json()["items"]) == 20
    assert second.json()["next_cursor"] is None
    assert len(exact_page.json()["items"]) == 100
    assert exact_page.json()["next_cursor"] is None
    first_ids = {item["pool_id"] for item in first_body["items"]}
    second_ids = {item["pool_id"] for item in second.json()["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert invalid.status_code == 422


def test_pool_list_shallow_path_avoids_detail_and_catalog_loaders(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with market_api(tmp_path) as context:
        seed_instruments(context.services.instruments)
        context.services.pools.publish_full_a()
        context.services.pools.create_custom(
            name="custom",
            symbols=("600000.SH",),
        )

        def reject_detail(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("pool list loaded detail or catalog items")

        monkeypatch.setattr(context.services.pools, "_load_preset", reject_detail)
        monkeypatch.setattr(context.services.pools, "_load_custom", reject_detail)
        monkeypatch.setattr(
            context.services.pools._instruments,
            "pinned_catalog",
            reject_detail,
        )
        monkeypatch.setattr(
            context.services.instruments,
            "pinned_catalog",
            reject_detail,
        )

        response = context.client.get("/api/market/pools")

    assert response.status_code == 200
    assert len(response.json()["items"]) == 2


def test_pool_list_maps_member_binding_corruption_to_generic_500(
    tmp_path: Path,
) -> None:
    with market_api(tmp_path) as context:
        seed_instruments(context.services.instruments)
        created = context.services.pools.create_custom(
            name="corrupt",
            symbols=("600000.SH", "000001.SZ"),
        )
        with context.services.engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM custom_pool_member "
                    "WHERE pool_id = :pool_id AND ordinal = 1"
                ),
                {"pool_id": created.pool_id},
            )

        response = context.client.get("/api/market/pools")

    assert response.status_code == 500
    assert response.json() == {"code": "internal_error"}


def test_pool_list_api_reads_one_snapshot_during_concurrent_update(
    tmp_path: Path,
) -> None:
    header_read = threading.Event()
    release_reader = threading.Event()
    transaction_states: list[bool] = []
    paused = False

    def pause_after_header(
        connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany: bool,
    ) -> None:
        nonlocal paused
        if (
            not paused
            and "FROM custom_pool" in statement
            and "custom_pool_member" not in statement
        ):
            paused = True
            transaction_states.append(
                bool(connection.connection.driver_connection.in_transaction)
            )
            header_read.set()
            assert release_reader.wait(timeout=5)

    with market_api(tmp_path) as context:
        seed_instruments(context.services.instruments)
        created = context.services.pools.create_custom(
            name="revision-one",
            symbols=("600000.SH",),
        )
        event.listen(
            context.services.engine, "after_cursor_execute", pause_after_header
        )
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                response_future = executor.submit(
                    context.client.get,
                    "/api/market/pools",
                )
                assert header_read.wait(timeout=5)
                updated = context.services.pools.update_custom(
                    created.pool_id,
                    expected_revision=1,
                    name="revision-two",
                    symbols=("000001.SZ",),
                )
                release_reader.set()
                response = response_future.result(timeout=5)
        finally:
            release_reader.set()
            event.remove(
                context.services.engine,
                "after_cursor_execute",
                pause_after_header,
            )

        released = context.services.pools.update_custom(
            created.pool_id,
            expected_revision=2,
            name="revision-three",
            symbols=("600000.SH",),
        )

    assert transaction_states == [True]
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["revision"] == 1
    assert item["name"] == "revision-one"
    assert updated.revision == 2
    assert released.revision == 3


def test_market_openapi_has_enforceable_success_request_and_error_schemas(
    tmp_path: Path,
) -> None:
    with market_api(tmp_path) as context:
        document = context.client.get("/openapi.json").json()

    paths = document["paths"]
    for path, method in (
        ("/api/market/instruments", "get"),
        ("/api/market/instruments/{symbol}", "get"),
        ("/api/market/pools", "get"),
        ("/api/market/pools/{pool_id}", "get"),
        ("/api/market/pools", "post"),
        ("/api/market/pools/{pool_id}", "put"),
        ("/api/market/bars", "get"),
        ("/api/market/updates", "post"),
    ):
        operation = paths[path][method]
        success = operation["responses"]["201" if method == "post" else "200"]
        assert success["content"]["application/json"]["schema"]
        for code in ("404", "409", "422", "500"):
            schema = operation["responses"][code]["content"]["application/json"][
                "schema"
            ]
            assert schema["$ref"].endswith("/MarketErrorResponse")

    update_schema = paths["/api/market/updates"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert update_schema["$ref"].endswith("/MarketUpdateRequestDTO")
    pool_request = document["components"]["schemas"]["CustomPoolCreateRequest"]
    symbols_schema = pool_request["properties"]["symbols"]
    assert symbols_schema["maxItems"] == 5_000
    assert symbols_schema["items"]["type"] == "string"
    assert symbols_schema["items"]["maxLength"] == 64
    assert "pattern" not in symbols_schema["items"]
    assert "minLength" not in symbols_schema["items"]
    assert all(
        "HTTPValidationError" not in str(paths[path][method]["responses"])
        for path, method in (
            ("/api/market/instruments", "get"),
            ("/api/market/instruments/{symbol}", "get"),
            ("/api/market/pools", "get"),
            ("/api/market/pools/{pool_id}", "get"),
            ("/api/market/pools", "post"),
            ("/api/market/pools/{pool_id}", "put"),
            ("/api/market/bars", "get"),
            ("/api/market/updates", "post"),
        )
    )
    assert (
        document["components"]["schemas"]["CachedBarsResponse"]["properties"]["bars"][
            "maxItems"
        ]
        == 100_000
    )


def test_pool_symbol_boundary_preserves_item_issues_and_rejects_oversize(
    tmp_path: Path,
) -> None:
    with market_api(tmp_path) as context:
        seed_instruments(context.services.instruments)
        item_issue = context.client.post(
            "/api/market/pools",
            json={"name": "invalid", "symbols": ["", "bad"]},
        )
        oversized = context.client.post(
            "/api/market/pools",
            json={"name": "invalid", "symbols": ["x" * 65]},
        )

    assert item_issue.status_code == 422
    assert item_issue.json() == {
        "code": "invalid_pool_members",
        "issues": [
            {"ordinal": 0, "code": "invalid"},
            {"ordinal": 1, "code": "invalid"},
        ],
    }
    assert oversized.status_code == 422
    assert oversized.json() == {"code": "invalid_request", "issues": []}
