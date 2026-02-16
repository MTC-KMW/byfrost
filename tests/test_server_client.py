"""Tests for daemon server-client module."""

import base64
import json
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

from daemon.server_client import (
    ServerClient,
    _decode_jwt_exp,
    detect_addresses,
)

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

_VALID_AUTH = {
    "server_url": "https://api.test.dev",
    "device_id": "dev-123",
    "device_token": "tok-abc",
    "access_token": "access.payload.sig",
    "refresh_token": "refresh.payload.sig",
    "pairing_id": "pair-456",
}


def _mock_response(
    data: dict | list | None = None, status_code: int = 200,
) -> MagicMock:
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data or {}
    if status_code < 400:
        resp.raise_for_status = MagicMock()
    else:
        from httpx import HTTPStatusError

        resp.raise_for_status.side_effect = HTTPStatusError(
            "error", request=MagicMock(), response=resp,
        )
    return resp


def _make_jwt(exp: float) -> str:
    """Create a fake JWT with the given expiry."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp, "sub": "user-1"}).encode(),
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


def _make_client(
    auth: dict | None = None,
    on_secret_rotated: object = None,
) -> ServerClient:
    """Create a ServerClient with mocked auth loading."""
    logger = logging.getLogger("test")
    client = ServerClient(
        config={"port": 9784},
        logger=logger,
        on_secret_rotated=on_secret_rotated,
    )
    if auth:
        client._server_url = auth.get("server_url", "").rstrip("/")
        client._device_id = auth.get("device_id", "")
        client._device_token = auth.get("device_token", "")
        client._access_token = auth.get("access_token", "")
        client._refresh_token = auth.get("refresh_token", "")
        client._pairing_id = auth.get("pairing_id", "")
    return client


# ---------------------------------------------------------------------------
# detect_addresses
# ---------------------------------------------------------------------------


class TestDetectAddresses:
    """Network address detection."""

    def test_returns_dict_with_port(self) -> None:
        result = detect_addresses(9784)
        assert result["port"] == 9784
        assert "local_ip" in result

    @patch("daemon.server_client.socket.socket")
    def test_fallback_to_localhost(self, mock_sock_cls: MagicMock) -> None:
        mock_sock_cls.return_value.connect.side_effect = OSError("no route")
        result = detect_addresses()
        assert result["local_ip"] == "127.0.0.1"


# ---------------------------------------------------------------------------
# _decode_jwt_exp
# ---------------------------------------------------------------------------


class TestDecodeJwtExp:
    """JWT expiry extraction."""

    def test_extracts_expiry(self) -> None:
        token = _make_jwt(1700000000.0)
        assert _decode_jwt_exp(token) == 1700000000.0

    def test_returns_none_for_invalid_token(self) -> None:
        assert _decode_jwt_exp("not.a.jwt") is None

    def test_returns_none_for_garbage(self) -> None:
        assert _decode_jwt_exp("garbage") is None

    def test_returns_zero_for_missing_exp(self) -> None:
        header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(b'{"sub":"user"}').rstrip(b"=").decode()
        token = f"{header}.{payload}.sig"
        assert _decode_jwt_exp(token) == 0.0


# ---------------------------------------------------------------------------
# ServerClient._load_auth
# ---------------------------------------------------------------------------


class TestServerClientLoadAuth:
    """Auth credential loading."""

    @patch("daemon.server_client.load_auth", return_value=_VALID_AUTH)
    def test_loads_valid_auth(self, _mock: MagicMock) -> None:
        client = _make_client()
        assert client._load_auth() is True
        assert client._server_url == "https://api.test.dev"
        assert client._device_id == "dev-123"
        assert client._pairing_id == "pair-456"

    @patch("daemon.server_client.load_auth", return_value=None)
    def test_returns_false_when_no_file(self, _mock: MagicMock) -> None:
        client = _make_client()
        assert client._load_auth() is False

    @patch("daemon.server_client.load_auth", return_value={"server_url": "https://x"})
    def test_returns_false_when_missing_fields(self, _mock: MagicMock) -> None:
        client = _make_client()
        assert client._load_auth() is False


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


class TestServerClientHeartbeat:
    """Heartbeat sending."""

    async def test_sends_heartbeat(self) -> None:
        client = _make_client(_VALID_AUTH)
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=_mock_response({"status": "ok"}))
        client._client = mock_http

        await client._send_heartbeat()

        mock_http.request.assert_called_once()
        call_args = mock_http.request.call_args
        assert call_args[0][0] == "POST"
        assert "/devices/dev-123/heartbeat" in call_args[0][1]

    async def test_heartbeat_uses_device_token(self) -> None:
        client = _make_client(_VALID_AUTH)
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=_mock_response({"status": "ok"}))
        client._client = mock_http

        await client._send_heartbeat()

        call_args = mock_http.request.call_args
        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer tok-abc"

    async def test_heartbeat_handles_error(self) -> None:
        client = _make_client(_VALID_AUTH)
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=_mock_response({}, 401))
        client._client = mock_http

        # Should not raise
        await client._send_heartbeat()


# ---------------------------------------------------------------------------
# Credential fetch
# ---------------------------------------------------------------------------


class TestCredentialFetch:
    """Worker credential fetching."""

    async def test_skips_when_no_pairing_id(self) -> None:
        auth = {**_VALID_AUTH, "pairing_id": ""}
        client = _make_client(auth)
        mock_http = AsyncMock()
        client._client = mock_http

        await client._fetch_credentials_if_needed()
        # No HTTP call should be made
        mock_http.request.assert_not_called()

    @patch("daemon.server_client.SECRET_FILE")
    @patch("daemon.server_client.TLSManager")
    async def test_skips_when_certs_exist(
        self, mock_tls: MagicMock, mock_secret: MagicMock,
    ) -> None:
        mock_tls.has_server_certs.return_value = True
        mock_secret.exists.return_value = True
        client = _make_client(_VALID_AUTH)
        mock_http = AsyncMock()
        client._client = mock_http

        await client._fetch_credentials_if_needed()
        mock_http.request.assert_not_called()

    @patch("daemon.server_client.SecretManager")
    @patch("daemon.server_client.TLSManager")
    @patch("daemon.server_client.CERTS_DIR")
    @patch("daemon.server_client.SECRET_FILE")
    async def test_fetches_and_saves(
        self,
        mock_secret_file: MagicMock,
        mock_certs_dir: MagicMock,
        mock_tls: MagicMock,
        mock_secret_mgr: MagicMock,
    ) -> None:
        mock_tls.has_server_certs.return_value = False
        mock_secret_file.exists.return_value = False

        hmac_secret = base64.b64encode(b"\xab\xcd" * 16).decode()
        creds = {
            "ca_cert": "-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----",
            "cert": "-----BEGIN CERTIFICATE-----\nSRV\n-----END CERTIFICATE-----",
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nKEY\n-----END RSA PRIVATE KEY-----",
            "hmac_secret": hmac_secret,
        }
        client = _make_client(_VALID_AUTH)
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=_mock_response(creds))
        client._client = mock_http

        await client._fetch_credentials_if_needed()

        mock_http.request.assert_called_once()
        mock_tls.CA_CERT.write_text.assert_called_once_with(creds["ca_cert"])
        mock_tls.SERVER_CERT.write_text.assert_called_once_with(creds["cert"])
        mock_tls.SERVER_KEY.write_text.assert_called_once_with(creds["private_key"])
        mock_secret_mgr.save.assert_called_once()

    @patch("daemon.server_client.SECRET_FILE")
    @patch("daemon.server_client.TLSManager")
    async def test_handles_fetch_failure(
        self, mock_tls: MagicMock, mock_secret: MagicMock,
    ) -> None:
        mock_tls.has_server_certs.return_value = False
        mock_secret.exists.return_value = False

        client = _make_client(_VALID_AUTH)
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=_mock_response({}, 403))
        client._client = mock_http

        # Should not raise
        await client._fetch_credentials_if_needed()


# ---------------------------------------------------------------------------
# JWT refresh
# ---------------------------------------------------------------------------


class TestTokenRefresh:
    """JWT access token refresh."""

    async def test_refreshes_when_near_expiry(self) -> None:
        # Token expires in 5 minutes (within 10-min margin)
        auth = {**_VALID_AUTH, "access_token": _make_jwt(time.time() + 300)}
        client = _make_client(auth)

        new_tokens = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "bearer",
        }
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=_mock_response(new_tokens))
        client._client = mock_http

        with patch("daemon.server_client.save_auth") as mock_save, \
             patch("daemon.server_client.load_auth", return_value=dict(auth)):
            await client._refresh_if_needed()

        assert client._access_token == "new-access"
        assert client._refresh_token == "new-refresh"
        mock_save.assert_called_once()

    async def test_skips_when_token_fresh(self) -> None:
        # Token expires in 2 hours (well outside margin)
        auth = {**_VALID_AUTH, "access_token": _make_jwt(time.time() + 7200)}
        client = _make_client(auth)
        mock_http = AsyncMock()
        client._client = mock_http

        await client._refresh_if_needed()
        # No HTTP call should be made
        mock_http.post.assert_not_called()

    async def test_skips_when_no_tokens(self) -> None:
        auth = {**_VALID_AUTH, "access_token": "", "refresh_token": ""}
        client = _make_client(auth)
        mock_http = AsyncMock()
        client._client = mock_http

        await client._refresh_if_needed()
        mock_http.post.assert_not_called()

    async def test_handles_refresh_failure(self) -> None:
        auth = {**_VALID_AUTH, "access_token": _make_jwt(time.time() + 300)}
        client = _make_client(auth)
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=_mock_response({}, 401))
        client._client = mock_http

        # Should not raise
        await client._refresh_if_needed()


# ---------------------------------------------------------------------------
# Rotation detection
# ---------------------------------------------------------------------------


class TestRotationDetection:
    """HMAC secret rotation detection."""

    @patch("daemon.server_client.SecretManager")
    async def test_detects_rotated_secret(self, mock_sm: MagicMock) -> None:
        mock_sm.load.return_value = "aa" * 32  # old secret
        callback = MagicMock()
        client = _make_client(_VALID_AUTH, on_secret_rotated=callback)

        # Server returns a different secret
        new_secret = b"\xbb" * 32
        creds = {"hmac_secret": base64.b64encode(new_secret).decode()}
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=_mock_response(creds))
        client._client = mock_http

        await client._check_rotation()

        mock_sm.rotate.assert_called_once()
        mock_sm.save.assert_called_once_with(new_secret.hex())
        callback.assert_called_once()

    @patch("daemon.server_client.SecretManager")
    async def test_no_change_when_secret_matches(self, mock_sm: MagicMock) -> None:
        local_secret = "bb" * 32
        mock_sm.load.return_value = local_secret
        callback = MagicMock()
        client = _make_client(_VALID_AUTH, on_secret_rotated=callback)

        # Server returns same secret
        secret_bytes = bytes.fromhex(local_secret)
        creds = {"hmac_secret": base64.b64encode(secret_bytes).decode()}
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=_mock_response(creds))
        client._client = mock_http

        await client._check_rotation()

        mock_sm.rotate.assert_not_called()
        callback.assert_not_called()

    @patch("daemon.server_client.SecretManager")
    async def test_handles_fetch_error(self, mock_sm: MagicMock) -> None:
        client = _make_client(_VALID_AUTH)
        mock_http = AsyncMock()
        mock_http.request = AsyncMock(return_value=_mock_response({}, 500))
        client._client = mock_http

        # Should not raise
        await client._check_rotation()
        mock_sm.rotate.assert_not_called()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestServerClientLifecycle:
    """Start/stop lifecycle."""

    @patch("daemon.server_client.load_auth", return_value=None)
    async def test_start_without_auth_returns_false(self, _mock: MagicMock) -> None:
        client = _make_client()
        result = await client.start()
        assert result is False

    @patch("daemon.server_client.TLSManager")
    @patch("daemon.server_client.SECRET_FILE")
    @patch("daemon.server_client.load_auth", return_value=_VALID_AUTH)
    async def test_start_and_stop(
        self,
        _mock_auth: MagicMock,
        mock_secret: MagicMock,
        mock_tls: MagicMock,
    ) -> None:
        mock_tls.has_server_certs.return_value = True
        mock_secret.exists.return_value = True

        client = _make_client()
        with patch.object(client, "_client", AsyncMock()):
            result = await client.start()
            assert result is True
            assert len(client._tasks) == 3

            await client.stop()
            assert len(client._tasks) == 0
            assert client._client is None
