from fastapi.testclient import TestClient

from stock_desk.main import create_app


def test_health_exposes_name_and_status() -> None:
    response = TestClient(create_app()).get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "name": "stock-desk",
        "status": "ok",
        "api_version": "v1",
    }
