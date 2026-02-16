"""Tests for byfrost connect command and pairing API methods."""

import base64
import stat
from unittest.mock import AsyncMock, MagicMock, patch

from cli.api_client import ByfrostAPIClient
from cli.main import _do_connect, _save_credentials

# ---------------------------------------------------------------------------
# Mock helpers (same pattern as test_login.py)
# ---------------------------------------------------------------------------


def _mock_response(data: dict | list, status_code: int = 200) -> MagicMock:
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    if status_code < 400:
        resp.raise_for_status = MagicMock()
    return resp


def _patch_httpx(mock_response: MagicMock):
    """Patch httpx.AsyncClient to return a mock response."""
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return patch("cli.api_client.httpx.AsyncClient", return_value=mock_client)


# ---------------------------------------------------------------------------
# API client pairing methods
# ---------------------------------------------------------------------------


class TestInitiatePairing:
    """POST /pair/initiate API calls."""

    async def test_successful_pairing(self) -> None:
        resp = _mock_response({
            "pairing_id": "pair-uuid-123",
            "status": "active",
        })
        with _patch_httpx(resp):
            api = ByfrostAPIClient(server_url="https://test.example.com")
            result = await api.initiate_pairing("jwt_tok", "worker-id", "ctrl-id")

        assert result["pairing_id"] == "pair-uuid-123"
        assert result["status"] == "active"

    async def test_409_returns_existing_pairing_id(self) -> None:
        resp = _mock_response(
            {"detail": {"message": "Already exists", "pairing_id": "existing-uuid"}},
            status_code=409,
        )
        with _patch_httpx(resp):
            api = ByfrostAPIClient(server_url="https://test.example.com")
            result = await api.initiate_pairing("jwt_tok", "worker-id", "ctrl-id")

        assert result["already_exists"] is True
        assert result["pairing_id"] == "existing-uuid"

    async def test_409_without_pairing_id(self) -> None:
        resp = _mock_response(
            {"detail": "Active pairing already exists"},
            status_code=409,
        )
        with _patch_httpx(resp):
            api = ByfrostAPIClient(server_url="https://test.example.com")
            result = await api.initiate_pairing("jwt_tok", "worker-id", "ctrl-id")

        assert result["already_exists"] is True
        assert "error" in result


class TestGetControllerCredentials:
    """GET /pair/{id}/credentials/controller."""

    async def test_fetches_credentials(self) -> None:
        resp = _mock_response({
            "ca_cert": "-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----",
            "cert": "-----BEGIN CERTIFICATE-----\nCLIENT\n-----END CERTIFICATE-----",
            "private_key": "-----BEGIN PRIVATE KEY-----\nKEY\n-----END PRIVATE KEY-----",
            "hmac_secret": base64.b64encode(b"secret123").decode(),
            "prev_hmac_secret": None,
        })
        with _patch_httpx(resp):
            api = ByfrostAPIClient(server_url="https://test.example.com")
            result = await api.get_controller_credentials("pair-uuid", "device-token")

        assert "ca_cert" in result
        assert "cert" in result
        assert "private_key" in result
        assert "hmac_secret" in result


class TestGetPairingAddresses:
    """GET /pair/{id}/addresses."""

    async def test_fetches_addresses(self) -> None:
        resp = _mock_response({
            "addresses": {
                "local_ip": "192.168.1.100",
                "tailscale_ip": "100.64.0.5",
                "public_ip": "203.0.113.10",
                "port": 9784,
            },
        })
        with _patch_httpx(resp):
            api = ByfrostAPIClient(server_url="https://test.example.com")
            result = await api.get_pairing_addresses("pair-uuid", "device-token")

        assert result["addresses"]["local_ip"] == "192.168.1.100"
        assert result["addresses"]["port"] == 9784

    async def test_null_addresses(self) -> None:
        resp = _mock_response({"addresses": None})
        with _patch_httpx(resp):
            api = ByfrostAPIClient(server_url="https://test.example.com")
            result = await api.get_pairing_addresses("pair-uuid", "device-token")

        assert result["addresses"] is None


# ---------------------------------------------------------------------------
# Credential saving
# ---------------------------------------------------------------------------


class TestSaveCredentials:
    """Save certs and HMAC secret to disk."""

    def test_saves_all_files(self, tmp_path: object) -> None:
        certs_dir = tmp_path / "certs"  # type: ignore[operator]
        certs_dir.mkdir()

        ca_cert = tmp_path / "certs" / "ca.pem"  # type: ignore[operator]
        client_cert = tmp_path / "certs" / "client.pem"  # type: ignore[operator]
        client_key = tmp_path / "certs" / "client.key"  # type: ignore[operator]

        secret_hex = b"\xab\xcd\xef\x01\x23\x45\x67\x89"
        creds = {
            "ca_cert": "---CA---",
            "cert": "---CLIENT---",
            "private_key": "---KEY---",
            "hmac_secret": base64.b64encode(secret_hex).decode(),
        }

        with (
            patch("cli.main.ensure_byfrost_dir"),
            patch("cli.main.CERTS_DIR", certs_dir),
            patch("cli.main.TLSManager") as mock_tls,
            patch("cli.main.SecretManager") as mock_secret,
        ):
            mock_tls.CA_CERT = ca_cert
            mock_tls.CLIENT_CERT = client_cert
            mock_tls.CLIENT_KEY = client_key
            _save_credentials(creds)

        assert ca_cert.read_text() == "---CA---"
        assert client_cert.read_text() == "---CLIENT---"
        assert client_key.read_text() == "---KEY---"

        # Check private key permissions
        mode = client_key.stat().st_mode
        assert mode & stat.S_IRUSR
        assert mode & stat.S_IWUSR
        assert not (mode & stat.S_IRWXG)
        assert not (mode & stat.S_IRWXO)

        # Check HMAC secret was saved
        mock_secret.save.assert_called_once_with(secret_hex.hex())


# ---------------------------------------------------------------------------
# Connect flow
# ---------------------------------------------------------------------------


class TestDoConnect:
    """High-level connect command tests."""

    async def test_not_logged_in(self) -> None:
        with patch("cli.main.load_auth", return_value=None):
            result = await _do_connect(None)
        assert result == 1

    async def test_worker_role_rejected(self) -> None:
        auth = {
            "access_token": "jwt",
            "device_id": "d-1",
            "device_token": "tok",
            "role": "worker",
        }
        with patch("cli.main.load_auth", return_value=auth):
            result = await _do_connect(None)
        assert result == 1

    async def test_no_workers_found(self) -> None:
        auth = {
            "access_token": "jwt",
            "device_id": "ctrl-1",
            "device_token": "tok",
            "role": "controller",
            "server_url": "https://test.example.com",
        }
        # list_devices returns only controllers
        devices = [{"id": "ctrl-1", "name": "my-pc", "role": "controller", "platform": "linux"}]

        mock_api = AsyncMock()
        mock_api.list_devices = AsyncMock(return_value=devices)

        with (
            patch("cli.main.load_auth", return_value=auth),
            patch("cli.main.ByfrostAPIClient", return_value=mock_api),
        ):
            result = await _do_connect(None)
        assert result == 1

    async def test_auto_selects_single_worker(self) -> None:
        auth = {
            "access_token": "jwt",
            "device_id": "ctrl-1",
            "device_token": "tok",
            "role": "controller",
            "server_url": "https://test.example.com",
        }
        devices = [
            {"id": "ctrl-1", "name": "my-pc", "role": "controller", "platform": "linux"},
            {"id": "worker-1", "name": "my-mac", "role": "worker", "platform": "macos"},
        ]

        mock_api = AsyncMock()
        mock_api.list_devices = AsyncMock(return_value=devices)
        mock_api.initiate_pairing = AsyncMock(return_value={
            "pairing_id": "pair-1",
            "status": "active",
        })
        mock_api.get_controller_credentials = AsyncMock(return_value={
            "ca_cert": "---CA---",
            "cert": "---CERT---",
            "private_key": "---KEY---",
            "hmac_secret": base64.b64encode(b"secret").decode(),
            "prev_hmac_secret": None,
        })
        mock_api.get_pairing_addresses = AsyncMock(return_value={"addresses": None})

        with (
            patch("cli.main.load_auth", return_value=auth),
            patch("cli.main.ByfrostAPIClient", return_value=mock_api),
            patch("cli.main._save_credentials"),
            patch("cli.main.save_auth"),
        ):
            result = await _do_connect(None)

        assert result == 0
        mock_api.initiate_pairing.assert_called_once_with("jwt", "worker-1", "ctrl-1")
        mock_api.get_controller_credentials.assert_called_once_with("pair-1", "tok")

    async def test_selects_worker_by_name(self) -> None:
        auth = {
            "access_token": "jwt",
            "device_id": "ctrl-1",
            "device_token": "tok",
            "role": "controller",
            "server_url": "https://test.example.com",
        }
        devices = [
            {"id": "worker-1", "name": "mac-mini", "role": "worker", "platform": "macos"},
            {"id": "worker-2", "name": "mac-studio", "role": "worker", "platform": "macos"},
        ]

        mock_api = AsyncMock()
        mock_api.list_devices = AsyncMock(return_value=devices)
        mock_api.initiate_pairing = AsyncMock(return_value={
            "pairing_id": "pair-2",
            "status": "active",
        })
        mock_api.get_controller_credentials = AsyncMock(return_value={
            "ca_cert": "---CA---",
            "cert": "---CERT---",
            "private_key": "---KEY---",
            "hmac_secret": base64.b64encode(b"secret").decode(),
            "prev_hmac_secret": None,
        })
        mock_api.get_pairing_addresses = AsyncMock(return_value={"addresses": None})

        with (
            patch("cli.main.load_auth", return_value=auth),
            patch("cli.main.ByfrostAPIClient", return_value=mock_api),
            patch("cli.main._save_credentials"),
            patch("cli.main.save_auth"),
        ):
            result = await _do_connect("mac-studio")

        assert result == 0
        mock_api.initiate_pairing.assert_called_once_with("jwt", "worker-2", "ctrl-1")

    async def test_handles_existing_pairing(self) -> None:
        auth = {
            "access_token": "jwt",
            "device_id": "ctrl-1",
            "device_token": "tok",
            "role": "controller",
            "server_url": "https://test.example.com",
        }
        devices = [
            {"id": "worker-1", "name": "my-mac", "role": "worker", "platform": "macos"},
        ]

        mock_api = AsyncMock()
        mock_api.list_devices = AsyncMock(return_value=devices)
        mock_api.initiate_pairing = AsyncMock(return_value={
            "pairing_id": "existing-pair",
            "already_exists": True,
        })
        mock_api.get_controller_credentials = AsyncMock(return_value={
            "ca_cert": "---CA---",
            "cert": "---CERT---",
            "private_key": "---KEY---",
            "hmac_secret": base64.b64encode(b"secret").decode(),
            "prev_hmac_secret": None,
        })
        mock_api.get_pairing_addresses = AsyncMock(return_value={"addresses": None})

        with (
            patch("cli.main.load_auth", return_value=auth),
            patch("cli.main.ByfrostAPIClient", return_value=mock_api),
            patch("cli.main._save_credentials"),
            patch("cli.main.save_auth"),
        ):
            result = await _do_connect(None)

        assert result == 0
        mock_api.get_controller_credentials.assert_called_once_with("existing-pair", "tok")
