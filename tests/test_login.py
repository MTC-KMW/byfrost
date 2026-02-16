"""Tests for byfrost login command and API client module."""

import base64
import json
import stat
from unittest.mock import AsyncMock, MagicMock, patch

from cli.api_client import (
    ByfrostAPIClient,
    detect_platform,
    detect_role,
    get_device_name,
    load_auth,
    save_auth,
)
from cli.main import _extract_username_from_jwt

# ---------------------------------------------------------------------------
# Platform and role detection
# ---------------------------------------------------------------------------


class TestDetectPlatform:
    """Platform string detection."""

    @patch("cli.api_client.sys")
    def test_darwin_is_macos(self, mock_sys: object) -> None:
        mock_sys.platform = "darwin"  # type: ignore[attr-defined]
        assert detect_platform() == "macos"

    @patch("cli.api_client.sys")
    def test_win32_is_windows(self, mock_sys: object) -> None:
        mock_sys.platform = "win32"  # type: ignore[attr-defined]
        assert detect_platform() == "windows"

    @patch("cli.api_client.sys")
    def test_linux_is_linux(self, mock_sys: object) -> None:
        mock_sys.platform = "linux"  # type: ignore[attr-defined]
        assert detect_platform() == "linux"


class TestDetectRole:
    """Role auto-detection from platform."""

    @patch("cli.api_client.sys")
    def test_darwin_is_worker(self, mock_sys: object) -> None:
        mock_sys.platform = "darwin"  # type: ignore[attr-defined]
        assert detect_role() == "worker"

    @patch("cli.api_client.sys")
    def test_linux_is_controller(self, mock_sys: object) -> None:
        mock_sys.platform = "linux"  # type: ignore[attr-defined]
        assert detect_role() == "controller"

    @patch("cli.api_client.sys")
    def test_windows_is_controller(self, mock_sys: object) -> None:
        mock_sys.platform = "win32"  # type: ignore[attr-defined]
        assert detect_role() == "controller"


class TestGetDeviceName:
    """Device hostname detection."""

    @patch("cli.api_client.platform")
    def test_returns_hostname(self, mock_platform: object) -> None:
        mock_platform.node = lambda: "my-macbook"  # type: ignore[attr-defined]
        assert get_device_name() == "my-macbook"

    @patch("cli.api_client.platform")
    def test_returns_unknown_if_empty(self, mock_platform: object) -> None:
        mock_platform.node = lambda: ""  # type: ignore[attr-defined]
        assert get_device_name() == "unknown"


# ---------------------------------------------------------------------------
# Auth file read/write
# ---------------------------------------------------------------------------


class TestAuthFile:
    """Auth file persistence."""

    def test_save_and_load_roundtrip(self, tmp_path: object) -> None:
        auth_file = tmp_path / "auth.json"  # type: ignore[operator]
        data = {
            "access_token": "abc123",
            "server_url": "https://example.com",
            "github_username": "testuser",
        }
        with (
            patch("cli.api_client.AUTH_FILE", auth_file),
            patch("cli.api_client.BRIDGE_DIR", tmp_path),
        ):
            save_auth(data)
            loaded = load_auth()

        assert loaded == data

    def test_file_permissions(self, tmp_path: object) -> None:
        auth_file = tmp_path / "auth.json"  # type: ignore[operator]
        with (
            patch("cli.api_client.AUTH_FILE", auth_file),
            patch("cli.api_client.BRIDGE_DIR", tmp_path),
        ):
            save_auth({"token": "secret"})

        mode = auth_file.stat().st_mode  # type: ignore[union-attr]
        # Owner read+write only
        assert mode & stat.S_IRUSR
        assert mode & stat.S_IWUSR
        # No group or other access
        assert not (mode & stat.S_IRWXG)
        assert not (mode & stat.S_IRWXO)

    def test_load_missing_returns_none(self, tmp_path: object) -> None:
        auth_file = tmp_path / "auth.json"  # type: ignore[operator]
        with patch("cli.api_client.AUTH_FILE", auth_file):
            assert load_auth() is None

    def test_load_corrupt_returns_none(self, tmp_path: object) -> None:
        auth_file = tmp_path / "auth.json"  # type: ignore[operator]
        auth_file.write_text("not json{{{")  # type: ignore[union-attr]
        with patch("cli.api_client.AUTH_FILE", auth_file):
            assert load_auth() is None


# ---------------------------------------------------------------------------
# JWT username extraction
# ---------------------------------------------------------------------------


class TestExtractUsername:
    """Extract GitHub username from JWT without verification."""

    def test_extracts_username(self) -> None:
        payload = json.dumps({"username": "octocat", "sub": "uuid-123"})
        b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        fake_jwt = f"header.{b64}.signature"
        assert _extract_username_from_jwt(fake_jwt) == "octocat"

    def test_missing_username_returns_unknown(self) -> None:
        payload = json.dumps({"sub": "uuid-123"})
        b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        fake_jwt = f"header.{b64}.signature"
        assert _extract_username_from_jwt(fake_jwt) == "unknown"

    def test_garbage_returns_unknown(self) -> None:
        assert _extract_username_from_jwt("not.a.jwt") == "unknown"

    def test_empty_returns_unknown(self) -> None:
        assert _extract_username_from_jwt("") == "unknown"


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


def _mock_response(data: dict, status_code: int = 200) -> MagicMock:
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    if status_code < 400:
        resp.raise_for_status = MagicMock()
    return resp


def _patch_httpx(mock_response: AsyncMock):
    """Patch httpx.AsyncClient to return a mock response."""
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return patch("cli.api_client.httpx.AsyncClient", return_value=mock_client)


class TestByfrostAPIClient:
    """Server API client methods."""

    async def test_request_device_code(self) -> None:
        resp = _mock_response({
            "device_code": "dc_123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        })
        with _patch_httpx(resp):
            api = ByfrostAPIClient(server_url="https://test.example.com")
            result = await api.request_device_code()

        assert result["user_code"] == "ABCD-1234"
        assert result["device_code"] == "dc_123"

    async def test_poll_device_token_pending(self) -> None:
        resp = _mock_response({"status": "pending"})
        with _patch_httpx(resp):
            api = ByfrostAPIClient(server_url="https://test.example.com")
            result = await api.poll_device_token("dc_123")

        assert result["status"] == "pending"

    async def test_poll_device_token_success(self) -> None:
        resp = _mock_response({
            "access_token": "jwt_access",
            "refresh_token": "jwt_refresh",
            "token_type": "bearer",
        })
        with _patch_httpx(resp):
            api = ByfrostAPIClient(server_url="https://test.example.com")
            result = await api.poll_device_token("dc_123")

        assert result["access_token"] == "jwt_access"

    async def test_poll_device_token_expired(self) -> None:
        resp = _mock_response({"detail": "Device flow failed: expired_token"}, 400)
        with _patch_httpx(resp):
            api = ByfrostAPIClient(server_url="https://test.example.com")
            result = await api.poll_device_token("dc_123")

        assert "error" in result

    async def test_register_device(self) -> None:
        resp = _mock_response({
            "device_id": "uuid-456",
            "device_token": "raw_token_abc",
        })
        with _patch_httpx(resp):
            api = ByfrostAPIClient(server_url="https://test.example.com")
            result = await api.register_device("jwt_tok", "my-host", "controller", "linux")

        assert result["device_id"] == "uuid-456"
        assert result["device_token"] == "raw_token_abc"

    async def test_list_devices(self) -> None:
        resp = _mock_response([{"id": "uuid-1", "name": "host1"}])
        resp.json.return_value = [{"id": "uuid-1", "name": "host1"}]
        with _patch_httpx(resp):
            api = ByfrostAPIClient(server_url="https://test.example.com")
            result = await api.list_devices("jwt_tok")

        assert len(result) == 1
        assert result[0]["name"] == "host1"
