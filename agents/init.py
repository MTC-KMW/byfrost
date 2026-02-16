"""byfrost init - agent team setup wizard.

Interactive setup that creates CLAUDE.md files, shared infrastructure,
and coordination directories in the user's project. Reads templates from
the byfrost package's agents/ directory.

Usage: byfrost init  (run in project root)
"""

import json
import platform
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEAM_CONFIG_FILE = ".byfrost-team.json"

ROLES_DIR = Path(__file__).parent / "roles"
TEMPLATES_DIR = Path(__file__).parent / "templates"

# Template file -> project output path
TEMPLATE_FILE_MAP = {
    "api-spec.yaml": "shared/api-spec.yaml",
    "decisions.md": "shared/decisions.md",
    "patterns.md": "compound/patterns.md",
    "anti-patterns.md": "compound/anti-patterns.md",
    "learnings.md": "compound/learnings.md",
    "review-checklist.md": "compound/review-checklist.md",
}

# Stack indicator files (glob patterns)
PROJECT_INDICATORS: dict[str, list[str]] = {
    "apple": ["*.xcodeproj", "*.xcworkspace", "Package.swift"],
    "backend": ["requirements.txt", "pyproject.toml", "go.mod", "Cargo.toml",
                "pom.xml", "build.gradle", "Gemfile"],
    "frontend": ["package.json"],
}


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _print_status(msg: str) -> None:
    print(f"\033[36m[byfrost]\033[0m {msg}")


def _print_error(msg: str) -> None:
    print(f"\033[31m[byfrost error]\033[0m {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    """Configuration for a single agent."""

    role: str  # "pm", "apple", "qa", "backend", "frontend"
    enabled: bool = True
    directory: str = ""
    settings: dict[str, str] = field(default_factory=dict)


@dataclass
class TeamConfig:
    """Full team configuration persisted to .byfrost-team.json."""

    project_name: str
    controller_hostname: str
    worker_hostname: str
    team_size: int
    agents: list[AgentConfig] = field(default_factory=list)
    communication_mode: str = "hybrid"
    created_at: str = ""

    def save(self, project_dir: Path) -> None:
        """Write config to .byfrost-team.json in project root."""
        path = project_dir / TEAM_CONFIG_FILE
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")

    @classmethod
    def load(cls, project_dir: Path) -> "TeamConfig | None":
        """Load config from .byfrost-team.json. Returns None if missing."""
        path = project_dir / TEAM_CONFIG_FILE
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            agents = [AgentConfig(**a) for a in data.pop("agents", [])]
            return cls(**data, agents=agents)
        except (json.JSONDecodeError, TypeError, KeyError):
            return None

    def has_agent(self, role: str) -> bool:
        """Check if an agent role is enabled."""
        return any(a.role == role and a.enabled for a in self.agents)

    def get_agent(self, role: str) -> AgentConfig | None:
        """Get agent config by role."""
        for a in self.agents:
            if a.role == role:
                return a
        return None

    def get_placeholder_values(self) -> dict[str, str]:
        """Build full placeholder dict for template processing."""
        values: dict[str, str] = {
            "PROJECT_NAME": self.project_name,
            "CONTROLLER_HOSTNAME": self.controller_hostname,
            "WORKER_HOSTNAME": self.worker_hostname,
        }
        for agent in self.agents:
            if agent.enabled:
                values.update(agent.settings)
        return values

    def get_active_agent_tags(self) -> set[str]:
        """Return active agent tags for conditional processing."""
        tags: set[str] = set()
        for agent in self.agents:
            if agent.enabled and agent.role in ("backend", "frontend"):
                tags.add(agent.role.upper())
        return tags


# ---------------------------------------------------------------------------
# Template engine
# ---------------------------------------------------------------------------


def process_conditionals(content: str, active_agents: set[str]) -> str:
    """Process [IF:X]...[/IF:X] and [IFNOT:X]...[/IFNOT:X] blocks."""

    def replace_if(match: re.Match[str]) -> str:
        tag = match.group(1)
        body = match.group(2)
        return body.strip("\n") if tag in active_agents else ""

    content = re.sub(
        r"\[IF:(\w+)\]\n?(.*?)\[/IF:\1\]",
        replace_if,
        content,
        flags=re.DOTALL,
    )

    def replace_ifnot(match: re.Match[str]) -> str:
        tag = match.group(1)
        body = match.group(2)
        return body.strip("\n") if tag not in active_agents else ""

    content = re.sub(
        r"\[IFNOT:(\w+)\]\n?(.*?)\[/IFNOT:\1\]",
        replace_ifnot,
        content,
        flags=re.DOTALL,
    )

    return content


def substitute_placeholders(content: str, values: dict[str, str]) -> str:
    """Replace [KEY] placeholders with values. Unknown keys left as-is."""
    for key, value in values.items():
        content = content.replace(f"[{key}]", value)
    return content


def process_template(
    content: str, values: dict[str, str], active_agents: set[str],
) -> str:
    """Full template processing: conditionals first, then placeholders."""
    content = process_conditionals(content, active_agents)
    content = substitute_placeholders(content, values)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content


# ---------------------------------------------------------------------------
# Project detection
# ---------------------------------------------------------------------------


def detect_project_stacks(project_dir: Path) -> dict[str, list[str]]:
    """Scan project directory for stack indicators."""
    found: dict[str, list[str]] = {}
    for stack, indicators in PROJECT_INDICATORS.items():
        matches = []
        for indicator in indicators:
            # Use glob for patterns with wildcards, direct check otherwise
            if "*" in indicator:
                results = list(project_dir.glob(indicator))
                if results:
                    matches.append(results[0].name)
            elif (project_dir / indicator).exists():
                matches.append(indicator)
        if matches:
            found[stack] = matches
    return found


def detect_apple_details(project_dir: Path) -> dict[str, str]:
    """Auto-detect Apple project details."""
    details: dict[str, str] = {}

    xcodeprojs = list(project_dir.glob("**/*.xcodeproj"))
    if xcodeprojs:
        details["XCODE_SCHEME"] = xcodeprojs[0].stem
        parent = xcodeprojs[0].parent
        rel = parent.relative_to(project_dir)
        details["APPLE_DIR"] = str(rel) if str(rel) != "." else "apple"

    if (project_dir / "Package.swift").exists():
        details.setdefault("APPLE_DIR", ".")

    details.setdefault("APPLE_FRAMEWORKS", "SwiftUI")
    details.setdefault("MIN_DEPLOY_TARGET", "iOS 17.0 / macOS 14.0")
    return details


def detect_backend_details(project_dir: Path) -> dict[str, str]:
    """Auto-detect backend project details."""
    details: dict[str, str] = {}

    for req_file in ["requirements.txt", "pyproject.toml"]:
        path = project_dir / req_file
        if path.exists():
            details["BACKEND_LANGUAGE"] = "Python"
            content = path.read_text().lower()
            if "fastapi" in content:
                details["BACKEND_FRAMEWORK"] = "FastAPI"
            elif "flask" in content:
                details["BACKEND_FRAMEWORK"] = "Flask"
            elif "django" in content:
                details["BACKEND_FRAMEWORK"] = "Django"
            break

    if (project_dir / "go.mod").exists():
        details.setdefault("BACKEND_LANGUAGE", "Go")
    if (project_dir / "Cargo.toml").exists():
        details.setdefault("BACKEND_LANGUAGE", "Rust")

    details.setdefault("BACKEND_DIR", "backend")
    details.setdefault("BACKEND_PORT", "8000")
    return details


def detect_frontend_details(project_dir: Path) -> dict[str, str]:
    """Auto-detect frontend project details."""
    details: dict[str, str] = {}

    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            deps: dict[str, Any] = {
                **pkg.get("dependencies", {}),
                **pkg.get("devDependencies", {}),
            }
            if "react" in deps or "next" in deps:
                details["FRONTEND_FRAMEWORK"] = "Next.js" if "next" in deps else "React"
            elif "vue" in deps:
                details["FRONTEND_FRAMEWORK"] = "Vue"
            elif "svelte" in deps:
                details["FRONTEND_FRAMEWORK"] = "Svelte"
        except (json.JSONDecodeError, OSError):
            pass

    details.setdefault("FRONTEND_DIR", "web")
    details.setdefault("FRONTEND_PORT", "3000")
    return details


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------


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


def _prompt_choice(question: str, choices: list[str], default: int = 0) -> int:
    """Numbered choice prompt. Returns index."""
    print(f"  {question}")
    for i, choice in enumerate(choices):
        marker = " *" if i == default else ""
        print(f"    ({i + 1}) {choice}{marker}")
    answer = input(f"  Choice [{default + 1}]: ").strip()
    if not answer:
        return default
    try:
        idx = int(answer) - 1
        if 0 <= idx < len(choices):
            return idx
    except ValueError:
        pass
    return default


# ---------------------------------------------------------------------------
# File generation
# ---------------------------------------------------------------------------


def create_coordination_dirs(project_dir: Path, config: TeamConfig) -> list[str]:
    """Create coordination directories. Returns list of created dir paths."""
    dirs = ["shared", "compound", "tasks/apple", "pm", "qa"]
    if config.has_agent("backend"):
        dirs.append("tasks/backend")
    if config.has_agent("frontend"):
        dirs.append("tasks/web")

    created = []
    for d in dirs:
        path = project_dir / d
        path.mkdir(parents=True, exist_ok=True)
        created.append(d)
    return created


def write_template_files(project_dir: Path, values: dict[str, str]) -> list[str]:
    """Copy and process template files to project. Returns list of created paths."""
    created = []
    for template_name, output_path in TEMPLATE_FILE_MAP.items():
        template_path = TEMPLATES_DIR / template_name
        if not template_path.exists():
            continue

        content = template_path.read_text()
        content = substitute_placeholders(content, values)

        out = project_dir / output_path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)
        created.append(output_path)
    return created


def write_role_claude_mds(
    project_dir: Path,
    config: TeamConfig,
    values: dict[str, str],
    active_tags: set[str],
) -> list[str]:
    """Generate and write role-specific CLAUDE.md files. Returns created paths."""
    created = []

    # PM -> pm/CLAUDE.md
    pm_template = ROLES_DIR / "pm.md"
    if pm_template.exists():
        content = process_template(pm_template.read_text(), values, active_tags)
        out_path = "pm/CLAUDE.md"
        (project_dir / "pm").mkdir(parents=True, exist_ok=True)
        (project_dir / out_path).write_text(content)
        created.append(out_path)

    # Apple Engineer -> {apple_dir}/CLAUDE.md
    apple = config.get_agent("apple")
    if apple and apple.enabled:
        template = ROLES_DIR / "apple-engineer.md"
        if template.exists():
            content = process_template(template.read_text(), values, active_tags)
            apple_dir = apple.settings.get("APPLE_DIR", "apple")
            out_path = f"{apple_dir}/CLAUDE.md"
            (project_dir / apple_dir).mkdir(parents=True, exist_ok=True)
            (project_dir / out_path).write_text(content)
            created.append(out_path)

    # QA -> qa/CLAUDE.md
    qa_template = ROLES_DIR / "qa-engineer.md"
    if qa_template.exists():
        content = process_template(qa_template.read_text(), values, active_tags)
        out_path = "qa/CLAUDE.md"
        (project_dir / "qa").mkdir(parents=True, exist_ok=True)
        (project_dir / out_path).write_text(content)
        created.append(out_path)

    # Backend -> {backend_dir}/CLAUDE.md
    backend = config.get_agent("backend")
    if backend and backend.enabled:
        template = ROLES_DIR / "backend-engineer.md"
        if template.exists():
            content = process_template(template.read_text(), values, active_tags)
            backend_dir = backend.settings.get("BACKEND_DIR", "backend")
            out_path = f"{backend_dir}/CLAUDE.md"
            (project_dir / backend_dir).mkdir(parents=True, exist_ok=True)
            (project_dir / out_path).write_text(content)
            created.append(out_path)

    # Frontend -> {frontend_dir}/CLAUDE.md
    frontend = config.get_agent("frontend")
    if frontend and frontend.enabled:
        template = ROLES_DIR / "frontend-engineer.md"
        if template.exists():
            content = process_template(template.read_text(), values, active_tags)
            frontend_dir = frontend.settings.get("FRONTEND_DIR", "web")
            out_path = f"{frontend_dir}/CLAUDE.md"
            (project_dir / frontend_dir).mkdir(parents=True, exist_ok=True)
            (project_dir / out_path).write_text(content)
            created.append(out_path)

    return created


def create_stub_files(project_dir: Path, config: TeamConfig) -> list[str]:
    """Create initial task spec and coordination stub files."""
    created = []
    task_stub = "# Current Task\n\n_No task assigned. PM will write the next task here._\n"

    paths = ["tasks/apple/current.md"]
    if config.has_agent("backend"):
        paths.append("tasks/backend/current.md")
    if config.has_agent("frontend"):
        paths.append("tasks/web/current.md")

    for p in paths:
        out = project_dir / p
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(task_stub)
        created.append(p)

    # PM status
    (project_dir / "pm/status.md").write_text(
        "# PM Status\n\n_Cycle tracking. Updated by PM after each phase._\n"
    )
    created.append("pm/status.md")

    # QA working files
    qa_files = {
        "qa/mac-changes.md": (
            "# Change Inventory\n\n"
            "_QA builds this during the Work phase from Apple Engineer stream._\n"
        ),
        "qa/review-report.md": (
            "# Review Report\n\n"
            "_QA writes this after the 8-lens review._\n"
        ),
    }
    for path, content in qa_files.items():
        (project_dir / path).write_text(content)
        created.append(path)

    return created


def generate_root_claude_md(config: TeamConfig) -> str:
    """Generate root CLAUDE.md with section markers for managed blocks."""
    lines = [f"# {config.project_name} - Agent Team\n"]

    # Team roster
    lines.append("<!-- byfrost:team -->")
    lines.append("## Team\n")
    lines.append("| Agent | Machine | Role |")
    lines.append("|-------|---------|------|")
    lines.append(f"| PM (you) | {config.controller_hostname} | Plans, routes, compounds |")
    lines.append(
        f"| Apple Engineer | {config.worker_hostname} | Apple platform work |"
    )
    lines.append(
        f"| QA Engineer | {config.controller_hostname} | Stream monitoring + 8-lens review |"
    )
    if config.has_agent("backend"):
        lines.append(
            f"| Back End Engineer | {config.controller_hostname} | APIs, databases, auth |"
        )
    if config.has_agent("frontend"):
        lines.append(
            f"| Front End Engineer | {config.controller_hostname} | Web components, state |"
        )
    lines.append("<!-- /byfrost:team -->\n")

    # Communication
    lines.append("<!-- byfrost:communication -->")
    lines.append("## Communication\n")
    lines.append("- **User to PM**: Claude Code conversation (direct)")
    lines.append(
        "- **PM to Apple Engineer**: task spec via `tasks/apple/current.md` (SSHFS) "
        "+ bridge trigger (`byfrost send`)"
    )
    lines.append(
        "- **Apple Engineer to PM**: streamed terminal output + `task.complete` over bridge"
    )
    lines.append(
        "- **QA**: monitors Apple stream, writes `qa/mac-changes.md` and `qa/review-report.md`"
    )
    if config.has_agent("backend") or config.has_agent("frontend"):
        lines.append(
            "- **PM to Backend/Frontend**: Claude Agent Teams messaging (controller, local)"
        )
    lines.append("<!-- /byfrost:communication -->\n")

    # Cycle
    lines.append("<!-- byfrost:cycle -->")
    lines.append("## Compound Engineering Cycle\n")
    lines.append("1. **Plan** - PM reads compound knowledge, writes task specs, dispatches")
    lines.append("2. **Work** - All agents implement. QA monitors Apple stream.")
    lines.append("3. **Review** - QA runs 8-lens review across all stacks.")
    lines.append("4. **Compound** - PM extracts learnings, promotes patterns.")
    lines.append("<!-- /byfrost:cycle -->\n")

    # Directory structure
    lines.append("## Directory Structure\n")
    lines.append("```")
    lines.append("shared/              Contracts shared across all stacks")
    lines.append("  api-spec.yaml      API contract (source of truth)")
    lines.append("  decisions.md       Cross-agent decision log")
    lines.append("compound/            Accumulated knowledge")
    lines.append("  patterns.md        Proven patterns (P-XXX)")
    lines.append("  anti-patterns.md   Known mistakes (A-XXX)")
    lines.append("  learnings.md       Raw observations (PM staging)")
    lines.append("  review-checklist.md Standard review checks")
    lines.append("tasks/               Task specs per agent")
    lines.append("  apple/current.md   Apple Engineer's current task")
    if config.has_agent("backend"):
        lines.append("  backend/current.md Back End task")
    if config.has_agent("frontend"):
        lines.append("  web/current.md     Front End task")
    lines.append("pm/                  PM coordination")
    lines.append("  status.md          Cycle tracking")
    lines.append("qa/                  QA working files")
    lines.append("  mac-changes.md     Change inventory from stream")
    lines.append("  review-report.md   8-lens review output")
    lines.append("```\n")

    return "\n".join(lines) + "\n"


def replace_marker_sections(
    existing: str, new_content: str, markers: list[str],
) -> str:
    """Replace content between byfrost markers in existing text.

    For each marker name, finds <!-- byfrost:NAME -->...<!-- /byfrost:NAME -->
    in both texts and replaces the section in existing with the one from new_content.
    """
    for marker in markers:
        pattern = rf"<!-- byfrost:{marker} -->.*?<!-- /byfrost:{marker} -->"
        match_new = re.search(pattern, new_content, re.DOTALL)
        if match_new:
            if re.search(pattern, existing, re.DOTALL):
                existing = re.sub(pattern, match_new.group(), existing, flags=re.DOTALL)
    return existing


def _merge_into_existing_claude_md(existing: str, team_content: str) -> str:
    """Merge team content into an existing CLAUDE.md.

    If existing has byfrost markers, replace those sections.
    Otherwise append with a separator.
    """
    if "<!-- byfrost:" in existing:
        result = replace_marker_sections(
            existing, team_content, ["team", "communication", "cycle"],
        )
        # Add any new marker sections not yet in existing
        for marker in ("team", "communication", "cycle"):
            pattern = rf"<!-- byfrost:{marker} -->.*?<!-- /byfrost:{marker} -->"
            match_new = re.search(pattern, team_content, re.DOTALL)
            if match_new and not re.search(pattern, result, re.DOTALL):
                result = result.rstrip() + "\n\n" + match_new.group() + "\n"
        return result
    return existing.rstrip() + "\n\n---\n\n" + team_content


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------


def run_init_wizard(project_dir: Path) -> int:
    """Run the byfrost init wizard. Returns 0 on success, 1 on failure."""
    try:
        return _run_init_impl(project_dir)
    except KeyboardInterrupt:
        print()
        _print_status("Setup cancelled.")
        return 1
    except EOFError:
        _print_error("No terminal input available. Run byfrost init in an interactive terminal.")
        return 1
    except PermissionError as e:
        _print_error(f"Permission denied: {e}")
        return 1


def _run_init_impl(project_dir: Path) -> int:
    """Implementation of the init wizard."""
    _print_status("Byfrost Agent Team Setup")
    print()

    # Check existing config
    existing = TeamConfig.load(project_dir)
    if existing:
        _print_status(f"Team already initialized ({existing.team_size} agents).")
        if not _prompt_yn("Reinitialize? This will overwrite existing team files.", default=False):
            return 0

    # Default team or custom?
    use_default = _prompt_yn("Install default agent team?", default=True)

    if not use_default:
        return _init_custom(project_dir)
    return _init_default_team(project_dir)


def _init_custom(project_dir: Path) -> int:
    """Custom team setup - minimal dirs + communication mode."""
    print()
    mode_idx = _prompt_choice(
        "Communication mode:",
        [
            "Full Git - all files sync via git",
            "Full SSHFS - all files sync via SSHFS mounts",
            "Hybrid - SSHFS for coordination, git for code",
        ],
        default=2,
    )
    mode = ["git", "sshfs", "hybrid"][mode_idx]

    project_name = _prompt("Project name", default=project_dir.name)

    config = TeamConfig(
        project_name=project_name,
        controller_hostname="",
        worker_hostname="",
        team_size=0,
        communication_mode=mode,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    dirs = create_coordination_dirs(project_dir, config)
    config.save(project_dir)

    print()
    _print_status("Minimal structure created:")
    for d in dirs:
        _print_status(f"  {d}/")
    _print_status(f"Config saved to {TEAM_CONFIG_FILE}")
    _print_status("Add your own CLAUDE.md files and agent configurations.")
    return 0


def _init_default_team(project_dir: Path) -> int:
    """Full default team setup wizard."""
    print()

    # Scan for project indicators
    _print_status("Scanning project directory...")
    detected = detect_project_stacks(project_dir)
    if detected:
        _print_status("Detected stacks:")
        for stack, indicators in detected.items():
            _print_status(f"  {stack}: {', '.join(indicators)}")
    else:
        _print_status("No known project indicators found.")
    print()

    # Team size
    size_idx = _prompt_choice(
        "Team size:",
        [
            "3 agents - PM + Apple Engineer + QA",
            "4 agents - add Back End or Front End",
            "5 agents - all five agents",
        ],
        default=0,
    )
    team_size = [3, 4, 5][size_idx]

    has_backend = team_size >= 5
    has_frontend = team_size >= 5

    if team_size == 4:
        print()
        add_idx = _prompt_choice(
            "Add which agent?",
            ["Back End Engineer", "Front End Engineer"],
            default=0 if "backend" in detected else 1,
        )
        has_backend = add_idx == 0
        has_frontend = add_idx == 1

    print()

    # Common details
    project_name = _prompt("Project name", default=project_dir.name)
    controller_hostname = _prompt("Controller hostname", default=platform.node())
    worker_hostname = _prompt("Worker (Mac) hostname", default="")

    # Build agents list
    agents: list[AgentConfig] = []

    # PM (always)
    agents.append(AgentConfig(role="pm"))

    # Apple (always)
    print()
    _print_status("Apple Engineer configuration:")
    apple_details = detect_apple_details(project_dir)
    apple_dir = _prompt("Apple directory", default=apple_details.get("APPLE_DIR", "apple"))
    xcode_scheme = _prompt(
        "Xcode scheme", default=apple_details.get("XCODE_SCHEME", project_name),
    )
    frameworks = _prompt(
        "Frameworks", default=apple_details.get("APPLE_FRAMEWORKS", "SwiftUI"),
    )
    min_deploy = _prompt(
        "Min deployment target",
        default=apple_details.get("MIN_DEPLOY_TARGET", "iOS 17.0"),
    )
    agents.append(AgentConfig(
        role="apple",
        directory=apple_dir,
        settings={
            "APPLE_DIR": apple_dir,
            "XCODE_SCHEME": xcode_scheme,
            "APPLE_FRAMEWORKS": frameworks,
            "MIN_DEPLOY_TARGET": min_deploy,
        },
    ))

    # QA (always)
    agents.append(AgentConfig(role="qa"))

    # Backend (optional)
    if has_backend:
        print()
        _print_status("Back End Engineer configuration:")
        bd = detect_backend_details(project_dir)
        backend_dir = _prompt("Backend directory", default=bd.get("BACKEND_DIR", "backend"))
        backend_framework = _prompt("Framework", default=bd.get("BACKEND_FRAMEWORK", ""))
        backend_language = _prompt("Language", default=bd.get("BACKEND_LANGUAGE", "Python"))
        backend_port = _prompt("Port", default=bd.get("BACKEND_PORT", "8000"))
        backend_entry = _prompt("Entry point", default="app.main:app")
        backend_test = _prompt("Test command", default="pytest tests/")
        db_type = _prompt("Database type", default="PostgreSQL")
        agents.append(AgentConfig(
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
        ))

    # Frontend (optional)
    if has_frontend:
        print()
        _print_status("Front End Engineer configuration:")
        fd = detect_frontend_details(project_dir)
        frontend_dir = _prompt("Frontend directory", default=fd.get("FRONTEND_DIR", "web"))
        frontend_framework = _prompt("Framework", default=fd.get("FRONTEND_FRAMEWORK", ""))
        frontend_dev = _prompt("Dev command", default="npm run dev")
        frontend_port = _prompt("Port", default=fd.get("FRONTEND_PORT", "3000"))
        frontend_build = _prompt("Build command", default="npm run build")
        frontend_test = _prompt("Test command", default="npm test")
        agents.append(AgentConfig(
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
        ))

    # Build config
    config = TeamConfig(
        project_name=project_name,
        controller_hostname=controller_hostname,
        worker_hostname=worker_hostname,
        team_size=team_size,
        agents=agents,
        communication_mode="hybrid",
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    # Generate everything
    print()
    _print_status("Generating team files...")

    values = config.get_placeholder_values()
    active_tags = config.get_active_agent_tags()

    dirs = create_coordination_dirs(project_dir, config)
    for d in dirs:
        _print_status(f"  Created: {d}/")

    templates = write_template_files(project_dir, values)
    for f in templates:
        _print_status(f"  Created: {f}")

    roles = write_role_claude_mds(project_dir, config, values, active_tags)
    for f in roles:
        _print_status(f"  Created: {f}")

    stubs = create_stub_files(project_dir, config)
    for f in stubs:
        _print_status(f"  Created: {f}")

    # Root CLAUDE.md
    root_content = generate_root_claude_md(config)
    root_path = project_dir / "CLAUDE.md"
    if root_path.exists():
        _print_status("Root CLAUDE.md exists - merging team section.")
        existing_content = root_path.read_text()
        root_content = _merge_into_existing_claude_md(existing_content, root_content)
    root_path.write_text(root_content)
    _print_status("  Created: CLAUDE.md")

    config.save(project_dir)
    _print_status(f"  Created: {TEAM_CONFIG_FILE}")

    # Summary
    print()
    _print_status("Agent team initialized!")
    _print_status(f"  Team size: {team_size}")
    _print_status(f"  Project: {project_name}")
    agent_names = ["PM", "Apple Engineer", "QA"]
    if has_backend:
        agent_names.append("Back End Engineer")
    if has_frontend:
        agent_names.append("Front End Engineer")
    _print_status(f"  Agents: {', '.join(agent_names)}")
    print()
    _print_status("Next steps:")
    _print_status("  1. Review generated CLAUDE.md files")
    _print_status("  2. Configure SSHFS mounts (byfrost sshfs)")
    _print_status("  3. Start your first compound cycle!")

    return 0
