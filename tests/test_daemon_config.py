"""Tests for daemon config persistence (~/.byfrost/daemon.json)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.config import load_daemon_config, save_daemon_config


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect DAEMON_CONFIG_FILE and BRIDGE_DIR to tmp_path."""
    monkeypatch.setattr("core.config.BRIDGE_DIR", tmp_path)
    monkeypatch.setattr("core.config.DAEMON_CONFIG_FILE", tmp_path / "daemon.json")


class TestLoadDaemonConfig:
    def test_returns_empty_dict_when_missing(self) -> None:
        assert load_daemon_config() == {}

    def test_loads_saved_config(self, tmp_path: Path) -> None:
        cfg = {"project_path": "/Users/me/MyProject"}
        (tmp_path / "daemon.json").write_text(json.dumps(cfg))
        assert load_daemon_config() == cfg


class TestSaveDaemonConfig:
    def test_creates_file(self, tmp_path: Path) -> None:
        save_daemon_config({"project_path": "/foo/bar"})
        assert (tmp_path / "daemon.json").exists()
        data = json.loads((tmp_path / "daemon.json").read_text())
        assert data["project_path"] == "/foo/bar"

    def test_round_trip(self) -> None:
        cfg = {"project_path": "/Users/me/Project", "extra": "value"}
        save_daemon_config(cfg)
        loaded = load_daemon_config()
        assert loaded == cfg

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        save_daemon_config({"project_path": "/old"})
        save_daemon_config({"project_path": "/new"})
        loaded = load_daemon_config()
        assert loaded["project_path"] == "/new"


class TestSetProjectCommand:
    def test_shows_current_path(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from cli.main import _do_set_project

        save_daemon_config({"project_path": "/Users/me/MyProject"})
        code = _do_set_project(None)
        assert code == 0
        out = capsys.readouterr().out
        assert "/Users/me/MyProject" in out

    def test_shows_no_path_set(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cli.main import _do_set_project

        code = _do_set_project(None)
        assert code == 0
        out = capsys.readouterr().out
        assert "No project path set" in out

    def test_saves_valid_path(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from cli.main import _do_set_project

        project = tmp_path / "myproject"
        project.mkdir()
        code = _do_set_project(str(project))
        assert code == 0
        loaded = load_daemon_config()
        assert loaded["project_path"] == str(project)
        out = capsys.readouterr().out
        assert "Project path set" in out

    def test_rejects_nonexistent_path(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cli.main import _do_set_project

        code = _do_set_project("/nonexistent/path")
        assert code == 1
        err = capsys.readouterr().err
        assert "Not a directory" in err


class TestDaemonLoadConfigPriority:
    def test_daemon_json_used_when_env_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """daemon.json project_path is used when MAC_PROJECT_PATH is not set."""
        save_daemon_config({"project_path": "/from/daemon/json"})

        # Clear env var
        monkeypatch.delenv("MAC_PROJECT_PATH", raising=False)
        monkeypatch.delenv("BRIDGE_SECRET", raising=False)
        monkeypatch.delenv("BRIDGE_PORT", raising=False)

        # Patch config.env lookup to find nothing
        monkeypatch.chdir(tmp_path)

        # Patch SecretManager to avoid side effects
        with patch("daemon.byfrost_daemon.SecretManager") as mock_sm:
            mock_sm.load.return_value = "testsecret"
            from daemon.byfrost_daemon import load_config
            config = load_config()

        assert config["project_path"] == "/from/daemon/json"

    def test_env_var_takes_priority(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """MAC_PROJECT_PATH env var takes priority over daemon.json."""
        save_daemon_config({"project_path": "/from/daemon/json"})
        monkeypatch.setenv("MAC_PROJECT_PATH", "/from/env")
        monkeypatch.delenv("BRIDGE_SECRET", raising=False)
        monkeypatch.delenv("BRIDGE_PORT", raising=False)
        monkeypatch.chdir(tmp_path)

        with patch("daemon.byfrost_daemon.SecretManager") as mock_sm:
            mock_sm.load.return_value = "testsecret"
            from daemon.byfrost_daemon import load_config
            config = load_config()

        assert config["project_path"] == "/from/env"


class TestAutoDiscoveryPersists:
    def test_discovered_path_saved_to_daemon_json(
        self, tmp_path: Path,
    ) -> None:
        """Auto-discovery saves the found path to daemon.json."""
        import logging

        from daemon.byfrost_daemon import validate_project_path

        # Create a project dir with an indicator
        project = tmp_path / "MyProject"
        project.mkdir()
        (project / "package.json").write_text("{}")

        config: dict = {"project_path": ""}
        log = logging.getLogger("test")

        with patch(
            "daemon.byfrost_daemon.discover_project_path",
            return_value=str(project),
        ):
            validate_project_path(config, log)

        assert config["project_path"] == str(project)
        saved = load_daemon_config()
        assert saved["project_path"] == str(project)
