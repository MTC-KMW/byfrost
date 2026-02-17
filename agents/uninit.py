"""Remove byfrost agent team files from a project.

Cleanly reverses `byfrost init` by removing the byfrost/ directory
and stripping the byfrost reference block from the root CLAUDE.md.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

BYFROST_SUBDIR = "byfrost"
BYFROST_MARKER = "\n---\n\n## Byfrost Agent Team"


def _print_status(msg: str) -> None:
    print(f"\033[36m[byfrost]\033[0m {msg}")


def _print_error(msg: str) -> None:
    print(f"\033[31m[byfrost error]\033[0m {msg}", file=sys.stderr)


def _count_files(path: Path) -> int:
    """Count files recursively in a directory."""
    return sum(1 for f in path.rglob("*") if f.is_file())


def _clean_root_claude_md(project_dir: Path) -> str | None:
    """Strip byfrost reference block from root CLAUDE.md.

    Returns a description of what was done, or None if no action taken.
    """
    root_md = project_dir / "CLAUDE.md"
    if not root_md.exists():
        return None

    content = root_md.read_text()
    if "## Byfrost Agent Team" not in content:
        return None

    idx = content.find(BYFROST_MARKER)
    if idx == -1:
        return None

    cleaned = content[:idx].rstrip()

    # If only a project heading remains (or empty), the file was created by init
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    if len(lines) <= 1 and (not lines or lines[0].startswith("# ")):
        root_md.unlink()
        return "Removed CLAUDE.md (created by byfrost init)"

    root_md.write_text(cleaned + "\n")
    return "Cleaned CLAUDE.md (removed byfrost reference)"


def _stop_sync_if_running() -> None:
    """Stop the file sync process if it's running."""
    try:
        from cli.file_sync import PID_FILE, stop_sync

        if PID_FILE.exists():
            stop_sync()
    except ImportError:
        pass


def run_uninit_wizard(project_dir: Path) -> int:
    """Remove byfrost from a project. Returns exit code."""
    bf_dir = project_dir / BYFROST_SUBDIR

    if not bf_dir.exists():
        _print_status("No byfrost/ directory found. Nothing to remove.")
        return 0

    file_count = _count_files(bf_dir)
    _print_status(f"Found byfrost/ with {file_count} files.")
    _print_status("This will remove:")
    _print_status(f"  - byfrost/ directory ({file_count} files)")

    root_md = project_dir / "CLAUDE.md"
    has_byfrost_ref = False
    if root_md.exists():
        content = root_md.read_text()
        if "## Byfrost Agent Team" in content:
            has_byfrost_ref = True
            _print_status("  - Byfrost reference from CLAUDE.md")

    try:
        answer = input("\n\033[36m[byfrost]\033[0m Proceed? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        _print_status("Cancelled.")
        return 1

    if answer != "y":
        _print_status("Cancelled.")
        return 1

    print()

    # Stop sync process first
    _stop_sync_if_running()

    # Remove byfrost/ directory
    shutil.rmtree(bf_dir)
    _print_status(f"Removed byfrost/ ({file_count} files)")

    # Clean root CLAUDE.md
    if has_byfrost_ref:
        result = _clean_root_claude_md(project_dir)
        if result:
            _print_status(result)

    print()
    _print_status("Done. Byfrost has been removed from this project.")
    return 0
