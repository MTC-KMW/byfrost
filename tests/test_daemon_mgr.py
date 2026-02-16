"""Tests for daemon lifecycle management."""

from unittest.mock import MagicMock, patch

from cli.daemon_mgr import (
    LaunchdManager,
    SystemdManager,
    WindowsManager,
    get_daemon_manager,
)
from cli.main import _do_daemon

# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestGetDaemonManager:
    """Platform-based manager selection."""

    @patch("cli.daemon_mgr.sys")
    def test_darwin_returns_launchd(self, mock_sys: object) -> None:
        mock_sys.platform = "darwin"  # type: ignore[attr-defined]
        mgr = get_daemon_manager()
        assert isinstance(mgr, LaunchdManager)

    @patch("cli.daemon_mgr.sys")
    def test_linux_returns_systemd(self, mock_sys: object) -> None:
        mock_sys.platform = "linux"  # type: ignore[attr-defined]
        mgr = get_daemon_manager()
        assert isinstance(mgr, SystemdManager)

    @patch("cli.daemon_mgr.sys")
    def test_win32_returns_windows(self, mock_sys: object) -> None:
        mock_sys.platform = "win32"  # type: ignore[attr-defined]
        mgr = get_daemon_manager()
        assert isinstance(mgr, WindowsManager)


# ---------------------------------------------------------------------------
# LaunchdManager
# ---------------------------------------------------------------------------


class TestLaunchdManager:
    """macOS launchd daemon management."""

    def test_install_writes_plist(self, tmp_path: object) -> None:
        mgr = LaunchdManager()
        mgr._agents_dir = tmp_path  # type: ignore[assignment]
        mgr._plist_path = tmp_path / "com.byfrost.daemon.plist"  # type: ignore[operator]

        with patch("cli.daemon_mgr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = mgr.install()

        assert result is True
        assert mgr._plist_path.exists()  # type: ignore[union-attr]
        content = mgr._plist_path.read_text()  # type: ignore[union-attr]
        assert "com.byfrost.daemon" in content
        assert "daemon.byfrost_daemon" in content

    def test_uninstall_removes_plist(self, tmp_path: object) -> None:
        mgr = LaunchdManager()
        mgr._agents_dir = tmp_path  # type: ignore[assignment]
        mgr._plist_path = tmp_path / "com.byfrost.daemon.plist"  # type: ignore[operator]
        mgr._plist_path.write_text("<plist/>")  # type: ignore[union-attr]

        with patch("cli.daemon_mgr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = mgr.uninstall()

        assert result is True
        assert not mgr._plist_path.exists()  # type: ignore[union-attr]

    def test_start_calls_launchctl(self) -> None:
        mgr = LaunchdManager()
        with patch("cli.daemon_mgr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = mgr.start()

        assert result is True
        mock_run.assert_called_once_with(
            ["launchctl", "start", "com.byfrost.daemon"],
            capture_output=True,
        )

    def test_stop_calls_launchctl(self) -> None:
        mgr = LaunchdManager()
        with patch("cli.daemon_mgr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = mgr.stop()

        assert result is True
        mock_run.assert_called_once_with(
            ["launchctl", "stop", "com.byfrost.daemon"],
            capture_output=True,
        )

    def test_status_not_installed(self, tmp_path: object) -> None:
        mgr = LaunchdManager()
        mgr._plist_path = tmp_path / "nonexistent.plist"  # type: ignore[operator]

        info = mgr.status()
        assert info["installed"] is False
        assert info["running"] is False

    def test_status_installed_running(self, tmp_path: object) -> None:
        mgr = LaunchdManager()
        plist = tmp_path / "com.byfrost.daemon.plist"  # type: ignore[operator]
        plist.write_text("<plist/>")
        mgr._plist_path = plist  # type: ignore[assignment]

        with patch("cli.daemon_mgr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='"PID" = 12345;\n"Label" = "com.byfrost.daemon";\n',
            )
            info = mgr.status()

        assert info["installed"] is True
        assert info["running"] is True
        assert info["pid"] == 12345


# ---------------------------------------------------------------------------
# SystemdManager
# ---------------------------------------------------------------------------


class TestSystemdManager:
    """Linux systemd daemon management."""

    def test_install_writes_unit(self, tmp_path: object) -> None:
        mgr = SystemdManager()
        mgr._unit_dir = tmp_path  # type: ignore[assignment]
        mgr._unit_path = tmp_path / "byfrost.service"  # type: ignore[operator]

        with patch("cli.daemon_mgr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = mgr.install()

        assert result is True
        assert mgr._unit_path.exists()  # type: ignore[union-attr]
        content = mgr._unit_path.read_text()  # type: ignore[union-attr]
        assert "daemon.byfrost_daemon" in content
        assert "Restart=on-failure" in content

    def test_uninstall_removes_unit(self, tmp_path: object) -> None:
        mgr = SystemdManager()
        mgr._unit_dir = tmp_path  # type: ignore[assignment]
        mgr._unit_path = tmp_path / "byfrost.service"  # type: ignore[operator]
        mgr._unit_path.write_text("[Unit]")  # type: ignore[union-attr]

        with patch("cli.daemon_mgr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = mgr.uninstall()

        assert result is True
        assert not mgr._unit_path.exists()  # type: ignore[union-attr]

    def test_start_calls_systemctl(self) -> None:
        mgr = SystemdManager()
        with patch("cli.daemon_mgr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = mgr.start()

        assert result is True
        mock_run.assert_called_once_with(
            ["systemctl", "--user", "start", "byfrost"],
            capture_output=True, text=True,
        )

    def test_status_not_installed(self, tmp_path: object) -> None:
        mgr = SystemdManager()
        mgr._unit_path = tmp_path / "nonexistent.service"  # type: ignore[operator]

        info = mgr.status()
        assert info["installed"] is False

    def test_status_installed_active(self, tmp_path: object) -> None:
        mgr = SystemdManager()
        unit = tmp_path / "byfrost.service"  # type: ignore[operator]
        unit.write_text("[Unit]")
        mgr._unit_path = unit  # type: ignore[assignment]

        with patch("cli.daemon_mgr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="ActiveState=active\nMainPID=9876\n",
            )
            info = mgr.status()

        assert info["installed"] is True
        assert info["running"] is True
        assert info["pid"] == 9876


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


class TestDoDaemon:
    """CLI daemon command dispatch."""

    def test_install_success(self, capsys: object) -> None:
        mock_mgr = MagicMock()
        mock_mgr.install.return_value = True
        with patch("cli.daemon_mgr.get_daemon_manager", return_value=mock_mgr):
            result = _do_daemon("install")

        assert result == 0
        captured = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "installed" in captured.lower()

    def test_status_not_installed(self, capsys: object) -> None:
        mock_mgr = MagicMock()
        mock_mgr.status.return_value = {"installed": False, "running": False, "pid": None}
        with patch("cli.daemon_mgr.get_daemon_manager", return_value=mock_mgr):
            result = _do_daemon("status")

        assert result == 0
        captured = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "not installed" in captured

    def test_status_running(self, capsys: object) -> None:
        mock_mgr = MagicMock()
        mock_mgr.status.return_value = {"installed": True, "running": True, "pid": 1234}
        with patch("cli.daemon_mgr.get_daemon_manager", return_value=mock_mgr):
            result = _do_daemon("status")

        assert result == 0
        captured = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "running" in captured
        assert "1234" in captured

    def test_unknown_action(self) -> None:
        result = _do_daemon("unknown")
        assert result == 1
