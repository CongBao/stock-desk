from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from stock_desk.api.market import MarketServices
from stock_desk.config import Settings
from stock_desk.desktop_session import DesktopSession, TAURI_WINDOWS_ORIGIN
from stock_desk.main import create_app
from stock_desk.onboarding.service import OnboardingService
from stock_desk.onboarding.store import OnboardingStateStore
from tests.unit.onboarding.test_service import _service


def test_welcome_state_does_not_eagerly_open_market_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_opened(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("market storage must remain lazy on the welcome step")

    monkeypatch.setattr(MarketServices, "open", fail_if_opened)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'lazy.db'}",
        data_dir=tmp_path / "data",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/onboarding/state")

    assert response.status_code == 200
    assert response.json()["current_step"] == "welcome"


def test_onboarding_api_uses_existing_desktop_origin_and_bearer_authority(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'api.db'}"
    settings = Settings(database_url=database_url, data_dir=tmp_path / "data")
    market = MarketServices.open(
        database_url=database_url,
        lake_root=(tmp_path / "market").resolve(),
    )
    service = OnboardingService(
        store=OnboardingStateStore((tmp_path / "state-v1.json").resolve()),
        market=market,
    )
    secret = "desktop-session-secret-that-is-long-enough"
    session = DesktopSession(
        origin=TAURI_WINDOWS_ORIGIN,
        secret=secret,
        host_version="1.1.0",
        frontend_version="1.1.0",
        sidecar_version="1.1.0",
        source_revision="a" * 40,
    )
    app = create_app(
        settings,
        market_services=market,
        onboarding_service=service,
        desktop_session=session,
    )
    try:
        with TestClient(app) as client:
            unauthorized = client.get("/api/v1/onboarding/state")
            forbidden = client.get(
                "/api/v1/onboarding/state",
                headers={"Authorization": f"Bearer {secret}"},
            )
            headers = {
                "Origin": TAURI_WINDOWS_ORIGIN,
                "Authorization": f"Bearer {secret}",
            }
            state = client.get("/api/v1/onboarding/state", headers=headers)
            sources = client.get("/api/v1/onboarding/sources", headers=headers)
    finally:
        market.close()

    assert unauthorized.status_code == 403
    assert unauthorized.json() == {"code": "desktop_origin_forbidden"}
    assert forbidden.status_code == 403
    assert state.status_code == 200
    assert state.json()["instrument"]["symbol"] == "000001.SS"
    assert sources.status_code == 200
    assert sources.json() == {
        "items": [
            {
                "id": "akshare",
                "label": "AKShare",
                "description": "免 Token 的 A 股公开数据源",
                "requires_token": False,
                "recommended": True,
                "status": "ready",
                "data_cutoff": None,
            },
            {
                "id": "baostock",
                "label": "BaoStock",
                "description": "免 Token 的 A 股公开数据源",
                "requires_token": False,
                "recommended": False,
                "status": "ready",
                "data_cutoff": None,
            },
        ]
    }


def test_onboarding_api_four_step_contract_pins_then_syncs_and_completes(
    tmp_path: Path,
) -> None:
    service, market = _service(tmp_path)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'onboarding.db'}",
        data_dir=tmp_path / "data",
    )
    app = create_app(
        settings,
        market_services=market,
        onboarding_service=service,
    )
    try:
        with TestClient(app) as client:
            preparation = client.put(
                "/api/v1/onboarding/progress",
                json={"current_step": "data_preparation"},
            )
            catalog = client.put(
                "/api/v1/onboarding/progress",
                json={
                    "current_step": "instrument_selection",
                    "source_id": "akshare",
                },
            )
            instruments = client.get(
                "/api/v1/onboarding/instruments",
                params={"q": "shangzheng", "limit": 20},
            )
            synchronized = client.post(
                "/api/v1/onboarding/sync",
                json={"source_id": "akshare", "symbol": "000001.SS"},
            )
            completed = client.post(
                "/api/v1/onboarding/complete",
                json={"symbol": "000001.SS"},
            )
            workspace = client.get("/api/v1/workspace")
    finally:
        market.close()

    assert preparation.status_code == 200
    assert preparation.json()["current_step"] == "data_preparation"
    assert preparation.json()["source"] is None
    assert catalog.status_code == 200
    assert catalog.json()["current_step"] == "instrument_selection"
    assert catalog.json()["source"]["id"] == "akshare"
    assert instruments.json()["items"] == [
        {
            "symbol": "000001.SS",
            "name": "上证指数",
            "exchange": "SH",
            "instrument_kind": "index",
        }
    ]
    assert synchronized.status_code == 200
    assert synchronized.json()["current_step"] == "synchronization"
    assert synchronized.json()["sync"]["status"] == "verified"
    assert synchronized.json()["sync"]["provider_id"] == "akshare"
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["current_step"] == "completed"
    assert workspace.status_code == 200
    assert workspace.json()["restored"] is True
    assert workspace.json()["revision"] == 1
    assert workspace.json()["workspace"]["instrument"] == {
        "symbol": "000001.SS",
        "name": "上证指数",
        "exchange": "SH",
        "kind": "index",
    }


def test_onboarding_validation_failures_use_a_stable_error_code(tmp_path: Path) -> None:
    service, market = _service(tmp_path)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'onboarding.db'}",
        data_dir=tmp_path / "data",
    )
    try:
        with TestClient(
            create_app(
                settings,
                market_services=market,
                onboarding_service=service,
            )
        ) as client:
            invalid = client.post(
                "/api/v1/onboarding/sync",
                json={"source_id": "not-a-provider", "symbol": "000001.SS"},
            )
    finally:
        market.close()

    assert invalid.status_code == 422
    assert invalid.json() == {"code": "invalid_request"}


def test_onboarding_api_can_exit_persisted_demo_into_real_setup(
    tmp_path: Path,
) -> None:
    service, market = _service(tmp_path)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'onboarding.db'}",
        data_dir=tmp_path / "data",
    )
    try:
        with TestClient(
            create_app(
                settings,
                market_services=market,
                onboarding_service=service,
            )
        ) as client:
            demo = client.post("/api/v1/onboarding/actions/demo")
            exited = client.post("/api/v1/onboarding/actions/exit_demo")
            prepared = client.put(
                "/api/v1/onboarding/progress",
                json={
                    "current_step": "instrument_selection",
                    "source_id": "akshare",
                },
            )
    finally:
        market.close()

    assert demo.status_code == 200
    assert demo.json()["demo_mode"] is True
    assert exited.status_code == 200
    assert exited.json()["demo_mode"] is False
    assert exited.json()["current_step"] == "data_preparation"
    assert exited.json()["source"] is None
    assert prepared.status_code == 200
    assert prepared.json()["source"]["id"] == "akshare"
