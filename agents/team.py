"""byfrost team - agent team management.

Add, remove, or show status of agents in an existing team setup.
Only backend and frontend agents can be added/removed. PM, Apple
Engineer, and QA are permanent.

Usage: byfrost team status|add|remove [backend|frontend]
"""

from pathlib import Path

from agents.init import (
    ROLES_DIR,
    AgentConfig,
    TeamConfig,
    _merge_into_existing_claude_md,
    _print_error,
    _print_status,
    _prompt,
    detect_backend_details,
    detect_frontend_details,
    generate_root_claude_md,
    process_template,
    replace_marker_sections,
)

# Agents that cannot be removed
PERMANENT_AGENTS = {"pm", "apple", "qa"}

# Marker sections in PM template
PM_MARKERS = ["team", "communication", "routing", "work-agents"]

# Marker sections in root CLAUDE.md
ROOT_MARKERS = ["team", "communication", "cycle"]


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def team_status(project_dir: Path) -> int:
    """Show current team composition. Returns 0 on success, 1 on failure."""
    config = TeamConfig.load(project_dir)
    if config is None:
        _print_error("No team config found. Run 'byfrost init' first.")
        return 1

    _print_status(f"Team: {config.project_name} ({config.team_size} agents)")
    _print_status(f"Communication: {config.communication_mode}")
    print()

    # Header
    print(f"  {'Agent':<22} {'Machine':<24} {'Covers'}")
    print(f"  {'-' * 22} {'-' * 24} {'-' * 30}")

    # PM (always)
    print(f"  {'PM':<22} {config.controller_hostname:<24} Plans, routes, compounds")

    # Apple (always)
    apple = config.get_agent("apple")
    apple_info = ""
    if apple:
        apple_info = apple.settings.get("APPLE_FRAMEWORKS", "Apple platform work")
    print(f"  {'Apple Engineer':<22} {config.worker_hostname:<24} {apple_info}")

    # QA (always)
    print(f"  {'QA Engineer':<22} {config.controller_hostname:<24} Stream monitoring + review")

    # Backend
    if config.has_agent("backend"):
        backend = config.get_agent("backend")
        fw = backend.settings.get("BACKEND_FRAMEWORK", "APIs, databases") if backend else ""
        print(f"  {'Back End Engineer':<22} {config.controller_hostname:<24} {fw}")
    else:
        print(f"  {'Back End':<22} {'(covered by PM)':<24}")

    # Frontend
    if config.has_agent("frontend"):
        frontend = config.get_agent("frontend")
        fw = frontend.settings.get("FRONTEND_FRAMEWORK", "Web components") if frontend else ""
        print(f"  {'Front End Engineer':<22} {config.controller_hostname:<24} {fw}")
    else:
        print(f"  {'Front End':<22} {'(covered by PM)':<24}")

    print()
    return 0


# ---------------------------------------------------------------------------
# Add
# ---------------------------------------------------------------------------


def _prompt_backend_config(project_dir: Path) -> AgentConfig:
    """Prompt for backend agent configuration."""
    _print_status("Back End Engineer configuration:")
    bd = detect_backend_details(project_dir)
    backend_dir = _prompt("Backend directory", default=bd.get("BACKEND_DIR", "backend"))
    backend_framework = _prompt("Framework", default=bd.get("BACKEND_FRAMEWORK", ""))
    backend_language = _prompt("Language", default=bd.get("BACKEND_LANGUAGE", "Python"))
    backend_port = _prompt("Port", default=bd.get("BACKEND_PORT", "8000"))
    backend_entry = _prompt("Entry point", default="app.main:app")
    backend_test = _prompt("Test command", default="pytest tests/")
    db_type = _prompt("Database type", default="PostgreSQL")
    return AgentConfig(
        role="backend",
        directory=backend_dir,
        settings={
            "BACKEND_DIR": backend_dir,
            "BACKEND_FRAMEWORK": backend_framework,
            "BACKEND_LANGUAGE": backend_language,
            "BACKEND_PORT": backend_port,
            "BACKEND_ENTRY": backend_entry,
            "BACKEND_TEST_CMD": backend_test,
            "DATABASE_TYPE": db_type,
        },
    )


def _prompt_frontend_config(project_dir: Path) -> AgentConfig:
    """Prompt for frontend agent configuration."""
    _print_status("Front End Engineer configuration:")
    fd = detect_frontend_details(project_dir)
    frontend_dir = _prompt("Frontend directory", default=fd.get("FRONTEND_DIR", "web"))
    frontend_framework = _prompt("Framework", default=fd.get("FRONTEND_FRAMEWORK", ""))
    frontend_dev = _prompt("Dev command", default="npm run dev")
    frontend_port = _prompt("Port", default=fd.get("FRONTEND_PORT", "3000"))
    frontend_build = _prompt("Build command", default="npm run build")
    frontend_test = _prompt("Test command", default="npm test")
    return AgentConfig(
        role="frontend",
        directory=frontend_dir,
        settings={
            "FRONTEND_DIR": frontend_dir,
            "FRONTEND_FRAMEWORK": frontend_framework,
            "FRONTEND_DEV_CMD": frontend_dev,
            "FRONTEND_PORT": frontend_port,
            "FRONTEND_BUILD_CMD": frontend_build,
            "FRONTEND_TEST_CMD": frontend_test,
        },
    )


def team_add(project_dir: Path, agent: str) -> int:
    """Add a backend or frontend agent. Returns 0 on success, 1 on failure."""
    if agent not in ("backend", "frontend"):
        _print_error(f"Cannot add '{agent}'. Only 'backend' or 'frontend' can be added.")
        return 1

    config = TeamConfig.load(project_dir)
    if config is None:
        _print_error("No team config found. Run 'byfrost init' first.")
        return 1

    if config.has_agent(agent):
        name = "Back End Engineer" if agent == "backend" else "Front End Engineer"
        _print_error(f"{name} already exists in the team.")
        return 1

    # Prompt for agent config
    print()
    if agent == "backend":
        agent_config = _prompt_backend_config(project_dir)
    else:
        agent_config = _prompt_frontend_config(project_dir)

    # Add to config
    config.agents.append(agent_config)
    config.team_size += 1

    # Create task directory + stub
    task_dir = "tasks/backend" if agent == "backend" else "tasks/web"
    (project_dir / task_dir).mkdir(parents=True, exist_ok=True)
    stub_path = project_dir / task_dir / "current.md"
    if not stub_path.exists():
        stub_path.write_text(
            "# Current Task\n\n_No task assigned. PM will write the next task here._\n"
        )
    _print_status(f"  Created: {task_dir}/current.md")

    # Generate agent CLAUDE.md
    template_name = "backend-engineer.md" if agent == "backend" else "frontend-engineer.md"
    template_path = ROLES_DIR / template_name
    if template_path.exists():
        values = config.get_placeholder_values()
        active_tags = config.get_active_agent_tags()
        content = process_template(template_path.read_text(), values, active_tags)
        agent_dir = agent_config.directory or agent_config.settings.get(
            "BACKEND_DIR" if agent == "backend" else "FRONTEND_DIR", agent,
        )
        out_path = project_dir / agent_dir / "CLAUDE.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content)
        _print_status(f"  Created: {agent_dir}/CLAUDE.md")

    # Partial-regen PM and root
    _partial_regen_pm(project_dir, config)
    _partial_regen_root(project_dir, config)

    # Save config
    config.save(project_dir)

    name = "Back End Engineer" if agent == "backend" else "Front End Engineer"
    print()
    _print_status(f"{name} added to the team. Team size: {config.team_size}")
    return 0


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------


def team_remove(project_dir: Path, agent: str) -> int:
    """Remove a backend or frontend agent. Returns 0 on success, 1 on failure."""
    if agent in PERMANENT_AGENTS:
        names = {"pm": "PM", "apple": "Apple Engineer", "qa": "QA Engineer"}
        _print_error(
            f"Cannot remove {names.get(agent, agent)}. Only backend/frontend can be removed."
        )
        return 1

    if agent not in ("backend", "frontend"):
        _print_error(f"Unknown agent '{agent}'. Only 'backend' or 'frontend' can be removed.")
        return 1

    config = TeamConfig.load(project_dir)
    if config is None:
        _print_error("No team config found. Run 'byfrost init' first.")
        return 1

    agent_config = config.get_agent(agent)
    if agent_config is None:
        name = "Back End Engineer" if agent == "backend" else "Front End Engineer"
        _print_error(f"{name} is not in the team.")
        return 1

    # Delete agent CLAUDE.md
    agent_dir = agent_config.directory or agent_config.settings.get(
        "BACKEND_DIR" if agent == "backend" else "FRONTEND_DIR", agent,
    )
    claude_path = project_dir / agent_dir / "CLAUDE.md"
    if claude_path.exists():
        claude_path.unlink()
        _print_status(f"  Removed: {agent_dir}/CLAUDE.md")

    # Remove from config
    config.agents = [a for a in config.agents if a.role != agent]
    config.team_size -= 1

    # Partial-regen PM and root
    _partial_regen_pm(project_dir, config)
    _partial_regen_root(project_dir, config)

    # Save config
    config.save(project_dir)

    name = "Back End Engineer" if agent == "backend" else "Front End Engineer"
    print()
    _print_status(f"{name} removed. PM now covers {agent} duties. Team size: {config.team_size}")
    return 0


# ---------------------------------------------------------------------------
# Partial regeneration
# ---------------------------------------------------------------------------


def _partial_regen_pm(project_dir: Path, config: TeamConfig) -> None:
    """Regenerate managed sections of PM's CLAUDE.md between markers."""
    pm_template_path = ROLES_DIR / "pm.md"
    pm_claude_path = project_dir / "pm" / "CLAUDE.md"

    if not pm_template_path.exists() or not pm_claude_path.exists():
        return

    values = config.get_placeholder_values()
    active_tags = config.get_active_agent_tags()
    processed = process_template(pm_template_path.read_text(), values, active_tags)

    existing = pm_claude_path.read_text()
    updated = replace_marker_sections(existing, processed, PM_MARKERS)
    pm_claude_path.write_text(updated)
    _print_status("  Updated: pm/CLAUDE.md (managed sections)")


def _partial_regen_root(project_dir: Path, config: TeamConfig) -> None:
    """Regenerate managed sections of root CLAUDE.md between markers."""
    root_path = project_dir / "CLAUDE.md"
    if not root_path.exists():
        return

    new_content = generate_root_claude_md(config)
    existing = root_path.read_text()
    updated = _merge_into_existing_claude_md(existing, new_content)
    root_path.write_text(updated)
    _print_status("  Updated: CLAUDE.md (managed sections)")


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------


def run_team_command(action: str, agent: str | None, project_dir: Path) -> int:
    """Dispatch team command. Returns 0 on success, 1 on failure."""
    try:
        if action == "status":
            return team_status(project_dir)
        if action == "add":
            if not agent:
                _print_error("Usage: byfrost team add <backend|frontend>")
                return 1
            return team_add(project_dir, agent)
        if action == "remove":
            if not agent:
                _print_error("Usage: byfrost team remove <backend|frontend>")
                return 1
            return team_remove(project_dir, agent)
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
