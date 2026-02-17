"""Controller-side file sync over WebSocket.

Maintains a persistent WebSocket connection to the Mac worker daemon,
watches local byfrost/ coordination subdirectories for changes, and
syncs bidirectionally. Runs as a standalone background process started
with `byfrost sync start`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

try:
    import websockets
    import websockets.exceptions
except ImportError:
    print("ERROR: websockets not installed. Run: pip install websockets")
    sys.exit(1)

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from core.config import BRIDGE_DIR, DEFAULT_PORT, source_env_file
from core.security import MessageSigner, SecretManager, TLSManager

# Coordination directories to sync (relative to byfrost/)
SYNC_DIRS = ["tasks", "shared", "compound", "pm", "qa"]

# Max file size for sync (256KB)
MAX_FILE_SIZE = 256 * 1024

# Debounce interval in milliseconds
DEBOUNCE_MS = 100

# Echo suppression TTL in seconds
SUPPRESS_TTL = 0.5

# Reconnect delay on connection loss
RECONNECT_DELAY = 5

# PID file for the sync process
PID_FILE = BRIDGE_DIR / "sync.pid"

# Log file
LOG_FILE = BRIDGE_DIR / "sync.log"


def _setup_logger() -> logging.Logger:
    """Set up logging for the sync process."""
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("byfrost.sync")
    logger.setLevel(logging.DEBUG)

    handler = logging.FileHandler(LOG_FILE)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(handler)

    # Also log to stderr when running in foreground
    if sys.stderr.isatty():
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(message)s"))
        console.setLevel(logging.INFO)
        logger.addHandler(console)

    return logger


def _load_config() -> dict:
    """Load connection config for the sync client."""
    from cli.api_client import load_auth

    config: dict = {
        "host": os.environ.get("BRIDGE_HOST", ""),
        "port": int(os.environ.get("BRIDGE_PORT", str(DEFAULT_PORT))),
    }

    _cli_env_map: dict = {
        "BRIDGE_HOST": ("host", str),
        "BRIDGE_PORT": ("port", int),
        "MAC_HOSTNAME": ("host", str),
    }

    for search in [Path.cwd(), Path.cwd() / ".agent-team"]:
        cfg_file = search / "config.env"
        if cfg_file.exists():
            source_env_file(cfg_file, config, _cli_env_map)
            break

    if not config["host"]:
        config["host"] = os.environ.get("MAC_HOSTNAME", "")
    if not config["host"]:
        auth = load_auth() or {}
        addrs = auth.get("worker_addresses", {})
        config["host"] = (
            addrs.get("tailscale_ip")
            or addrs.get("local_ip")
            or "localhost"
        )

    config["secret"] = SecretManager.load()
    return config


class SyncClient:
    """Controller-side file sync process."""

    def __init__(
        self, project_dir: Path, config: dict, logger: logging.Logger,
    ) -> None:
        self.project_dir = project_dir
        self.byfrost_dir = project_dir / "byfrost"
        self.config = config
        self.log = logger
        self._signer = MessageSigner(config["secret"]) if config.get("secret") else None
        self._use_tls = TLSManager.has_client_certs()
        self._ws: websockets.WebSocketClientProtocol | None = None  # type: ignore[name-defined]
        self._observer: Observer | None = None  # type: ignore[valid-type]
        self._suppressed: dict[str, float] = {}
        self._pending: dict[str, asyncio.TimerHandle] = {}
        self._running = True
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(self) -> None:
        """Main loop: watch files and maintain WebSocket connection."""
        import ssl as _ssl

        self._loop = asyncio.get_event_loop()
        self._start_watcher()
        self.log.info(f"Sync client started for {self.project_dir}")

        while self._running:
            try:
                await self._connect_and_sync()
            except _ssl.SSLError as e:
                self.log.error(f"TLS error: {e}. Fix certs or re-run 'byfrost connect'.")
                break
            except (ConnectionRefusedError, OSError) as e:
                self.log.warning(f"Connection failed: {e}. Retrying in {RECONNECT_DELAY}s...")
                await asyncio.sleep(RECONNECT_DELAY)
            except websockets.exceptions.ConnectionClosed as e:
                self.log.warning(f"Connection lost: {e}. Reconnecting in {RECONNECT_DELAY}s...")
                await asyncio.sleep(RECONNECT_DELAY)
            except Exception as e:
                self.log.error(f"Unexpected error: {e}. Reconnecting in {RECONNECT_DELAY}s...")
                await asyncio.sleep(RECONNECT_DELAY)

    async def _connect_and_sync(self) -> None:
        """Connect to daemon, send manifest, listen for sync messages."""
        uri = self._build_uri()
        ssl_ctx = self._get_ssl_context()

        self._ws = await websockets.connect(
            uri, ssl=ssl_ctx, ping_interval=20, ping_timeout=10,
            close_timeout=5, max_size=2**20,
        )
        self.log.info(f"Connected to daemon at {uri}")

        # Send our local files for initial sync
        await self._send_manifest()

        # Listen for incoming sync messages
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")
            if msg_type in ("file.sync", "file.changed"):
                await self._handle_inbound_sync(msg)
            # Ignore other message types (pong, task.output, etc.)

    # --- Outbound: local file change -> send to daemon ---

    def _start_watcher(self) -> None:
        """Start watchdog observer on byfrost/ coordination subdirs."""
        self.byfrost_dir.mkdir(parents=True, exist_ok=True)
        for d in SYNC_DIRS:
            (self.byfrost_dir / d).mkdir(parents=True, exist_ok=True)

        self._observer = Observer()
        handler = _SyncEventHandler(self)
        for d in SYNC_DIRS:
            watch_path = self.byfrost_dir / d
            self._observer.schedule(handler, str(watch_path), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        self.log.info(f"Watching: {self.byfrost_dir}")

    def on_local_change(self, abs_path: str, deleted: bool = False) -> None:
        """Called by watchdog handler. Schedules debounced sync send."""
        rel = self._relative_path(abs_path)
        if rel is None:
            return

        suppress_until = self._suppressed.get(rel, 0)
        if time.time() < suppress_until:
            return

        existing = self._pending.pop(rel, None)
        if existing:
            existing.cancel()

        if self._loop is None:
            return
        handle = self._loop.call_later(
            DEBOUNCE_MS / 1000,
            lambda r=rel, d=deleted: self._loop.create_task(self._send_file(r, d))  # type: ignore[misc, union-attr]
        )
        self._pending[rel] = handle

    async def _send_file(self, rel_path: str, deleted: bool) -> None:
        """Send file contents or deletion to daemon."""
        self._pending.pop(rel_path, None)

        if self._ws is None or self._ws.closed:
            return

        if deleted:
            msg = self._sign({"type": "file.changed", "path": rel_path, "deleted": True})
            await self._ws.send(json.dumps(msg))
            self.log.debug(f"Synced deletion: {rel_path}")
            return

        abs_path = self.byfrost_dir / rel_path
        if not abs_path.is_file():
            return

        try:
            size = abs_path.stat().st_size
        except OSError:
            return

        if size > MAX_FILE_SIZE:
            self.log.warning(f"File too large ({size} bytes), skipping: {rel_path}")
            return

        try:
            data = abs_path.read_bytes()
            mtime = abs_path.stat().st_mtime
        except OSError as e:
            self.log.warning(f"Failed to read {rel_path}: {e}")
            return

        checksum = hashlib.sha256(data).hexdigest()
        msg = self._sign({
            "type": "file.sync",
            "path": rel_path,
            "data": base64.b64encode(data).decode("ascii"),
            "checksum": checksum,
            "mtime": mtime,
        })
        await self._ws.send(json.dumps(msg))
        self.log.debug(f"Synced file: {rel_path} ({len(data)} bytes)")

    # --- Inbound: message from daemon -> write locally ---

    async def _handle_inbound_sync(self, msg: dict) -> None:
        """Handle incoming file.sync or file.changed message."""
        rel_path = msg.get("path", "")
        if not self._validate_path(rel_path):
            self.log.warning(f"Rejected invalid sync path: {rel_path}")
            return

        abs_path = self.byfrost_dir / rel_path

        # Symlink escape check: verify resolved path stays inside byfrost/
        if not self._is_inside_byfrost(abs_path) and abs_path.exists():
            self.log.warning(f"Rejected path escaping byfrost dir: {rel_path}")
            return

        deleted = msg.get("deleted", False)

        if deleted:
            if abs_path.exists() or abs_path.is_symlink():
                if not self._is_inside_byfrost(abs_path):
                    self.log.warning(f"Rejected deletion escaping byfrost dir: {rel_path}")
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
                    return
            except OSError:
                pass

        abs_path.parent.mkdir(parents=True, exist_ok=True)

        # Verify parent dir is inside byfrost/ after mkdir (catches symlinked parents)
        if not self._is_inside_byfrost(abs_path):
            self.log.warning(f"Rejected path escaping byfrost dir after mkdir: {rel_path}")
            return

        self._suppress(rel_path)
        abs_path.write_bytes(data)

        if remote_mtime:
            try:
                os.utime(abs_path, (remote_mtime, remote_mtime))
            except OSError:
                pass

        self.log.debug(f"Wrote synced file: {rel_path} ({len(data)} bytes)")

    # --- Initial sync ---

    async def _send_manifest(self) -> None:
        """Send all local byfrost/ coordination files on connect."""
        count = 0
        for d in SYNC_DIRS:
            dir_path = self.byfrost_dir / d
            if not dir_path.exists():
                continue
            for f in dir_path.rglob("*"):
                if f.is_symlink() or not f.is_file():
                    continue
                if not self._is_inside_byfrost(f):
                    continue
                try:
                    if f.stat().st_size > MAX_FILE_SIZE:
                        continue
                except OSError:
                    continue
                rel = str(f.relative_to(self.byfrost_dir))
                await self._send_file(rel, deleted=False)
                count += 1
                await asyncio.sleep(0)

        self.log.info(f"Sent manifest: {count} files")

    # --- Helpers ---

    def _sign(self, msg: dict) -> dict:
        """Sign outgoing message with HMAC."""
        if self._signer:
            return self._signer.sign(msg)
        msg["timestamp"] = time.time()
        return msg

    def _suppress(self, rel_path: str) -> None:
        """Suppress watchdog events for this path after writing."""
        self._suppressed[rel_path] = time.time() + SUPPRESS_TTL

    def _relative_path(self, abs_path: str) -> str | None:
        """Convert absolute path to relative path under byfrost/."""
        try:
            rel = Path(abs_path).relative_to(self.byfrost_dir)
            rel_str = str(rel)
            if self._validate_path(rel_str):
                return rel_str
        except ValueError:
            pass
        return None

    def _validate_path(self, rel_path: str) -> bool:
        """Check that a relative path is in a sync directory and safe."""
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
        if not parts or parts[0] not in SYNC_DIRS:
            return False
        return True

    def _is_inside_byfrost(self, abs_path: Path) -> bool:
        """Verify resolved path stays inside byfrost_dir (symlink check)."""
        try:
            resolved = abs_path.resolve()
            resolved.relative_to(self.byfrost_dir.resolve())
            return True
        except (ValueError, OSError):
            return False

    def _build_uri(self) -> str:
        """Build WebSocket URI from config."""
        protocol = "wss" if self._use_tls else "ws"
        return f"{protocol}://{self.config['host']}:{self.config['port']}"

    def _get_ssl_context(self):  # type: ignore[return]
        """Get TLS context for mTLS connection.

        When TLS certs are present, failure to load them raises rather
        than silently degrading to plaintext.
        """
        if self._use_tls:
            return TLSManager.get_client_ssl_context()
        return None

    def stop(self) -> None:
        """Stop the sync client."""
        self._running = False
        if self._observer:
            self._observer.stop()  # type: ignore[attr-defined]
            self._observer.join(timeout=3)  # type: ignore[attr-defined]
        for handle in self._pending.values():
            handle.cancel()
        self._pending.clear()


class _SyncEventHandler(FileSystemEventHandler):
    """Watchdog event handler that delegates to SyncClient."""

    def __init__(self, sync: SyncClient) -> None:
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


# --- Process management ---

def _run_sync_foreground(project_dir: Path) -> None:
    """Run sync client in foreground (called by background process)."""
    logger = _setup_logger()
    config = _load_config()

    if not config.get("host") or config["host"] == "localhost":
        logger.error("No worker host configured. Run 'byfrost connect' first.")
        sys.exit(1)

    client = SyncClient(project_dir, config, logger)

    def _handle_sigterm(*_: object) -> None:
        client.stop()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        client.stop()


def start_sync(project_dir: Path) -> int:
    """Start the sync process in background. Returns 0 on success."""
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)

    # Check if already running
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # Check if alive
            print(f"[byfrost] Sync already running (PID {pid})")
            return 0
        except (ProcessLookupError, ValueError):
            PID_FILE.unlink(missing_ok=True)

    # Launch as background subprocess
    proc = subprocess.Popen(
        [sys.executable, "-m", "cli.file_sync", str(project_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))
    print(f"[byfrost] Sync started (PID {proc.pid})")
    print(f"[byfrost] Log: {LOG_FILE}")
    return 0


def stop_sync() -> int:
    """Stop the sync process. Returns 0 on success."""
    if not PID_FILE.exists():
        print("[byfrost] Sync is not running")
        return 1

    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"[byfrost] Sync stopped (PID {pid})")
    except (ProcessLookupError, ValueError):
        print("[byfrost] Sync process not found (stale PID file)")
    finally:
        PID_FILE.unlink(missing_ok=True)
    return 0


def sync_status() -> int:
    """Check sync process status. Returns 0 if running, 1 if not."""
    if not PID_FILE.exists():
        print("[byfrost] Sync is not running")
        return 1

    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        print(f"[byfrost] Sync is running (PID {pid})")
        return 0
    except (ProcessLookupError, ValueError):
        PID_FILE.unlink(missing_ok=True)
        print("[byfrost] Sync is not running (stale PID file removed)")
        return 1


def run_sync_command(action: str, project_dir: Path) -> int:
    """Dispatch sync subcommand. Returns exit code."""
    try:
        if action == "start":
            return start_sync(project_dir)
        elif action == "stop":
            return stop_sync()
        elif action == "status":
            return sync_status()
        else:
            print(f"[byfrost] Unknown sync action: {action}")
            return 1
    except KeyboardInterrupt:
        return 130


# Allow running as a module: python -m cli.file_sync /path/to/project
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m cli.file_sync <project_dir>")
        sys.exit(1)
    _run_sync_foreground(Path(sys.argv[1]))
