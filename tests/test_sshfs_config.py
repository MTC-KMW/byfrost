"""Tests for cli/sshfs_config.py - SSHFS mount configuration."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from cli.sshfs_config import (
    MOUNT_DIRS,
    SSHFSConfig,
    _generate_sshfs_plist,
    check_prerequisites,
    check_ssh_connectivity,
    detect_macfuse,
    detect_sshfs,
    install_launchd_remount,
    load_sshfs_config,
    run_sshfs_command,
    save_sshfs_config,
    sshfs_mount,
    sshfs_status,
    sshfs_unmount,
    uninstall_launchd_remount,
    verify_mounts,
)

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestDetectMacfuse:
    """macFUSE detection."""

    @patch("cli.sshfs_config.Path.exists", return_value=True)
    def test_found(self, _mock: object) -> None:
        assert detect_macfuse() is True

    @patch("cli.sshfs_config.MACFUSE_PATHS", [Path("/nonexistent/path")])
    def test_not_found(self) -> None:
        assert detect_macfuse() is False


class TestDetectSshfs:
    """sshfs command detection."""

    @patch("cli.sshfs_config.shutil.which", return_value="/usr/local/bin/sshfs")
    def test_found(self, _mock: object) -> None:
        assert detect_sshfs() is True

    @patch("cli.sshfs_config.shutil.which", return_value=None)
    def test_not_found(self, _mock: object) -> None:
        assert detect_sshfs() is False


class TestCheckPrerequisites:
    """Prerequisite checking."""

    @patch("cli.sshfs_config.detect_sshfs", return_value=True)
    @patch("cli.sshfs_config.detect_macfuse", return_value=True)
    def test_all_present(self, _a: object, _b: object) -> None:
        ok, missing = check_prerequisites()
        assert ok is True
        assert len(missing) == 0

    @patch("cli.sshfs_config.detect_sshfs", return_value=True)
    @patch("cli.sshfs_config.detect_macfuse", return_value=False)
    def test_macfuse_missing(self, _a: object, _b: object) -> None:
        ok, missing = check_prerequisites()
        assert ok is False
        assert any("macFUSE" in m for m in missing)

    @patch("cli.sshfs_config.detect_sshfs", return_value=False)
    @patch("cli.sshfs_config.detect_macfuse", return_value=True)
    def test_sshfs_missing(self, _a: object, _b: object) -> None:
        ok, missing = check_prerequisites()
        assert ok is False
        assert any("sshfs" in m for m in missing)

    @patch("cli.sshfs_config.detect_sshfs", return_value=False)
    @patch("cli.sshfs_config.detect_macfuse", return_value=False)
    def test_both_missing(self, _a: object, _b: object) -> None:
        ok, missing = check_prerequisites()
        assert ok is False
        assert len(missing) == 2


class TestCheckSshConnectivity:
    """SSH connectivity testing."""

    @patch("cli.sshfs_config.subprocess.run")
    def test_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        assert check_ssh_connectivity("host") is True

    @patch("cli.sshfs_config.subprocess.run")
    def test_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=255)
        assert check_ssh_connectivity("host") is False

    @patch("cli.sshfs_config.subprocess.run", side_effect=FileNotFoundError)
    def test_ssh_not_found(self, _mock: object) -> None:
        assert check_ssh_connectivity("host") is False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestSSHFSConfig:
    """Config save/load."""

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        config = SSHFSConfig(
            controller_hostname="linux-box",
            project_path="/home/user/project",
            local_project=str(tmp_path),
        )
        save_sshfs_config(tmp_path, config)
        loaded = load_sshfs_config(tmp_path)
        assert loaded is not None
        assert loaded.controller_hostname == "linux-box"
        assert loaded.project_path == "/home/user/project"

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        assert load_sshfs_config(tmp_path) is None

    def test_load_empty_hostname_returns_none(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".agent-team"
        config_dir.mkdir()
        (config_dir / "config.env").write_text(
            "LINUX_HOSTNAME=\nLINUX_PROJECT_PATH=/path\n"
        )
        assert load_sshfs_config(tmp_path) is None

    def test_ignores_comments_and_blank_lines(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".agent-team"
        config_dir.mkdir()
        (config_dir / "config.env").write_text(
            "# Comment\n\n"
            "LINUX_HOSTNAME=host\n"
            "LINUX_PROJECT_PATH=/path\n"
        )
        loaded = load_sshfs_config(tmp_path)
        assert loaded is not None
        assert loaded.controller_hostname == "host"


# ---------------------------------------------------------------------------
# Mount operations
# ---------------------------------------------------------------------------


class TestMount:
    """SSHFS mount operations."""

    @patch("cli.sshfs_config.subprocess.run")
    @patch("cli.sshfs_config.os.path.ismount", return_value=False)
    def test_mounts_all_dirs(
        self, _mock_ismount: object, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        config = SSHFSConfig("host", "/remote/path", str(tmp_path))

        success, mounted = sshfs_mount(tmp_path, config)

        assert success is True
        assert len(mounted) == len(MOUNT_DIRS)
        assert mock_run.call_count == len(MOUNT_DIRS)

    @patch("cli.sshfs_config.os.path.ismount", return_value=True)
    def test_skips_already_mounted(
        self, _mock: object, tmp_path: Path,
    ) -> None:
        config = SSHFSConfig("host", "/remote/path", str(tmp_path))
        success, mounted = sshfs_mount(tmp_path, config)
        assert success is True
        assert len(mounted) == len(MOUNT_DIRS)

    @patch("cli.sshfs_config.subprocess.run")
    @patch("cli.sshfs_config.os.path.ismount", return_value=False)
    def test_handles_mount_failure(
        self, _mock_ismount: object, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        mock_run.return_value = MagicMock(returncode=1, stderr="mount failed")
        config = SSHFSConfig("host", "/remote/path", str(tmp_path))

        success, mounted = sshfs_mount(tmp_path, config)

        assert success is False
        assert len(mounted) == 0


class TestUnmount:
    """SSHFS unmount operations."""

    @patch("cli.sshfs_config.subprocess.run")
    @patch("cli.sshfs_config.os.path.ismount", return_value=True)
    def test_unmounts_all(
        self, _mock_ismount: object, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        # Create mount point dirs so rmdir works
        for d in MOUNT_DIRS:
            (tmp_path / d).mkdir(parents=True)

        _, unmounted = sshfs_unmount(tmp_path)

        assert len(unmounted) == len(MOUNT_DIRS)

    @patch("cli.sshfs_config.os.path.ismount", return_value=False)
    def test_skips_unmounted(self, _mock: object, tmp_path: Path) -> None:
        _, unmounted = sshfs_unmount(tmp_path)
        assert len(unmounted) == 0

    @patch("cli.sshfs_config.subprocess.run")
    @patch("cli.sshfs_config.os.path.ismount", return_value=True)
    def test_restores_backup(
        self, _mock_ismount: object, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        # Create mount point and backup
        mount_dir = tmp_path / "shared"
        mount_dir.mkdir()
        backup = tmp_path / "shared.local-backup"
        backup.mkdir()
        (backup / "test.txt").write_text("original")

        sshfs_unmount(tmp_path)

        # Backup should be restored
        assert (tmp_path / "shared" / "test.txt").exists()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestStatus:
    """SSHFS status checking."""

    @patch("cli.sshfs_config.os.access", return_value=True)
    @patch("cli.sshfs_config.os.path.ismount", return_value=True)
    def test_all_mounted(self, _a: object, _b: object, tmp_path: Path) -> None:
        result = sshfs_status(tmp_path)
        assert len(result) == len(MOUNT_DIRS)
        for info in result.values():
            assert info["mounted"] is True
            assert info["readable"] is True
            assert info["writable"] is True

    @patch("cli.sshfs_config.os.path.ismount", return_value=False)
    def test_none_mounted(self, _mock: object, tmp_path: Path) -> None:
        result = sshfs_status(tmp_path)
        for info in result.values():
            assert info["mounted"] is False
            assert info["readable"] is False


# ---------------------------------------------------------------------------
# Verify mounts
# ---------------------------------------------------------------------------


class TestVerifyMounts:
    """Mount read/write verification."""

    @patch("cli.sshfs_config.os.path.ismount", return_value=True)
    def test_pass(self, _mock: object, tmp_path: Path) -> None:
        # Create dirs so write test works
        for d in MOUNT_DIRS:
            (tmp_path / d).mkdir(parents=True)

        assert verify_mounts(tmp_path) is True

    @patch("cli.sshfs_config.os.path.ismount", return_value=False)
    def test_fail_not_mounted(self, _mock: object, tmp_path: Path) -> None:
        assert verify_mounts(tmp_path) is False


# ---------------------------------------------------------------------------
# Launchd plist
# ---------------------------------------------------------------------------


class TestLaunchdPlist:
    """Launchd plist generation and install."""

    def test_generates_valid_xml(self, tmp_path: Path) -> None:
        plist = _generate_sshfs_plist(tmp_path)
        assert "<?xml version" in plist
        assert "com.byfrost.sshfs" in plist
        assert str(tmp_path) in plist
        assert "RunAtLoad" in plist
        assert "sshfs-mount.sh" in plist

    @patch("cli.sshfs_config.subprocess.run")
    @patch("cli.sshfs_config.LAUNCHD_PLIST")
    def test_install(self, mock_plist: MagicMock, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_plist.parent = tmp_path
        mock_plist.write_text = MagicMock()
        mock_run.return_value = MagicMock(returncode=0)

        result = install_launchd_remount(tmp_path)
        assert result is True
        mock_plist.write_text.assert_called_once()

    @patch("cli.sshfs_config.LAUNCHD_PLIST")
    def test_uninstall_no_plist(self, mock_plist: MagicMock) -> None:
        mock_plist.exists.return_value = False
        assert uninstall_launchd_remount() is True

    @patch("cli.sshfs_config.subprocess.run")
    @patch("cli.sshfs_config.LAUNCHD_PLIST")
    def test_uninstall_with_plist(
        self, mock_plist: MagicMock, mock_run: MagicMock,
    ) -> None:
        mock_plist.exists.return_value = True
        mock_run.return_value = MagicMock(returncode=0)

        assert uninstall_launchd_remount() is True
        mock_plist.unlink.assert_called_once()


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------


class TestRunSshfsSetup:
    """Interactive setup wizard."""

    @patch("cli.sshfs_config.sys.platform", "linux")
    def test_non_darwin_exits(self, tmp_path: Path) -> None:
        from cli.sshfs_config import run_sshfs_setup
        result = run_sshfs_setup(tmp_path)
        assert result == 1

    def test_full_setup(self, tmp_path: Path) -> None:
        with (
            patch("cli.sshfs_config.sys.platform", "darwin"),
            patch("cli.sshfs_config.TeamConfig.load", return_value=None),
            patch("cli.sshfs_config._prompt", side_effect=["linux-box", "/home/user/proj"]),
            patch("cli.sshfs_config._prompt_yn", return_value=True),
            patch("cli.sshfs_config.check_prerequisites", return_value=(True, [])),
            patch("cli.sshfs_config.check_ssh_connectivity", return_value=True),
            patch("cli.sshfs_config.sshfs_mount", return_value=(True, MOUNT_DIRS)),
            patch("cli.sshfs_config.verify_mounts", return_value=True),
            patch("cli.sshfs_config.install_launchd_remount", return_value=True),
        ):
            from cli.sshfs_config import run_sshfs_setup
            result = run_sshfs_setup(tmp_path)
            assert result == 0

        # Config should be saved
        config = load_sshfs_config(tmp_path)
        assert config is not None
        assert config.controller_hostname == "linux-box"

    def test_missing_prereqs_exits(self, tmp_path: Path) -> None:
        with (
            patch("cli.sshfs_config.sys.platform", "darwin"),
            patch("cli.sshfs_config.check_prerequisites", return_value=(False, ["macFUSE"])),
        ):
            from cli.sshfs_config import run_sshfs_setup
            result = run_sshfs_setup(tmp_path)
            assert result == 1


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------


class TestRunSshfsCommand:
    """Command dispatch."""

    @patch("cli.sshfs_config.sshfs_status")
    def test_status_dispatch(self, mock_status: MagicMock, tmp_path: Path) -> None:
        mock_status.return_value = {d: {"mounted": False, "readable": False, "writable": False}
                                    for d in MOUNT_DIRS}
        result = run_sshfs_command("status", tmp_path)
        assert result == 0

    def test_mount_no_config(self, tmp_path: Path) -> None:
        result = run_sshfs_command("mount", tmp_path)
        assert result == 1

    @patch("cli.sshfs_config.sshfs_unmount", return_value=(True, []))
    def test_unmount_dispatch(self, _mock: object, tmp_path: Path) -> None:
        result = run_sshfs_command("unmount", tmp_path)
        assert result == 0

    def test_unknown_action(self, tmp_path: Path) -> None:
        result = run_sshfs_command("invalid", tmp_path)
        assert result == 1

    @patch("cli.sshfs_config.sshfs_status", side_effect=KeyboardInterrupt)
    def test_keyboard_interrupt(self, _mock: object, tmp_path: Path) -> None:
        result = run_sshfs_command("status", tmp_path)
        assert result == 130
