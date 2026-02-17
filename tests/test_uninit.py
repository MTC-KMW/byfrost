"""Tests for agents/uninit.py - byfrost uninit command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agents.uninit import (
    _clean_root_claude_md,
    _count_files,
    run_uninit_wizard,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Create a minimal byfrost project structure."""
    bf = tmp_path / "byfrost"
    bf.mkdir()
    (bf / "tasks" / "apple").mkdir(parents=True)
    (bf / "shared").mkdir()
    (bf / "pm").mkdir()
    (bf / "qa").mkdir()
    (bf / ".byfrost-team.json").write_text("{}")
    (bf / "tasks" / "apple" / "current.md").write_text("# Current Task")
    (bf / "shared" / "decisions.md").write_text("# Decisions")
    (bf / "pm" / "CLAUDE.md").write_text("# PM")
    (bf / "qa" / "CLAUDE.md").write_text("# QA")
    return tmp_path


class TestCountFiles:
    def test_counts_files(self, project: Path) -> None:
        assert _count_files(project / "byfrost") == 5

    def test_empty_dir(self, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        assert _count_files(d) == 0


class TestCleanRootClaudeMd:
    def test_strips_byfrost_block_preserves_content(self, tmp_path: Path) -> None:
        original = "# My Project\n\nSome existing docs.\n"
        byfrost_ref = (
            "\n---\n\n## Byfrost Agent Team\n\n"
            "See `byfrost/CLAUDE.md` for team configuration and coordination.\n"
        )
        (tmp_path / "CLAUDE.md").write_text(original.rstrip() + byfrost_ref)

        result = _clean_root_claude_md(tmp_path)

        assert result is not None
        assert "Cleaned" in result
        restored = (tmp_path / "CLAUDE.md").read_text()
        assert "Byfrost Agent Team" not in restored
        assert "Some existing docs." in restored
        assert restored.endswith("\n")

    def test_deletes_init_created_file(self, tmp_path: Path) -> None:
        content = (
            "# TestApp\n"
            "\n---\n\n## Byfrost Agent Team\n\n"
            "See `byfrost/CLAUDE.md` for team configuration and coordination.\n"
        )
        (tmp_path / "CLAUDE.md").write_text(content)

        result = _clean_root_claude_md(tmp_path)

        assert result is not None
        assert "Removed" in result
        assert not (tmp_path / "CLAUDE.md").exists()

    def test_no_claude_md(self, tmp_path: Path) -> None:
        result = _clean_root_claude_md(tmp_path)
        assert result is None

    def test_no_byfrost_reference(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("# My Project\n\nNo byfrost here.\n")
        result = _clean_root_claude_md(tmp_path)
        assert result is None


class TestRunUninitWizard:
    def test_no_byfrost_dir(self, tmp_path: Path) -> None:
        code = run_uninit_wizard(tmp_path)
        assert code == 0

    def test_removes_byfrost_dir(self, project: Path) -> None:
        with patch("builtins.input", return_value="y"):
            code = run_uninit_wizard(project)

        assert code == 0
        assert not (project / "byfrost").exists()

    def test_cancelled(self, project: Path) -> None:
        with patch("builtins.input", return_value="n"):
            code = run_uninit_wizard(project)

        assert code == 1
        assert (project / "byfrost").exists()

    def test_cleans_claude_md(self, project: Path) -> None:
        original = "# Existing Project\n\nImportant docs.\n"
        byfrost_ref = (
            "\n---\n\n## Byfrost Agent Team\n\n"
            "See `byfrost/CLAUDE.md` for coordination.\n"
        )
        (project / "CLAUDE.md").write_text(original.rstrip() + byfrost_ref)

        with patch("builtins.input", return_value="y"):
            code = run_uninit_wizard(project)

        assert code == 0
        assert not (project / "byfrost").exists()
        content = (project / "CLAUDE.md").read_text()
        assert "Important docs." in content
        assert "Byfrost Agent Team" not in content

    def test_stops_sync(self, project: Path) -> None:
        with (
            patch("builtins.input", return_value="y"),
            patch("agents.uninit._stop_sync_if_running") as mock_stop,
        ):
            run_uninit_wizard(project)

        mock_stop.assert_called_once()

    def test_keyboard_interrupt(self, project: Path) -> None:
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            code = run_uninit_wizard(project)
        assert code == 1
        assert (project / "byfrost").exists()

    def test_eof(self, project: Path) -> None:
        with patch("builtins.input", side_effect=EOFError):
            code = run_uninit_wizard(project)
        assert code == 1
        assert (project / "byfrost").exists()
