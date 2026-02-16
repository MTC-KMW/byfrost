#!/usr/bin/env python3
"""
Byfrost CLI - cross-platform controller.

Send tasks to the Mac daemon, check status, stream output, and manage
the bridge connection.

Usage:
    byfrost send "Read ios/CLAUDE.md and implement tasks/ios/current.md"
    byfrost send --priority high "Fix the crash in ProfileView.swift"
    byfrost status
    byfrost status <task-id>
    byfrost attach
    byfrost cancel <task-id>
    byfrost logs
    byfrost ping
    byfrost rotate          - rotate the shared HMAC secret
    byfrost security        - show security status (TLS, certs, rate limits)

Config: reads BRIDGE_HOST, BRIDGE_PORT from .agent-team/config.env or environment.
        Secret is loaded from ~/.byfrost/secret (NEVER from config.env).
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

try:
    import websockets
except ImportError:
    print("ERROR: websockets not installed. Run: pip3 install websockets --break-system-packages")
    sys.exit(1)

import httpx

from cli.api_client import (
    ByfrostAPIClient,
    detect_platform,
    detect_role,
    get_device_name,
    load_auth,
    save_auth,
)
from core.config import DEFAULT_PORT, DEFAULT_SERVER_URL, source_env_file
from core.security import MessageSigner, SecretManager, TLSManager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config():
    """Load CLI config from environment or .agent-team/config.env."""
    config = {
        "host": os.environ.get("BRIDGE_HOST", ""),
        "port": int(os.environ.get("BRIDGE_PORT", str(DEFAULT_PORT))),
    }

    # Env file key mapping for CLI config
    # NOTE: BRIDGE_SECRET intentionally excluded - never read from config.env
    _cli_env_map: dict = {
        "BRIDGE_HOST": ("host", str),
        "BRIDGE_PORT": ("port", int),
        "MAC_HOSTNAME": ("host", str),  # fallback for host
    }

    for search in [Path.cwd(), Path.cwd() / ".agent-team"]:
        cfg_file = search / "config.env"
        if cfg_file.exists():
            source_env_file(cfg_file, config, _cli_env_map)
            break

    # BRIDGE_HOST falls back to MAC_HOSTNAME from environment
    if not config["host"]:
        config["host"] = os.environ.get("MAC_HOSTNAME", "localhost")

    # Load secret from secure file only
    config["secret"] = SecretManager.load()

    return config


# ---------------------------------------------------------------------------
# WebSocket Client
# ---------------------------------------------------------------------------

class ByfrostClient:
    def __init__(self, config):
        self.config = config
        self._signer = MessageSigner(config["secret"]) if config.get("secret") else None

        # Determine TLS availability
        self._use_tls = TLSManager.has_client_certs()
        protocol = "wss" if self._use_tls else "ws"
        self.uri = f"{protocol}://{config['host']}:{config['port']}"

    async def _connect(self):
        ssl_context = None
        if self._use_tls:
            try:
                ssl_context = TLSManager.get_client_ssl_context()
            except Exception as e:
                _print_error(f"TLS setup failed: {e}")
                _print_error("Falling back to plaintext (Tailscale encryption only)")
                self.uri = self.uri.replace("wss://", "ws://")

        try:
            return await websockets.connect(
                self.uri,
                ssl=ssl_context,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            )
        except (ConnectionRefusedError, OSError) as e:
            print(f"ERROR: Cannot connect to byfrost daemon at {self.uri}")
            print(f"  {e}")
            print("\nIs the daemon running on the Mac?")
            print(f"  ssh {self.config['host']} 'python3 -m daemon.byfrost_daemon'")
            sys.exit(1)

    def _sign(self, msg):
        """Sign outgoing message with HMAC."""
        if self._signer:
            return self._signer.sign(msg)
        msg["timestamp"] = time.time()
        return msg

    async def send_task(self, prompt, priority=0, project_path=None, tools=None):
        """Submit a task and stream output until completion."""
        ws = await self._connect()
        task_id = os.urandom(6).hex()

        msg = self._sign({
            "type": "task.submit",
            "task_id": task_id,
            "prompt": prompt,
            "priority": priority,
        })
        if project_path:
            msg["project_path"] = project_path
        if tools:
            msg["allowed_tools"] = tools

        await ws.send(json.dumps(msg))

        # Stream responses
        try:
            async for raw in ws:
                data = json.loads(raw)
                msg_type = data.get("type", "")

                if msg_type == "task.accepted":
                    pos = data.get("queue_position", 0)
                    _print_status(f"Task accepted: {data.get('task_id')}")
                    if pos > 0:
                        _print_status(f"Queue position: {pos}")

                elif msg_type == "task.output":
                    chunk = data.get("chunk", "")
                    # Print raw output for the PM to see
                    sys.stdout.write(chunk)
                    sys.stdout.flush()

                elif msg_type == "task.complete":
                    exit_code = data.get("exit_code", 0)
                    duration = data.get("duration", 0)
                    git_pushed = data.get("git_pushed", False)
                    print()
                    _print_status(f"Task complete (exit={exit_code}, {duration:.1f}s)")
                    if git_pushed:
                        _print_status("Auto git-push: changes pushed to remote")
                    return exit_code

                elif msg_type == "task.error":
                    error = data.get("error", "Unknown error")
                    print()
                    _print_error(f"Task failed: {error}")
                    return 1

                elif msg_type == "task.cancelled":
                    _print_status("Task cancelled")
                    return 2

                elif msg_type == "error":
                    _print_error(data.get("message", "Unknown error"))
                    return 1

        except websockets.exceptions.ConnectionClosed:
            _print_error("Connection lost to daemon")
            return 1
        finally:
            await ws.close()

    async def get_status(self, task_id=None):
        """Get daemon or task status."""
        ws = await self._connect()
        msg = self._sign({
            "type": "task.status",
        })
        if task_id:
            msg["task_id"] = task_id

        await ws.send(json.dumps(msg))

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(raw)

            if data.get("type") == "error":
                _print_error(data.get("message", "Unknown error"))
                return

            if task_id:
                _print_task_detail(data)
            else:
                _print_queue_status(data)

        except asyncio.TimeoutError:
            _print_error("Timeout waiting for status response")
        finally:
            await ws.close()

    async def cancel_task(self, task_id):
        """Cancel a running or queued task."""
        ws = await self._connect()
        msg = self._sign({
            "type": "task.cancel",
            "task_id": task_id,
        })
        await ws.send(json.dumps(msg))

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(raw)
            if data.get("type") == "task.cancelled":
                _print_status(f"Cancelled task: {task_id}")
            else:
                _print_error(data.get("message", "Cancel failed"))
        except asyncio.TimeoutError:
            _print_error("Timeout waiting for cancel response")
        finally:
            await ws.close()

    async def attach(self):
        """Attach to the active task's output stream."""
        ws = await self._connect()
        msg = self._sign({"type": "session.attach"})
        await ws.send(json.dumps(msg))

        try:
            async for raw in ws:
                data = json.loads(raw)
                msg_type = data.get("type", "")

                if msg_type == "session.output":
                    tid = data.get("task_id", "?")
                    hint = data.get("hint", "")
                    _print_status(f"Attached to task: {tid}")
                    if hint:
                        _print_status(f"Direct access: {hint}")
                    for line in data.get("lines", []):
                        print(line)
                    print()
                    _print_status("Streaming live output... (Ctrl+C to detach)")

                elif msg_type == "task.output":
                    sys.stdout.write(data.get("chunk", ""))
                    sys.stdout.flush()

                elif msg_type == "task.complete":
                    print()
                    _print_status(f"Task complete (exit={data.get('exit_code', '?')})")
                    return

                elif msg_type == "error":
                    _print_error(data.get("message", ""))
                    return

        except KeyboardInterrupt:
            print("\nDetached.")
        except websockets.exceptions.ConnectionClosed:
            _print_error("Connection lost")
        finally:
            await ws.close()

    async def ping(self):
        """Ping the daemon to verify connectivity."""
        ws = await self._connect()
        msg = self._sign({"type": "ping"})
        start = time.time()
        await ws.send(json.dumps(msg))

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            latency = (time.time() - start) * 1000
            data = json.loads(raw)
            if data.get("type") == "pong":
                uptime = data.get("uptime", 0)
                queue = data.get("queue", {})
                security = data.get("security", {})
                _print_status(f"Pong from {self.config['host']} ({latency:.0f}ms)")
                _print_status(f"Daemon uptime: {_format_duration(uptime)}")
                conn = "TLS (mTLS)" if self._use_tls else "plaintext (Tailscale)"
                _print_status(f"Connection: {conn}")
                _print_status(f"Daemon TLS: {'enabled' if security.get('tls') else 'disabled'}")
                active = queue.get("active")
                if active:
                    _print_status(f"Active task: {active['id']} ({active['status']})")
                else:
                    _print_status("No active task")
                _print_status(f"Queued: {queue.get('queue_size', 0)}")
                rl = security.get("rate_limiter", {})
                lockouts = rl.get("active_lockouts", {})
                if lockouts:
                    _print_status(f"Active lockouts: {len(lockouts)}")
            elif data.get("type") == "error":
                _print_error(data.get("message", "Auth failed"))
        except asyncio.TimeoutError:
            _print_error("Ping timeout")
        finally:
            await ws.close()

    async def send_followup(self, task_id, text):
        """Send a follow-up instruction to a running task."""
        ws = await self._connect()
        msg = self._sign({
            "type": "task.followup",
            "task_id": task_id,
            "text": text,
        })
        await ws.send(json.dumps(msg))

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(raw)
            if data.get("type") == "task.followup_sent":
                _print_status(f"Follow-up sent to task {task_id}")
            else:
                _print_error(data.get("message", "Follow-up failed"))
        except asyncio.TimeoutError:
            _print_error("Timeout sending follow-up")
        finally:
            await ws.close()


# ---------------------------------------------------------------------------
# Output Formatting
# ---------------------------------------------------------------------------

def _print_status(msg):
    print(f"\033[36m[byfrost]\033[0m {msg}")

def _print_error(msg):
    print(f"\033[31m[byfrost error]\033[0m {msg}", file=sys.stderr)

def _format_duration(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds/60:.0f}m {seconds%60:.0f}s"
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m"

def _format_time(ts):
    if not ts:
        return "-"
    return time.strftime("%H:%M:%S", time.localtime(ts))

def _print_task_detail(data):
    print(f"\n  Task:     {data.get('id', '?')}")
    print(f"  Status:   {data.get('status', '?')}")
    print(f"  Priority: {data.get('priority', 0)}")
    print(f"  Prompt:   {data.get('prompt', data.get('prompt_preview', '?'))[:100]}")
    print(f"  Created:  {_format_time(data.get('created_at'))}")
    print(f"  Started:  {_format_time(data.get('started_at'))}")
    print(f"  Finished: {_format_time(data.get('completed_at'))}")
    if data.get("exit_code") is not None:
        print(f"  Exit:     {data['exit_code']}")
    if data.get("tmux_session"):
        print(f"  Session:  {data['tmux_session']}")
    if data.get("error"):
        print(f"  Error:    {data['error']}")
    lines = data.get("output_lines", [])
    if lines:
        print(f"  Output ({len(lines)} lines):")
        for line in lines[-10:]:
            print(f"    {line}")
    print()

def _print_queue_status(data):
    active = data.get("active")
    queued = data.get("queued", [])
    history = data.get("recent_history", [])

    print(f"\n  {'='*50}")
    print("  Byfrost Status")
    print(f"  {'='*50}")

    if active:
        print("\n  Active Task:")
        print(f"    ID:      {active['id']}")
        print(f"    Prompt:  {active.get('prompt_preview', '?')}")
        print(f"    Started: {_format_time(active.get('started_at'))}")
        print(f"    Session: {active.get('tmux_session', '-')}")
        print(f"    Output:  {active.get('output_line_count', 0)} lines")
    else:
        print("\n  No active task")

    if queued:
        print(f"\n  Queued ({len(queued)}):")
        for t in queued:
            print(f"    [{t['id']}] {t.get('prompt_preview', '?')}")
    else:
        print("\n  Queue empty")

    if history:
        print("\n  Recent History:")
        for t in history:
            status = t.get("status", "?")
            icon = {"complete": "+", "failed": "X", "cancelled": "-"}.get(status, "?")
            print(f"    [{icon}] {t['id']}  - {status} (exit={t.get('exit_code', '?')})")

    print()


# ---------------------------------------------------------------------------
# Login helpers
# ---------------------------------------------------------------------------


def _extract_username_from_jwt(token: str) -> str:
    """Decode JWT payload to extract GitHub username.

    Uses base64 decoding only - no signature verification needed since
    we just received this token from the server we trust.
    """
    import base64

    try:
        payload_b64 = token.split(".")[1]
        # Add padding
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("username", "unknown")  # type: ignore[no-any-return]
    except (IndexError, ValueError, json.JSONDecodeError):
        return "unknown"


async def _do_login(server_url: str | None) -> int:
    """Execute device flow login: get code, poll, register device.

    Returns 0 on success, 1 on failure.
    """
    # Check if already logged in
    existing = load_auth()
    if existing and existing.get("access_token"):
        _print_status(f"Already logged in as {existing.get('github_username', '?')}")
        _print_status("Run 'byfrost logout' first to switch accounts.")
        return 0

    # Resolve server URL: --server flag > env var > default
    if server_url is None:
        server_url = os.environ.get("BYFROST_SERVER", DEFAULT_SERVER_URL)

    api = ByfrostAPIClient(server_url=server_url)

    # Step 1: Request device code
    _print_status("Starting GitHub device authorization...")
    try:
        code_data = await api.request_device_code()
    except httpx.HTTPStatusError as e:
        _print_error(f"Server error: {e.response.status_code}")
        return 1
    except httpx.ConnectError:
        _print_error(f"Cannot reach server at {server_url}")
        _print_error("Check the URL and your network connection.")
        return 1

    user_code = code_data["user_code"]
    verification_uri = code_data["verification_uri"]
    expires_in = code_data["expires_in"]
    interval = code_data.get("interval", 5)
    device_code = code_data["device_code"]

    # Step 2: Display instructions
    print()
    _print_status("Open this URL in your browser:")
    print(f"\n    {verification_uri}\n")
    _print_status("Enter this code when prompted:")
    print(f"\n    {user_code}\n")
    _print_status(f"Waiting for authorization (expires in {expires_in // 60} minutes)...")

    # Step 3: Poll for completion
    tokens = None
    while True:
        await asyncio.sleep(interval)
        try:
            result = await api.poll_device_token(device_code)
        except (httpx.HTTPStatusError, httpx.ConnectError):
            continue  # transient error, retry

        if "access_token" in result:
            tokens = result
            break
        elif result.get("status") == "pending":
            continue
        elif result.get("status") == "slow_down":
            interval = result.get("interval", interval + 5)
            continue
        elif "error" in result:
            _print_error(f"Authorization failed: {result['error']}")
            return 1

    print()
    _print_status("Authorization successful!")

    # Step 4: Detect platform and role
    plat = detect_platform()
    role = detect_role()
    device_name = get_device_name()

    if plat == "macos":
        _print_status(f"Platform: {plat}, role: {role} (macOS is always worker)")
    else:
        _print_status(f"Platform: {plat}, role: {role}")

    # Step 5: Register device
    _print_status(f"Registering device '{device_name}' as {role}...")
    try:
        reg = await api.register_device(
            tokens["access_token"], device_name, role, plat
        )
    except httpx.HTTPStatusError as e:
        _print_error(f"Device registration failed: {e.response.status_code}")
        return 1

    # Step 6: Save credentials
    github_username = _extract_username_from_jwt(tokens["access_token"])
    auth_data = {
        "server_url": server_url,
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "device_id": str(reg["device_id"]),
        "device_token": reg["device_token"],
        "github_username": github_username,
        "platform": plat,
        "role": role,
    }
    save_auth(auth_data)

    print()
    _print_status(f"Logged in as {github_username}")
    _print_status(f"Device registered: {device_name} ({role}/{plat})")
    _print_status(f"Server: {server_url}")
    _print_status("Credentials saved to ~/.byfrost/auth.json")

    if role == "controller":
        print()
        _print_status("Next step: run 'byfrost connect' to pair with your worker.")
    else:
        print()
        _print_status("Next step: run 'byfrost daemon install' to start the daemon.")

    return 0


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="byfrost",
        description="Byfrost CLI - communicate with the Mac daemon"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # byfrost login
    p_login = sub.add_parser("login", help="Sign in with GitHub and register device")
    p_login.add_argument(
        "--server", default=None,
        help="Server URL (default: https://api.byfrost.dev or BYFROST_SERVER env)",
    )

    # byfrost send
    p_send = sub.add_parser("send", help="Send a task to the Mac agent")
    p_send.add_argument("prompt", help="Task prompt for Claude Code")
    p_send.add_argument("--priority", choices=["normal", "high", "urgent"],
                        default="normal", help="Task priority")
    p_send.add_argument("--project", help="Override project path on Mac")
    p_send.add_argument("--tools", help="Override allowed tools")

    # byfrost status
    p_status = sub.add_parser("status", help="Check daemon and queue status")
    p_status.add_argument("task_id", nargs="?", help="Specific task ID")

    # byfrost cancel
    p_cancel = sub.add_parser("cancel", help="Cancel a task")
    p_cancel.add_argument("task_id", help="Task ID to cancel")

    # byfrost attach
    sub.add_parser("attach", help="Stream output from the active task")

    # byfrost ping
    sub.add_parser("ping", help="Verify daemon connectivity")

    # byfrost followup
    p_follow = sub.add_parser("followup", help="Send follow-up to active task")
    p_follow.add_argument("task_id", help="Task ID")
    p_follow.add_argument("text", help="Follow-up instruction")

    # byfrost logs
    p_logs = sub.add_parser("logs", help="View daemon logs (via SSH)")
    p_logs.add_argument("-n", "--lines", type=int, default=50, help="Number of lines")
    p_logs.add_argument("-f", "--follow", action="store_true", help="Follow log output")

    # byfrost rotate
    sub.add_parser("rotate", help="Rotate the shared HMAC secret")

    # byfrost security
    sub.add_parser("security", help="Show security status (TLS, certs, secret)")

    # byfrost audit
    p_audit = sub.add_parser("audit", help="View audit log (via SSH)")
    p_audit.add_argument("-n", "--lines", type=int, default=50, help="Number of lines")
    p_audit.add_argument("-f", "--follow", action="store_true", help="Follow audit output")

    args = parser.parse_args()

    # Commands that don't need the daemon WebSocket client
    if args.command == "login":
        sys.exit(asyncio.run(_do_login(args.server)))

    # All remaining commands need daemon config + WebSocket
    config = load_config()
    client = ByfrostClient(config)

    priority_map = {"normal": 0, "high": 1, "urgent": 2}

    if args.command == "send":
        exit_code = asyncio.run(client.send_task(
            args.prompt,
            priority=priority_map.get(args.priority, 0),
            project_path=args.project,
            tools=args.tools,
        ))
        sys.exit(exit_code or 0)

    elif args.command == "status":
        asyncio.run(client.get_status(task_id=getattr(args, "task_id", None)))

    elif args.command == "cancel":
        asyncio.run(client.cancel_task(args.task_id))

    elif args.command == "attach":
        asyncio.run(client.attach())

    elif args.command == "ping":
        asyncio.run(client.ping())

    elif args.command == "followup":
        asyncio.run(client.send_followup(args.task_id, args.text))

    elif args.command == "logs":
        # View remote logs via SSH
        host = config["host"]
        cmd = ["ssh", host, "tail"]
        if args.follow:
            cmd.append("-f")
        cmd.extend([f"-n{args.lines}", "~/.byfrost/logs/daemon.log"])
        os.execvp("ssh", cmd)

    elif args.command == "rotate":
        _print_status("Rotating HMAC secret...")
        SecretManager.load()
        SecretManager.rotate()
        _print_status("New secret generated and saved to ~/.byfrost/secret")
        _print_status(f"Old secret valid for {SecretManager.GRACE_PERIOD}s grace period")
        _print_status("")
        _print_status("IMPORTANT: Copy the new secret to the other machine:")
        _print_status(f"  scp ~/.byfrost/secret {config['host']}:~/.byfrost/secret")
        _print_status("")
        _print_status("The daemon will pick up the new secret on its next heartbeat.")
        _print_status("Both old and new secrets will work during the grace period.")

    elif args.command == "security":
        _print_status("Security Status")
        print()

        # Secret
        secret = SecretManager.load()
        if secret:
            _print_status(f"HMAC Secret: configured ({len(secret)} chars)")
            _print_status("  Location: ~/.byfrost/secret")
        else:
            _print_error("HMAC Secret: NOT CONFIGURED")

        # TLS certs
        cert_info = TLSManager.cert_info()
        print()
        certs_dir = TLSManager.CERTS_DIR if TLSManager.CERTS_DIR.exists() else "not found"
        _print_status(f"TLS Certificates: {certs_dir}")
        _print_status(f"  CA:     {'found' if cert_info['ca_exists'] else 'MISSING'}")
        _print_status(f"  Server: {'found' if cert_info['server_exists'] else 'MISSING'}")
        _print_status(f"  Client: {'found' if cert_info['client_exists'] else 'MISSING'}")
        if cert_info.get("server_expires"):
            _print_status(f"  Server expires: {cert_info['server_expires']}")

        # Connection test
        print()
        use_tls = TLSManager.has_client_certs()
        _print_status(f"Client TLS: {'enabled (mTLS)' if use_tls else 'disabled (plaintext)'}")

        if not use_tls or not secret:
            print()
            _print_status("Run deploy/setup-bridge.sh to generate certificates and secrets")

    elif args.command == "audit":
        host = config["host"]
        cmd = ["ssh", host, "tail"]
        if args.follow:
            cmd.append("-f")
        cmd.extend([f"-n{args.lines}", "~/.byfrost/logs/audit.log"])
        os.execvp("ssh", cmd)


if __name__ == "__main__":
    main()
