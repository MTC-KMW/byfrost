"""Tests for byfrost account and logout commands."""

from unittest.mock import AsyncMock, patch

from cli.main import _do_account, _do_logout

# ---------------------------------------------------------------------------
# Account command
# ---------------------------------------------------------------------------


class TestDoAccount:
    """byfrost account - display account info."""

    async def test_not_logged_in(self) -> None:
        with patch("cli.main.load_auth", return_value=None):
            result = await _do_account()
        assert result == 1

    async def test_shows_local_info(self, capsys: object) -> None:
        auth = {
            "access_token": "jwt",
            "github_username": "octocat",
            "server_url": "https://api.byfrost.dev",
            "device_id": "d-123",
            "role": "controller",
            "platform": "linux",
            "pairing_id": "pair-456",
        }
        mock_api = AsyncMock()
        mock_api.list_devices = AsyncMock(return_value=[
            {
                "id": "d-123",
                "name": "my-pc",
                "role": "controller",
                "platform": "linux",
                "last_heartbeat": None,
            },
            {
                "id": "w-789",
                "name": "my-mac",
                "role": "worker",
                "platform": "macos",
                "last_heartbeat": "2026-02-16T10:00:00",
            },
        ])

        with (
            patch("cli.main.load_auth", return_value=auth),
            patch("cli.main.ByfrostAPIClient", return_value=mock_api),
        ):
            result = await _do_account()

        assert result == 0
        captured = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "octocat" in captured
        assert "pair-456" in captured
        assert "my-mac" in captured

    async def test_api_failure_still_shows_local(self, capsys: object) -> None:
        auth = {
            "access_token": "jwt",
            "github_username": "octocat",
            "server_url": "https://api.byfrost.dev",
            "device_id": "d-123",
            "role": "controller",
            "platform": "linux",
        }
        mock_api = AsyncMock()
        mock_api.list_devices = AsyncMock(side_effect=Exception("network error"))

        with (
            patch("cli.main.load_auth", return_value=auth),
            patch("cli.main.ByfrostAPIClient", return_value=mock_api),
        ):
            result = await _do_account()

        assert result == 0
        captured = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "octocat" in captured
        assert "expired" in captured  # warning about token


# ---------------------------------------------------------------------------
# Logout command
# ---------------------------------------------------------------------------


class TestDoLogout:
    """byfrost logout - unregister and cleanup."""

    async def test_not_logged_in(self) -> None:
        with patch("cli.main.load_auth", return_value=None):
            result = await _do_logout()
        assert result == 1

    async def test_full_cleanup(self, tmp_path: object) -> None:
        auth_file = tmp_path / "auth.json"  # type: ignore[operator]
        auth_file.write_text("{}")
        secret_file = tmp_path / "secret"  # type: ignore[operator]
        secret_file.write_text("abc123")
        certs_dir = tmp_path / "certs"  # type: ignore[operator]
        certs_dir.mkdir()
        (certs_dir / "ca.pem").write_text("ca")
        (certs_dir / "client.pem").write_text("cert")
        (certs_dir / "client.key").write_text("key")

        auth = {
            "access_token": "jwt",
            "device_id": "d-123",
            "github_username": "octocat",
            "server_url": "https://api.byfrost.dev",
        }
        mock_api = AsyncMock()
        mock_api.delete_device = AsyncMock()

        with (
            patch("cli.main.load_auth", return_value=auth),
            patch("cli.main.ByfrostAPIClient", return_value=mock_api),
            patch("cli.main.AUTH_FILE", auth_file),
            patch("cli.main.SECRET_FILE", secret_file),
            patch("cli.main.CERTS_DIR", certs_dir),
        ):
            result = await _do_logout()

        assert result == 0
        mock_api.delete_device.assert_called_once_with("jwt", "d-123")
        assert not auth_file.exists()
        assert not secret_file.exists()
        assert not certs_dir.exists()

    async def test_server_unreachable_still_cleans(self, tmp_path: object) -> None:
        auth_file = tmp_path / "auth.json"  # type: ignore[operator]
        auth_file.write_text("{}")
        secret_file = tmp_path / "secret"  # type: ignore[operator]
        secret_file.write_text("abc")
        certs_dir = tmp_path / "certs"  # type: ignore[operator]
        certs_dir.mkdir()

        auth = {
            "access_token": "jwt",
            "device_id": "d-123",
            "github_username": "octocat",
            "server_url": "https://api.byfrost.dev",
        }
        mock_api = AsyncMock()
        mock_api.delete_device = AsyncMock(side_effect=Exception("offline"))

        with (
            patch("cli.main.load_auth", return_value=auth),
            patch("cli.main.ByfrostAPIClient", return_value=mock_api),
            patch("cli.main.AUTH_FILE", auth_file),
            patch("cli.main.SECRET_FILE", secret_file),
            patch("cli.main.CERTS_DIR", certs_dir),
        ):
            result = await _do_logout()

        assert result == 0
        assert not auth_file.exists()
        assert not secret_file.exists()
