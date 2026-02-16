"""Tests for auth endpoints - GitHub OAuth and JWT management."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.jwt import create_access_token, create_refresh_token, decode_token
from app.config import get_settings
from app.database import get_db
from app.main import create_app

MOCK_GITHUB_USER = {
    "id": 12345,
    "login": "testuser",
    "email": "test@example.com",
}


@pytest.fixture()
def client():
    """Create a test client with a fresh app."""
    app = create_app()
    return TestClient(app)


@pytest.fixture()
async def async_client():
    """Async test client with a fresh DB engine (avoids asyncpg pool contamination)."""
    test_engine = create_async_engine(get_settings().database_url)
    test_session = async_sessionmaker(test_engine, expire_on_commit=False)

    async def override_get_db():
        async with test_session() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await test_engine.dispose()


@pytest.fixture()
def user_id():
    """A consistent UUID for test tokens."""
    return uuid.uuid4()


# -- JWT unit tests --

class TestJWT:
    """JWT token creation and validation."""

    def test_create_and_decode_access_token(self) -> None:
        uid = uuid.uuid4()
        token = create_access_token(uid, "testuser")
        payload = decode_token(token)
        assert payload["sub"] == str(uid)
        assert payload["username"] == "testuser"
        assert payload["type"] == "access"

    def test_create_and_decode_refresh_token(self) -> None:
        uid = uuid.uuid4()
        token = create_refresh_token(uid)
        payload = decode_token(token)
        assert payload["sub"] == str(uid)
        assert payload["type"] == "refresh"

    def test_expired_token_raises(self) -> None:
        settings = get_settings()
        payload = {
            "sub": str(uuid.uuid4()),
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            "type": "access",
        }
        token = jwt.encode(
            payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
        )
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            decode_token(token)
        assert exc_info.value.status_code == 401

    def test_invalid_token_raises(self) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            decode_token("not-a-valid-token")


# -- Browser flow tests --

class TestBrowserFlow:
    """GitHub OAuth browser flow."""

    def test_github_redirect(self, client: TestClient) -> None:
        """GET /auth/github redirects to GitHub."""
        resp = client.get("/auth/github", follow_redirects=False)
        assert resp.status_code == 307
        location = resp.headers["location"]
        assert "github.com/login/oauth/authorize" in location
        assert "client_id=" in location
        assert "state=" in location

    @patch("app.auth.router.exchange_code_for_token", new_callable=AsyncMock)
    @patch("app.auth.router.get_github_user", new_callable=AsyncMock)
    def test_callback_creates_user(
        self,
        mock_get_user: AsyncMock,
        mock_exchange: AsyncMock,
        client: TestClient,
    ) -> None:
        """Callback exchanges code, creates user, returns JWT."""
        mock_exchange.return_value = "gho_fake_token"
        mock_get_user.return_value = MOCK_GITHUB_USER

        # First, get a valid state by hitting the redirect
        resp = client.get("/auth/github", follow_redirects=False)
        location = resp.headers["location"]
        state = location.split("state=")[1].split("&")[0]

        # Hit callback with valid state
        resp = client.get(f"/auth/github/callback?code=test_code&state={state}")
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"

    def test_callback_invalid_state(self, client: TestClient) -> None:
        """Callback rejects invalid state parameter."""
        resp = client.get("/auth/github/callback?code=test&state=invalid")
        assert resp.status_code == 400


# -- Device flow tests --

class TestDeviceFlow:
    """GitHub OAuth device flow."""

    @patch("app.auth.router.request_device_code", new_callable=AsyncMock)
    def test_device_code_returns_user_code(
        self, mock_request: AsyncMock, client: TestClient
    ) -> None:
        mock_request.return_value = {
            "device_code": "dc_123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
        resp = client.post("/auth/device/code")
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_code"] == "ABCD-1234"
        assert data["verification_uri"] == "https://github.com/login/device"

    @patch("app.auth.router.poll_device_token", new_callable=AsyncMock)
    def test_device_token_pending(
        self, mock_poll: AsyncMock, client: TestClient
    ) -> None:
        mock_poll.return_value = {"error": "authorization_pending"}
        resp = client.post(
            "/auth/device/token", json={"device_code": "dc_123"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"

    @patch("app.auth.router.get_github_user", new_callable=AsyncMock)
    @patch("app.auth.router.poll_device_token", new_callable=AsyncMock)
    async def test_device_token_complete(
        self,
        mock_poll: AsyncMock,
        mock_get_user: AsyncMock,
        async_client: httpx.AsyncClient,
    ) -> None:
        mock_poll.return_value = {"access_token": "gho_fake"}
        mock_get_user.return_value = MOCK_GITHUB_USER
        resp = await async_client.post(
            "/auth/device/token", json={"device_code": "dc_123"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data

    @patch("app.auth.router.poll_device_token", new_callable=AsyncMock)
    def test_device_token_expired(
        self, mock_poll: AsyncMock, client: TestClient
    ) -> None:
        mock_poll.return_value = {"error": "expired_token"}
        resp = client.post(
            "/auth/device/token", json={"device_code": "dc_123"}
        )
        assert resp.status_code == 400


# -- Token refresh tests --

class TestRefresh:
    """Token refresh endpoint."""

    @patch("app.auth.router.exchange_code_for_token", new_callable=AsyncMock)
    @patch("app.auth.router.get_github_user", new_callable=AsyncMock)
    async def test_refresh_valid(
        self,
        mock_get_user: AsyncMock,
        mock_exchange: AsyncMock,
        async_client: httpx.AsyncClient,
    ) -> None:
        """Valid refresh token returns new access + refresh tokens."""
        mock_exchange.return_value = "gho_fake"
        mock_get_user.return_value = MOCK_GITHUB_USER

        # Create a user via callback first
        resp = await async_client.get("/auth/github", follow_redirects=False)
        location = resp.headers["location"]
        state = location.split("state=")[1].split("&")[0]
        resp = await async_client.get(
            f"/auth/github/callback?code=test&state={state}"
        )
        refresh_token = resp.json()["refresh_token"]

        # Use refresh token
        resp = await async_client.post(
            "/auth/refresh", json={"refresh_token": refresh_token}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data

    def test_refresh_rejects_access_token(self, client: TestClient) -> None:
        """Access tokens cannot be used as refresh tokens."""
        token = create_access_token(uuid.uuid4(), "test")
        resp = client.post("/auth/refresh", json={"refresh_token": token})
        assert resp.status_code == 401

    def test_refresh_rejects_invalid(self, client: TestClient) -> None:
        """Invalid tokens are rejected."""
        resp = client.post("/auth/refresh", json={"refresh_token": "garbage"})
        assert resp.status_code == 401
