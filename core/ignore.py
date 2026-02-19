"""Shared ignore logic for bridge file sync.

Uses pathspec to match .gitignore-style patterns. Both the daemon and
CLI file sync modules use this to decide which files to skip.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pathspec

# Patterns always ignored regardless of .gitignore contents.
# Uses .gitignore syntax (pathspec gitwildmatch).
DEFAULT_IGNORE_PATTERNS = [
    # VCS
    ".git/",
    # OS metadata
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    # Python
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".env",
    "venv/",
    ".venv/",
    "*.egg-info/",
    "dist/",
    # JS / Node
    "node_modules/",
    # Apple / Xcode
    ".build/",
    "DerivedData/",
    "*.xcworkspace/",
    "Pods/",
    ".swiftpm/",
    # Build artifacts
    "build/",
]

# Max file size for sync (2MB) - covers source code, config, docs.
# Binary assets larger than this are skipped.
MAX_FILE_SIZE = 2 * 1024 * 1024

# Agent team coordination directory that must always be synced even if
# .gitignore excludes it. Uses gitignore negation syntax.
# All coordination files live under byfrost/ (tasks/, compound/, pm/, etc.)
SYNC_ALLOW_PATTERNS = [
    "!byfrost/",
]


def load_ignore_spec(project_dir: Path, *, for_sync: bool = False) -> pathspec.PathSpec:
    """Load ignore patterns from .gitignore + defaults.

    Reads .gitignore from project_dir (if it exists) and merges with
    DEFAULT_IGNORE_PATTERNS. Returns a compiled PathSpec for matching.

    When for_sync=True, appends negation patterns so that agent team
    coordination directories are never ignored by the bridge file sync
    (even though .gitignore correctly excludes them from git).
    """
    lines = list(DEFAULT_IGNORE_PATTERNS)
    gitignore = project_dir / ".gitignore"
    if gitignore.is_file():
        try:
            text = gitignore.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    lines.append(stripped)
        except OSError:
            pass
    if for_sync:
        lines.extend(SYNC_ALLOW_PATTERNS)
    return pathspec.PathSpec.from_lines("gitignore", lines)


def should_ignore(rel_path: str, spec: pathspec.PathSpec) -> bool:
    """Check if a relative path should be ignored.

    Args:
        rel_path: Path relative to project root (forward slashes).
        spec: Compiled PathSpec from load_ignore_spec().

    Returns:
        True if the path matches any ignore pattern.
    """
    return spec.match_file(rel_path)


def generate_checksums(
    project_dir: Path, spec: pathspec.PathSpec,
) -> dict[str, str]:
    """Walk project and return {rel_path: sha256_hex} for all synced files.

    Skips ignored files, symlinks, and files larger than MAX_FILE_SIZE.
    Used for parity validation between controller and worker.
    """
    result: dict[str, str] = {}
    for f in project_dir.rglob("*"):
        if f.is_symlink() or not f.is_file():
            continue
        try:
            rel = str(f.relative_to(project_dir))
        except ValueError:
            continue
        if should_ignore(rel, spec):
            continue
        try:
            if f.stat().st_size > MAX_FILE_SIZE:
                continue
            data = f.read_bytes()
        except OSError:
            continue
        result[rel] = hashlib.sha256(data).hexdigest()
    return result
