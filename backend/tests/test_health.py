from fastapi.testclient import TestClient

from api.main import app


def test_health_returns_ok_and_db_status():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert isinstance(body["db"], bool)


def test_local_web_origin_is_allowed():
    client = TestClient(app)
    response = client.options(
        "/runs",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
