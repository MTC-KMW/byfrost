"""Tests for daemon/file_sync.py - bridge-native file sync."""

import asyncio
import base64
import hashlib
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from daemon.file_sync import (
    MAX_FILE_SIZE,
    SYNC_DIRS,
    DaemonFileSync,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sync(tmp_path: Path) -> DaemonFileSync:
    """Create a DaemonFileSync for testing."""
    bf = tmp_path / "byfrost"
    bf.mkdir(exist_ok=True)
    for d in SYNC_DIRS:
        (bf / d).mkdir(parents=True, exist_ok=True)
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
    """_validate_path rejects unsafe or out-of-scope paths."""

    @pytest.mark.parametrize("rel", [
        "tasks/apple/current.md",
        "shared/api-spec.yaml",
        "compound/patterns.md",
        "pm/CLAUDE.md",
        "qa/review-report.md",
        "tasks/backend/current.md",
    ])
    def test_valid_sync_paths(self, tmp_path: Path, rel: str) -> None:
        sync = _make_sync(tmp_path)
        assert sync._validate_path(rel) is True

    @pytest.mark.parametrize("rel", [
        "../etc/passwd",
        "tasks/../../etc/passwd",
        "tasks/apple/../../../secret",
    ])
    def test_rejects_traversal(self, tmp_path: Path, rel: str) -> None:
        sync = _make_sync(tmp_path)
        assert sync._validate_path(rel) is False

    def test_rejects_absolute(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        assert sync._validate_path("/etc/passwd") is False

    @pytest.mark.parametrize("rel", [
        "apple/Sources/App.swift",
        "backend/app/main.py",
        "web/src/index.tsx",
        "other/file.txt",
    ])
    def test_rejects_code_dirs(self, tmp_path: Path, rel: str) -> None:
        sync = _make_sync(tmp_path)
        assert sync._validate_path(rel) is False

    def test_accepts_all_five_sync_dirs(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        for d in SYNC_DIRS:
            assert sync._validate_path(f"{d}/file.md") is True

    @pytest.mark.parametrize("rel", [
        "tasks/.DS_Store",
        "shared/.DS_Store",
        "tasks/apple/.DS_Store",
        "pm/Thumbs.db",
        "qa/desktop.ini",
    ])
    def test_rejects_ignored_files(self, tmp_path: Path, rel: str) -> None:
        sync = _make_sync(tmp_path)
        assert sync._validate_path(rel) is False


# ---------------------------------------------------------------------------
# Relative path conversion
# ---------------------------------------------------------------------------

class TestRelativePath:
    """_relative_path converts absolute to validated relative."""

    def test_valid_abs_path(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        bf = tmp_path / "byfrost"
        abs_path = str(bf / "tasks" / "apple" / "current.md")
        assert sync._relative_path(abs_path) == "tasks/apple/current.md"

    def test_outside_byfrost(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        abs_path = str(tmp_path / "other" / "file.txt")
        assert sync._relative_path(abs_path) is None

    def test_code_dir_returns_none(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        bf = tmp_path / "byfrost"
        abs_path = str(bf / "apple" / "App.swift")
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
            sync._suppress("tasks/apple/current.md")
            bf = tmp_path / "byfrost"
            abs_path = str(bf / "tasks" / "apple" / "current.md")
            sync.on_local_change(abs_path)
            # Should NOT schedule a pending send
            assert "tasks/apple/current.md" not in sync._pending
        finally:
            sync._loop.close()

    def test_suppress_expires(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        # Set suppression in the past
        sync._suppressed["tasks/test.md"] = time.time() - 1
        sync._loop = asyncio.new_event_loop()
        try:
            bf = tmp_path / "byfrost"
            (bf / "tasks" / "test.md").write_text("hello")
            abs_path = str(bf / "tasks" / "test.md")
            sync.on_local_change(abs_path)
            # Should schedule a pending send (suppression expired)
            assert "tasks/test.md" in sync._pending
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
            "path": "tasks/test.md",
            "data": b64,
            "checksum": checksum,
            "mtime": time.time(),
        })
        written = (tmp_path / "byfrost" / "tasks" / "test.md").read_bytes()
        assert written == data

    @pytest.mark.asyncio
    async def test_invalid_checksum_rejected(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        data = b"test content"
        b64, _ = _encode_file(data)
        await sync.handle_file_sync(None, {
            "path": "tasks/test.md",
            "data": b64,
            "checksum": "bad_checksum",
            "mtime": time.time(),
        })
        assert not (tmp_path / "byfrost" / "tasks" / "test.md").exists()

    @pytest.mark.asyncio
    async def test_invalid_base64_rejected(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        checksum = hashlib.sha256(b"test").hexdigest()
        await sync.handle_file_sync(None, {
            "path": "tasks/test.md",
            "data": "not!valid@base64",
            "checksum": checksum,
            "mtime": time.time(),
        })
        assert not (tmp_path / "byfrost" / "tasks" / "test.md").exists()


# ---------------------------------------------------------------------------
# Symlink escape prevention
# ---------------------------------------------------------------------------

class TestSymlinkEscape:
    """Symlink attacks are rejected by _is_inside_byfrost."""

    @pytest.mark.asyncio
    async def test_symlink_write_rejected(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        bf = tmp_path / "byfrost"
        # Create a symlink inside tasks/ pointing outside byfrost/
        escape_dir = tmp_path / "escape_target"
        escape_dir.mkdir()
        (bf / "tasks" / "escape").symlink_to(escape_dir)

        data = b"malicious"
        b64, checksum = _encode_file(data)
        await sync.handle_file_sync(None, {
            "path": "tasks/escape/payload.md",
            "data": b64,
            "checksum": checksum,
            "mtime": time.time(),
        })
        # File should NOT be written to the escape target
        assert not (escape_dir / "payload.md").exists()

    @pytest.mark.asyncio
    async def test_symlink_deletion_rejected(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        bf = tmp_path / "byfrost"
        # Create a real file outside byfrost
        outside = tmp_path / "important.txt"
        outside.write_text("keep me")
        # Symlink inside sync dir pointing to it
        (bf / "tasks" / "important.txt").symlink_to(outside)

        await sync.handle_file_sync(None, {
            "path": "tasks/important.txt",
            "deleted": True,
        })
        # The outside file should NOT be deleted
        assert outside.exists()

    def test_is_inside_byfrost_true(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        bf = tmp_path / "byfrost"
        target = bf / "tasks" / "test.md"
        target.write_text("ok")
        assert sync._is_inside_byfrost(target) is True

    def test_is_inside_byfrost_false(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("nope")
        assert sync._is_inside_byfrost(outside) is False


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
            "path": "tasks/test.md",
            "data": b64,
            "checksum": checksum,
            "mtime": 9999999999.0,  # Year 2286
        })
        target = tmp_path / "byfrost" / "tasks" / "test.md"
        assert target.exists()
        # mtime should be clamped to roughly now, not year 2286
        assert target.stat().st_mtime < time.time() + 86401

    @pytest.mark.asyncio
    async def test_ancient_mtime_clamped(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        data = b"content"
        b64, checksum = _encode_file(data)
        await sync.handle_file_sync(None, {
            "path": "tasks/test.md",
            "data": b64,
            "checksum": checksum,
            "mtime": 100.0,  # Year 1970
        })
        target = tmp_path / "byfrost" / "tasks" / "test.md"
        assert target.exists()
        # mtime should be clamped to roughly now
        assert target.stat().st_mtime > 946684800


# ---------------------------------------------------------------------------
# File size limit
# ---------------------------------------------------------------------------

class TestFileSize:
    """Files exceeding MAX_FILE_SIZE are skipped."""

    @pytest.mark.asyncio
    async def test_oversized_file_skipped(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        bf = tmp_path / "byfrost"
        big_file = bf / "tasks" / "big.md"
        big_file.write_bytes(b"x" * (MAX_FILE_SIZE + 1))

        await sync._send_file("tasks/big.md", deleted=False)
        sync._broadcast.assert_not_called()

    @pytest.mark.asyncio
    async def test_at_limit_accepted(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        bf = tmp_path / "byfrost"
        limit_file = bf / "tasks" / "limit.md"
        limit_file.write_bytes(b"x" * MAX_FILE_SIZE)

        await sync._send_file("tasks/limit.md", deleted=False)
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
        mtime = time.time() - 10  # Some time in the past

        await sync.handle_file_sync(None, {
            "path": "shared/test.yaml",
            "data": b64,
            "checksum": checksum,
            "mtime": mtime,
        })

        written = (tmp_path / "byfrost" / "shared" / "test.yaml")
        assert written.exists()
        assert written.read_bytes() == data

    @pytest.mark.asyncio
    async def test_deletion_removes_file(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        target = tmp_path / "byfrost" / "tasks" / "to_delete.md"
        target.write_text("old content")
        assert target.exists()

        await sync.handle_file_sync(None, {
            "path": "tasks/to_delete.md",
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
            "path": "tasks/timed.md",
            "data": b64,
            "checksum": checksum,
            "mtime": target_mtime,
        })

        written = tmp_path / "byfrost" / "tasks" / "timed.md"
        assert abs(written.stat().st_mtime - target_mtime) < 1


# ---------------------------------------------------------------------------
# Last-write-wins
# ---------------------------------------------------------------------------

class TestLastWriteWins:
    """Newer remote files overwrite, older ones do not."""

    @pytest.mark.asyncio
    async def test_newer_remote_overwrites(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        target = tmp_path / "byfrost" / "tasks" / "conflict.md"
        target.write_text("local")
        old_mtime = target.stat().st_mtime

        new_data = b"remote wins"
        b64, checksum = _encode_file(new_data)
        await sync.handle_file_sync(None, {
            "path": "tasks/conflict.md",
            "data": b64,
            "checksum": checksum,
            "mtime": old_mtime + 100,  # Newer
        })
        assert target.read_bytes() == new_data

    @pytest.mark.asyncio
    async def test_older_remote_does_not_overwrite(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        target = tmp_path / "byfrost" / "tasks" / "conflict.md"
        target.write_text("local wins")
        local_mtime = target.stat().st_mtime

        old_data = b"old remote"
        b64, checksum = _encode_file(old_data)
        await sync.handle_file_sync(None, {
            "path": "tasks/conflict.md",
            "data": b64,
            "checksum": checksum,
            "mtime": local_mtime - 100,  # Older
        })
        assert target.read_text() == "local wins"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class TestManifest:
    """send_full_manifest sends all files in SYNC_DIRS."""

    @pytest.mark.asyncio
    async def test_sends_all_files(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        bf = tmp_path / "byfrost"
        # Create files in different sync dirs
        (bf / "tasks" / "apple" / "current.md").parent.mkdir(parents=True, exist_ok=True)
        (bf / "tasks" / "apple" / "current.md").write_text("task spec")
        (bf / "shared" / "api-spec.yaml").write_text("openapi: 3.0")
        (bf / "compound" / "patterns.md").write_text("# Patterns")

        ws = AsyncMock()
        await sync.send_full_manifest(ws)
        assert sync._send.call_count == 3

    @pytest.mark.asyncio
    async def test_skips_oversized(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        bf = tmp_path / "byfrost"
        (bf / "tasks" / "small.md").write_text("small")
        (bf / "tasks" / "big.md").write_bytes(b"x" * (MAX_FILE_SIZE + 1))

        ws = AsyncMock()
        await sync.send_full_manifest(ws)
        # Only the small file should be sent
        assert sync._send.call_count == 1


# ---------------------------------------------------------------------------
# Outbound: _send_file
# ---------------------------------------------------------------------------

class TestSendFile:
    """_send_file broadcasts file contents or deletions."""

    @pytest.mark.asyncio
    async def test_send_file_content(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        bf = tmp_path / "byfrost"
        (bf / "tasks" / "test.md").write_text("hello")

        await sync._send_file("tasks/test.md", deleted=False)
        sync._broadcast.assert_called_once()
        call_args = sync._broadcast.call_args
        assert call_args[0][0] == "file.sync"
        payload = call_args[0][1]
        assert payload["path"] == "tasks/test.md"
        assert payload["checksum"] == hashlib.sha256(b"hello").hexdigest()

    @pytest.mark.asyncio
    async def test_send_deletion(self, tmp_path: Path) -> None:
        sync = _make_sync(tmp_path)
        await sync._send_file("tasks/deleted.md", deleted=True)
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

        # Mock Popen to not actually start a process
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

        # os.kill will raise ProcessLookupError (fake PID)
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
