from pathlib import Path

from fastapi.testclient import TestClient

from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.desktop_session import DesktopSession, TAURI_WINDOWS_ORIGIN


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                data_dir=tmp_path / "data",
                database_url=f"sqlite:///{tmp_path / 'stock-desk.db'}",
            )
        )
    )


def test_workspace_get_put_delete_contract_and_unknown_field_rejection(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        initial = client.get("/api/v1/workspace")
        assert initial.status_code == 200
        assert initial.json()["notice"] == "workspace_missing"
        default = initial.json()["workspace"]
        assert default["current_page"] == "/market"
        assert default["instrument"] == {
            "symbol": "000001.SS",
            "name": "上证指数",
            "exchange": "SH",
            "kind": "index",
        }

        forbidden = client.put(
            "/api/v1/workspace",
            json={
                "expected_revision": 0,
                **default,
                "url": "https://example.invalid/?token=secret",
            },
        )
        assert forbidden.status_code == 422
        assert forbidden.json() == {"code": "invalid_request"}

        saved = client.put(
            "/api/v1/workspace",
            json={"expected_revision": 0, **default},
        )
        assert saved.status_code == 200
        assert saved.json()["revision"] == 1
        assert saved.json()["restored"] is True

        conflict = client.put(
            "/api/v1/workspace",
            json={"expected_revision": 0, **default},
        )
        assert conflict.status_code == 409
        assert conflict.json() == {"code": "workspace_revision_conflict"}

        deleted = client.delete("/api/v1/workspace")
        assert deleted.status_code == 204
        assert client.delete("/api/v1/workspace").status_code == 204


def test_workspace_never_accepts_arbitrary_url_or_session_material(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        base = client.get("/api/v1/workspace").json()["workspace"]
        for field, value in (
            ("current_page", "/market?session_key=secret"),
            ("current_page", "https://example.invalid/market"),
            ("main_chart", "token=secret"),
            ("subchart", {"kind": "formula", "formula_version_id": "../../session"}),
        ):
            response = client.put(
                "/api/v1/workspace",
                json={"expected_revision": 0, **base, field: value},
            )
            assert response.status_code == 422
            assert response.json() == {"code": "invalid_request"}


def test_workspace_api_uses_desktop_origin_and_bearer_authentication(
    tmp_path: Path,
) -> None:
    secret = "workspace-desktop-secret-that-is-long-enough"
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{tmp_path / 'stock-desk.db'}",
    )
    app = create_app(
        settings,
        desktop_session=DesktopSession(
            origin=TAURI_WINDOWS_ORIGIN,
            secret=secret,
            host_version="1.1.0",
            frontend_version="1.1.0",
            sidecar_version="1.1.0",
            source_revision="a" * 40,
        ),
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/workspace").status_code == 403
        assert (
            client.get(
                "/api/v1/workspace",
                headers={"Authorization": f"Bearer {secret}"},
            ).status_code
            == 403
        )
        authorized = client.get(
            "/api/v1/workspace",
            headers={
                "Origin": TAURI_WINDOWS_ORIGIN,
                "Authorization": f"Bearer {secret}",
            },
        )
        assert authorized.status_code == 200
