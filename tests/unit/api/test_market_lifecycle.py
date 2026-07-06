from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event

import stock_desk.api.market as market_api
from stock_desk.api.market import MarketServices
from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.market.lake import MarketLake
from stock_desk.storage.database import create_engine_for_url, migrate


def test_health_and_task_routes_do_not_initialize_market_root(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database_path = data_dir / "stock-desk.db"
    application = create_app(
        Settings(
            data_dir=data_dir,
            database_url=f"sqlite:///{database_path}",
        )
    )

    assert not data_dir.exists()
    with TestClient(application) as client:
        assert client.get("/api/health").status_code == 200
        assert not data_dir.exists()
        assert client.get("/api/tasks").status_code == 200
        assert database_path.exists()
        assert not (data_dir / "market").exists()


def test_first_market_request_initializes_database_and_absolute_root(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    database_path = data_dir / "stock-desk.db"
    application = create_app(
        Settings(
            data_dir=data_dir,
            database_url=f"sqlite:///{database_path}",
        )
    )

    with TestClient(application) as client:
        response = client.get(
            "/api/market/instruments",
            params={"q": "浦发", "limit": 10},
        )

    assert response.status_code == 404
    assert database_path.exists()
    assert (data_dir / "market").is_dir()
    assert (data_dir / "market").is_absolute()


def test_concurrent_first_market_requests_initialize_services_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    database_path = data_dir / "stock-desk.db"
    calls: list[Path] = []
    call_lock = threading.Lock()
    original_open = MarketServices.open.__func__

    def counted_open(cls, *, database_url: str, lake_root: Path):
        with call_lock:
            calls.append(lake_root)
        return original_open(cls, database_url=database_url, lake_root=lake_root)

    monkeypatch.setattr(MarketServices, "open", classmethod(counted_open))
    application = create_app(
        Settings(
            data_dir=data_dir,
            database_url=f"sqlite:///{database_path}",
        )
    )
    barrier = threading.Barrier(2)

    with TestClient(application) as client:

        def request_market() -> int:
            barrier.wait(timeout=5)
            return client.get("/api/market/pools").status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = tuple(executor.map(lambda _index: request_market(), range(2)))

    assert statuses == (200, 200)
    assert calls == [(data_dir / "market").absolute()]


def test_market_services_partial_initialization_disposes_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'partial.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    disposals: list[bool] = []

    def fail_lake(_self, *, engine, root) -> None:
        raise RuntimeError("lake init failed")

    event.listen(engine, "engine_disposed", lambda _engine: disposals.append(True))
    monkeypatch.setattr(market_api, "create_engine_for_url", lambda _url: engine)
    monkeypatch.setattr(MarketLake, "__init__", fail_lake)

    with pytest.raises(RuntimeError, match="lake init failed"):
        MarketServices.open(
            database_url=database_url,
            lake_root=(tmp_path / "market").resolve(),
        )

    assert disposals == [True]


def test_owned_market_services_close_once_and_injected_services_are_not_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned_url = f"sqlite:///{tmp_path / 'owned.db'}"
    migrate(owned_url)
    owned_engine = create_engine_for_url(owned_url)
    owned = MarketServices(
        engine=owned_engine,
        lake_root=(tmp_path / "owned-market").resolve(),
    )
    owned_closes: list[bool] = []
    monkeypatch.setattr(owned, "close", lambda: owned_closes.append(True))
    monkeypatch.setattr(
        MarketServices,
        "open",
        classmethod(lambda _cls, **_kwargs: owned),
    )
    with TestClient(
        create_app(Settings(database_url=owned_url, data_dir=tmp_path))
    ) as client:
        assert client.get("/api/market/pools").status_code == 200
    assert owned_closes == [True]
    owned_engine.dispose()

    injected_url = f"sqlite:///{tmp_path / 'injected.db'}"
    migrate(injected_url)
    injected_engine = create_engine_for_url(injected_url)
    injected = MarketServices(
        engine=injected_engine,
        lake_root=(tmp_path / "injected-market").resolve(),
    )
    injected_disposals: list[bool] = []
    event.listen(
        injected_engine,
        "engine_disposed",
        lambda _engine: injected_disposals.append(True),
    )
    try:
        with TestClient(create_app(market_services=injected)) as client:
            assert client.get("/api/market/pools").status_code == 200
        assert injected_disposals == []
    finally:
        injected.close()


def test_static_spa_does_not_capture_unknown_market_api(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>spa</h1>", encoding="utf-8")

    with TestClient(
        create_app(Settings(web_dist_dir=dist), market_services=None)
    ) as client:
        response = client.get("/api/market/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Not Found"}
