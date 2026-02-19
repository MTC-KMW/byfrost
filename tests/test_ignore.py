"""Tests for core/ignore.py - shared ignore logic."""

import hashlib
from pathlib import Path

from core.ignore import (
    MAX_FILE_SIZE,
    generate_checksums,
    load_ignore_spec,
    should_ignore,
)

# ---------------------------------------------------------------------------
# Default patterns
# ---------------------------------------------------------------------------

class TestDefaultPatterns:
    """DEFAULT_IGNORE_PATTERNS catches common unwanted files."""

    def test_git_dir_ignored(self, tmp_path: Path) -> None:
        spec = load_ignore_spec(tmp_path)
        assert should_ignore(".git/HEAD", spec)
        assert should_ignore(".git/objects/abc123", spec)

    def test_ds_store_ignored(self, tmp_path: Path) -> None:
        spec = load_ignore_spec(tmp_path)
        assert should_ignore(".DS_Store", spec)
        assert should_ignore("subdir/.DS_Store", spec)

    def test_pycache_ignored(self, tmp_path: Path) -> None:
        spec = load_ignore_spec(tmp_path)
        assert should_ignore("__pycache__/module.pyc", spec)
        assert should_ignore("src/__pycache__/foo.pyc", spec)

    def test_pyc_ignored(self, tmp_path: Path) -> None:
        spec = load_ignore_spec(tmp_path)
        assert should_ignore("module.pyc", spec)

    def test_node_modules_ignored(self, tmp_path: Path) -> None:
        spec = load_ignore_spec(tmp_path)
        assert should_ignore("node_modules/express/index.js", spec)

    def test_derived_data_ignored(self, tmp_path: Path) -> None:
        spec = load_ignore_spec(tmp_path)
        assert should_ignore("DerivedData/Build/Products/Debug/app", spec)

    def test_source_files_not_ignored(self, tmp_path: Path) -> None:
        spec = load_ignore_spec(tmp_path)
        assert not should_ignore("src/main.py", spec)
        assert not should_ignore("ios/App.swift", spec)
        assert not should_ignore("README.md", spec)
        assert not should_ignore("byfrost/tasks/apple/current.md", spec)


# ---------------------------------------------------------------------------
# .gitignore integration
# ---------------------------------------------------------------------------

class TestGitignoreIntegration:
    """load_ignore_spec reads .gitignore and merges with defaults."""

    def test_reads_gitignore(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("*.log\nsecrets/\n")
        spec = load_ignore_spec(tmp_path)
        assert should_ignore("app.log", spec)
        assert should_ignore("secrets/api_key.txt", spec)
        # Defaults still apply
        assert should_ignore(".DS_Store", spec)

    def test_gitignore_comments_skipped(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("# comment\n*.tmp\n")
        spec = load_ignore_spec(tmp_path)
        assert should_ignore("file.tmp", spec)
        assert not should_ignore("# comment", spec)

    def test_no_gitignore_uses_defaults(self, tmp_path: Path) -> None:
        spec = load_ignore_spec(tmp_path)
        assert should_ignore(".git/HEAD", spec)
        assert not should_ignore("src/main.py", spec)


# ---------------------------------------------------------------------------
# for_sync mode (bridge file sync ignores .gitignore)
# ---------------------------------------------------------------------------

class TestForSyncMode:
    """for_sync=True skips .gitignore - bridge syncs all project files."""

    def test_gitignore_not_applied(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("*.log\nsecrets/\nbyfrost/\n")
        spec = load_ignore_spec(tmp_path, for_sync=True)
        # .gitignore patterns should NOT be applied
        assert not should_ignore("app.log", spec)
        assert not should_ignore("secrets/api_key.txt", spec)
        assert not should_ignore("byfrost/tasks/apple/current.md", spec)

    def test_defaults_still_applied(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("*.log\n")
        spec = load_ignore_spec(tmp_path, for_sync=True)
        # Build artifacts and caches still ignored
        assert should_ignore(".git/HEAD", spec)
        assert should_ignore("__pycache__/foo.pyc", spec)
        assert should_ignore("node_modules/express/index.js", spec)
        assert should_ignore(".DS_Store", spec)

    def test_source_files_pass(self, tmp_path: Path) -> None:
        spec = load_ignore_spec(tmp_path, for_sync=True)
        assert not should_ignore("src/main.py", spec)
        assert not should_ignore("mac-app/Byfrost/AppDelegate.swift", spec)
        assert not should_ignore("byfrost/compound/patterns.md", spec)


# ---------------------------------------------------------------------------
# generate_checksums
# ---------------------------------------------------------------------------

class TestGenerateChecksums:
    """generate_checksums walks project and returns hashes."""

    def test_returns_correct_hashes(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.txt").write_text("world")
        spec = load_ignore_spec(tmp_path)
        result = generate_checksums(tmp_path, spec)
        assert result["a.txt"] == hashlib.sha256(b"hello").hexdigest()
        assert result["b.txt"] == hashlib.sha256(b"world").hexdigest()

    def test_skips_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("ok")
        (tmp_path / ".DS_Store").write_bytes(b"\x00" * 10)
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main")
        spec = load_ignore_spec(tmp_path)
        result = generate_checksums(tmp_path, spec)
        assert "a.txt" in result
        assert ".DS_Store" not in result
        assert ".git/HEAD" not in result

    def test_skips_large_files(self, tmp_path: Path) -> None:
        (tmp_path / "small.txt").write_text("ok")
        (tmp_path / "big.bin").write_bytes(b"x" * (MAX_FILE_SIZE + 1))
        spec = load_ignore_spec(tmp_path)
        result = generate_checksums(tmp_path, spec)
        assert "small.txt" in result
        assert "big.bin" not in result

    def test_skips_symlinks(self, tmp_path: Path) -> None:
        (tmp_path / "real.txt").write_text("real")
        (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
        spec = load_ignore_spec(tmp_path)
        result = generate_checksums(tmp_path, spec)
        assert "real.txt" in result
        assert "link.txt" not in result
