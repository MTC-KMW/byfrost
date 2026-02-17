"""Tests for agents/team.py - team management."""

from pathlib import Path
from unittest.mock import patch

from agents.init import (
    BYFROST_SUBDIR,
    AgentConfig,
    TeamConfig,
    generate_root_claude_md,
    replace_marker_sections,
    write_role_claude_mds,
)
from agents.team import (
    _partial_regen_pm,
    _partial_regen_root,
    run_team_command,
    team_add,
    team_remove,
    team_status,
)

BF = BYFROST_SUBDIR  # shorthand for assertions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    team_size: int = 3, has_backend: bool = False, has_frontend: bool = False,
) -> TeamConfig:
    """Create a test TeamConfig."""
    agents = [
        AgentConfig(role="pm"),
        AgentConfig(role="apple", directory="apple", settings={
            "APPLE_DIR": "apple", "XCODE_SCHEME": "TestApp",
            "APPLE_FRAMEWORKS": "SwiftUI", "MIN_DEPLOY_TARGET": "iOS 17.0",
        }),
        AgentConfig(role="qa"),
    ]
    if has_backend:
        agents.append(AgentConfig(role="backend", directory="backend", settings={
            "BACKEND_DIR": "backend", "BACKEND_FRAMEWORK": "FastAPI",
            "BACKEND_LANGUAGE": "Python", "BACKEND_PORT": "8000",
            "BACKEND_ENTRY": "app.main:app", "BACKEND_TEST_CMD": "pytest tests/",
            "DATABASE_TYPE": "PostgreSQL",
        }))
    if has_frontend:
        agents.append(AgentConfig(role="frontend", directory="web", settings={
            "FRONTEND_DIR": "web", "FRONTEND_FRAMEWORK": "React",
            "FRONTEND_DEV_CMD": "npm run dev", "FRONTEND_PORT": "3000",
            "FRONTEND_BUILD_CMD": "npm run build", "FRONTEND_TEST_CMD": "npm test",
        }))
    return TeamConfig(
        project_name="TestApp",
        controller_hostname="controller",
        worker_hostname="mac-mini",
        team_size=team_size,
        agents=agents,
        created_at="2026-02-16T00:00:00Z",
    )


def _setup_project(tmp_path: Path, config: TeamConfig) -> None:
    """Save config and generate PM + root CLAUDE.md for a project."""
    config.save(tmp_path)
    values = config.get_placeholder_values()
    active_tags = config.get_active_agent_tags()

    # Generate PM CLAUDE.md from template
    write_role_claude_mds(tmp_path, config, values, active_tags)

    # Generate root CLAUDE.md (under byfrost/)
    root_content = generate_root_claude_md(config)
    bf_dir = tmp_path / BF
    bf_dir.mkdir(parents=True, exist_ok=True)
    (bf_dir / "CLAUDE.md").write_text(root_content)


# ---------------------------------------------------------------------------
# team_status
# ---------------------------------------------------------------------------


class TestTeamStatus:
    """Team status display."""

    def test_3_agent_status(self, tmp_path: Path, capsys: object) -> None:
        config = _make_config(3)
        config.save(tmp_path)
        result = team_status(tmp_path)
        assert result == 0

    def test_5_agent_status(self, tmp_path: Path) -> None:
        config = _make_config(5, has_backend=True, has_frontend=True)
        config.save(tmp_path)
        result = team_status(tmp_path)
        assert result == 0

    def test_no_config_returns_1(self, tmp_path: Path) -> None:
        result = team_status(tmp_path)
        assert result == 1


# ---------------------------------------------------------------------------
# team_add
# ---------------------------------------------------------------------------


class TestTeamAdd:
    """Adding agents to the team."""

    @patch("agents.team._prompt", side_effect=[
        "backend", "FastAPI", "Python", "8000", "app.main:app", "pytest tests/", "PostgreSQL",
    ])
    def test_add_backend(self, _mock_prompt: object, tmp_path: Path) -> None:
        config = _make_config(3)
        _setup_project(tmp_path, config)

        result = team_add(tmp_path, "backend")
        assert result == 0

        # Config updated
        loaded = TeamConfig.load(tmp_path)
        assert loaded is not None
        assert loaded.team_size == 4
        assert loaded.has_agent("backend") is True

        # Agent CLAUDE.md created under byfrost/
        assert (tmp_path / BF / "backend" / "CLAUDE.md").exists()

        # Task stub created under byfrost/
        assert (tmp_path / BF / "tasks" / "backend" / "current.md").exists()

    @patch("agents.team._prompt", side_effect=[
        "web", "React", "npm run dev", "3000", "npm run build", "npm test",
    ])
    def test_add_frontend(self, _mock_prompt: object, tmp_path: Path) -> None:
        config = _make_config(3)
        _setup_project(tmp_path, config)

        result = team_add(tmp_path, "frontend")
        assert result == 0

        loaded = TeamConfig.load(tmp_path)
        assert loaded is not None
        assert loaded.team_size == 4
        assert loaded.has_agent("frontend") is True
        assert (tmp_path / BF / "frontend" / "CLAUDE.md").exists()
        assert (tmp_path / BF / "tasks" / "web" / "current.md").exists()

    def test_add_duplicate_backend(self, tmp_path: Path) -> None:
        config = _make_config(4, has_backend=True)
        config.save(tmp_path)

        result = team_add(tmp_path, "backend")
        assert result == 1

    def test_add_invalid_agent(self, tmp_path: Path) -> None:
        config = _make_config(3)
        config.save(tmp_path)

        result = team_add(tmp_path, "qa")
        assert result == 1

    def test_add_no_config(self, tmp_path: Path) -> None:
        result = team_add(tmp_path, "backend")
        assert result == 1


# ---------------------------------------------------------------------------
# team_remove
# ---------------------------------------------------------------------------


class TestTeamRemove:
    """Removing agents from the team."""

    def test_remove_backend(self, tmp_path: Path) -> None:
        config = _make_config(4, has_backend=True)
        _setup_project(tmp_path, config)

        result = team_remove(tmp_path, "backend")
        assert result == 0

        loaded = TeamConfig.load(tmp_path)
        assert loaded is not None
        assert loaded.team_size == 3
        assert loaded.has_agent("backend") is False

        # CLAUDE.md deleted from byfrost/
        assert not (tmp_path / BF / "backend" / "CLAUDE.md").exists()

    def test_remove_frontend(self, tmp_path: Path) -> None:
        config = _make_config(4, has_frontend=True)
        _setup_project(tmp_path, config)

        result = team_remove(tmp_path, "frontend")
        assert result == 0

        loaded = TeamConfig.load(tmp_path)
        assert loaded is not None
        assert loaded.team_size == 3
        assert loaded.has_agent("frontend") is False

    def test_refuse_remove_pm(self, tmp_path: Path) -> None:
        config = _make_config(3)
        config.save(tmp_path)

        result = team_remove(tmp_path, "pm")
        assert result == 1

    def test_refuse_remove_apple(self, tmp_path: Path) -> None:
        config = _make_config(3)
        config.save(tmp_path)

        result = team_remove(tmp_path, "apple")
        assert result == 1

    def test_refuse_remove_qa(self, tmp_path: Path) -> None:
        config = _make_config(3)
        config.save(tmp_path)

        result = team_remove(tmp_path, "qa")
        assert result == 1

    def test_remove_nonexistent(self, tmp_path: Path) -> None:
        config = _make_config(3)
        config.save(tmp_path)

        result = team_remove(tmp_path, "backend")
        assert result == 1

    def test_remove_no_config(self, tmp_path: Path) -> None:
        result = team_remove(tmp_path, "backend")
        assert result == 1


# ---------------------------------------------------------------------------
# Partial regeneration
# ---------------------------------------------------------------------------


class TestPartialRegenPM:
    """PM CLAUDE.md partial regeneration."""

    def test_markers_updated_on_add(self, tmp_path: Path) -> None:
        """Adding backend should update PM's team section to include it."""
        config = _make_config(3)
        _setup_project(tmp_path, config)

        pm_path = tmp_path / BF / "pm" / "CLAUDE.md"
        pm_before = pm_path.read_text()
        assert "Back End Engineer" not in pm_before or "you handle this directly" in pm_before

        # Simulate adding backend
        config.agents.append(AgentConfig(role="backend", directory="backend", settings={
            "BACKEND_DIR": "backend", "BACKEND_FRAMEWORK": "FastAPI",
            "BACKEND_LANGUAGE": "Python", "BACKEND_PORT": "8000",
            "BACKEND_ENTRY": "app.main:app", "BACKEND_TEST_CMD": "pytest tests/",
            "DATABASE_TYPE": "PostgreSQL",
        }))
        config.team_size = 4

        _partial_regen_pm(tmp_path, config)

        pm_after = pm_path.read_text()
        # Team section should now list backend
        assert "**Back End Engineer**: controller" in pm_after
        # IFNOT:BACKEND block should be gone (PM no longer handles it directly)
        assert "you handle this directly" not in pm_after or "Front End" in pm_after

    def test_non_marker_content_preserved(self, tmp_path: Path) -> None:
        """User content outside markers should not change."""
        config = _make_config(3)
        _setup_project(tmp_path, config)

        # Add custom content outside markers
        pm_path = tmp_path / BF / "pm" / "CLAUDE.md"
        content = pm_path.read_text()
        content += "\n## My Custom Rules\n\nDo not delete this.\n"
        pm_path.write_text(content)

        # Regen
        config.agents.append(AgentConfig(role="backend", directory="backend", settings={
            "BACKEND_DIR": "backend", "BACKEND_FRAMEWORK": "FastAPI",
            "BACKEND_LANGUAGE": "Python", "BACKEND_PORT": "8000",
            "BACKEND_ENTRY": "app.main:app", "BACKEND_TEST_CMD": "pytest tests/",
            "DATABASE_TYPE": "PostgreSQL",
        }))
        config.team_size = 4
        _partial_regen_pm(tmp_path, config)

        pm_after = pm_path.read_text()
        assert "My Custom Rules" in pm_after
        assert "Do not delete this." in pm_after


class TestPartialRegenRoot:
    """Root CLAUDE.md partial regeneration."""

    def test_markers_updated_on_add(self, tmp_path: Path) -> None:
        config = _make_config(3)
        _setup_project(tmp_path, config)

        root_path = tmp_path / BF / "CLAUDE.md"
        root_before = root_path.read_text()
        assert "Back End Engineer" not in root_before

        config.agents.append(AgentConfig(role="backend", directory="backend", settings={
            "BACKEND_DIR": "backend", "BACKEND_FRAMEWORK": "FastAPI",
            "BACKEND_LANGUAGE": "Python", "BACKEND_PORT": "8000",
            "BACKEND_ENTRY": "app.main:app", "BACKEND_TEST_CMD": "pytest tests/",
            "DATABASE_TYPE": "PostgreSQL",
        }))
        config.team_size = 4
        _partial_regen_root(tmp_path, config)

        root_after = root_path.read_text()
        assert "Back End Engineer" in root_after

    def test_non_marker_content_preserved(self, tmp_path: Path) -> None:
        config = _make_config(3)
        _setup_project(tmp_path, config)

        root_path = tmp_path / BF / "CLAUDE.md"
        content = root_path.read_text()
        content += "\n## Project Notes\n\nKeep this.\n"
        root_path.write_text(content)

        config.agents.append(AgentConfig(role="backend", directory="backend", settings={
            "BACKEND_DIR": "backend", "BACKEND_FRAMEWORK": "FastAPI",
            "BACKEND_LANGUAGE": "Python", "BACKEND_PORT": "8000",
            "BACKEND_ENTRY": "app.main:app", "BACKEND_TEST_CMD": "pytest tests/",
            "DATABASE_TYPE": "PostgreSQL",
        }))
        config.team_size = 4
        _partial_regen_root(tmp_path, config)

        root_after = root_path.read_text()
        assert "Project Notes" in root_after
        assert "Keep this." in root_after
        assert "Back End Engineer" in root_after


# ---------------------------------------------------------------------------
# replace_marker_sections (unit test)
# ---------------------------------------------------------------------------


class TestReplaceMarkerSections:
    """Low-level marker replacement."""

    def test_replaces_existing_marker(self) -> None:
        existing = (
            "before\n"
            "<!-- byfrost:team -->\nold team\n<!-- /byfrost:team -->\n"
            "after"
        )
        new_content = "<!-- byfrost:team -->\nnew team\n<!-- /byfrost:team -->"
        result = replace_marker_sections(existing, new_content, ["team"])
        assert "new team" in result
        assert "old team" not in result
        assert "before" in result
        assert "after" in result

    def test_ignores_missing_marker_in_new(self) -> None:
        existing = "<!-- byfrost:team -->\nold\n<!-- /byfrost:team -->"
        result = replace_marker_sections(existing, "no markers", ["team"])
        assert "old" in result

    def test_ignores_missing_marker_in_existing(self) -> None:
        new_content = "<!-- byfrost:team -->\nnew\n<!-- /byfrost:team -->"
        result = replace_marker_sections("no markers", new_content, ["team"])
        assert "no markers" in result


# ---------------------------------------------------------------------------
# run_team_command
# ---------------------------------------------------------------------------


class TestRunTeamCommand:
    """Command dispatch."""

    def test_status_dispatch(self, tmp_path: Path) -> None:
        config = _make_config(3)
        config.save(tmp_path)
        result = run_team_command("status", None, tmp_path)
        assert result == 0

    def test_add_missing_agent_arg(self, tmp_path: Path) -> None:
        result = run_team_command("add", None, tmp_path)
        assert result == 1

    def test_remove_missing_agent_arg(self, tmp_path: Path) -> None:
        result = run_team_command("remove", None, tmp_path)
        assert result == 1

    def test_unknown_action(self, tmp_path: Path) -> None:
        result = run_team_command("unknown", None, tmp_path)
        assert result == 1

    def test_keyboard_interrupt(self, tmp_path: Path) -> None:
        with patch("agents.team.team_status", side_effect=KeyboardInterrupt):
            result = run_team_command("status", None, tmp_path)
            assert result == 130
