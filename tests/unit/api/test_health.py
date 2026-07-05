from fastapi.testclient import TestClient

from stock_desk.config import Settings
from stock_desk.main import create_app


def test_health_exposes_name_and_status() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "name": "stock-desk",
        "status": "ok",
        "api_version": "v1",
    }


def test_custom_app_title_does_not_change_health_identity() -> None:
    application = create_app(Settings(app_name="My Stock Desk"))

    with TestClient(application) as client:
        response = client.get("/api/health")

    assert application.title == "My Stock Desk"
    assert response.json()["name"] == "stock-desk"
