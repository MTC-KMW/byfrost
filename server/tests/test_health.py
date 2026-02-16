"""Tests for the health endpoint and basic app configuration."""

from fastapi.testclient import TestClient

from app.main import create_app


class TestHealth:
    """Health check endpoint."""

    def test_health_returns_ok(self) -> None:
        app = create_app()
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "byfrost-server"

    def test_health_requires_no_auth(self) -> None:
        """Health endpoint is unauthenticated."""
        app = create_app()
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
