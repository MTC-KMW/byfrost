"""Worker-side file sync over WebSocket.

Watches project files for changes using watchdog, sends file contents
to connected controllers via file.sync messages. Receives inbound
file.sync messages and writes files locally. Syncs ALL project files
(not just coordination dirs) - the bridge is the sole transport.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from core.ignore import MAX_FILE_SIZE, load_ignore_spec, should_ignore

# Debounce interval in milliseconds - wait for writes to finish
DEBOUNCE_MS = 100

# Echo suppression TTL in seconds - prevent re-syncing files we just wrote
SUPPRESS_TTL = 0.5


class DaemonFileSync:
    """Watches project files and syncs over WebSocket."""

    def __init__(
        self,
        project_path: str,
        broadcast_fn: Callable[..., Coroutine[Any, Any, None]],
        send_fn: Callable[..., Coroutine[Any, Any, None]],
        logger: logging.Logger,
    ) -> None:
        self.project_path = Path(project_path)
        self._broadcast = broadcast_fn
        self._send = send_fn
        self.log = logger
        self._ignore_spec = load_ignore_spec(self.project_path, for_sync=True)
        self._observer: Observer | None = None  # type: ignore[valid-type]
        self._suppressed: dict[str, float] = {}  # rel_path -> suppress_until
        self._pending: dict[str, asyncio.TimerHandle] = {}  # rel_path -> debounce handle
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start watchdog observer on project directory."""
        self._loop = loop
        self._observer = Observer()
        handler = _SyncEventHandler(self)
        self._observer.schedule(handler, str(self.project_path), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        self.log.info(f"File sync watching: {self.project_path}")

    async def stop(self) -> None:
        """Stop the watchdog observer."""
        if self._observer:
            self._observer.stop()  # type: ignore[attr-defined]
            self._observer.join(timeout=3)  # type: ignore[attr-defined]
            self._observer = None
        # Cancel pending debounce timers
        for handle in self._pending.values():
            handle.cancel()
        self._pending.clear()

    # --- Outbound: local file change -> send to controllers ---

    def on_local_change(self, abs_path: str, deleted: bool = False) -> None:
        """Called by watchdog handler. Schedules debounced sync send."""
        rel = self._relative_path(abs_path)
        if rel is None:
            return

        # Check echo suppression
        suppress_until = self._suppressed.get(rel, 0)
        if time.time() < suppress_until:
            return

        # Cancel existing debounce timer for this path
        existing = self._pending.pop(rel, None)
        if existing:
            existing.cancel()

        # Schedule new debounced send
        if self._loop is None:
            return
        handle = self._loop.call_later(
            DEBOUNCE_MS / 1000,
            lambda r=rel, d=deleted: self._loop.create_task(self._send_file(r, d))  # type: ignore[misc, union-attr]
        )
        self._pending[rel] = handle

    async def _send_file(self, rel_path: str, deleted: bool) -> None:
        """Send a file's contents (or deletion) to all connected controllers."""
        self._pending.pop(rel_path, None)

        if deleted:
            try:
                await self._broadcast("file.changed", {
                    "path": rel_path,
                    "deleted": True,
                })
            except Exception:
                self.log.debug(f"Broadcast failed (no clients?): {rel_path}")
                return
            self.log.debug(f"Synced deletion: {rel_path}")
            return

        abs_path = self.project_path / rel_path
        if not abs_path.is_file():
            return

        try:
            size = abs_path.stat().st_size
        except OSError:
            return

        if size > MAX_FILE_SIZE:
            self.log.warning(f"File too large ({size} bytes), skipping sync: {rel_path}")
            return

        try:
            data = abs_path.read_bytes()
            mtime = abs_path.stat().st_mtime
        except OSError as e:
            self.log.warning(f"Failed to read {rel_path}: {e}")
            return

        checksum = hashlib.sha256(data).hexdigest()
        try:
            await self._broadcast("file.sync", {
                "path": rel_path,
                "data": base64.b64encode(data).decode("ascii"),
                "checksum": checksum,
                "mtime": mtime,
            })
        except Exception:
            self.log.debug(f"Broadcast failed (no clients?): {rel_path}")
            return
        self.log.debug(f"Synced file: {rel_path} ({len(data)} bytes)")

    # --- Inbound: message from controller -> write locally ---

    async def handle_file_sync(self, ws: Any, msg: dict, source: str = "") -> None:
        """Handle incoming file.sync or file.changed message."""
        rel_path = msg.get("path", "")
        if not self._validate_path(rel_path):
            self.log.warning(f"Rejected invalid sync path: {rel_path}")
            return

        abs_path = self.project_path / rel_path

        # Symlink escape check: verify resolved path stays inside project
        if not self._is_inside_project(abs_path) and abs_path.exists():
            self.log.warning(f"Rejected path escaping project dir: {rel_path}")
            return

        deleted = msg.get("deleted", False)

        if deleted:
            if abs_path.exists() or abs_path.is_symlink():
                if not self._is_inside_project(abs_path):
                    self.log.warning(f"Rejected deletion escaping project dir: {rel_path}")
                    return
                self._suppress(rel_path)
                abs_path.unlink()
                self.log.debug(f"Deleted synced file: {rel_path}")
            return

        data_b64 = msg.get("data", "")
        checksum = msg.get("checksum", "")

        if not data_b64:
            return

        try:
            data = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            self.log.warning(f"Invalid base64 for {rel_path}: {e}")
            return
        if hashlib.sha256(data).hexdigest() != checksum:
            self.log.warning(f"Checksum mismatch for {rel_path}, ignoring")
            return

        # Clamp remote mtime to a sane range (prevent LWW poisoning)
        now = time.time()
        remote_mtime = msg.get("mtime", 0)
        if remote_mtime > now + 86400 or remote_mtime < 946684800:
            remote_mtime = now

        # Last-write-wins: only overwrite if remote is newer
        if abs_path.exists():
            try:
                local_mtime = abs_path.stat().st_mtime
                if local_mtime > remote_mtime:
                    return  # Local is newer, keep it
            except OSError:
                pass  # File disappeared, write anyway

        abs_path.parent.mkdir(parents=True, exist_ok=True)

        # Verify parent dir is inside project after mkdir (catches symlinked parents)
        if not self._is_inside_project(abs_path):
            self.log.warning(f"Rejected path escaping project dir after mkdir: {rel_path}")
            return

        self._suppress(rel_path)
        abs_path.write_bytes(data)

        if remote_mtime:
            try:
                os.utime(abs_path, (remote_mtime, remote_mtime))
            except OSError:
                pass

        self.log.debug(f"Wrote synced file: {rel_path} ({len(data)} bytes)")

    # --- Initial sync: send all files to a newly connected client ---

    async def send_full_manifest(self, ws: Any) -> None:
        """Send all project files to one client for initial sync.

        Yields between files and aborts if the connection drops.
        """
        count = 0
        for f in self.project_path.rglob("*"):
            # Abort if the client disconnected mid-manifest
            if getattr(ws, "closed", False) is True:
                self.log.warning(
                    f"Manifest aborted (client disconnected) after {count} files"
                )
                return

            if f.is_symlink() or not f.is_file():
                continue
            if not self._is_inside_project(f):
                continue
            try:
                rel = str(f.relative_to(self.project_path))
            except ValueError:
                continue
            if should_ignore(rel, self._ignore_spec):
                continue
            try:
                if f.stat().st_size > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue

            try:
                data = f.read_bytes()
                mtime = f.stat().st_mtime
            except OSError:
                continue

            checksum = hashlib.sha256(data).hexdigest()
            try:
                await self._send(ws, "file.sync", {
                    "path": rel,
                    "data": base64.b64encode(data).decode("ascii"),
                    "checksum": checksum,
                    "mtime": mtime,
                })
            except Exception:
                self.log.warning(
                    f"Manifest aborted (send error) after {count} files"
                )
                return
            count += 1
            # Yield to event loop - prevents starving ping/pong
            await asyncio.sleep(0)

        self.log.info(f"Sent manifest: {count} files")

    # --- Echo suppression ---

    def _suppress(self, rel_path: str) -> None:
        """Suppress watchdog events for this path after writing."""
        self._suppressed[rel_path] = time.time() + SUPPRESS_TTL

    # --- Path validation ---

    def _relative_path(self, abs_path: str) -> str | None:
        """Convert absolute path to relative path under project dir, or None."""
        try:
            rel = Path(abs_path).relative_to(self.project_path)
            rel_str = str(rel)
            if self._validate_path(rel_str):
                return rel_str
        except ValueError:
            pass
        return None

    def _validate_path(self, rel_path: str) -> bool:
        """Check that a relative path is safe and not ignored."""
        if not rel_path:
            return False
        try:
            p = Path(rel_path)
            parts = p.parts
        except (ValueError, TypeError):
            return False
        if any(part == ".." for part in parts):
            return False
        if str(p).startswith(("/", "\\")):
            return False
        if not parts:
            return False
        if should_ignore(rel_path, self._ignore_spec):
            return False
        return True

    def _is_inside_project(self, abs_path: Path) -> bool:
        """Verify resolved path stays inside project_path (symlink check)."""
        try:
            resolved = abs_path.resolve()
            resolved.relative_to(self.project_path.resolve())
            return True
        except (ValueError, OSError):
            return False


class _SyncEventHandler(FileSystemEventHandler):
    """Watchdog event handler that delegates to DaemonFileSync."""

    def __init__(self, sync: DaemonFileSync) -> None:
        self._sync = sync

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._sync.on_local_change(str(event.src_path))

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._sync.on_local_change(str(event.src_path))

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._sync.on_local_change(str(event.src_path), deleted=True)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._sync.on_local_change(str(event.src_path), deleted=True)
            if hasattr(event, "dest_path"):
                self._sync.on_local_change(str(event.dest_path))
