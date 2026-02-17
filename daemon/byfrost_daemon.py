#!/usr/bin/env python3
"""
Byfrost Daemon - runs on the Mac.

WebSocket server that accepts tasks from a controller, spawns Claude Code
in tmux sessions with full native Xcode access, streams output back in
real-time, and reports completion.

Usage:
    python3 -m daemon.byfrost_daemon                   # foreground
    python3 -m daemon.byfrost_daemon --daemonize       # background (launchd preferred)

Config: reads from .agent-team/config.env (BRIDGE_PORT, etc.) or environment.
Logs:   ~/.byfrost/logs/daemon.log (rotated daily)
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

try:
    import websockets
    from websockets import serve
except ImportError:
    print("ERROR: websockets not installed. Run: pip3 install websockets")
    sys.exit(1)

from core.config import BRIDGE_DIR, DEFAULT_PORT, LOG_DIR, source_env_file
from core.security import (
    AuditLogger,
    MessageSigner,
    PromptSanitizer,
    RateLimiter,
    SecretManager,
    TLSManager,
)
from daemon.server_client import ServerClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STATE_FILE = BRIDGE_DIR / "state.json"
PID_FILE = BRIDGE_DIR / "daemon.pid"

DEFAULT_HEARTBEAT_INTERVAL = 30
DEFAULT_SESSION_TIMEOUT = 3600  # 1 hour max task runtime
MAX_OUTPUT_BUFFER = 500  # max lines kept in memory per task

def load_config():
    """Load daemon config from .agent-team/config.env or environment."""
    config = {
        "port": int(os.environ.get("BRIDGE_PORT", DEFAULT_PORT)),
        "secret": os.environ.get("BRIDGE_SECRET", ""),
        "project_path": os.environ.get("MAC_PROJECT_PATH", ""),
        "heartbeat_interval": int(os.environ.get("BRIDGE_HEARTBEAT", DEFAULT_HEARTBEAT_INTERVAL)),
        "session_timeout": int(os.environ.get("BRIDGE_TIMEOUT", DEFAULT_SESSION_TIMEOUT)),
        "claude_path": os.environ.get("CLAUDE_PATH", "claude"),
        "allowed_tools": os.environ.get("BRIDGE_ALLOWED_TOOLS",
                                         "Bash,Read,Write,Edit,MultiEdit"),
        "auto_git": os.environ.get("BRIDGE_AUTO_GIT", "true").lower() in ("true", "1", "yes"),
    }

    # Env file key mapping for daemon config
    # NOTE: BRIDGE_SECRET intentionally excluded - never read from config.env
    _daemon_env_map = {
        "BRIDGE_PORT": ("port", int),
        "MAC_PROJECT_PATH": ("project_path", str),
        "BRIDGE_HEARTBEAT": ("heartbeat_interval", int),
        "BRIDGE_TIMEOUT": ("session_timeout", int),
        "CLAUDE_PATH": ("claude_path", str),
        "BRIDGE_ALLOWED_TOOLS": ("allowed_tools", str),
        "BRIDGE_AUTO_GIT": ("auto_git", lambda v: v.lower() in ("true", "1", "yes")),
    }

    for search in [Path.cwd(), Path.cwd() / ".agent-team"]:
        cfg_file = search / "config.env"
        if cfg_file.exists():
            source_env_file(cfg_file, config, _daemon_env_map)
            break

    # Load persistent config from ~/.byfrost/daemon.json
    # Priority: env vars > config.env > daemon.json > auto-discovery
    from core.config import load_daemon_config
    daemon_cfg = load_daemon_config()
    if not config["project_path"] and daemon_cfg.get("project_path"):
        config["project_path"] = daemon_cfg["project_path"]

    if not config["secret"]:
        config["secret"] = SecretManager.load()
    if not config["secret"]:
        # First run: generate and save
        config["secret"] = SecretManager.generate()
        SecretManager.save(config["secret"])

    return config


# Project indicators that mark a directory as a project root
_PROJECT_INDICATORS = (
    "*.xcodeproj", "*.xcworkspace", "Package.swift",
    "package.json", "requirements.txt", "pyproject.toml",
    "go.mod", "Cargo.toml", "Gemfile", "build.gradle",
    "pom.xml", "CMakeLists.txt", "Makefile",
)

# Directories to always skip during discovery
_SKIP_DIRS = {
    "node_modules", "venv", ".venv", "__pycache__", "build", "dist",
    ".git", ".hg", ".svn", "DerivedData", "Pods", ".build",
    "target", "vendor", "env", "byfrost",
}

# macOS protected dirs - searching these may trigger permission prompts.
# Search them only as a last resort.
_MACOS_PROTECTED = {
    "Photos", "Movies", "Music", "Documents", "Downloads",
    "Desktop", "Pictures", "Library", "Public",
}


def _has_project_indicators(directory: Path) -> bool:
    """Check if a directory contains any project indicator files."""
    for pattern in _PROJECT_INDICATORS:
        if list(directory.glob(pattern)):
            return True
    return False


def discover_project_path(log) -> Optional[str]:
    """Auto-discover a project directory by scanning common locations.

    Search order:
    1. Current working directory
    2. Home directory children (non-protected), up to 3 levels deep
    3. Home directory protected dirs (last resort), up to 3 levels deep

    Returns the project path string, or None if nothing found.
    """
    cwd = Path.cwd()

    # 1. Check cwd (skip if cwd name is in the skip list, e.g. the byfrost repo)
    if cwd.name not in _SKIP_DIRS and _has_project_indicators(cwd):
        log.info(f"Auto-discover: found project at cwd ({cwd})")
        return str(cwd)

    # 2. Scan from home directory
    home = Path.home()
    if not home.exists():
        return None

    # Split children into normal and protected
    normal_dirs: list[Path] = []
    protected_dirs: list[Path] = []
    try:
        for child in sorted(home.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            if child.name in _SKIP_DIRS:
                continue
            if child.name in _MACOS_PROTECTED:
                protected_dirs.append(child)
            else:
                normal_dirs.append(child)
    except PermissionError:
        log.warning("Auto-discover: cannot read home directory")
        return None

    # Search normal dirs first (up to 3 levels), then protected
    for search_group in [normal_dirs, protected_dirs]:
        for top in search_group:
            # Level 1: direct child of home
            if _has_project_indicators(top):
                log.info(f"Auto-discover: found project at {top}")
                return str(top)
            # Level 2-3: subdirectories
            try:
                for sub in sorted(top.iterdir()):
                    if not sub.is_dir() or sub.name.startswith(".") or sub.name in _SKIP_DIRS:
                        continue
                    if _has_project_indicators(sub):
                        log.info(f"Auto-discover: found project at {sub}")
                        return str(sub)
                    # Level 3
                    try:
                        for subsub in sorted(sub.iterdir()):
                            if (not subsub.is_dir()
                                    or subsub.name.startswith(".")
                                    or subsub.name in _SKIP_DIRS):
                                continue
                            if _has_project_indicators(subsub):
                                log.info(f"Auto-discover: found project at {subsub}")
                                return str(subsub)
                    except PermissionError:
                        continue
            except PermissionError:
                continue

    log.warning("Auto-discover: no project found")
    return None


def validate_project_path(config: dict, log) -> None:
    """Validate the project path in config. Falls back to auto-discovery.

    Updates config["project_path"] in place if auto-discovery finds something.
    """
    path_str = config.get("project_path", "")

    if path_str:
        project = Path(path_str)
        if project.is_dir():
            log.info(f"Project path: {path_str} (valid)")
            return
        if project.exists():
            log.warning(f"Project path exists but is not a directory: {path_str}")
        else:
            log.warning(
                f"Project path does not exist: {path_str} "
                "(must be an absolute path, e.g. /Users/you/MyProject)"
            )
        # Fall through to auto-discovery
        log.info("Attempting auto-discovery as fallback...")
    else:
        log.info("No MAC_PROJECT_PATH set, attempting auto-discovery...")

    discovered = discover_project_path(log)
    if discovered:
        config["project_path"] = discovered
        log.info(f"Using discovered project: {discovered}")
        # Persist so auto-discovery doesn't need to run again
        from core.config import load_daemon_config, save_daemon_config
        daemon_cfg = load_daemon_config()
        daemon_cfg["project_path"] = discovered
        save_daemon_config(daemon_cfg)
        log.info("Saved project path to ~/.byfrost/daemon.json")
    else:
        log.warning(
            "No project found. Set MAC_PROJECT_PATH to the absolute path "
            "of your project directory."
        )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(verbose=False):
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # File handler with rotation (10MB, keep 5)
    file_handler = RotatingFileHandler(
        LOG_DIR / "daemon.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    return logging.getLogger("byfrost")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(int, Enum):
    NORMAL = 0
    HIGH = 1
    URGENT = 2


@dataclass
class Task:
    id: str
    prompt: str
    status: TaskStatus = TaskStatus.QUEUED
    priority: TaskPriority = TaskPriority.NORMAL
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    exit_code: Optional[int] = None
    tmux_session: Optional[str] = None
    output_lines: list = field(default_factory=list)
    error: Optional[str] = None
    project_path: Optional[str] = None
    allowed_tools: Optional[str] = None

    def to_dict(self):
        d = asdict(self)
        d["status"] = self.status.value
        d["priority"] = self.priority.value
        # Truncate output for status messages
        if len(d["output_lines"]) > 20:
            d["output_lines"] = d["output_lines"][-20:]
            d["output_truncated"] = True
        return d

    def summary(self):
        return {
            "id": self.id,
            "status": self.status.value,
            "priority": self.priority.value,
            "prompt_preview": self.prompt[:80] + ("..." if len(self.prompt) > 80 else ""),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "exit_code": self.exit_code,
            "tmux_session": self.tmux_session,
            "output_line_count": len(self.output_lines),
        }


# ---------------------------------------------------------------------------
# Session Manager - tmux integration
# ---------------------------------------------------------------------------

class SessionManager:
    """Manages tmux sessions for Claude Code."""

    def __init__(self, config, logger):
        self.config = config
        self.log = logger
        self._verify_tmux()

    def _verify_tmux(self):
        try:
            subprocess.run(["tmux", "-V"], capture_output=True, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            self.log.error("tmux not found. Install with: brew install tmux")
            sys.exit(1)

    def session_name(self, task_id):
        return f"byfrost-{task_id[:8]}"

    def git_pull(self, project_path):
        """Run git pull in the project directory. Returns (success, output)."""
        try:
            result = subprocess.run(
                ["git", "-C", project_path, "pull", "--ff-only"],
                capture_output=True, timeout=30
            )
            output = result.stdout.decode().strip()
            if result.returncode != 0:
                err = result.stderr.decode().strip()
                self.log.warning(f"git pull failed: {err}")
                return False, err
            self.log.info(f"git pull: {output}")
            return True, output
        except subprocess.TimeoutExpired:
            self.log.warning("git pull timed out (30s)")
            return False, "timeout"
        except Exception as e:
            self.log.warning(f"git pull error: {e}")
            return False, str(e)

    def git_push(self, project_path):
        """Run git push in the project directory. Returns (success, output)."""
        try:
            result = subprocess.run(
                ["git", "-C", project_path, "push"],
                capture_output=True, timeout=30
            )
            output = result.stdout.decode().strip()
            if result.returncode != 0:
                err = result.stderr.decode().strip()
                self.log.warning(f"git push failed: {err}")
                return False, err
            self.log.info(f"git push: {output}")
            return True, output
        except subprocess.TimeoutExpired:
            self.log.warning("git push timed out (30s)")
            return False, "timeout"
        except Exception as e:
            self.log.warning(f"git push error: {e}")
            return False, str(e)

    def create_session(self, task):
        """Spawn Claude Code in a new tmux session.

        If BRIDGE_AUTO_GIT is enabled, runs git pull before spawning so the
        agent always sees the latest task files and compound knowledge.
        """
        name = self.session_name(task.id)
        project = task.project_path or self.config["project_path"]
        claude = self.config["claude_path"]
        tools = task.allowed_tools or self.config["allowed_tools"]

        # Auto-sync: auto git-pull before spawning
        if self.config.get("auto_git", True):
            success, output = self.git_pull(project)
            if success:
                self.log.info("Pre-task git pull succeeded")
            else:
                # Non-fatal: agent can still work with what's there
                self.log.warning(f"Pre-task git pull failed: {output} (continuing)")

        # Build the claude command
        # Use -p (print mode) for autonomous execution
        # Prompt is sanitized via shlex.quote() for shell safety
        safe_prompt = PromptSanitizer.sanitize(task.prompt)
        claude_cmd = (
            f'cd {project} && '
            f'{claude} -p '
            f'--allowedTools "{tools}" '
            f'{safe_prompt}'
        )

        # Create tmux session running the command
        try:
            exit_marker = f'/tmp/byfrost-{task.id}.exit'
            wrapper = claude_cmd + f'; echo "EXIT_CODE:$?" > {exit_marker}; sleep 2'
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", name, "-x", "200", "-y", "50",
                 "bash", "-c", wrapper],
                check=True, capture_output=True
            )
            task.tmux_session = name
            self.log.info(f"Spawned tmux session: {name}")
            return name
        except subprocess.CalledProcessError as e:
            self.log.error(f"Failed to create tmux session: {e.stderr.decode()}")
            raise

    def capture_output(self, task):
        """Set up a pipe to capture tmux pane output."""
        name = task.tmux_session
        pipe_path = f"/tmp/byfrost-{task.id}.pipe"

        # Create named pipe
        if not os.path.exists(pipe_path):
            os.mkfifo(pipe_path)

        # Tell tmux to pipe the pane output
        try:
            subprocess.run(
                ["tmux", "pipe-pane", "-t", name, "-o", f"cat >> {pipe_path}"],
                check=True, capture_output=True
            )
            return pipe_path
        except subprocess.CalledProcessError as e:
            self.log.error(f"Failed to pipe tmux output: {e.stderr.decode()}")
            return None

    def send_keys(self, session_name, text):
        """Send keystrokes to a tmux session (for followup)."""
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, text, "Enter"],
                check=True, capture_output=True
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def is_session_alive(self, session_name):
        """Check if a tmux session still exists."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True
        )
        return result.returncode == 0

    def get_exit_code(self, task_id):
        """Read the exit code file left by the wrapper script."""
        exit_file = Path(f"/tmp/byfrost-{task_id}.exit")
        if exit_file.exists():
            try:
                content = exit_file.read_text().strip()
                if content.startswith("EXIT_CODE:"):
                    return int(content.split(":")[1])
            except (ValueError, IndexError):
                pass
        return None

    def kill_session(self, session_name):
        """Kill a tmux session."""
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True
            )
            self.log.info(f"Killed session: {session_name}")
        except subprocess.CalledProcessError:
            pass

    def cleanup(self, task_id):
        """Clean up temp files for a task."""
        for suffix in [".pipe", ".exit"]:
            path = Path(f"/tmp/byfrost-{task_id}{suffix}")
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Task Queue
# ---------------------------------------------------------------------------

class TaskQueue:
    """Priority queue with max concurrency of 1."""

    def __init__(self, logger):
        self.log = logger
        self._queue: deque[Task] = deque()
        self._active: Optional[Task] = None
        self._history: list[Task] = []
        self._max_history = 50

    @property
    def active(self):
        return self._active

    @property
    def pending(self):
        return list(self._queue)

    def enqueue(self, task):
        """Add task to queue, sorted by priority."""
        # Deduplicate by prompt (avoid double-sends)
        for existing in self._queue:
            if existing.prompt == task.prompt and existing.status == TaskStatus.QUEUED:
                self.log.warning(f"Duplicate task rejected: {task.id}")
                return existing

        if task.priority.value > 0:
            # Insert at front for high-priority
            self._queue.appendleft(task)
        else:
            self._queue.append(task)

        self.log.info(f"Task queued: {task.id} (queue size: {len(self._queue)})")
        return task

    def dequeue(self):
        """Get next task if no active task."""
        if self._active or not self._queue:
            return None
        self._active = self._queue.popleft()
        self._active.status = TaskStatus.RUNNING
        self._active.started_at = time.time()
        return self._active

    def complete(self, task, exit_code=0, error=None):
        """Mark active task as complete."""
        if task.status == TaskStatus.RUNNING:
            task.status = TaskStatus.COMPLETE if exit_code == 0 else TaskStatus.FAILED
            task.completed_at = time.time()
            task.exit_code = exit_code
            task.error = error
        if self._active and self._active.id == task.id:
            self._active = None
        self._history.append(task)
        if len(self._history) > self._max_history:
            self._history.pop(0)
        self.log.info(f"Task {task.status.value}: {task.id} (exit={exit_code})")

    def cancel(self, task_id):
        """Cancel a queued or running task."""
        # Check queue
        for i, task in enumerate(self._queue):
            if task.id == task_id:
                task.status = TaskStatus.CANCELLED
                task.completed_at = time.time()
                del self._queue[i]
                self._history.append(task)
                return task
        # Check active
        if self._active and self._active.id == task_id:
            self._active.status = TaskStatus.CANCELLED
            self._active.completed_at = time.time()
            task = self._active
            self._active = None
            self._history.append(task)
            return task
        return None

    def find(self, task_id):
        """Find a task by ID in queue, active, or history."""
        if self._active and self._active.id == task_id:
            return self._active
        for task in self._queue:
            if task.id == task_id:
                return task
        for task in self._history:
            if task.id == task_id:
                return task
        return None

    def status_summary(self):
        return {
            "active": self._active.summary() if self._active else None,
            "queue_size": len(self._queue),
            "queued": [t.summary() for t in self._queue],
            "recent_history": [t.summary() for t in self._history[-5:]],
        }


# ---------------------------------------------------------------------------
# Byfrost Daemon - the main server
# ---------------------------------------------------------------------------

class ByfrostDaemon:
    """WebSocket server orchestrating tasks and sessions."""

    def __init__(self, config, logger):
        self.config = config
        self.log = logger
        self.queue = TaskQueue(logger)
        self.sessions = SessionManager(config, logger)
        self._clients = set()
        self._running = True
        self._task_runners = {}  # task_id -> asyncio.Task

        # Security components
        self.audit = AuditLogger()
        self.rate_limiter = RateLimiter()

        # Create signers for all valid secrets (current + grace period)
        valid_secrets = SecretManager.get_valid_secrets()
        self._signers = [MessageSigner(s) for s in valid_secrets]
        # Primary signer for outgoing messages (always the current secret)
        self._primary_signer = MessageSigner(config["secret"]) if config["secret"] else None

        # Server communication (heartbeat, credential fetch, rotation)
        self.server_client = ServerClient(
            config, logger, on_secret_rotated=self._refresh_signers,
        )

        # File sync for coordination directories
        from daemon.file_sync import DaemonFileSync
        self.file_sync = DaemonFileSync(
            project_path=config.get("project_path", ""),
            broadcast_fn=self._broadcast,
            send_fn=self._send,
            logger=logger,
        )

    def _refresh_signers(self) -> None:
        """Reload HMAC signers after secret rotation."""
        valid = SecretManager.get_valid_secrets()
        self._signers = [MessageSigner(s) for s in valid]
        self._primary_signer = MessageSigner(valid[0]) if valid else None
        self.log.info(f"Signers refreshed ({len(valid)} valid secrets)")

    # --- Authentication ---

    def _authenticate(self, message, source: str) -> tuple[bool, str]:
        """
        Verify HMAC signature against all valid secrets (current + grace period).
        Enforces rate limiting on failures.
        """
        if self.rate_limiter.is_locked(source):
            remaining = self.rate_limiter._lockouts.get(source, 0) - time.time()
            return False, f"locked_out ({remaining:.0f}s remaining)"

        if not self._signers:
            return False, "no_secret_configured"

        # Try each valid signer (current secret first, then grace period secrets)
        for signer in self._signers:
            is_valid, reason = signer.verify(message)
            if is_valid:
                self.rate_limiter.record_success(source)
                self.audit.auth_success(source)
                return True, "ok"

        # All signers failed
        locked = self.rate_limiter.record_failure(source)
        self.audit.auth_failure(source, reason)

        if locked:
            self.audit.lockout(source, RateLimiter.LOCKOUT_SECONDS)
            self.log.warning(f"Source locked out: {source} (too many auth failures)")

        return False, reason

    # --- Message Handling ---

    async def handle_client(self, websocket):
        """Handle a single WebSocket client connection."""
        self._clients.add(websocket)
        peer = websocket.remote_address
        source = f"{peer[0]}:{peer[1]}" if peer else "unknown"
        self.log.info(f"Client connected: {source}")

        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send(websocket, "error", {"message": "Invalid JSON"})
                    continue

                # Authenticate via HMAC
                is_valid, reason = self._authenticate(msg, source)
                if not is_valid:
                    await self._send(websocket, "error", {
                        "message": f"Authentication failed: {reason}"
                    })
                    if "locked_out" in reason:
                        self.log.warning(f"Rejecting locked-out source: {source}")
                    continue

                msg_type = msg.get("type", "")
                handler = {
                    "task.submit": self._handle_submit,
                    "task.cancel": self._handle_cancel,
                    "task.followup": self._handle_followup,
                    "task.status": self._handle_status,
                    "session.attach": self._handle_attach,
                    "ping": self._handle_ping,
                    "project.info": self._handle_project_info,
                    "file.sync": self.file_sync.handle_file_sync,
                    "file.changed": self.file_sync.handle_file_sync,
                }.get(msg_type)

                if handler:
                    await handler(websocket, msg, source)
                else:
                    await self._send(websocket, "error",
                                     {"message": f"Unknown message type: {msg_type}"})

                # Send file manifest after the first handler response,
                # so one-shot commands (ping, status) get their reply first
                if not getattr(websocket, "_manifest_sent", False):
                    websocket._manifest_sent = True  # type: ignore[attr-defined]
                    if self.config.get("project_path"):
                        await self.file_sync.send_full_manifest(websocket)

        except websockets.exceptions.ConnectionClosed:
            self.log.info(f"Client disconnected: {source}")
        finally:
            self._clients.discard(websocket)

    async def _send(self, ws, msg_type, payload=None):
        """Send a signed message to a client."""
        message = {"type": msg_type}
        if payload:
            message.update(payload)
        # Sign outgoing messages
        if self._primary_signer:
            message = self._primary_signer.sign(message)
        else:
            message["timestamp"] = time.time()
        try:
            await ws.send(json.dumps(message))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _broadcast(self, msg_type, payload=None):
        """Send signed message to all connected clients."""
        for ws in list(self._clients):
            await self._send(ws, msg_type, payload)

    # --- Task Handlers ---

    async def _handle_submit(self, ws, msg, source="unknown"):
        prompt = msg.get("prompt", "").strip()
        if not prompt:
            await self._send(ws, "error", {"message": "Empty prompt"})
            return

        # Sanitize prompt against shell injection
        is_safe, reason = PromptSanitizer.validate(prompt)
        if not is_safe:
            self.audit.prompt_rejected(source, reason)
            self.log.warning(f"Prompt rejected from {source}: {reason}")
            await self._send(ws, "error", {
                "message": f"Prompt rejected: {reason}",
                "code": "PROMPT_UNSAFE"
            })
            return

        task = Task(
            id=msg.get("task_id", uuid.uuid4().hex[:12]),
            prompt=prompt,
            priority=TaskPriority(msg.get("priority", 0)),
            project_path=msg.get("project_path"),
            allowed_tools=msg.get("allowed_tools"),
        )

        self.audit.task_submit(source, task.id, prompt[:80])
        task = self.queue.enqueue(task)
        await self._send(ws, "task.accepted", {
            "task_id": task.id,
            "queue_position": len(self.queue.pending),
            "status": task.status.value,
        })

        # Try to start it immediately if nothing is running
        await self._process_queue(ws)

    async def _handle_cancel(self, ws, msg, source="unknown"):
        task_id = msg.get("task_id", "")
        task = self.queue.cancel(task_id)
        if task:
            if task.tmux_session:
                self.sessions.kill_session(task.tmux_session)
                self.sessions.cleanup(task.id)
            # Cancel the runner coroutine
            runner = self._task_runners.pop(task_id, None)
            if runner:
                runner.cancel()
            self.audit.task_cancel(source, task_id)
            await self._send(ws, "task.cancelled", {"task_id": task_id})
        else:
            await self._send(ws, "error", {"message": f"Task not found: {task_id}"})

    async def _handle_followup(self, ws, msg, source="unknown"):
        task = self.queue.active
        if not task or task.id != msg.get("task_id", ""):
            await self._send(ws, "error", {"message": "No matching active task"})
            return

        followup_text = msg.get("text", "").strip()
        if not followup_text:
            await self._send(ws, "error", {"message": "Empty followup text"})
            return

        if task.tmux_session and self.sessions.is_session_alive(task.tmux_session):
            success = self.sessions.send_keys(task.tmux_session, followup_text)
            await self._send(ws, "task.followup_sent", {
                "task_id": task.id,
                "success": success,
            })
        else:
            await self._send(ws, "error", {"message": "Session not running"})

    async def _handle_status(self, ws, msg, source="unknown"):
        task_id = msg.get("task_id")
        if task_id:
            task = self.queue.find(task_id)
            if task:
                await self._send(ws, "task.status", task.to_dict())
            else:
                await self._send(ws, "error", {"message": f"Task not found: {task_id}"})
        else:
            await self._send(ws, "status", self.queue.status_summary())

    async def _handle_attach(self, ws, msg, source="unknown"):
        task = self.queue.active
        if not task:
            await self._send(ws, "error", {"message": "No active task"})
            return

        # Send current buffered output
        await self._send(ws, "session.output", {
            "task_id": task.id,
            "lines": task.output_lines[-50:],
            "tmux_session": task.tmux_session,
            "hint": f"tmux attach -t {task.tmux_session}",
        })

    async def _handle_project_info(self, ws, msg, source="unknown"):
        """Return project details from the worker's project directory.

        Includes a diagnostic status field:
        - "ok": path valid, detection ran
        - "no_project_path": not set, auto-discovery found nothing
        - "path_not_found": set but doesn't exist
        - "path_not_directory": exists but not a directory
        """
        project = self.config["project_path"]
        info: dict = {"project_path": project}

        # Diagnostic checks
        if not project:
            info["_status"] = "no_project_path"
            info["_message"] = (
                "No project path configured. Set MAC_PROJECT_PATH to the "
                "absolute path of your project (e.g. /Users/you/MyProject)."
            )
            await self._send(ws, "project.info", info)
            return

        project_dir = Path(project)
        if not project_dir.exists():
            info["_status"] = "path_not_found"
            info["_message"] = (
                f"Path does not exist: {project}. "
                "MAC_PROJECT_PATH must be an absolute path "
                "(e.g. /Users/you/MyProject, not /MyProject)."
            )
            await self._send(ws, "project.info", info)
            return

        if not project_dir.is_dir():
            info["_status"] = "path_not_directory"
            info["_message"] = (
                f"Path exists but is not a directory: {project}. "
                "MAC_PROJECT_PATH should point to the project root."
            )
            await self._send(ws, "project.info", info)
            return

        info["_status"] = "ok"

        # Detect Xcode project
        xcodeprojs = list(project_dir.glob("*.xcodeproj"))
        if xcodeprojs:
            info["xcode_scheme"] = xcodeprojs[0].stem
            info["apple_dir"] = "."
        else:
            # Check subdirectories one level deep
            xcodeprojs = list(project_dir.glob("*/*.xcodeproj"))
            if xcodeprojs:
                info["xcode_scheme"] = xcodeprojs[0].stem
                rel = xcodeprojs[0].parent.relative_to(project_dir)
                info["apple_dir"] = str(rel)

        # Scan Swift files for frameworks
        swift_files = list(project_dir.rglob("*.swift"))[:30]
        frameworks: set[str] = set()
        known = {
            "SwiftUI", "UIKit", "AppKit", "SwiftData", "CoreData",
            "Combine", "MapKit", "CloudKit", "StoreKit", "WidgetKit",
        }
        for sf in swift_files:
            try:
                for line in sf.read_text().splitlines()[:30]:
                    line = line.strip()
                    if line.startswith("import "):
                        fw = line.split()[1] if len(line.split()) > 1 else ""
                        if fw in known:
                            frameworks.add(fw)
            except OSError:
                continue
        if frameworks:
            info["apple_frameworks"] = ", ".join(sorted(frameworks))

        # Detect deployment target from Package.swift
        pkg_swift = project_dir / "Package.swift"
        if pkg_swift.exists():
            try:
                import re as _re
                content = pkg_swift.read_text()
                pat = r"\.(iOS|macOS)\(.v(\d+(?:_\d+)?)\)"
                targets = []
                for m in _re.finditer(pat, content):
                    plat = m.group(1)
                    ver = m.group(2).replace("_", ".")
                    targets.append(f"{plat} {ver}")
                if targets:
                    info["min_deploy_target"] = " / ".join(targets)
            except OSError:
                pass

        await self._send(ws, "project.info", info)

    async def _handle_ping(self, ws, msg, source="unknown"):
        await self._send(ws, "pong", {
            "uptime": time.time() - self._start_time,
            "queue": self.queue.status_summary(),
            "security": {
                "tls": TLSManager.has_server_certs(),
                "rate_limiter": self.rate_limiter.status(),
            },
        })

    # --- Task Execution ---

    async def _process_queue(self, ws=None):
        """Try to start the next queued task."""
        task = self.queue.dequeue()
        if not task:
            return

        self.log.info(f"Starting task: {task.id}")

        try:
            self.sessions.create_session(task)
        except Exception as e:
            self.queue.complete(task, exit_code=1, error=str(e))
            await self._broadcast("task.error", {
                "task_id": task.id,
                "error": str(e),
            })
            return

        # Start output streaming in background
        runner = asyncio.create_task(self._run_task(task))
        self._task_runners[task.id] = runner

    async def _run_task(self, task):
        """Monitor a running task, stream output, detect completion."""
        pipe_path = self.sessions.capture_output(task)
        start = time.time()
        timeout = self.config["session_timeout"]

        # Open the pipe for reading (non-blocking)
        pipe_fd = None
        if pipe_path and os.path.exists(pipe_path):
            try:
                # Open non-blocking
                pipe_fd = os.open(pipe_path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                pipe_fd = None

        try:
            while self._running:
                # Check timeout
                if time.time() - start > timeout:
                    self.log.warning(f"Task timed out: {task.id}")
                    self.sessions.kill_session(task.tmux_session)
                    self.queue.complete(task, exit_code=-1, error="Timeout")
                    await self._broadcast("task.error", {
                        "task_id": task.id,
                        "error": f"Task timed out after {timeout}s",
                    })
                    break

                # Read output from pipe
                if pipe_fd is not None:
                    try:
                        data = os.read(pipe_fd, 65536)
                        if data:
                            text = data.decode("utf-8", errors="replace")
                            lines = text.splitlines()
                            for line in lines:
                                if line.strip():
                                    task.output_lines.append(line)
                                    # Keep buffer bounded
                                    if len(task.output_lines) > MAX_OUTPUT_BUFFER:
                                        task.output_lines.pop(0)
                            await self._broadcast("task.output", {
                                "task_id": task.id,
                                "chunk": text,
                                "line_count": len(task.output_lines),
                            })
                    except (OSError, BlockingIOError):
                        pass  # No data available yet

                # Check if session is still alive
                if not self.sessions.is_session_alive(task.tmux_session):
                    exit_code = self.sessions.get_exit_code(task.id)
                    self.queue.complete(task, exit_code=exit_code or 0)

                    # Auto-sync: auto git-push after task completion
                    git_pushed = False
                    if self.config.get("auto_git", True) and (exit_code or 0) == 0:
                        project = task.project_path or self.config["project_path"]
                        success, output = self.sessions.git_push(project)
                        git_pushed = success
                        if not success:
                            self.log.warning(f"Post-task git push failed: {output}")

                    await self._broadcast("task.complete", {
                        "task_id": task.id,
                        "exit_code": exit_code or 0,
                        "duration": time.time() - start,
                        "output_lines": len(task.output_lines),
                        "git_pushed": git_pushed,
                    })
                    break

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            self.log.info(f"Task runner cancelled: {task.id}")
        finally:
            if pipe_fd is not None:
                try:
                    os.close(pipe_fd)
                except OSError:
                    pass
            self.sessions.cleanup(task.id)
            self._task_runners.pop(task.id, None)

            # Process next task in queue
            await self._process_queue()

    # --- Health Monitor ---

    async def _health_loop(self):
        """Periodic health check: clean dead sessions, log status."""
        while self._running:
            try:
                active = self.queue.active
                if active and active.tmux_session:
                    if not self.sessions.is_session_alive(active.tmux_session):
                        self.log.warning(f"Active session died unexpectedly: {active.id}")
                        exit_code = self.sessions.get_exit_code(active.id)
                        self.queue.complete(active, exit_code=exit_code or -1,
                                            error="Session died unexpectedly")
                        self.sessions.cleanup(active.id)
                        await self._broadcast("task.error", {
                            "task_id": active.id,
                            "error": "Session died unexpectedly",
                        })
                        await self._process_queue()

                # Log heartbeat
                summary = self.queue.status_summary()
                self.log.debug(
                    f"Heartbeat: clients={len(self._clients)} "
                    f"active={'yes' if summary['active'] else 'no'} "
                    f"queued={summary['queue_size']}"
                )

            except Exception as e:
                self.log.error(f"Health check error: {e}")

            # Prune expired secrets from history
            SecretManager.prune_history()

            # Refresh signers in case of rotation
            valid = SecretManager.get_valid_secrets()
            self._signers = [MessageSigner(s) for s in valid]

            await asyncio.sleep(self.config["heartbeat_interval"])

    # --- Server Lifecycle ---

    async def _start_server_client(self) -> None:
        """Initialize server client (non-blocking, failures are non-fatal)."""
        try:
            await self.server_client.start()
        except Exception as e:
            self.log.warning(f"Server client init failed: {e}")
            self.log.warning("Daemon continues without server communication")

    async def start(self):
        """Start the WebSocket server and health monitor."""
        self._start_time = time.time()
        BRIDGE_DIR.mkdir(parents=True, exist_ok=True)

        # Write PID file
        PID_FILE.write_text(str(os.getpid()))

        port = self.config["port"]
        self.log.info(f"Byfrost Daemon starting on port {port}")
        self.log.info(f"Project path: {self.config['project_path']}")
        self.log.info(f"Claude path: {self.config['claude_path']}")
        self.log.info(f"Session timeout: {self.config['session_timeout']}s")
        self.log.info(f"Heartbeat interval: {self.config['heartbeat_interval']}s")
        auto_git = "enabled" if self.config.get("auto_git") else "disabled"
        self.log.info(f"Auto git pull/push: {auto_git}")

        # Fetch credentials from server BEFORE deciding TLS
        # This ensures certs are available if a pairing exists
        await self._start_server_client()

        # TLS setup (after credential fetch so certs may now be available)
        ssl_context = None
        use_tls = TLSManager.has_server_certs()
        if use_tls:
            try:
                ssl_context = TLSManager.get_server_ssl_context()
                self.log.info("TLS: enabled (mTLS with client certificate verification)")
            except Exception as e:
                self.log.warning(f"TLS: failed to load certs ({e}), falling back to plaintext")
                use_tls = False
        else:
            self.log.warning("TLS: disabled (no server certificates found)")

        protocol = "wss" if use_tls else "ws"

        if self.config["secret"]:
            self.log.info("HMAC auth: enabled (secret in ~/.byfrost/secret)")
        else:
            self.log.warning("HMAC auth: DISABLED (no secret configured)")

        self.audit.daemon_start(port, use_tls)

        # Print connection info
        print(f"\n{'='*56}")
        print("  Byfrost Daemon")
        print(f"  Port:    {port} ({protocol}://)")
        print(f"  TLS:     {'mTLS (mutual)' if use_tls else 'DISABLED'}")
        print(f"  HMAC:    {'enabled' if self.config['secret'] else 'DISABLED'}")
        print(f"  AutoGit: {'ON' if self.config.get('auto_git') else 'OFF'}")
        print(f"  Audit:   {LOG_DIR / 'audit.log'}")
        print(f"  Logs:    {LOG_DIR / 'daemon.log'}")
        print(f"  PID:     {os.getpid()}")
        print(f"{'='*56}\n")

        # Start health monitor
        health_task = asyncio.create_task(self._health_loop())

        # Start file sync watcher
        file_sync_task = None
        if self.config.get("project_path"):
            file_sync_task = asyncio.create_task(
                self.file_sync.start(asyncio.get_event_loop())
            )

        # Start WebSocket server
        try:
            async with serve(
                self.handle_client,
                "0.0.0.0",
                port,
                ssl=ssl_context,
                ping_interval=20,
                ping_timeout=10,
                max_size=2**20,  # 1MB max message
            ):
                self.log.info(f"WebSocket server ready ({protocol}://0.0.0.0:{port})")
                await asyncio.Future()  # Run forever
        except asyncio.CancelledError:
            self.log.info("Server shutting down...")
        finally:
            health_task.cancel()
            if file_sync_task:
                file_sync_task.cancel()
            await self.file_sync.stop()
            await self.server_client.stop()
            self._running = False

            # Kill any active sessions
            active = self.queue.active
            if active and active.tmux_session:
                self.sessions.kill_session(active.tmux_session)
                self.sessions.cleanup(active.id)

            PID_FILE.unlink(missing_ok=True)
            self.log.info("Daemon stopped")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Byfrost Daemon")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    parser.add_argument("--port", type=int, help="Override port")
    parser.add_argument("--daemonize", action="store_true", help="Run in background")
    args = parser.parse_args()

    config = load_config()
    if args.port:
        config["port"] = args.port

    log = setup_logging(verbose=args.verbose)

    # Validate and auto-discover project path
    validate_project_path(config, log)

    # Handle signals for graceful shutdown
    def shutdown(sig, frame):
        log.info(f"Received signal {sig}, shutting down...")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    daemon = ByfrostDaemon(config, log)

    if args.daemonize:
        # Fork into background
        pid = os.fork()
        if pid > 0:
            print(f"Daemon started with PID {pid}")
            sys.exit(0)
        os.setsid()
        # Redirect stdio
        devnull = open(os.devnull, "r+b")
        os.dup2(devnull.fileno(), sys.stdin.fileno())
        os.dup2(devnull.fileno(), sys.stdout.fileno())
        os.dup2(devnull.fileno(), sys.stderr.fileno())

    asyncio.run(daemon.start())


if __name__ == "__main__":
    main()
