from pathlib import Path

from fastapi.testclient import TestClient

from stock_desk.config import Settings
from stock_desk.main import create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'stock-desk.db'}",
    )


def test_guidance_preferences_api_persists_below_data_root(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        initial = client.get("/api/v1/guidance/preferences")
        assert initial.status_code == 200
        assert initial.json() == {
            "schema_version": 1,
            "revision": 0,
            "pages": {},
        }

        saved = client.put(
            "/api/v1/guidance/preferences",
            json={
                "expected_revision": 0,
                "page": "market",
                "content_version": 1,
                "status": "completed",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["revision"] == 1

    assert (tmp_path / "guidance" / "preferences.json").is_file()


def test_guidance_preferences_api_fails_closed_on_stale_revision(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        body = {
            "expected_revision": 0,
            "page": "tasks",
            "content_version": 1,
            "status": "dismissed",
        }
        assert client.put("/api/v1/guidance/preferences", json=body).status_code == 200
        stale = client.put("/api/v1/guidance/preferences", json=body)

    assert stale.status_code == 409
    assert stale.json() == {"code": "guidance_revision_conflict"}


def test_guidance_preferences_api_rejects_unknown_fields_without_leaking_paths(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.put(
            "/api/v1/guidance/preferences",
            json={
                "expected_revision": 0,
                "page": "market",
                "content_version": 1,
                "status": "completed",
                "token": "secret",
            },
        )

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request"}
    assert str(tmp_path) not in response.text
    assert "secret" not in response.text
