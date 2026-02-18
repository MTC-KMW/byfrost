"""Tests for daemon/file_sync.py - bridge-native file sync."""

import asyncio
import base64
import hashlib
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.ignore import MAX_FILE_SIZE
from daemon.file_sync import (
    DaemonFileSync,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sync(tmp_path: Path) -> DaemonFileSync:
    """Create a DaemonFileSync for testing."""
    # Create a minimal project structure
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "byfrost" / "tasks").mkdir(parents=True, exist_ok=True)
    broadcast = AsyncMock()
    send = AsyncMock()
    sync = DaemonFileSync(
        project_path=str(tmp_path),
        broadcast_fn=broadcast,
        send_fn=send,
        logger=logging.getLogger("test"),
    )
    return sync


def _encode_file(data: bytes) -> tuple[str, str]:
    """Return (base64_data, sha256_checksum) for file contents."""
    return (
        base64.b64encode(data).decode("ascii"),
        hashlib.sha256(data).hexdigest(),
    )


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------

class TestPathValidation:
    """_validate_path rejects unsafe or ignored paths."""

    @pytest.mark.parametrize("rel", [
        "src/main.py",
        "byfrost/tasks/apple/current.md",
        "ios/App.swift",
        "README.md",
        "backend/app/routes.py",
    ])
    def test_valid_project_paths(self, tmp_path: Path, rel: str) -> None:
        sync = _make_sync(tmp_path)
        assert sync._validate_path(rel) is True

    @pytest.mark.parametrize("rel", [
        "../etc/passwd",
        "src/../../etc/passwd",
        "byfrost/../../../secret",
    ])
    def test_rejects_traversal(self, tmp_path: Path, rel: str) -> None:
        sync = _make_sync(tmp_path)
        assert sync._validate_path(rel) is False

    def test_rejects_absolute(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        assert sync._validate_path("/etc/passwd") is False

    @pytest.mark.parametrize("rel", [
        ".git/HEAD",
        ".git/objects/abc123",
        "__pycache__/mod.pyc",
        "node_modules/express/index.js",
        ".DS_Store",
        "src/.DS_Store",
    ])
    def test_rejects_ignored_paths(self, tmp_path: Path, rel: str) -> None:
        sync = _make_sync(tmp_path)
        assert sync._validate_path(rel) is False

    def test_empty_path_rejected(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        assert sync._validate_path("") is False


# ---------------------------------------------------------------------------
# Relative path conversion
# ---------------------------------------------------------------------------

class TestRelativePath:
    """_relative_path converts absolute to validated relative."""

    def test_valid_abs_path(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        abs_path = str(tmp_path / "src" / "main.py")
        assert sync._relative_path(abs_path) == "src/main.py"

    def test_byfrost_path(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        abs_path = str(tmp_path / "byfrost" / "tasks" / "apple" / "current.md")
        assert sync._relative_path(abs_path) == "byfrost/tasks/apple/current.md"

    def test_outside_project(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        abs_path = str(tmp_path.parent / "other" / "file.txt")
        assert sync._relative_path(abs_path) is None

    def test_ignored_path_returns_none(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        abs_path = str(tmp_path / ".git" / "HEAD")
        assert sync._relative_path(abs_path) is None


# ---------------------------------------------------------------------------
# Echo suppression
# ---------------------------------------------------------------------------

class TestEchoSuppression:
    """_suppress prevents watchdog events from re-syncing received files."""

    def test_suppress_blocks_on_local_change(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        sync._loop = asyncio.new_event_loop()
        try:
            sync._suppress("src/main.py")
            abs_path = str(tmp_path / "src" / "main.py")
            sync.on_local_change(abs_path)
            assert "src/main.py" not in sync._pending
        finally:
            sync._loop.close()

    def test_suppress_expires(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        sync._suppressed["src/test.py"] = time.time() - 1
        sync._loop = asyncio.new_event_loop()
        try:
            (tmp_path / "src" / "test.py").write_text("hello")
            abs_path = str(tmp_path / "src" / "test.py")
            sync.on_local_change(abs_path)
            assert "src/test.py" in sync._pending
        finally:
            for h in sync._pending.values():
                h.cancel()
            sync._loop.close()


# ---------------------------------------------------------------------------
# Checksum validation
# ---------------------------------------------------------------------------

class TestChecksum:
    """Inbound sync validates SHA-256 checksum."""

    @pytest.mark.asyncio
    async def test_valid_checksum_accepted(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        data = b"test content"
        b64, checksum = _encode_file(data)
        await sync.handle_file_sync(None, {
            "path": "src/test.md",
            "data": b64,
            "checksum": checksum,
            "mtime": time.time(),
        })
        written = (tmp_path / "src" / "test.md").read_bytes()
        assert written == data

    @pytest.mark.asyncio
    async def test_invalid_checksum_rejected(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        data = b"test content"
        b64, _ = _encode_file(data)
        await sync.handle_file_sync(None, {
            "path": "src/test.md",
            "data": b64,
            "checksum": "bad_checksum",
            "mtime": time.time(),
        })
        assert not (tmp_path / "src" / "test.md").exists()

    @pytest.mark.asyncio
    async def test_invalid_base64_rejected(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        checksum = hashlib.sha256(b"test").hexdigest()
        await sync.handle_file_sync(None, {
            "path": "src/test.md",
            "data": "not!valid@base64",
            "checksum": checksum,
            "mtime": time.time(),
        })
        assert not (tmp_path / "src" / "test.md").exists()


# ---------------------------------------------------------------------------
# Symlink escape prevention
# ---------------------------------------------------------------------------

class TestSymlinkEscape:
    """Symlink attacks are rejected by _is_inside_project."""

    @pytest.mark.asyncio
    async def test_symlink_write_rejected(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        # Create escape target OUTSIDE the project directory
        escape_dir = tmp_path.parent / "escape_target"
        escape_dir.mkdir(exist_ok=True)
        (tmp_path / "src" / "escape").symlink_to(escape_dir)

        data = b"malicious"
        b64, checksum = _encode_file(data)
        await sync.handle_file_sync(None, {
            "path": "src/escape/payload.md",
            "data": b64,
            "checksum": checksum,
            "mtime": time.time(),
        })
        assert not (escape_dir / "payload.md").exists()

    @pytest.mark.asyncio
    async def test_symlink_deletion_rejected(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        outside = tmp_path / "important.txt"
        outside.write_text("keep me")
        (tmp_path / "src" / "important.txt").symlink_to(outside)

        await sync.handle_file_sync(None, {
            "path": "src/important.txt",
            "deleted": True,
        })
        assert outside.exists()

    def test_is_inside_project_true(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        target = tmp_path / "src" / "test.md"
        target.write_text("ok")
        assert sync._is_inside_project(target) is True

    def test_is_inside_project_false(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("nope")
        assert sync._is_inside_project(outside) is False


# ---------------------------------------------------------------------------
# mtime clamping
# ---------------------------------------------------------------------------

class TestMtimeClamping:
    """Far-future or ancient mtime values are clamped to prevent LWW poisoning."""

    @pytest.mark.asyncio
    async def test_far_future_mtime_clamped(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        data = b"content"
        b64, checksum = _encode_file(data)
        await sync.handle_file_sync(None, {
            "path": "src/test.md",
            "data": b64,
            "checksum": checksum,
            "mtime": 9999999999.0,  # Year 2286
        })
        target = tmp_path / "src" / "test.md"
        assert target.exists()
        assert target.stat().st_mtime < time.time() + 86401

    @pytest.mark.asyncio
    async def test_ancient_mtime_clamped(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        data = b"content"
        b64, checksum = _encode_file(data)
        await sync.handle_file_sync(None, {
            "path": "src/test.md",
            "data": b64,
            "checksum": checksum,
            "mtime": 100.0,  # Year 1970
        })
        target = tmp_path / "src" / "test.md"
        assert target.exists()
        assert target.stat().st_mtime > 946684800


# ---------------------------------------------------------------------------
# File size limit
# ---------------------------------------------------------------------------

class TestFileSize:
    """Files exceeding MAX_FILE_SIZE are skipped."""

    @pytest.mark.asyncio
    async def test_oversized_file_skipped(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        big_file = tmp_path / "src" / "big.bin"
        big_file.write_bytes(b"x" * (MAX_FILE_SIZE + 1))

        await sync._send_file("src/big.bin", deleted=False)
        sync._broadcast.assert_not_called()

    @pytest.mark.asyncio
    async def test_at_limit_accepted(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        limit_file = tmp_path / "src" / "limit.txt"
        limit_file.write_bytes(b"x" * MAX_FILE_SIZE)

        await sync._send_file("src/limit.txt", deleted=False)
        sync._broadcast.assert_called_once()
        call_args = sync._broadcast.call_args
        assert call_args[0][0] == "file.sync"


# ---------------------------------------------------------------------------
# Inbound sync: file.sync writes to disk
# ---------------------------------------------------------------------------

class TestInboundSync:
    """handle_file_sync writes files and handles deletions."""

    @pytest.mark.asyncio
    async def test_write_new_file(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        data = b"hello world"
        b64, checksum = _encode_file(data)
        mtime = time.time() - 10

        await sync.handle_file_sync(None, {
            "path": "src/test.py",
            "data": b64,
            "checksum": checksum,
            "mtime": mtime,
        })

        written = tmp_path / "src" / "test.py"
        assert written.exists()
        assert written.read_bytes() == data

    @pytest.mark.asyncio
    async def test_deletion_removes_file(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        target = tmp_path / "src" / "to_delete.py"
        target.write_text("old content")
        assert target.exists()

        await sync.handle_file_sync(None, {
            "path": "src/to_delete.py",
            "deleted": True,
        })
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_mtime_preserved(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        data = b"timestamped"
        b64, checksum = _encode_file(data)
        target_mtime = 1700000000.0

        await sync.handle_file_sync(None, {
            "path": "src/timed.py",
            "data": b64,
            "checksum": checksum,
            "mtime": target_mtime,
        })

        written = tmp_path / "src" / "timed.py"
        assert abs(written.stat().st_mtime - target_mtime) < 1


# ---------------------------------------------------------------------------
# Last-write-wins
# ---------------------------------------------------------------------------

class TestLastWriteWins:
    """Newer remote files overwrite, older ones do not."""

    @pytest.mark.asyncio
    async def test_newer_remote_overwrites(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        target = tmp_path / "src" / "conflict.py"
        target.write_text("local")
        old_mtime = target.stat().st_mtime

        new_data = b"remote wins"
        b64, checksum = _encode_file(new_data)
        await sync.handle_file_sync(None, {
            "path": "src/conflict.py",
            "data": b64,
            "checksum": checksum,
            "mtime": old_mtime + 100,
        })
        assert target.read_bytes() == new_data

    @pytest.mark.asyncio
    async def test_older_remote_does_not_overwrite(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        target = tmp_path / "src" / "conflict.py"
        target.write_text("local wins")
        local_mtime = target.stat().st_mtime

        old_data = b"old remote"
        b64, checksum = _encode_file(old_data)
        await sync.handle_file_sync(None, {
            "path": "src/conflict.py",
            "data": b64,
            "checksum": checksum,
            "mtime": local_mtime - 100,
        })
        assert target.read_text() == "local wins"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class TestManifest:
    """send_full_manifest sends all project files."""

    @pytest.mark.asyncio
    async def test_sends_all_files(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        (tmp_path / "src" / "main.py").write_text("print('hello')")
        (tmp_path / "README.md").write_text("# Project")
        (tmp_path / "byfrost" / "tasks" / "current.md").write_text("task")

        ws = AsyncMock()
        await sync.send_full_manifest(ws)
        assert sync._send.call_count == 3

    @pytest.mark.asyncio
    async def test_skips_oversized(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        (tmp_path / "small.txt").write_text("small")
        (tmp_path / "big.bin").write_bytes(b"x" * (MAX_FILE_SIZE + 1))

        ws = AsyncMock()
        await sync.send_full_manifest(ws)
        assert sync._send.call_count == 1

    @pytest.mark.asyncio
    async def test_skips_ignored(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        (tmp_path / "src" / "main.py").write_text("ok")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main")
        (tmp_path / ".DS_Store").write_bytes(b"\x00" * 10)

        ws = AsyncMock()
        await sync.send_full_manifest(ws)
        # Only src/main.py should be sent
        assert sync._send.call_count == 1


# ---------------------------------------------------------------------------
# Outbound: _send_file
# ---------------------------------------------------------------------------

class TestSendFile:
    """_send_file broadcasts file contents or deletions."""

    @pytest.mark.asyncio
    async def test_send_file_content(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        (tmp_path / "src" / "test.py").write_text("hello")

        await sync._send_file("src/test.py", deleted=False)
        sync._broadcast.assert_called_once()
        call_args = sync._broadcast.call_args
        assert call_args[0][0] == "file.sync"
        payload = call_args[0][1]
        assert payload["path"] == "src/test.py"
        assert payload["checksum"] == hashlib.sha256(b"hello").hexdigest()

    @pytest.mark.asyncio
    async def test_send_deletion(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        await sync._send_file("src/deleted.py", deleted=True)
        sync._broadcast.assert_called_once()
        call_args = sync._broadcast.call_args
        assert call_args[0][0] == "file.changed"
        assert call_args[0][1]["deleted"] is True


# ---------------------------------------------------------------------------
# Process management (cli/file_sync.py)
# ---------------------------------------------------------------------------

class TestProcessManagement:
    """start_sync/stop_sync/sync_status manage PID file."""

    def test_start_writes_pid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli import file_sync

        pid_file = tmp_path / "sync.pid"
        log_file = tmp_path / "sync.log"
        monkeypatch.setattr(file_sync, "PID_FILE", pid_file)
        monkeypatch.setattr(file_sync, "LOG_FILE", log_file)
        monkeypatch.setattr(file_sync, "BRIDGE_DIR", tmp_path)

        class FakeProc:
            pid = 12345

        monkeypatch.setattr(
            "subprocess.Popen", lambda *a, **kw: FakeProc()
        )

        result = file_sync.start_sync(tmp_path)
        assert result == 0
        assert pid_file.exists()
        assert pid_file.read_text() == "12345"

    def test_stop_removes_pid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli import file_sync

        pid_file = tmp_path / "sync.pid"
        pid_file.write_text("99999")
        monkeypatch.setattr(file_sync, "PID_FILE", pid_file)

        result = file_sync.stop_sync()
        assert result == 0
        assert not pid_file.exists()

    def test_status_not_running(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli import file_sync

        pid_file = tmp_path / "sync.pid"
        monkeypatch.setattr(file_sync, "PID_FILE", pid_file)

        result = file_sync.sync_status()
        assert result == 1

    def test_status_stale_pid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli import file_sync

        pid_file = tmp_path / "sync.pid"
        pid_file.write_text("99999")
        monkeypatch.setattr(file_sync, "PID_FILE", pid_file)

        result = file_sync.sync_status()
        assert result == 1
        assert not pid_file.exists()
