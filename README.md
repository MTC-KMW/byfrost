# Byfrost

Remote Claude Code execution on Mac from any machine. Secure bridge + optional multi-agent team.

## Installation

```bash
# Recommended: pipx (global CLI in isolated venv)
pipx install git+https://github.com/MTC-KMW/byfrost.git

# Or pip
pip install git+https://github.com/MTC-KMW/byfrost.git
```

## Infrastructure

| Component | Technology | Location |
|---|---|---|
| Coordination server | FastAPI on Fly.io | `https://byfrost-server.fly.dev` |
| Database | Fly Postgres | Attached to `byfrost-server` |
| Cache / rate limiting | Upstash Redis | Connected via `REDIS_URL` |
| Auth | GitHub OAuth (device flow) | Browser + headless CLI |
| CI | GitHub Actions | Lint, typecheck, tests on push/PR |

The server handles auth, device registration, and pairing. All task
data flows peer-to-peer over mTLS + HMAC - the server never sees
prompts, code, or output.

## Development Setup

```bash
git clone https://github.com/MTC-KMW/byfrost.git
cd byfrost
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Server (separate deps)
cd server && pip install -e ".[dev]"
```

## Running Tests

```bash
ruff check .                    # lint
mypy core/ daemon/ cli/         # type check
pytest tests/                   # unit tests (247)

# Server tests (requires server deps)
cd server && pytest tests/ -v   # server tests (84)
```

## Project Structure

```
core/        Shared Python (security, protocol, config)
daemon/      Mac worker daemon (WebSocket, tmux, task queue)
cli/         Cross-platform CLI (byfrost command, daemon management, file sync)
server/      Coordination server (FastAPI, OAuth, CA)
agents/      End-user deliverable templates (byfrost init + byfrost team)
mac-app/     macOS SwiftUI menu bar app (GUI)
linux-app/   Linux GTK 4 tray app (GUI)
windows-app/ Windows WPF system tray controller (GUI)
deploy/      launchd plist, systemd unit, setup scripts
tests/       Cross-module tests
```

Desktop users get polished GUIs. CLI can do everything the GUIs can,
so headless servers and SSH sessions work perfectly.

## Contributing

1. Fork the repo
2. Create a feature branch (`feat/my-feature`)
3. Conventional commits (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`)
4. Open a PR against `main`
