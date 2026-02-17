"""byfrost init - agent team setup wizard.

Interactive setup that creates CLAUDE.md files, shared infrastructure,
and coordination directories in the user's project. Reads templates from
the byfrost package's agents/ directory.

Usage: byfrost init  (run in project root)
"""

import json
import platform
import re
import subprocess
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


def detect_project_name(project_dir: Path) -> str:
    """Auto-detect project name from project metadata files.

    Priority: package.json > pyproject.toml > *.xcodeproj > git remote > dir name.
    """
    # package.json
    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            name = pkg.get("name", "")
            if name and not name.startswith("@"):
                return name
        except (json.JSONDecodeError, OSError):
            pass

    # pyproject.toml
    pyproject = project_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text()
            for pattern in [r'name\s*=\s*"([^"]+)"', r"name\s*=\s*'([^']+)'"]:
                m = re.search(pattern, content)
                if m:
                    return m.group(1)
        except OSError:
            pass

    # Xcode project
    xcodeprojs = list(project_dir.glob("*.xcodeproj"))
    if xcodeprojs:
        return xcodeprojs[0].stem

    # Git remote
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            name = url.rstrip("/").rsplit("/", 1)[-1]
            if name.endswith(".git"):
                name = name[:-4]
            if name:
                return name
    except (FileNotFoundError, OSError):
        pass

    return project_dir.name


def detect_team_size(
    detected_stacks: dict[str, list[str]],
) -> tuple[int, bool, bool]:
    """Determine team size from detected stacks.

    Returns (team_size, has_backend, has_frontend).
    Base team is always 3: PM + Apple Engineer + QA.
    """
    has_backend = "backend" in detected_stacks
    has_frontend = "frontend" in detected_stacks

    if has_backend and has_frontend:
        return 5, True, True
    if has_backend:
        return 4, True, False
    if has_frontend:
        return 4, False, True
    return 3, False, False


def detect_apple_details(project_dir: Path) -> dict[str, str]:
    """Auto-detect Apple project details."""
    details: dict[str, str] = {}

    xcodeprojs = list(project_dir.glob("**/*.xcodeproj"))
    if xcodeprojs:
        details["XCODE_SCHEME"] = xcodeprojs[0].stem
        parent = xcodeprojs[0].parent
        rel = parent.relative_to(project_dir)
        details["APPLE_DIR"] = str(rel) if str(rel) != "." else "."

    if (project_dir / "Package.swift").exists():
        details.setdefault("APPLE_DIR", ".")
        # Parse deployment target from Package.swift
        try:
            content = (project_dir / "Package.swift").read_text()
            # Match .iOS(.vNN) or .macOS(.vNN)
            targets = []
            pat = r"\.(iOS|macOS|watchOS|tvOS|visionOS)\(.v(\d+(?:_\d+)?)\)"
            for m in re.finditer(pat, content):
                plat = m.group(1)
                ver = m.group(2).replace("_", ".")
                targets.append(f"{plat} {ver}")
            if targets:
                details["MIN_DEPLOY_TARGET"] = " / ".join(targets)
        except OSError:
            pass

    # Scan Swift files for framework imports (sample up to 30 files)
    swift_files = list(project_dir.rglob("*.swift"))[:30]
    if swift_files:
        frameworks: set[str] = set()
        known = {
            "SwiftUI", "UIKit", "AppKit", "SwiftData", "CoreData",
            "Combine", "RealityKit", "MapKit", "CloudKit", "StoreKit",
            "WidgetKit", "GameKit", "ARKit", "SceneKit", "SpriteKit",
        }
        for sf in swift_files:
            try:
                for line in sf.read_text().splitlines()[:30]:
                    line = line.strip()
                    if line.startswith("import "):
                        fw = line.split()[1] if len(line.split()) > 1 else ""
                        if fw in known:
                            frameworks.add(fw)
            except OSError:
                continue
        if frameworks:
            details["APPLE_FRAMEWORKS"] = ", ".join(sorted(frameworks))

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
                details.setdefault("BACKEND_ENTRY", "app.main:app")
                details.setdefault("BACKEND_PORT", "8000")
            elif "flask" in content:
                details["BACKEND_FRAMEWORK"] = "Flask"
                details.setdefault("BACKEND_PORT", "5000")
                # Scan for common Flask entry points
                for entry in ["app.py", "wsgi.py", "run.py", "main.py"]:
                    if (project_dir / entry).exists():
                        details.setdefault("BACKEND_ENTRY", entry)
                        break
                details.setdefault("BACKEND_ENTRY", "app.py")
            elif "django" in content:
                details["BACKEND_FRAMEWORK"] = "Django"
                details.setdefault("BACKEND_ENTRY", "manage.py runserver")
                details.setdefault("BACKEND_PORT", "8000")
                details.setdefault("BACKEND_TEST_CMD", "python manage.py test")
            break

    # More languages
    if (project_dir / "go.mod").exists():
        details.setdefault("BACKEND_LANGUAGE", "Go")
        details.setdefault("BACKEND_ENTRY", "main.go")
        details.setdefault("BACKEND_TEST_CMD", "go test ./...")
    if (project_dir / "Cargo.toml").exists():
        details.setdefault("BACKEND_LANGUAGE", "Rust")
        details.setdefault("BACKEND_ENTRY", "src/main.rs")
        details.setdefault("BACKEND_TEST_CMD", "cargo test")
    if (project_dir / "Gemfile").exists():
        details.setdefault("BACKEND_LANGUAGE", "Ruby")
        try:
            content = (project_dir / "Gemfile").read_text().lower()
            if "rails" in content:
                details.setdefault("BACKEND_FRAMEWORK", "Rails")
                details.setdefault("BACKEND_ENTRY", "bin/rails server")
                details.setdefault("BACKEND_TEST_CMD", "rails test")
        except OSError:
            pass
    if (project_dir / "pom.xml").exists():
        details.setdefault("BACKEND_LANGUAGE", "Java")
        try:
            content = (project_dir / "pom.xml").read_text().lower()
            if "spring-boot" in content:
                details.setdefault("BACKEND_FRAMEWORK", "Spring Boot")
        except OSError:
            pass
        details.setdefault("BACKEND_TEST_CMD", "mvn test")
    if (project_dir / "composer.json").exists():
        details.setdefault("BACKEND_LANGUAGE", "PHP")
        try:
            pkg = json.loads((project_dir / "composer.json").read_text())
            deps = {**pkg.get("require", {}), **pkg.get("require-dev", {})}
            if "laravel/framework" in deps:
                details.setdefault("BACKEND_FRAMEWORK", "Laravel")
                details.setdefault("BACKEND_ENTRY", "artisan serve")
                details.setdefault("BACKEND_TEST_CMD", "php artisan test")
        except (json.JSONDecodeError, OSError):
            pass

    # Detect test command from common Python patterns
    if details.get("BACKEND_LANGUAGE") == "Python":
        if (project_dir / "pytest.ini").exists() or (project_dir / "tests").is_dir():
            details.setdefault("BACKEND_TEST_CMD", "pytest tests/")
        elif (project_dir / "test").is_dir():
            details.setdefault("BACKEND_TEST_CMD", "pytest test/")
        else:
            details.setdefault("BACKEND_TEST_CMD", "pytest")

    # Detect database from Python deps
    for req_file in ["requirements.txt", "pyproject.toml"]:
        path = project_dir / req_file
        if path.exists():
            try:
                content = path.read_text().lower()
                if "psycopg" in content or "sqlalchemy" in content:
                    details.setdefault("DATABASE_TYPE", "PostgreSQL")
                elif "pymongo" in content or "motor" in content:
                    details.setdefault("DATABASE_TYPE", "MongoDB")
                elif "mysql" in content or "pymysql" in content:
                    details.setdefault("DATABASE_TYPE", "MySQL")
                elif "sqlite" in content:
                    details.setdefault("DATABASE_TYPE", "SQLite")
            except OSError:
                pass
            break

    # Detect database from docker-compose
    for dc_file in ["docker-compose.yml", "docker-compose.yaml", "compose.yml"]:
        dc_path = project_dir / dc_file
        if dc_path.exists():
            try:
                content = dc_path.read_text().lower()
                if "postgres" in content:
                    details.setdefault("DATABASE_TYPE", "PostgreSQL")
                elif "mysql" in content or "mariadb" in content:
                    details.setdefault("DATABASE_TYPE", "MySQL")
                elif "mongo" in content:
                    details.setdefault("DATABASE_TYPE", "MongoDB")
            except OSError:
                pass
            break

    # Detect database/port from .env files
    for env_file in [".env", ".env.example", ".env.local"]:
        env_path = project_dir / env_file
        if env_path.exists():
            try:
                content = env_path.read_text()
                # DATABASE_URL scheme
                db_match = re.search(r"DATABASE_URL\s*=\s*(\w+)://", content)
                if db_match:
                    scheme = db_match.group(1).lower()
                    if "postgres" in scheme:
                        details.setdefault("DATABASE_TYPE", "PostgreSQL")
                    elif "mysql" in scheme:
                        details.setdefault("DATABASE_TYPE", "MySQL")
                    elif "sqlite" in scheme:
                        details.setdefault("DATABASE_TYPE", "SQLite")
                    elif "mongo" in scheme:
                        details.setdefault("DATABASE_TYPE", "MongoDB")
                # Port from env
                port_match = re.search(
                    r"(?:PORT|APP_PORT|SERVER_PORT)\s*=\s*(\d{4,5})", content,
                )
                if port_match:
                    details.setdefault("BACKEND_PORT", port_match.group(1))
            except OSError:
                pass
            break

    # Detect backend directory
    for candidate in ["backend", "server", "api", "src", "app"]:
        if (project_dir / candidate).is_dir():
            details.setdefault("BACKEND_DIR", candidate)
            break

    details.setdefault("BACKEND_DIR", ".")
    details.setdefault("BACKEND_PORT", "8000")
    return details


def _detect_package_manager(project_dir: Path) -> str:
    """Detect the frontend package manager from lock files."""
    if (project_dir / "bun.lockb").exists() or (project_dir / "bun.lock").exists():
        return "bun"
    if (project_dir / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (project_dir / "yarn.lock").exists():
        return "yarn"
    return "npm"


def detect_frontend_details(project_dir: Path) -> dict[str, str]:
    """Auto-detect frontend project details."""
    details: dict[str, str] = {}

    # Find package.json -- check subdirectories too
    pkg_path = None
    pkg_dir = project_dir
    for candidate in [
        project_dir / "package.json",
        project_dir / "web" / "package.json",
        project_dir / "frontend" / "package.json",
        project_dir / "client" / "package.json",
    ]:
        if candidate.exists():
            pkg_path = candidate
            pkg_dir = candidate.parent
            # Set frontend dir from where we found it
            rel = candidate.parent.relative_to(project_dir)
            if str(rel) != ".":
                details["FRONTEND_DIR"] = str(rel)
            break

    if pkg_path:
        # Detect package manager from lock files
        pm = _detect_package_manager(pkg_dir)
        run_prefix = {"npm": "npm run ", "yarn": "yarn ", "pnpm": "pnpm ", "bun": "bun run "}[pm]
        run_cmd = {"npm": "npm", "yarn": "yarn", "pnpm": "pnpm", "bun": "bun"}[pm]

        try:
            pkg = json.loads(pkg_path.read_text())
            deps: dict[str, Any] = {
                **pkg.get("dependencies", {}),
                **pkg.get("devDependencies", {}),
            }
            scripts = pkg.get("scripts", {})

            # Detect framework
            if "next" in deps:
                details["FRONTEND_FRAMEWORK"] = "Next.js"
            elif "@remix-run/react" in deps:
                details["FRONTEND_FRAMEWORK"] = "Remix"
            elif "nuxt" in deps:
                details["FRONTEND_FRAMEWORK"] = "Nuxt"
            elif "gatsby" in deps:
                details["FRONTEND_FRAMEWORK"] = "Gatsby"
            elif "astro" in deps:
                details["FRONTEND_FRAMEWORK"] = "Astro"
            elif "react" in deps or "react-dom" in deps:
                details["FRONTEND_FRAMEWORK"] = "React"
            elif "vue" in deps:
                details["FRONTEND_FRAMEWORK"] = "Vue"
            elif "@sveltejs/kit" in deps:
                details["FRONTEND_FRAMEWORK"] = "SvelteKit"
            elif "svelte" in deps:
                details["FRONTEND_FRAMEWORK"] = "Svelte"
            elif "@angular/core" in deps:
                details["FRONTEND_FRAMEWORK"] = "Angular"
            elif "solid-js" in deps:
                details["FRONTEND_FRAMEWORK"] = "SolidJS"

            # Detect commands from scripts (adjusted for package manager)
            if "dev" in scripts:
                details["FRONTEND_DEV_CMD"] = f"{run_prefix}dev"
            elif "start" in scripts:
                details["FRONTEND_DEV_CMD"] = f"{run_cmd} start"
            if "build" in scripts:
                details["FRONTEND_BUILD_CMD"] = f"{run_prefix}build"
            if "test" in scripts:
                details["FRONTEND_TEST_CMD"] = f"{run_cmd} test"

            # Detect port from dev script
            dev_script = scripts.get("dev", "") + scripts.get("start", "")
            port_match = re.search(r"(?:--port|PORT=?|-p)\s*(\d{4,5})", dev_script)
            if port_match:
                details["FRONTEND_PORT"] = port_match.group(1)
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


def _detect_byfrost_connection() -> dict[str, str]:
    """Pull device and pairing info from ~/.byfrost/auth.json and server.

    Returns dict with worker_hostname (if available).
    """
    info: dict[str, str] = {}
    try:
        from cli.api_client import ByfrostAPIClient, load_auth
        auth = load_auth()
        if not auth:
            return info

        # Check locally first
        if auth.get("worker_name"):
            info["worker_hostname"] = auth["worker_name"]
            return info

        # Query server for device list to find worker name
        if auth.get("access_token"):
            import asyncio
            async def _fetch_worker_name() -> str | None:
                api = ByfrostAPIClient(server_url=auth.get("server_url"))
                devices = await api.list_devices(auth["access_token"])
                for d in devices:
                    if d.get("role") == "worker":
                        return str(d.get("name", ""))
                return None
            name = asyncio.run(_fetch_worker_name())
            if name:
                info["worker_hostname"] = name
    except Exception:
        pass
    return info


def _fetch_worker_project_info() -> dict[str, str]:
    """Query the worker daemon for Apple project details.

    Connects via WebSocket, sends project.info request, returns response.
    Returns empty dict if worker is unreachable.
    """
    try:
        import asyncio

        from cli.main import load_config
        from core.security import MessageSigner

        config = load_config()
        host = config["host"]
        port = config["port"]
        secret = config["secret"]

        async def _query() -> dict[str, str]:
            import websockets

            from core.security import TLSManager

            use_tls = TLSManager.has_client_certs()
            protocol = "wss" if use_tls else "ws"
            uri = f"{protocol}://{host}:{port}"

            ssl_ctx = None
            if use_tls:
                try:
                    ssl_ctx = TLSManager.get_client_ssl_context()
                except Exception:
                    uri = f"ws://{host}:{port}"

            signer = MessageSigner(secret) if secret else None

            async with websockets.connect(
                uri, ssl=ssl_ctx, open_timeout=5, close_timeout=3,
            ) as ws:
                msg: dict[str, Any] = {"type": "project.info"}
                if signer:
                    msg.update(signer.sign(msg))
                await ws.send(json.dumps(msg))

                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                data = json.loads(raw)
                if data.get("type") == "project.info":
                    result: dict[str, str] = {}
                    for key in (
                        "xcode_scheme", "apple_dir",
                        "apple_frameworks", "min_deploy_target",
                    ):
                        if data.get(key):
                            result[key] = str(data[key])
                    return result
            return {}

        return asyncio.run(_query())
    except Exception:
        return {}


def _build_auto_config(
    project_dir: Path,
) -> tuple[TeamConfig, list[tuple[str, str, str]]]:
    """Auto-detect everything and build a TeamConfig.

    Returns (config, fields) where fields is a list of
    (label, key, value) tuples for display and editing.
    """
    # Detect stacks
    detected = detect_project_stacks(project_dir)
    team_size, has_backend, has_frontend = detect_team_size(detected)

    # Project info
    project_name = detect_project_name(project_dir)
    controller_hostname = platform.node()
    connection = _detect_byfrost_connection()
    worker_hostname = connection.get("worker_hostname", "")

    # Stack details (local)
    apple = detect_apple_details(project_dir)
    backend = detect_backend_details(project_dir) if has_backend else {}
    frontend = detect_frontend_details(project_dir) if has_frontend else {}

    # Query worker daemon for Apple project details (remote)
    _print_status("Querying worker for Apple project details...")
    worker_info = _fetch_worker_project_info()
    if worker_info:
        _print_status(f"  Worker project: {worker_info.get('xcode_scheme', '?')}")
        # Worker info overrides local Apple detection
        for wk, ak in [
            ("xcode_scheme", "XCODE_SCHEME"),
            ("apple_dir", "APPLE_DIR"),
            ("apple_frameworks", "APPLE_FRAMEWORKS"),
            ("min_deploy_target", "MIN_DEPLOY_TARGET"),
        ]:
            if wk in worker_info:
                apple[ak] = worker_info[wk]
    else:
        _print_status("  Worker unreachable (using local defaults)")

    # Build agents
    agents: list[AgentConfig] = [AgentConfig(role="pm")]

    apple_dir = apple.get("APPLE_DIR", "apple")
    xcode_scheme = apple.get("XCODE_SCHEME", project_name)
    frameworks = apple.get("APPLE_FRAMEWORKS", "SwiftUI")
    min_deploy = apple.get("MIN_DEPLOY_TARGET", "iOS 17.0 / macOS 14.0")
    agents.append(AgentConfig(
        role="apple", directory=apple_dir,
        settings={
            "APPLE_DIR": apple_dir, "XCODE_SCHEME": xcode_scheme,
            "APPLE_FRAMEWORKS": frameworks, "MIN_DEPLOY_TARGET": min_deploy,
        },
    ))

    agents.append(AgentConfig(role="qa"))

    if has_backend:
        bd = {
            "BACKEND_DIR": backend.get("BACKEND_DIR", "backend"),
            "BACKEND_FRAMEWORK": backend.get("BACKEND_FRAMEWORK", ""),
            "BACKEND_LANGUAGE": backend.get("BACKEND_LANGUAGE", "Python"),
            "BACKEND_PORT": backend.get("BACKEND_PORT", "8000"),
            "BACKEND_ENTRY": backend.get("BACKEND_ENTRY", "app.main:app"),
            "BACKEND_TEST_CMD": backend.get("BACKEND_TEST_CMD", "pytest tests/"),
            "DATABASE_TYPE": backend.get("DATABASE_TYPE", "PostgreSQL"),
        }
        agents.append(AgentConfig(
            role="backend", directory=bd["BACKEND_DIR"], settings=bd,
        ))

    if has_frontend:
        fd = {
            "FRONTEND_DIR": frontend.get("FRONTEND_DIR", "web"),
            "FRONTEND_FRAMEWORK": frontend.get("FRONTEND_FRAMEWORK", ""),
            "FRONTEND_DEV_CMD": frontend.get("FRONTEND_DEV_CMD", "npm run dev"),
            "FRONTEND_PORT": frontend.get("FRONTEND_PORT", "3000"),
            "FRONTEND_BUILD_CMD": frontend.get("FRONTEND_BUILD_CMD", "npm run build"),
            "FRONTEND_TEST_CMD": frontend.get("FRONTEND_TEST_CMD", "npm test"),
        }
        agents.append(AgentConfig(
            role="frontend", directory=fd["FRONTEND_DIR"], settings=fd,
        ))

    config = TeamConfig(
        project_name=project_name,
        controller_hostname=controller_hostname,
        worker_hostname=worker_hostname,
        team_size=team_size,
        agents=agents,
        communication_mode="hybrid",
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    # Build editable fields list: (label, config_path, value)
    fields: list[tuple[str, str, str]] = [
        ("Project name", "project_name", project_name),
        ("Team size", "team_size", str(team_size)),
        ("Controller", "controller_hostname", controller_hostname),
        ("Worker (Mac)", "worker_hostname", worker_hostname),
        ("Apple dir", "apple.APPLE_DIR", apple_dir),
        ("Xcode scheme", "apple.XCODE_SCHEME", xcode_scheme),
        ("Frameworks", "apple.APPLE_FRAMEWORKS", frameworks),
        ("Min deploy target", "apple.MIN_DEPLOY_TARGET", min_deploy),
    ]
    if has_backend:
        fields.extend([
            ("Backend dir", "backend.BACKEND_DIR", bd["BACKEND_DIR"]),
            ("Backend framework", "backend.BACKEND_FRAMEWORK", bd["BACKEND_FRAMEWORK"]),
            ("Backend language", "backend.BACKEND_LANGUAGE", bd["BACKEND_LANGUAGE"]),
            ("Backend port", "backend.BACKEND_PORT", bd["BACKEND_PORT"]),
            ("Backend entry", "backend.BACKEND_ENTRY", bd["BACKEND_ENTRY"]),
            ("Backend test cmd", "backend.BACKEND_TEST_CMD", bd["BACKEND_TEST_CMD"]),
            ("Database type", "backend.DATABASE_TYPE", bd["DATABASE_TYPE"]),
        ])
    if has_frontend:
        fields.extend([
            ("Frontend dir", "frontend.FRONTEND_DIR", fd["FRONTEND_DIR"]),
            ("Frontend framework", "frontend.FRONTEND_FRAMEWORK", fd["FRONTEND_FRAMEWORK"]),
            ("Frontend dev cmd", "frontend.FRONTEND_DEV_CMD", fd["FRONTEND_DEV_CMD"]),
            ("Frontend port", "frontend.FRONTEND_PORT", fd["FRONTEND_PORT"]),
            ("Frontend build cmd", "frontend.FRONTEND_BUILD_CMD", fd["FRONTEND_BUILD_CMD"]),
            ("Frontend test cmd", "frontend.FRONTEND_TEST_CMD", fd["FRONTEND_TEST_CMD"]),
        ])

    return config, fields


def _apply_field_edit(
    config: TeamConfig, fields: list[tuple[str, str, str]],
    field_idx: int, new_value: str,
) -> None:
    """Apply a single field edit to the config and fields list."""
    label, key, _ = fields[field_idx]
    fields[field_idx] = (label, key, new_value)

    # Update the config object
    if key == "project_name":
        config.project_name = new_value
    elif key == "team_size":
        config.team_size = int(new_value)
    elif key == "controller_hostname":
        config.controller_hostname = new_value
    elif key == "worker_hostname":
        config.worker_hostname = new_value
    elif "." in key:
        role, setting = key.split(".", 1)
        agent = config.get_agent(role)
        if agent:
            agent.settings[setting] = new_value
            if setting.endswith("_DIR"):
                agent.directory = new_value


def _display_summary(
    config: TeamConfig, detected: dict[str, list[str]],
) -> None:
    """Print a formatted summary of the detected configuration."""
    # Detection summary
    parts = []
    for stack, indicators in detected.items():
        parts.append(f"{stack} ({', '.join(indicators)})")
    if parts:
        _print_status(f"Detected: {', '.join(parts)}")
    print()

    # Project info
    _print_status(f"Project:    {config.project_name}")
    _print_status(f"Controller: {config.controller_hostname}")
    worker_label = config.worker_hostname or "(not detected - will prompt)"
    _print_status(f"Worker:     {worker_label}")
    print()

    # Team table
    _print_status(f"Team ({config.team_size} agents):")
    _print_status(f"  {'PM':<16} {config.controller_hostname:<20} Plans, routes, compounds")

    apple = config.get_agent("apple")
    apple_desc = apple.settings.get("APPLE_FRAMEWORKS", "SwiftUI") if apple else "SwiftUI"
    apple_target = apple.settings.get("MIN_DEPLOY_TARGET", "") if apple else ""
    worker = config.worker_hostname or "?"
    _print_status(f"  {'Apple Eng':<16} {worker:<20} {apple_desc}, {apple_target}")

    _print_status(f"  {'QA Eng':<16} {config.controller_hostname:<20} Stream monitoring + review")

    be = config.get_agent("backend")
    if be:
        fw = be.settings.get("BACKEND_FRAMEWORK", "")
        lang = be.settings.get("BACKEND_LANGUAGE", "")
        db = be.settings.get("DATABASE_TYPE", "")
        desc = " / ".join(filter(None, [fw, lang, db]))
        _print_status(f"  {'Back End':<16} {config.controller_hostname:<20} {desc}")

    fe = config.get_agent("frontend")
    if fe:
        fw = fe.settings.get("FRONTEND_FRAMEWORK", "")
        port = fe.settings.get("FRONTEND_PORT", "")
        desc = " / ".join(filter(None, [fw, f"port {port}" if port else ""]))
        _print_status(f"  {'Front End':<16} {config.controller_hostname:<20} {desc}")


def _edit_fields(
    config: TeamConfig, fields: list[tuple[str, str, str]],
) -> None:
    """Interactive field editor -- user picks a number and changes the value."""
    while True:
        print()
        for i, (label, _, value) in enumerate(fields, 1):
            print(f"  {i:>2}. {label + ':':<22} {value}")
        print()
        choice = input("  Edit field # (or Enter to accept all): ").strip()
        if not choice:
            break
        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(fields):
                _print_error("Invalid field number.")
                continue
        except ValueError:
            _print_error("Enter a number or press Enter to accept.")
            continue

        label, key, old_val = fields[idx]
        new_val = input(f"  {label} [{old_val}]: ").strip()
        if new_val and new_val != old_val:
            _apply_field_edit(config, fields, idx, new_val)
            _print_status(f"Updated {label} to: {new_val}")


def _init_default_team(project_dir: Path) -> int:
    """Auto-detect team configuration and confirm with user."""
    print()
    _print_status("Scanning project...")

    # Auto-detect everything
    detected = detect_project_stacks(project_dir)
    config, fields = _build_auto_config(project_dir)

    # Display summary
    _display_summary(config, detected)
    print()

    # Prompt for missing required fields
    if not config.worker_hostname:
        config.worker_hostname = _prompt("Worker (Mac) hostname")
        # Update in fields list too
        for i, (label, key, _) in enumerate(fields):
            if key == "worker_hostname":
                fields[i] = (label, key, config.worker_hostname)
                break

    # Single confirmation
    answer = input("  Look good? [Y/n/edit]: ").strip().lower()
    if answer in ("n", "no"):
        _print_status("Setup cancelled.")
        return 0
    if answer == "edit":
        _edit_fields(config, fields)

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
    _print_status(f"  Team size: {config.team_size}")
    _print_status(f"  Project: {config.project_name}")
    agent_names = ["PM", "Apple Engineer", "QA"]
    if config.has_agent("backend"):
        agent_names.append("Back End Engineer")
    if config.has_agent("frontend"):
        agent_names.append("Front End Engineer")
    _print_status(f"  Agents: {', '.join(agent_names)}")
    print()
    _print_status("Next steps:")
    _print_status("  1. Review generated CLAUDE.md files")
    _print_status("  2. Configure SSHFS mounts (byfrost sshfs)")
    _print_status("  3. Start your first compound cycle!")

    return 0
