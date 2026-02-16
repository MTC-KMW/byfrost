# Byfrost - Codebase Context

Byfrost is a secure bridge for real-time remote Claude Code execution on Mac from
any other machine. This file is context for Claude Code sessions working
on the Byfrost codebase itself.

## Getting Started

Read `byfrost-build-plan.md` before writing any code. It is the
single source of truth for architecture, naming, and task scope.

Start with Phase 0. Work through tasks in order (0.1, 0.2, ...).
Do not skip ahead to later phases.

## Architecture

- `core/` - shared Python: security (TLS, HMAC, sanitization, rate limiting, audit), wire protocol, config
- `daemon/` - Mac worker: WebSocket server, tmux session manager, task queue, git ops, server heartbeat
- `cli/` - cross-platform CLI: `byfrost` command with login, connect, init, team, send, status, daemon management, SSHFS config
- `server/` - coordination server: FastAPI, GitHub OAuth (browser + device flow), device registration, per-pairing CA, HMAC secrets
- `agents/` - end-user deliverable templates that `byfrost init` processes (NOT dev docs). Includes `team.py` for add/remove/status with partial regeneration.
- `mac-app/` - SwiftUI menu bar app (GUI, optional on headless Macs)
- `linux-app/` - GTK 4 tray app (GUI, optional on headless Linux)
- `windows-app/` - WPF system tray controller (GUI, optional on headless Windows)
- `deploy/` - launchd plist, systemd unit, setup scripts, SSHFS mount/unmount/remount

## Development Guidelines

1. Don't create files unless necessary
2. Prefer editing over creating new files
3. Test all changes before marking complete
4. Never use the em dash. Use en dashes or hyphens instead.

## Design Philosophy

- Keep it simple, efficient, robust, best practice and scalable. No overengineering!
- GUIs are the default desktop experience - polished wizards and tray apps
- CLI can do everything the GUIs can - headless machines work perfectly
- GUIs detect display server on launch, exit silently if absent
- All three GUIs call the same underlying CLI/daemon operations

## Agent Team

Byfrost ships with an optional default agent team (the "Berserkers" in
end-user docs). Users choose during `byfrost init` whether to install
it or bring their own agents.

- Default team uses Hybrid communication: SSHFS for coordination files, git for code
- Custom teams choose from Full Git, Full SSHFS, or Hybrid
- Team tiers: 3 (PM + Apple Engineer + QA), 4 (add backend or frontend), 5 (all five)
- QA is always required
- `byfrost team add/remove/status` modifies the team after setup
- Role templates use section markers (`<!-- byfrost:team -->` etc.) for partial regeneration
- `agents/team.py` handles add/remove/status and rewrites only managed blocks
- SSHFS mounts coordination directories (tasks/, shared/, compound/, pm/, qa/) on the Mac
- Code directories (apple/, backend/, web/) sync via git only

See `byfrost-build-plan.md` Section 10 for the full agent team spec.

## Key Facts

- **Always ask clarifying questions** before performing work
- **Keep code simple and well-commented** - this project is maintained by one person
- CLI command name is `byfrost`
- The Mac-side agent is called "Apple Engineer" (covers all Apple platforms)
- Apple code directory is `apple/` (not `ios/`)
- Config/credentials live in `~/.byfrost/`
- Wire protocol: WebSocket over mTLS (per-pairing CA) + HMAC-SHA256 per message
- Server generates certs during pairing then discards the CA private key
- All task data flows peer-to-peer. Server never sees prompts or output.

## Commands

```bash
ruff check .                    # lint
mypy core/ daemon/ cli/         # type check
pytest tests/                   # unit tests

# Server
cd server && pip install -e ".[dev]"      # install server deps
cd server && uvicorn app.main:app --reload  # run server standalone
cd server && pytest tests/ -v               # server tests
cd server && docker compose up              # full stack (app + postgres + redis)
```

## Conventions

- Conventional commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`
- Python: ruff formatting, type hints, docstrings on public functions
- No `sys.path` hacks - use proper package imports via `core.`, `daemon.`, `cli.`

## Build Plan

See `byfrost-build-plan.md` for the complete product architecture and
phased implementation plan.
