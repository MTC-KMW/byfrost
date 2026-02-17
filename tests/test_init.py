"""Tests for byfrost init agent team setup."""

import json
from pathlib import Path
from unittest.mock import patch

from agents.init import (
    BYFROST_SUBDIR,
    AgentConfig,
    TeamConfig,
    _merge_into_existing_claude_md,
    create_coordination_dirs,
    create_stub_files,
    detect_backend_details,
    detect_frontend_details,
    detect_project_stacks,
    generate_root_claude_md,
    process_conditionals,
    process_template,
    substitute_placeholders,
    write_role_claude_mds,
    write_template_files,
)

# ---------------------------------------------------------------------------
# Template engine
# ---------------------------------------------------------------------------


class TestSubstitutePlaceholders:
    """Placeholder substitution."""

    def test_replaces_known_keys(self) -> None:
        content = "Hello [NAME], welcome to [PROJECT]."
        result = substitute_placeholders(content, {"NAME": "Alice", "PROJECT": "Byfrost"})
        assert result == "Hello Alice, welcome to Byfrost."

    def test_leaves_unknown_keys(self) -> None:
        content = "Hello [NAME], see [UNKNOWN]."
        result = substitute_placeholders(content, {"NAME": "Alice"})
        assert result == "Hello Alice, see [UNKNOWN]."


class TestProcessConditionals:
    """Conditional block processing."""

    def test_if_active_keeps_content(self) -> None:
        content = "before\n[IF:BACKEND]\nbackend content\n[/IF:BACKEND]\nafter"
        result = process_conditionals(content, {"BACKEND"})
        assert "backend content" in result
        assert "[IF:BACKEND]" not in result

    def test_if_inactive_removes_content(self) -> None:
        content = "before\n[IF:BACKEND]\nbackend content\n[/IF:BACKEND]\nafter"
        result = process_conditionals(content, set())
        assert "backend content" not in result
        assert "before" in result
        assert "after" in result

    def test_ifnot_active_removes_content(self) -> None:
        content = "before\n[IFNOT:BACKEND]\nno backend\n[/IFNOT:BACKEND]\nafter"
        result = process_conditionals(content, {"BACKEND"})
        assert "no backend" not in result

    def test_ifnot_inactive_keeps_content(self) -> None:
        content = "before\n[IFNOT:BACKEND]\nno backend\n[/IFNOT:BACKEND]\nafter"
        result = process_conditionals(content, set())
        assert "no backend" in result


class TestProcessTemplate:
    """Full template processing pipeline."""

    def test_full_pipeline(self) -> None:
        content = (
            "# [PROJECT_NAME]\n\n"
            "[IF:BACKEND]\n- Backend: [BACKEND_FRAMEWORK]\n[/IF:BACKEND]\n"
            "[IFNOT:BACKEND]\n- No backend\n[/IFNOT:BACKEND]\n"
        )
        result = process_template(
            content,
            {"PROJECT_NAME": "MyApp", "BACKEND_FRAMEWORK": "FastAPI"},
            {"BACKEND"},
        )
        assert "# MyApp" in result
        assert "Backend: FastAPI" in result
        assert "No backend" not in result

    def test_cleans_double_blank_lines(self) -> None:
        content = "line1\n\n\n\n\nline2"
        result = process_template(content, {}, set())
        assert "\n\n\n" not in result


# ---------------------------------------------------------------------------
# Project detection
# ---------------------------------------------------------------------------


class TestDetectProjectStacks:
    """Stack detection from project files."""

    def test_detects_xcodeproj(self, tmp_path: Path) -> None:
        (tmp_path / "MyApp.xcodeproj").mkdir()
        result = detect_project_stacks(tmp_path)
        assert "apple" in result

    def test_detects_requirements_txt(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("flask\n")
        result = detect_project_stacks(tmp_path)
        assert "backend" in result

    def test_detects_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        result = detect_project_stacks(tmp_path)
        assert "frontend" in result

    def test_detects_go_mod(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/app")
        result = detect_project_stacks(tmp_path)
        assert "backend" in result

    def test_no_indicators(self, tmp_path: Path) -> None:
        result = detect_project_stacks(tmp_path)
        assert result == {}


class TestDetectDetails:
    """Auto-detection of framework details."""

    def test_backend_fastapi(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
        result = detect_backend_details(tmp_path)
        assert result["BACKEND_LANGUAGE"] == "Python"
        assert result["BACKEND_FRAMEWORK"] == "FastAPI"

    def test_backend_go(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/app")
        result = detect_backend_details(tmp_path)
        assert result["BACKEND_LANGUAGE"] == "Go"

    def test_frontend_react(self, tmp_path: Path) -> None:
        pkg = {"dependencies": {"react": "^18.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        result = detect_frontend_details(tmp_path)
        assert result["FRONTEND_FRAMEWORK"] == "React"

    def test_frontend_nextjs(self, tmp_path: Path) -> None:
        pkg = {"dependencies": {"next": "^14.0.0", "react": "^18.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        result = detect_frontend_details(tmp_path)
        assert result["FRONTEND_FRAMEWORK"] == "Next.js"


# ---------------------------------------------------------------------------
# TeamConfig
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
        communication_mode="hybrid",
        created_at="2026-02-16T00:00:00Z",
    )


BF = BYFROST_SUBDIR  # shorthand for assertions


class TestTeamConfig:
    """Config persistence and helpers."""

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        config = _make_config(5, has_backend=True, has_frontend=True)
        config.save(tmp_path)
        loaded = TeamConfig.load(tmp_path)
        assert loaded is not None
        assert loaded.project_name == "TestApp"
        assert loaded.team_size == 5
        assert len(loaded.agents) == 5

    def test_save_writes_to_byfrost_subdir(self, tmp_path: Path) -> None:
        config = _make_config(3)
        config.save(tmp_path)
        assert (tmp_path / BF / ".byfrost-team.json").exists()
        assert not (tmp_path / ".byfrost-team.json").exists()

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        assert TeamConfig.load(tmp_path) is None

    def test_load_corrupt_returns_none(self, tmp_path: Path) -> None:
        bf_dir = tmp_path / BF
        bf_dir.mkdir()
        (bf_dir / ".byfrost-team.json").write_text("not json")
        assert TeamConfig.load(tmp_path) is None

    def test_has_agent(self) -> None:
        config = _make_config(5, has_backend=True, has_frontend=True)
        assert config.has_agent("pm") is True
        assert config.has_agent("apple") is True
        assert config.has_agent("backend") is True
        assert config.has_agent("frontend") is True

    def test_has_agent_missing(self) -> None:
        config = _make_config(3)
        assert config.has_agent("backend") is False
        assert config.has_agent("frontend") is False

    def test_get_placeholder_values(self) -> None:
        config = _make_config(3)
        values = config.get_placeholder_values()
        assert values["PROJECT_NAME"] == "TestApp"
        assert values["APPLE_DIR"] == "apple"
        assert values["XCODE_SCHEME"] == "TestApp"

    def test_get_active_agent_tags(self) -> None:
        config = _make_config(5, has_backend=True, has_frontend=True)
        tags = config.get_active_agent_tags()
        assert tags == {"BACKEND", "FRONTEND"}

    def test_get_active_agent_tags_3_agent(self) -> None:
        config = _make_config(3)
        tags = config.get_active_agent_tags()
        assert tags == set()


# ---------------------------------------------------------------------------
# Directory and file creation
# ---------------------------------------------------------------------------


class TestCreateCoordinationDirs:
    """Coordination directory creation under byfrost/."""

    def test_3_agent_dirs(self, tmp_path: Path) -> None:
        config = _make_config(3)
        dirs = create_coordination_dirs(tmp_path, config)
        assert f"{BF}/shared" in dirs
        assert f"{BF}/compound" in dirs
        assert f"{BF}/tasks/apple" in dirs
        assert f"{BF}/pm" in dirs
        assert f"{BF}/qa" in dirs
        assert f"{BF}/tasks/backend" not in dirs
        assert f"{BF}/tasks/web" not in dirs

    def test_5_agent_dirs(self, tmp_path: Path) -> None:
        config = _make_config(5, has_backend=True, has_frontend=True)
        dirs = create_coordination_dirs(tmp_path, config)
        assert f"{BF}/tasks/backend" in dirs
        assert f"{BF}/tasks/web" in dirs

    def test_dirs_actually_exist(self, tmp_path: Path) -> None:
        config = _make_config(3)
        create_coordination_dirs(tmp_path, config)
        assert (tmp_path / BF / "shared").is_dir()
        assert (tmp_path / BF / "compound").is_dir()
        assert (tmp_path / BF / "tasks" / "apple").is_dir()


class TestWriteTemplateFiles:
    """Template file writing under byfrost/."""

    def test_creates_all_template_files(self, tmp_path: Path) -> None:
        files = write_template_files(tmp_path, {"PROJECT_NAME": "TestApp"})
        assert f"{BF}/shared/api-spec.yaml" in files
        assert f"{BF}/shared/decisions.md" in files
        assert f"{BF}/compound/patterns.md" in files
        assert (tmp_path / BF / "shared" / "api-spec.yaml").exists()

    def test_substitutes_placeholders(self, tmp_path: Path) -> None:
        write_template_files(tmp_path, {"PROJECT_NAME": "TestApp"})
        content = (tmp_path / BF / "shared" / "api-spec.yaml").read_text()
        assert "TestApp" in content


class TestWriteRoleCLAUDEMDs:
    """Role CLAUDE.md generation under byfrost/."""

    def test_3_agent_writes_pm_apple_qa(self, tmp_path: Path) -> None:
        config = _make_config(3)
        values = config.get_placeholder_values()
        tags = config.get_active_agent_tags()
        files = write_role_claude_mds(tmp_path, config, values, tags)
        assert f"{BF}/pm/CLAUDE.md" in files
        assert f"{BF}/apple/CLAUDE.md" in files
        assert f"{BF}/qa/CLAUDE.md" in files

    def test_5_agent_writes_all(self, tmp_path: Path) -> None:
        config = _make_config(5, has_backend=True, has_frontend=True)
        values = config.get_placeholder_values()
        tags = config.get_active_agent_tags()
        files = write_role_claude_mds(tmp_path, config, values, tags)
        assert f"{BF}/backend/CLAUDE.md" in files
        assert f"{BF}/frontend/CLAUDE.md" in files

    def test_placeholder_substitution_in_roles(self, tmp_path: Path) -> None:
        config = _make_config(3)
        values = config.get_placeholder_values()
        tags = config.get_active_agent_tags()
        write_role_claude_mds(tmp_path, config, values, tags)
        content = (tmp_path / BF / "apple" / "CLAUDE.md").read_text()
        assert "TestApp" in content
        assert "[PROJECT_NAME]" not in content

    def test_conditionals_processed_in_pm(self, tmp_path: Path) -> None:
        config = _make_config(5, has_backend=True, has_frontend=True)
        values = config.get_placeholder_values()
        tags = config.get_active_agent_tags()
        write_role_claude_mds(tmp_path, config, values, tags)
        content = (tmp_path / BF / "pm" / "CLAUDE.md").read_text()
        assert "Back End Engineer" in content
        assert "[IF:BACKEND]" not in content
        assert "you handle this directly" not in content

    def test_apple_dir_dot_no_overwrite_root(self, tmp_path: Path) -> None:
        """When APPLE_DIR='.', role goes to byfrost/apple/, not root."""
        config = _make_config(3)
        apple = config.get_agent("apple")
        apple.settings["APPLE_DIR"] = "."
        apple.directory = "."
        values = config.get_placeholder_values()
        tags = config.get_active_agent_tags()
        # Pre-create a root CLAUDE.md
        (tmp_path / "CLAUDE.md").write_text("# My Project\nOriginal content.")
        write_role_claude_mds(tmp_path, config, values, tags)
        # Root CLAUDE.md is untouched
        assert "Original content" in (tmp_path / "CLAUDE.md").read_text()
        # Apple role is in byfrost/apple/
        assert (tmp_path / BF / "apple" / "CLAUDE.md").exists()


class TestCreateStubFiles:
    """Stub file creation under byfrost/."""

    def test_3_agent_stubs(self, tmp_path: Path) -> None:
        config = _make_config(3)
        files = create_stub_files(tmp_path, config)
        assert f"{BF}/tasks/apple/current.md" in files
        assert f"{BF}/pm/status.md" in files
        assert f"{BF}/qa/mac-changes.md" in files
        assert f"{BF}/qa/review-report.md" in files

    def test_5_agent_stubs(self, tmp_path: Path) -> None:
        config = _make_config(5, has_backend=True, has_frontend=True)
        files = create_stub_files(tmp_path, config)
        assert f"{BF}/tasks/backend/current.md" in files
        assert f"{BF}/tasks/web/current.md" in files


# ---------------------------------------------------------------------------
# Root CLAUDE.md
# ---------------------------------------------------------------------------


class TestGenerateRootCLAUDEMD:
    """Root CLAUDE.md generation."""

    def test_3_agent_roster(self) -> None:
        config = _make_config(3)
        content = generate_root_claude_md(config)
        assert "PM (you)" in content
        assert "Apple Engineer" in content
        assert "QA Engineer" in content
        assert "Back End Engineer" not in content

    def test_5_agent_roster(self) -> None:
        config = _make_config(5, has_backend=True, has_frontend=True)
        content = generate_root_claude_md(config)
        assert "Back End Engineer" in content
        assert "Front End Engineer" in content

    def test_has_section_markers(self) -> None:
        config = _make_config(3)
        content = generate_root_claude_md(config)
        assert "<!-- byfrost:team -->" in content
        assert "<!-- /byfrost:team -->" in content
        assert "<!-- byfrost:communication -->" in content
        assert "<!-- /byfrost:communication -->" in content
        assert "<!-- byfrost:cycle -->" in content
        assert "<!-- /byfrost:cycle -->" in content

    def test_directory_structure_has_byfrost_prefix(self) -> None:
        config = _make_config(3)
        content = generate_root_claude_md(config)
        assert "byfrost/" in content


class TestMergeExistingCLAUDE:
    """Merging into existing CLAUDE.md."""

    def test_appends_when_no_markers(self) -> None:
        existing = "# My Project\n\nSome content."
        team = "<!-- byfrost:team -->\n## Team\n<!-- /byfrost:team -->"
        result = _merge_into_existing_claude_md(existing, team)
        assert "My Project" in result
        assert "---" in result
        assert "<!-- byfrost:team -->" in result

    def test_replaces_existing_markers(self) -> None:
        existing = (
            "# My Project\n\n"
            "<!-- byfrost:team -->\nOld team\n<!-- /byfrost:team -->\n"
        )
        team = "<!-- byfrost:team -->\nNew team\n<!-- /byfrost:team -->"
        result = _merge_into_existing_claude_md(existing, team)
        assert "New team" in result
        assert "Old team" not in result


# ---------------------------------------------------------------------------
# Full wizard (integration-style tests with mocked input)
# ---------------------------------------------------------------------------


class TestInitWizard:
    """Full wizard flow with mocked input."""

    def test_3_agent_team(self, tmp_path: Path) -> None:
        """Auto-detect finds no stacks -> 3 agents. User confirms."""
        inputs = iter([
            "y",    # Install default team?
            "y",    # Look good? (auto-detected 3 agents)
        ])

        with patch("builtins.input", lambda _: next(inputs)):
            from agents.init import run_init_wizard
            result = run_init_wizard(tmp_path)

        assert result == 0
        assert (tmp_path / "CLAUDE.md").exists()
        assert (tmp_path / BF / "apple" / "CLAUDE.md").exists()
        assert (tmp_path / BF / "pm" / "CLAUDE.md").exists()
        assert (tmp_path / BF / "qa" / "CLAUDE.md").exists()
        assert (tmp_path / BF / "shared" / "api-spec.yaml").exists()
        assert (tmp_path / BF / "compound" / "patterns.md").exists()
        assert (tmp_path / BF / ".byfrost-team.json").exists()
        # No backend/frontend dirs (no stacks detected)
        assert not (tmp_path / BF / "backend" / "CLAUDE.md").exists()
        assert not (tmp_path / BF / "frontend" / "CLAUDE.md").exists()

    def test_5_agent_team(self, tmp_path: Path) -> None:
        """Auto-detect finds backend + frontend stacks -> 5 agents."""
        # Create indicator files so detection finds both stacks
        (tmp_path / "requirements.txt").write_text("flask\npsycopg2\n")
        web_dir = tmp_path / "web"
        web_dir.mkdir()
        (web_dir / "package.json").write_text(json.dumps({
            "name": "my-app",
            "dependencies": {"react": "^18.0.0"},
            "scripts": {"dev": "vite", "build": "vite build", "test": "vitest"},
        }))
        (tmp_path / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n")

        inputs = iter([
            "y",    # Install default team?
            "y",    # Look good? (auto-detected 5 agents)
        ])

        with patch("builtins.input", lambda _: next(inputs)):
            from agents.init import run_init_wizard
            result = run_init_wizard(tmp_path)

        assert result == 0

        # Verify config
        config = TeamConfig.load(tmp_path)
        assert config is not None
        assert config.team_size == 5
        assert config.has_agent("backend")
        assert config.has_agent("frontend")

        # Backend detection
        be = config.get_agent("backend")
        assert be is not None
        assert be.settings["BACKEND_FRAMEWORK"] == "Flask"
        assert be.settings["BACKEND_LANGUAGE"] == "Python"
        assert be.settings["DATABASE_TYPE"] == "PostgreSQL"

        # Frontend detection
        fe = config.get_agent("frontend")
        assert fe is not None
        assert fe.settings["FRONTEND_FRAMEWORK"] == "React"

    def test_auto_detect_backend_only(self, tmp_path: Path) -> None:
        """Auto-detect finds backend but no frontend -> 4 agents."""
        (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")

        inputs = iter([
            "y",    # Install default team?
            "y",    # Look good?
        ])

        with patch("builtins.input", lambda _: next(inputs)):
            from agents.init import run_init_wizard
            result = run_init_wizard(tmp_path)

        assert result == 0
        config = TeamConfig.load(tmp_path)
        assert config is not None
        assert config.team_size == 4
        assert config.has_agent("backend")
        assert not config.has_agent("frontend")

    def test_cancel_at_confirmation(self, tmp_path: Path) -> None:
        """User says 'n' at confirmation -> no files created."""
        inputs = iter([
            "y",    # Install default team?
            "n",    # Look good? -> No
        ])

        with patch("builtins.input", lambda _: next(inputs)):
            from agents.init import run_init_wizard
            result = run_init_wizard(tmp_path)

        assert result == 0
        assert not (tmp_path / BF / ".byfrost-team.json").exists()

    def test_custom_mode(self, tmp_path: Path) -> None:
        inputs = iter([
            "n",          # Don't install default team
            "3",          # Hybrid mode
            "TestApp",    # Project name
        ])

        with patch("builtins.input", lambda _: next(inputs)):
            from agents.init import run_init_wizard
            result = run_init_wizard(tmp_path)

        assert result == 0
        assert (tmp_path / BF / ".byfrost-team.json").exists()
        config = TeamConfig.load(tmp_path)
        assert config is not None
        assert config.communication_mode == "hybrid"
        assert config.team_size == 0

    def test_reinit_decline(self, tmp_path: Path) -> None:
        # Pre-create config
        config = _make_config(3)
        config.save(tmp_path)

        inputs = iter(["n"])  # Don't reinitialize

        with patch("builtins.input", lambda _: next(inputs)):
            from agents.init import run_init_wizard
            result = run_init_wizard(tmp_path)

        assert result == 0

    def test_keyboard_interrupt(self, tmp_path: Path) -> None:
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            from agents.init import run_init_wizard
            result = run_init_wizard(tmp_path)
        assert result == 1

    def test_root_claude_md_not_overwritten(self, tmp_path: Path) -> None:
        """Root CLAUDE.md preserves original content, adds reference."""
        original = "# My CRM App\n\nThis is my project.\n"
        (tmp_path / "CLAUDE.md").write_text(original)

        inputs = iter(["y", "y"])
        with patch("builtins.input", lambda _: next(inputs)):
            from agents.init import run_init_wizard
            run_init_wizard(tmp_path)

        content = (tmp_path / "CLAUDE.md").read_text()
        assert "My CRM App" in content
        assert "This is my project" in content
        assert "Byfrost Agent Team" in content
        assert f"{BF}/CLAUDE.md" in content
