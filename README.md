# Byfrost

Remote Claude Code execution on Mac from any machine. Secure bridge + optional multi-agent team.

## Development Setup

```bash
git clone https://github.com/your-org/byfrost.git
cd byfrost
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

## Running Tests

```bash
ruff check .                    # lint
mypy core/ daemon/ cli/         # type check
pytest tests/                   # unit tests
```

## Project Structure

```
core/        Shared Python (security, protocol, config)
daemon/      Mac worker daemon (WebSocket, tmux, task queue)
cli/         Cross-platform CLI (byfrost command, daemon management, SSHFS config)
server/      Coordination server (FastAPI, OAuth, CA)
agents/      End-user deliverable templates (byfrost init + byfrost team)
mac-app/     macOS SwiftUI menu bar app (GUI)
linux-app/   Linux GTK 4 tray app (GUI)
windows-app/ Windows WPF system tray controller (GUI)
deploy/      launchd plist, systemd unit, setup scripts, SSHFS mount
tests/       Cross-module tests
```

Desktop users get polished GUIs. CLI can do everything the GUIs can,
so headless servers and SSH sessions work perfectly.

## Architecture

See [byfrost-build-plan.md](byfrost-build-plan.md) for the complete
product architecture and phased implementation plan.

## Contributing

1. Fork the repo
2. Create a feature branch (`feat/my-feature`)
3. Conventional commits (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`)
4. Open a PR against `main`


