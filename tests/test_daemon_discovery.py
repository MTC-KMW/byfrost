"""Tests for daemon project discovery and validation."""

import logging
from pathlib import Path
from unittest.mock import patch

from daemon.byfrost_daemon import (
    _has_project_indicators,
    discover_project_path,
    validate_project_path,
)

# ---------------------------------------------------------------------------
# _has_project_indicators
# ---------------------------------------------------------------------------


class TestHasProjectIndicators:
    """Test project indicator detection."""

    def test_xcodeproj(self, tmp_path: Path) -> None:
        (tmp_path / "MyApp.xcodeproj").mkdir()
        assert _has_project_indicators(tmp_path) is True

    def test_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        assert _has_project_indicators(tmp_path) is True

    def test_requirements_txt(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("flask\n")
        assert _has_project_indicators(tmp_path) is True

    def test_go_mod(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/foo\n")
        assert _has_project_indicators(tmp_path) is True

    def test_cargo_toml(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text("[package]\n")
        assert _has_project_indicators(tmp_path) is True

    def test_pyproject_toml(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        assert _has_project_indicators(tmp_path) is True

    def test_empty_dir(self, tmp_path: Path) -> None:
        assert _has_project_indicators(tmp_path) is False

    def test_only_text_files(self, tmp_path: Path) -> None:
        (tmp_path / "readme.md").write_text("# Hello\n")
        assert _has_project_indicators(tmp_path) is False


# ---------------------------------------------------------------------------
# discover_project_path
# ---------------------------------------------------------------------------


class TestDiscoverProjectPath:
    """Test auto-discovery of project directories."""

    def test_finds_project_at_cwd(self, tmp_path: Path) -> None:
        (tmp_path / "MyApp.xcodeproj").mkdir()
        log = logging.getLogger("test")
        with patch("daemon.byfrost_daemon.Path.cwd", return_value=tmp_path):
            result = discover_project_path(log)
        assert result == str(tmp_path)

    def test_finds_project_in_home_child(self, tmp_path: Path) -> None:
        # Create a project in a subdirectory of "home"
        proj = tmp_path / "MyProject"
        proj.mkdir()
        (proj / "Package.swift").write_text("// swift\n")
        log = logging.getLogger("test")
        with (
            patch("daemon.byfrost_daemon.Path.cwd", return_value=tmp_path),
            patch("daemon.byfrost_daemon.Path.home", return_value=tmp_path),
        ):
            result = discover_project_path(log)
        assert result == str(proj)

    def test_finds_project_2_levels_deep(self, tmp_path: Path) -> None:
        proj = tmp_path / "code" / "MyProject"
        proj.mkdir(parents=True)
        (proj / "requirements.txt").write_text("flask\n")
        log = logging.getLogger("test")
        with (
            patch("daemon.byfrost_daemon.Path.cwd", return_value=tmp_path),
            patch("daemon.byfrost_daemon.Path.home", return_value=tmp_path),
        ):
            result = discover_project_path(log)
        assert result == str(proj)

    def test_finds_project_3_levels_deep(self, tmp_path: Path) -> None:
        proj = tmp_path / "Users" / "me" / "MyApp"
        proj.mkdir(parents=True)
        (proj / "MyApp.xcodeproj").mkdir()
        log = logging.getLogger("test")
        with (
            patch("daemon.byfrost_daemon.Path.cwd", return_value=tmp_path),
            patch("daemon.byfrost_daemon.Path.home", return_value=tmp_path),
        ):
            result = discover_project_path(log)
        assert result == str(proj)

    def test_skips_hidden_dirs(self, tmp_path: Path) -> None:
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "package.json").write_text("{}")
        log = logging.getLogger("test")
        with (
            patch("daemon.byfrost_daemon.Path.cwd", return_value=tmp_path),
            patch("daemon.byfrost_daemon.Path.home", return_value=tmp_path),
        ):
            result = discover_project_path(log)
        assert result is None

    def test_skips_node_modules(self, tmp_path: Path) -> None:
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / "package.json").write_text("{}")
        log = logging.getLogger("test")
        with (
            patch("daemon.byfrost_daemon.Path.cwd", return_value=tmp_path),
            patch("daemon.byfrost_daemon.Path.home", return_value=tmp_path),
        ):
            result = discover_project_path(log)
        assert result is None

    def test_returns_none_when_no_project(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("nothing here\n")
        log = logging.getLogger("test")
        with (
            patch("daemon.byfrost_daemon.Path.cwd", return_value=tmp_path),
            patch("daemon.byfrost_daemon.Path.home", return_value=tmp_path),
        ):
            result = discover_project_path(log)
        assert result is None

    def test_prefers_normal_over_protected_dirs(self, tmp_path: Path) -> None:
        """Projects in non-protected dirs should be found before protected."""
        # Protected dir with project
        docs = tmp_path / "Documents"
        docs.mkdir()
        (docs / "package.json").write_text("{}")

        # Normal dir with project
        code = tmp_path / "code"
        code.mkdir()
        (code / "requirements.txt").write_text("flask\n")

        log = logging.getLogger("test")
        with (
            patch("daemon.byfrost_daemon.Path.cwd", return_value=tmp_path),
            patch("daemon.byfrost_daemon.Path.home", return_value=tmp_path),
        ):
            result = discover_project_path(log)
        assert result == str(code)


# ---------------------------------------------------------------------------
# validate_project_path
# ---------------------------------------------------------------------------


class TestValidateProjectPath:
    """Test project path validation."""

    def test_valid_path(self, tmp_path: Path) -> None:
        config = {"project_path": str(tmp_path)}
        log = logging.getLogger("test")
        validate_project_path(config, log)
        assert config["project_path"] == str(tmp_path)

    def test_nonexistent_path_falls_back(self, tmp_path: Path) -> None:
        proj = tmp_path / "MyProject"
        proj.mkdir()
        (proj / "Package.swift").write_text("// swift\n")
        config = {"project_path": "/nonexistent/path"}
        log = logging.getLogger("test")
        with (
            patch("daemon.byfrost_daemon.Path.cwd", return_value=tmp_path),
            patch("daemon.byfrost_daemon.Path.home", return_value=tmp_path),
        ):
            validate_project_path(config, log)
        assert config["project_path"] == str(proj)

    def test_empty_path_falls_back(self, tmp_path: Path) -> None:
        proj = tmp_path / "MyProject"
        proj.mkdir()
        (proj / "go.mod").write_text("module foo\n")
        config = {"project_path": ""}
        log = logging.getLogger("test")
        with (
            patch("daemon.byfrost_daemon.Path.cwd", return_value=tmp_path),
            patch("daemon.byfrost_daemon.Path.home", return_value=tmp_path),
        ):
            validate_project_path(config, log)
        assert config["project_path"] == str(proj)

    def test_file_path_not_directory(self, tmp_path: Path) -> None:
        f = tmp_path / "somefile.txt"
        f.write_text("not a dir\n")
        config = {"project_path": str(f)}
        log = logging.getLogger("test")
        with (
            patch("daemon.byfrost_daemon.Path.cwd", return_value=tmp_path),
            patch("daemon.byfrost_daemon.Path.home", return_value=tmp_path),
        ):
            validate_project_path(config, log)
        # Falls back to auto-discovery; no project found -> stays as-is
        # (auto-discovery returns None, so path stays unchanged)

    def test_no_discovery_keeps_empty(self, tmp_path: Path) -> None:
        config = {"project_path": ""}
        log = logging.getLogger("test")
        with (
            patch("daemon.byfrost_daemon.Path.cwd", return_value=tmp_path),
            patch("daemon.byfrost_daemon.Path.home", return_value=tmp_path),
        ):
            validate_project_path(config, log)
        # No project found, path stays empty
        assert config["project_path"] == ""
