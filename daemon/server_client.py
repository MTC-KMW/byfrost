"""Daemon-to-server communication module.

Handles outbound HTTPS calls from the daemon to the coordination server:
- Periodic heartbeat (network addresses)
- Credential fetch on startup (certs + HMAC secret)
- JWT auto-refresh before expiry
- HMAC rotation detection and re-fetch

All communication is best-effort -- the daemon keeps running even if the
server is unreachable.
"""

import asyncio
import base64
import json
import logging
import socket
import sys
import time
from typing import Any, Callable

import httpx

from cli.api_client import load_auth, save_auth
from core.config import CERTS_DIR, DEFAULT_PORT, SECRET_FILE
from core.security import SecretManager, TLSManager

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEARTBEAT_INTERVAL = 300  # 5 minutes
TOKEN_REFRESH_CHECK = 300  # Check JWT expiry every 5 min
TOKEN_REFRESH_MARGIN = 600  # Refresh 10 min before expiry
MAX_RETRY_BACKOFF = 300  # Max 5 min between retries
INITIAL_RETRY_DELAY = 5  # Start at 5 seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def detect_addresses(port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Detect this machine's network addresses for heartbeat reporting.

    Returns dict with local_ip, tailscale_ip (if available), and port.
    """
    addresses: dict[str, Any] = {"port": port}

    # Local IP via UDP socket trick (no actual data sent)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        addresses["local_ip"] = s.getsockname()[0]
        s.close()
    except OSError:
        addresses["local_ip"] = "127.0.0.1"

    # Tailscale IP: scan all network interfaces for 100.x.y.z (CGNAT range)
    import re
    import subprocess

    # Use full paths - launchd/systemd may have minimal PATH
    if sys.platform == "darwin":
        net_cmds = [["/sbin/ifconfig"], ["ifconfig"]]
    else:
        net_cmds = [["/sbin/ip", "-4", "addr"], ["ip", "-4", "addr"]]

    for cmd in net_cmds:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for match in re.finditer(r"inet\s+(100\.\d+\.\d+\.\d+)", result.stdout):
                    addresses["tailscale_ip"] = match.group(1)
                    break
            if "tailscale_ip" in addresses:
                break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    return addresses


def _decode_jwt_exp(token: str) -> float | None:
    """Extract expiry timestamp from JWT without verification.

    Only needs the 'exp' claim for refresh timing. Server verifies signatures.
    """
    try:
        payload_b64 = token.split(".")[1]
        # Pad base64url to multiple of 4
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return float(payload.get("exp", 0))
    except (IndexError, ValueError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# ServerClient
# ---------------------------------------------------------------------------


class ServerClient:
    """Async client for daemon-to-server communication.

    Lifecycle:
        1. Created by ByfrostDaemon.__init__()
        2. start() called during daemon startup - kicks off background tasks
        3. stop() called during daemon shutdown - cancels background tasks
    """

    def __init__(
        self,
        config: dict[str, Any],
        logger: logging.Logger,
        on_secret_rotated: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.log = logger
        self._on_secret_rotated = on_secret_rotated
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []
        self._client: httpx.AsyncClient | None = None

        # Auth state (loaded from ~/.byfrost/auth.json)
        self._server_url = ""
        self._device_id = ""
        self._device_token = ""
        self._access_token = ""
        self._refresh_token = ""
        self._pairing_id = ""

    # -- Lifecycle --

    async def start(self) -> bool:
        """Load auth, fetch credentials if needed, start background loops.

        Returns True if initialized, False if auth is missing.
        Daemon should continue running either way.
        """
        if not self._load_auth():
            self.log.warning(
                "Server client: no auth credentials found. "
                "Run 'byfrost login' on this machine first."
            )
            return False

        self._client = httpx.AsyncClient(timeout=30.0)
        self._running = True

        # Discover pairing if not in auth.json
        await self._discover_pairing()

        # Fetch credentials on startup (certs + HMAC)
        await self._fetch_credentials_if_needed()

        # Start background loops
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
        self._tasks.append(asyncio.create_task(self._token_refresh_loop()))
        self._tasks.append(asyncio.create_task(self._rotation_check_loop()))

        self.log.info("Server client started")
        return True

    async def stop(self) -> None:
        """Cancel background tasks and close HTTP client."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        if self._client:
            await self._client.aclose()
            self._client = None
        self.log.info("Server client stopped")

    # -- Auth loading --

    def _load_auth(self) -> bool:
        """Load credentials from ~/.byfrost/auth.json."""
        auth = load_auth()
        if not auth:
            return False

        self._server_url = auth.get("server_url", "").rstrip("/")
        self._device_id = auth.get("device_id", "")
        self._device_token = auth.get("device_token", "")
        self._access_token = auth.get("access_token", "")
        self._refresh_token = auth.get("refresh_token", "")
        self._pairing_id = auth.get("pairing_id", "")

        return bool(self._server_url and self._device_id and self._device_token)

    # -- Pairing discovery --

    async def _discover_pairing(self) -> None:
        """If pairing_id is not in auth.json, ask the server for it.

        Called on daemon startup and re-checked on each heartbeat until
        a pairing is found. The device_id and device_token are enough
        to look up the active pairing on the server.
        """
        if self._pairing_id:
            return  # Already known

        if not self._device_id or not self._device_token:
            return

        try:
            resp = await self._request(
                "GET",
                f"/devices/{self._device_id}/pairing",
                use_device_token=True,
            )
            if resp.status_code == 404:
                self.log.info("No active pairing found (not yet paired)")
                return
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            self.log.warning(f"Pairing discovery failed: {e}")
            return

        data = resp.json()
        self._pairing_id = str(data["pairing_id"])
        self.log.info(f"Discovered pairing: {self._pairing_id} (role={data['role']})")

        # Persist so we don't need to discover again
        auth = load_auth() or {}
        auth["pairing_id"] = self._pairing_id
        save_auth(auth)

    # -- HTTP helpers --

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        use_device_token: bool = False,
    ) -> httpx.Response:
        """Make an authenticated request to the coordination server."""
        assert self._client is not None
        token = self._device_token if use_device_token else self._access_token
        headers = {"Authorization": f"Bearer {token}"}
        return await self._client.request(
            method,
            f"{self._server_url}{path}",
            json=json_body,
            headers=headers,
        )

    # -- Heartbeat --

    async def _heartbeat_loop(self) -> None:
        """Send heartbeat every HEARTBEAT_INTERVAL seconds."""
        delay = INITIAL_RETRY_DELAY
        while self._running:
            try:
                await self._send_heartbeat()
                delay = INITIAL_RETRY_DELAY
            except Exception as e:
                self.log.warning(f"Heartbeat failed: {e}")
                delay = min(delay * 2, MAX_RETRY_BACKOFF)

            # If not yet paired, re-check frequently (every 15s instead
            # of 5min). Covers the case where daemon starts before
            # controller runs `byfrost connect`.
            if not self._pairing_id:
                await self._discover_pairing()
                if self._pairing_id:
                    await self._fetch_credentials_if_needed()
                await asyncio.sleep(15)
            else:
                await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _send_heartbeat(self) -> None:
        """POST /devices/{device_id}/heartbeat with current addresses."""
        addresses = detect_addresses(self.config.get("port", DEFAULT_PORT))
        resp = await self._request(
            "POST",
            f"/devices/{self._device_id}/heartbeat",
            json_body={"addresses": addresses},
            use_device_token=True,
        )
        if resp.status_code == 200:
            self.log.debug(f"Heartbeat sent: {addresses}")
        else:
            self.log.warning(f"Heartbeat: HTTP {resp.status_code}")

    # -- Credential fetch --

    async def _fetch_credentials_if_needed(self) -> None:
        """Fetch worker certs + HMAC from server if needed."""
        if not self._pairing_id:
            self.log.info("No pairing_id - skipping credential fetch")
            return

        if TLSManager.has_server_certs() and SECRET_FILE.exists():
            self.log.info("Worker credentials already present locally")
            return

        await self._fetch_and_save_credentials()

    async def _fetch_and_save_credentials(self) -> None:
        """GET /pair/{pairing_id}/credentials/worker and save to disk."""
        try:
            resp = await self._request(
                "GET",
                f"/pair/{self._pairing_id}/credentials/worker",
                use_device_token=True,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            self.log.error(f"Credential fetch failed: HTTP {e.response.status_code}")
            return
        except httpx.RequestError as e:
            self.log.error(f"Credential fetch failed: {e}")
            return

        creds = resp.json()
        self._save_worker_credentials(creds)
        self.log.info("Worker credentials fetched and saved")

    def _save_worker_credentials(self, creds: dict[str, Any]) -> None:
        """Write worker certs and HMAC secret to ~/.byfrost/."""
        CERTS_DIR.mkdir(parents=True, exist_ok=True)
        CERTS_DIR.chmod(0o700)

        TLSManager.CA_CERT.write_text(creds["ca_cert"])
        TLSManager.SERVER_CERT.write_text(creds["cert"])
        TLSManager.SERVER_KEY.write_text(creds["private_key"])
        TLSManager.SERVER_KEY.chmod(0o600)

        # Server returns base64-encoded bytes, save as hex
        secret_bytes = base64.b64decode(creds["hmac_secret"])
        SecretManager.save(secret_bytes.hex())

    # -- JWT auto-refresh --

    async def _token_refresh_loop(self) -> None:
        """Refresh JWT access token before it expires."""
        while self._running:
            try:
                await self._refresh_if_needed()
            except Exception as e:
                self.log.warning(f"Token refresh failed: {e}")
            await asyncio.sleep(TOKEN_REFRESH_CHECK)

    async def _refresh_if_needed(self) -> None:
        """Check access_token expiry and refresh if within margin."""
        if not self._access_token or not self._refresh_token:
            return

        exp = _decode_jwt_exp(self._access_token)
        if exp is None:
            return

        remaining = exp - time.time()
        if remaining > TOKEN_REFRESH_MARGIN:
            return

        self.log.info(f"Access token expires in {remaining:.0f}s, refreshing...")
        assert self._client is not None
        try:
            resp = await self._client.post(
                f"{self._server_url}/auth/refresh",
                json={"refresh_token": self._refresh_token},
            )
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            self.log.error(f"Token refresh request failed: {e}")
            return

        data = resp.json()
        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]

        # Persist to auth.json
        auth = load_auth() or {}
        auth["access_token"] = self._access_token
        auth["refresh_token"] = self._refresh_token
        save_auth(auth)
        self.log.info("Access token refreshed")

    # -- Rotation detection --

    async def _rotation_check_loop(self) -> None:
        """Periodically check if HMAC secret has been rotated on the server."""
        while self._running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if not self._pairing_id:
                continue
            try:
                await self._check_rotation()
            except Exception as e:
                self.log.warning(f"Rotation check failed: {e}")

    async def _check_rotation(self) -> None:
        """Fetch credentials and compare HMAC secret to detect rotation."""
        try:
            resp = await self._request(
                "GET",
                f"/pair/{self._pairing_id}/credentials/worker",
                use_device_token=True,
            )
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError):
            return

        creds = resp.json()
        server_secret_bytes = base64.b64decode(creds.get("hmac_secret", ""))
        server_secret_hex = server_secret_bytes.hex()

        local_secret = SecretManager.load()
        if not local_secret or server_secret_hex == local_secret:
            return

        self.log.info("HMAC secret rotation detected, updating local secret")
        SecretManager.rotate()
        SecretManager.save(server_secret_hex)
        self.log.info("HMAC secret updated from server")

        if self._on_secret_rotated:
            self._on_secret_rotated()
