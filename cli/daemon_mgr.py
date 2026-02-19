"""Daemon lifecycle management - install, start, stop, status across platforms.

Supports launchd (macOS), systemd (Linux), and Task Scheduler (Windows).
All operations use user-level services - no root/sudo required.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from core.config import BRIDGE_DIR, LOG_DIR

# ---------------------------------------------------------------------------
# Service identifiers
# ---------------------------------------------------------------------------

LABEL = "com.byfrost.daemon"
SERVICE_NAME = "byfrost"
DAEMON_MODULE = "daemon.byfrost_daemon"
VENV_DIR = BRIDGE_DIR / ".venv"


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class DaemonManager:
    """Base class for platform-specific daemon management."""

    def install(self) -> bool:
        """Install the daemon as a system service. Returns True on success."""
        raise NotImplementedError

    def uninstall(self) -> bool:
        """Remove the daemon service. Returns True on success."""
        raise NotImplementedError

    def start(self) -> bool:
        """Start the daemon. Returns True on success."""
        raise NotImplementedError

    def stop(self) -> bool:
        """Stop the daemon. Returns True on success."""
        raise NotImplementedError

    def restart(self) -> bool:
        """Restart the daemon. Returns True on success."""
        self.stop()
        return self.start()

    def status(self) -> dict[str, Any]:
        """Return daemon status: {installed, running, pid}."""
        raise NotImplementedError

    # --- Python environment resolution ---

    def _ensure_python_env(self) -> str:
        """Ensure a working Python environment for the daemon.

        Resolution order:
        1. If already in a venv, use current sys.executable.
        2. If all required deps are importable, use sys.executable.
        3. Create ~/.byfrost/.venv, install byfrost, return its python.
        """
        # Already in a venv - use it
        if sys.prefix != sys.base_prefix:
            return sys.executable

        # System Python - check if all deps are available
        for mod in ("websockets", "watchdog", "pathspec", "httpx"):
            try:
                __import__(mod)
            except ImportError:
                break
        else:
            return sys.executable

        # Deps missing, need a venv
        return self._create_venv()

    def _create_venv(self) -> str:
        """Create ~/.byfrost/.venv and install byfrost into it."""
        BRIDGE_DIR.mkdir(parents=True, exist_ok=True)

        venv_python = VENV_DIR / "bin" / "python"
        if sys.platform == "win32":
            venv_python = VENV_DIR / "Scripts" / "python.exe"

        if not venv_python.exists():
            print(f"  Creating virtual environment at {VENV_DIR}...")
            result = subprocess.run(
                [sys.executable, "-m", "venv", str(VENV_DIR)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Failed to create venv: {result.stderr.strip()}")

        # Install byfrost (editable if source available, else from PyPI)
        project_root = self._find_project_root()
        if project_root:
            install_target = ["-e", str(project_root)]
        else:
            install_target = ["byfrost"]

        print(f"  Installing byfrost into {VENV_DIR}...")
        result = subprocess.run(
            [str(venv_python), "-m", "pip", "install", *install_target],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"pip install failed: {result.stderr.strip()[:200]}")

        return str(venv_python)

    @staticmethod
    def _find_project_root() -> Path | None:
        """Walk up from this file to find pyproject.toml."""
        current = Path(__file__).resolve().parent
        for _ in range(5):
            if (current / "pyproject.toml").exists():
                return current
            parent = current.parent
            if parent == current:
                break
            current = parent
        return None


# ---------------------------------------------------------------------------
# macOS - launchd
# ---------------------------------------------------------------------------


class LaunchdManager(DaemonManager):
    """Manage daemon via launchd on macOS."""

    def __init__(self) -> None:
        self._agents_dir = Path.home() / "Library" / "LaunchAgents"
        self._plist_path = self._agents_dir / f"{LABEL}.plist"

    def _generate_plist(self, python_path: str | None = None) -> str:
        """Generate plist XML with real paths."""
        python = python_path or sys.executable
        log_dir = str(LOG_DIR)
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>{DAEMON_MODULE}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>WorkingDirectory</key>
    <string>{Path.home()}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
        <key>BYFROST_HOME</key>
        <string>{BRIDGE_DIR}</string>
    </dict>

    <key>StandardOutPath</key>
    <string>{log_dir}/launchd-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/launchd-stderr.log</string>

    <key>SoftResourceLimits</key>
    <dict>
        <key>NumberOfFiles</key>
        <integer>4096</integer>
    </dict>
</dict>
</plist>
"""

    def install(self) -> bool:
        try:
            python_path = self._ensure_python_env()
        except RuntimeError as e:
            print(f"  ERROR: {e}")
            return False

        self._agents_dir.mkdir(parents=True, exist_ok=True)
        self._plist_path.write_text(self._generate_plist(python_path))
        result = subprocess.run(
            ["launchctl", "load", str(self._plist_path)],
            capture_output=True,
        )
        if result.returncode != 0:
            return False
        # launchctl load with RunAtLoad=true auto-starts the daemon
        return True

    def uninstall(self) -> bool:
        if self._plist_path.exists():
            subprocess.run(
                ["launchctl", "unload", str(self._plist_path)],
                capture_output=True,
            )
            self._plist_path.unlink(missing_ok=True)
        return True

    def start(self) -> bool:
        result = subprocess.run(
            ["launchctl", "start", LABEL],
            capture_output=True,
        )
        return result.returncode == 0

    def stop(self) -> bool:
        result = subprocess.run(
            ["launchctl", "stop", LABEL],
            capture_output=True,
        )
        return result.returncode == 0

    def status(self) -> dict[str, Any]:
        installed = self._plist_path.exists()
        if not installed:
            return {"installed": False, "running": False, "pid": None}

        result = subprocess.run(
            ["launchctl", "list", LABEL],
            capture_output=True, text=True,
        )
        running = result.returncode == 0
        pid = None
        if running:
            # Parse PID from launchctl list output
            for line in result.stdout.splitlines():
                if '"PID"' in line:
                    parts = line.strip().rstrip(";").split("=")
                    if len(parts) == 2:
                        try:
                            pid = int(parts[1].strip())
                        except ValueError:
                            pass

        return {"installed": True, "running": running, "pid": pid}


# ---------------------------------------------------------------------------
# Linux - systemd (user-level)
# ---------------------------------------------------------------------------


class SystemdManager(DaemonManager):
    """Manage daemon via systemd user services on Linux."""

    def __init__(self) -> None:
        self._unit_dir = Path.home() / ".config" / "systemd" / "user"
        self._unit_path = self._unit_dir / f"{SERVICE_NAME}.service"

    def _generate_unit(self, python_path: str | None = None) -> str:
        """Generate systemd unit file with real paths."""
        python = python_path or sys.executable
        home = str(Path.home())
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        return f"""[Unit]
Description=Byfrost Worker Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={python} -m {DAEMON_MODULE}
WorkingDirectory={home}
Restart=on-failure
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=3

Environment=BYFROST_HOME={BRIDGE_DIR}
Environment=PATH={os.environ.get('PATH', '/usr/bin:/bin')}

NoNewPrivileges=true

[Install]
WantedBy=default.target
"""

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run systemctl --user with given args."""
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True,
        )

    def install(self) -> bool:
        try:
            python_path = self._ensure_python_env()
        except RuntimeError as e:
            print(f"  ERROR: {e}")
            return False

        self._unit_dir.mkdir(parents=True, exist_ok=True)
        self._unit_path.write_text(self._generate_unit(python_path))
        self._systemctl("daemon-reload")
        result = self._systemctl("enable", SERVICE_NAME)
        if result.returncode != 0:
            return False
        # Auto-start after install
        self.start()
        return True

    def uninstall(self) -> bool:
        self._systemctl("stop", SERVICE_NAME)
        self._systemctl("disable", SERVICE_NAME)
        self._unit_path.unlink(missing_ok=True)
        self._systemctl("daemon-reload")
        return True

    def start(self) -> bool:
        result = self._systemctl("start", SERVICE_NAME)
        return result.returncode == 0

    def stop(self) -> bool:
        result = self._systemctl("stop", SERVICE_NAME)
        return result.returncode == 0

    def status(self) -> dict[str, Any]:
        installed = self._unit_path.exists()
        if not installed:
            return {"installed": False, "running": False, "pid": None}

        result = self._systemctl("show", SERVICE_NAME,
                                 "--property=ActiveState,MainPID")
        active = False
        pid = None
        for line in result.stdout.splitlines():
            if line.startswith("ActiveState="):
                active = line.split("=", 1)[1] == "active"
            elif line.startswith("MainPID="):
                try:
                    p = int(line.split("=", 1)[1])
                    pid = p if p > 0 else None
                except ValueError:
                    pass

        return {"installed": True, "running": active, "pid": pid}


# ---------------------------------------------------------------------------
# Windows - Task Scheduler
# ---------------------------------------------------------------------------


class WindowsManager(DaemonManager):
    """Manage daemon via Task Scheduler on Windows."""

    TASK_NAME = "ByfrostDaemon"

    def install(self) -> bool:
        try:
            python_path = self._ensure_python_env()
        except RuntimeError as e:
            print(f"  ERROR: {e}")
            return False

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "schtasks", "/create",
                "/tn", self.TASK_NAME,
                "/tr", f'"{python_path}" -m {DAEMON_MODULE}',
                "/sc", "onlogon",
                "/rl", "limited",
                "/f",
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False
        # Auto-start after install
        self.start()
        return True

    def uninstall(self) -> bool:
        result = subprocess.run(
            ["schtasks", "/delete", "/tn", self.TASK_NAME, "/f"],
            capture_output=True, text=True,
        )
        return result.returncode == 0

    def start(self) -> bool:
        result = subprocess.run(
            ["schtasks", "/run", "/tn", self.TASK_NAME],
            capture_output=True, text=True,
        )
        return result.returncode == 0

    def stop(self) -> bool:
        result = subprocess.run(
            ["schtasks", "/end", "/tn", self.TASK_NAME],
            capture_output=True, text=True,
        )
        return result.returncode == 0

    def status(self) -> dict[str, Any]:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", self.TASK_NAME, "/fo", "list"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return {"installed": False, "running": False, "pid": None}

        running = "Running" in result.stdout
        return {"installed": True, "running": running, "pid": None}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_daemon_manager() -> DaemonManager:
    """Return the platform-appropriate daemon manager."""
    if sys.platform == "darwin":
        return LaunchdManager()
    elif sys.platform == "win32":
        return WindowsManager()
    else:
        return SystemdManager()
