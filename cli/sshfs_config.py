"""SSHFS mount configuration for Mac workers.

Detects macFUSE + sshfs, configures mount points for coordination
directories (tasks/, shared/, compound/, pm/, qa/), writes launchd
auto-remount plist, and verifies mounts with read/write tests.

Platform: macOS only. Used by Mac wizard and headless CLI setup.

Usage: byfrost sshfs [setup|mount|unmount|remount|status]
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from agents.init import TeamConfig

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MOUNT_DIRS = ["tasks", "shared", "compound", "pm", "qa"]
AGENT_TEAM_DIR = ".agent-team"
CONFIG_ENV_FILE = "config.env"
LAUNCHD_LABEL = "com.byfrost.sshfs"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

SSHFS_OPTIONS = [
    "reconnect",
    "ServerAliveInterval=15",
    "ServerAliveCountMax=3",
    "StrictHostKeyChecking=no",
    "NumberOfPasswordPrompts=0",
    "cache=yes",
    "cache_timeout=5",
    "auto_cache",
    "follow_symlinks",
    "volname=byfrost",
]

MACFUSE_PATHS = [
    Path("/Library/Filesystems/macfuse.fs"),
    Path("/usr/local/lib/libfuse.dylib"),
    Path("/opt/homebrew/lib/libfuse.dylib"),
]


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _print_status(msg: str) -> None:
    print(f"\033[36m[byfrost]\033[0m {msg}")


def _print_error(msg: str) -> None:
    print(f"\033[31m[byfrost error]\033[0m {msg}", file=sys.stderr)


def _prompt(question: str, default: str = "") -> str:
    """Prompt user with optional default."""
    if default:
        answer = input(f"  {question} [{default}]: ").strip()
        return answer or default
    return input(f"  {question}: ").strip()


def _prompt_yn(question: str, default: bool = True) -> bool:
    """Yes/no prompt."""
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"  {question} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_macfuse() -> bool:
    """Check if macFUSE is installed."""
    return any(p.exists() for p in MACFUSE_PATHS)


def detect_sshfs() -> bool:
    """Check if sshfs command is available."""
    return shutil.which("sshfs") is not None


def check_prerequisites() -> tuple[bool, list[str]]:
    """Check for macFUSE and sshfs. Returns (ok, missing_items)."""
    missing: list[str] = []
    if not detect_macfuse():
        missing.append("macFUSE - install with: brew install --cask macfuse")
    if not detect_sshfs():
        missing.append("sshfs - install with: brew install sshfs")
    return (len(missing) == 0, missing)


def check_ssh_connectivity(hostname: str) -> bool:
    """Test SSH connectivity to hostname with 5-second timeout."""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", hostname, "true"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class SSHFSConfig:
    """SSHFS mount configuration."""

    controller_hostname: str  # SSH hostname of the controller
    project_path: str  # Project path on the controller
    local_project: str  # Local project path on the Mac


def load_sshfs_config(project_dir: Path) -> SSHFSConfig | None:
    """Load SSHFS config from .agent-team/config.env."""
    config_path = project_dir / AGENT_TEAM_DIR / CONFIG_ENV_FILE
    if not config_path.exists():
        return None

    values: dict[str, str] = {}
    for line in config_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()

    hostname = values.get("LINUX_HOSTNAME", "")
    project_path = values.get("LINUX_PROJECT_PATH", "")
    local_project = values.get("PROJECT_ROOT", str(project_dir))

    if not hostname or not project_path:
        return None

    return SSHFSConfig(
        controller_hostname=hostname,
        project_path=project_path,
        local_project=local_project,
    )


def save_sshfs_config(project_dir: Path, config: SSHFSConfig) -> None:
    """Write SSHFS config to .agent-team/config.env."""
    config_dir = project_dir / AGENT_TEAM_DIR
    config_dir.mkdir(parents=True, exist_ok=True)

    content = (
        "# Byfrost SSHFS configuration\n"
        f"LINUX_HOSTNAME={config.controller_hostname}\n"
        f"LINUX_PROJECT_PATH={config.project_path}\n"
        f"PROJECT_ROOT={config.local_project}\n"
    )
    (config_dir / CONFIG_ENV_FILE).write_text(content)


# ---------------------------------------------------------------------------
# Mount operations
# ---------------------------------------------------------------------------


def sshfs_mount(project_dir: Path, config: SSHFSConfig) -> tuple[bool, list[str]]:
    """Mount all coordination directories. Returns (all_ok, mounted_dirs)."""
    mounted: list[str] = []
    opts = ",".join(SSHFS_OPTIONS)

    for d in MOUNT_DIRS:
        local_path = project_dir / d
        remote = f"{config.controller_hostname}:{config.project_path}/{d}"

        # Skip if already mounted
        if os.path.ismount(str(local_path)):
            mounted.append(d)
            continue

        # Backup local content if present
        if local_path.exists() and any(local_path.iterdir()):
            backup = project_dir / f"{d}.local-backup"
            if not backup.exists():
                local_path.rename(backup)

        local_path.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            ["sshfs", remote, str(local_path), "-o", opts],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            mounted.append(d)
        else:
            _print_error(f"Failed to mount {d}: {result.stderr.strip()}")

    return (len(mounted) == len(MOUNT_DIRS), mounted)


def sshfs_unmount(project_dir: Path) -> tuple[bool, list[str]]:
    """Unmount all coordination directories. Returns (all_ok, unmounted_dirs)."""
    unmounted: list[str] = []

    for d in MOUNT_DIRS:
        local_path = project_dir / d
        if not os.path.ismount(str(local_path)):
            continue

        result = subprocess.run(
            ["umount", str(local_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            unmounted.append(d)
            # Restore backup if present
            backup = project_dir / f"{d}.local-backup"
            if backup.exists():
                # Remove empty mount point, restore backup
                if local_path.exists():
                    local_path.rmdir()
                backup.rename(local_path)
        else:
            _print_error(f"Failed to unmount {d}: {result.stderr.strip()}")

    return (True, unmounted)


def sshfs_remount(
    project_dir: Path, config: SSHFSConfig,
) -> tuple[bool, list[str]]:
    """Unmount then remount all directories."""
    sshfs_unmount(project_dir)
    return sshfs_mount(project_dir, config)


def sshfs_status(project_dir: Path) -> dict[str, dict[str, bool]]:
    """Check mount status of all coordination directories."""
    result: dict[str, dict[str, bool]] = {}
    for d in MOUNT_DIRS:
        local_path = project_dir / d
        mounted = os.path.ismount(str(local_path))
        readable = False
        writable = False
        if mounted:
            readable = os.access(str(local_path), os.R_OK)
            writable = os.access(str(local_path), os.W_OK)
        result[d] = {"mounted": mounted, "readable": readable, "writable": writable}
    return result


def verify_mounts(project_dir: Path) -> bool:
    """Verify all mounts with a read/write test."""
    test_file = ".byfrost-mount-test"
    all_ok = True

    for d in MOUNT_DIRS:
        local_path = project_dir / d
        if not os.path.ismount(str(local_path)):
            _print_error(f"  {d}/ - not mounted")
            all_ok = False
            continue

        test_path = local_path / test_file
        try:
            test_path.write_text("byfrost-test")
            content = test_path.read_text()
            test_path.unlink()
            if content != "byfrost-test":
                raise ValueError("read-back mismatch")
            _print_status(f"  {d}/ - OK")
        except (OSError, ValueError) as e:
            _print_error(f"  {d}/ - read/write test failed: {e}")
            all_ok = False

    return all_ok


# ---------------------------------------------------------------------------
# Launchd auto-remount
# ---------------------------------------------------------------------------


def _generate_sshfs_plist(project_dir: Path) -> str:
    """Generate launchd plist XML for auto-remount."""
    script_path = project_dir / "deploy" / "sshfs-mount.sh"
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{script_path}</string>
        <string>mount</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>Crashed</key>
        <true/>
    </dict>
    <key>WorkingDirectory</key>
    <string>{project_dir}</string>
    <key>StandardOutPath</key>
    <string>{Path.home()}/.byfrost/sshfs-mount.log</string>
    <key>StandardErrorPath</key>
    <string>{Path.home()}/.byfrost/sshfs-mount.log</string>
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
"""


def install_launchd_remount(project_dir: Path) -> bool:
    """Install launchd plist for auto-remount on login."""
    plist_content = _generate_sshfs_plist(project_dir)
    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHD_PLIST.write_text(plist_content)

    result = subprocess.run(
        ["launchctl", "load", str(LAUNCHD_PLIST)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def uninstall_launchd_remount() -> bool:
    """Remove launchd auto-remount plist."""
    if LAUNCHD_PLIST.exists():
        subprocess.run(
            ["launchctl", "unload", str(LAUNCHD_PLIST)],
            capture_output=True,
        )
        LAUNCHD_PLIST.unlink()
    return True


# ---------------------------------------------------------------------------
# Interactive setup
# ---------------------------------------------------------------------------


def run_sshfs_setup(project_dir: Path) -> int:
    """Interactive SSHFS setup wizard. Returns 0 on success, 1 on failure."""
    if sys.platform != "darwin":
        _print_error("SSHFS mounts are only needed on macOS (the Mac worker).")
        _print_error("Run this command on the Mac where the daemon runs.")
        return 1

    _print_status("SSHFS Mount Configuration")
    print()

    # Check prerequisites
    ok, missing = check_prerequisites()
    if not ok:
        _print_error("Missing prerequisites:")
        for item in missing:
            _print_error(f"  - {item}")
        return 1

    _print_status("Prerequisites: macFUSE + sshfs detected")

    # Load team config for defaults
    team_config = TeamConfig.load(project_dir)
    default_host = ""
    if team_config:
        default_host = team_config.controller_hostname

    # Prompt for config
    print()
    hostname = _prompt("Controller hostname (SSH)", default=default_host)
    project_path = _prompt("Project path on controller", default="")

    if not hostname or not project_path:
        _print_error("Hostname and project path are required.")
        return 1

    # Test connectivity
    _print_status(f"Testing SSH connection to {hostname}...")
    if not check_ssh_connectivity(hostname):
        _print_error(f"Cannot connect to {hostname} via SSH.")
        _print_error("Ensure SSH keys are configured and the host is reachable.")
        return 1
    _print_status("SSH connection successful")

    # Save config
    config = SSHFSConfig(
        controller_hostname=hostname,
        project_path=project_path,
        local_project=str(project_dir),
    )
    save_sshfs_config(project_dir, config)
    _print_status(f"Config saved to {AGENT_TEAM_DIR}/{CONFIG_ENV_FILE}")

    # Mount
    print()
    _print_status("Mounting coordination directories...")
    success, mounted = sshfs_mount(project_dir, config)
    for d in mounted:
        _print_status(f"  Mounted: {d}/")

    if not success:
        _print_error("Some directories failed to mount.")
        return 1

    # Verify
    print()
    _print_status("Verifying mounts...")
    if not verify_mounts(project_dir):
        _print_error("Mount verification failed.")
        return 1

    # Launchd auto-remount
    print()
    if _prompt_yn("Install auto-remount on login?"):
        if install_launchd_remount(project_dir):
            _print_status("Auto-remount installed (launchd)")
        else:
            _print_error("Failed to install auto-remount")

    print()
    _print_status("SSHFS configuration complete!")
    return 0


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------


def run_sshfs_command(action: str, project_dir: Path) -> int:
    """Dispatch SSHFS command. Returns 0 on success, 1 on failure."""
    try:
        if action == "setup":
            return run_sshfs_setup(project_dir)

        if action == "status":
            result = sshfs_status(project_dir)
            for d, info in result.items():
                icon = "mounted" if info["mounted"] else "not mounted"
                rw = ""
                if info["mounted"]:
                    r = "R" if info["readable"] else "-"
                    w = "W" if info["writable"] else "-"
                    rw = f" ({r}{w})"
                _print_status(f"  {d + '/':<12} {icon}{rw}")
            return 0

        # mount, unmount, remount need config
        config = load_sshfs_config(project_dir)

        if action == "unmount":
            _, unmounted = sshfs_unmount(project_dir)
            for d in unmounted:
                _print_status(f"  Unmounted: {d}/")
            return 0

        if config is None:
            _print_error(
                "No SSHFS config found. Run 'byfrost sshfs setup' first."
            )
            return 1

        if action == "mount":
            success, mounted = sshfs_mount(project_dir, config)
            for d in mounted:
                _print_status(f"  Mounted: {d}/")
            return 0 if success else 1

        if action == "remount":
            success, mounted = sshfs_remount(project_dir, config)
            for d in mounted:
                _print_status(f"  Mounted: {d}/")
            return 0 if success else 1

        _print_error(f"Unknown action: {action}")
        return 1

    except KeyboardInterrupt:
        print("\nAborted.")
        return 130
    except EOFError:
        print("\nAborted.")
        return 130
    except PermissionError as e:
        _print_error(f"Permission denied: {e}")
        return 1
