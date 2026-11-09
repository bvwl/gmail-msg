from __future__ import annotations

from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_service_info() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["service"] == "imap-mail-monitor"
    assert response.json()["status"] == "ok"


def test_liveness() -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_unimplemented_dependencies() -> None:
    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["postgres"] in {"unavailable", "timeout"}
