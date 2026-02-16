"""Reusable HTTP client for the Byfrost coordination server.

Handles auth token management and common API calls. Used by CLI
commands (login, connect, account, logout) and daemon heartbeat.
"""

import json
import os
import platform
import stat
import sys
from typing import Any

import httpx

from core.config import AUTH_FILE, BRIDGE_DIR, DEFAULT_SERVER_URL

# ---------------------------------------------------------------------------
# Auth file helpers
# ---------------------------------------------------------------------------


def ensure_byfrost_dir() -> None:
    """Create ~/.byfrost/ with mode 700 if it does not exist."""
    if not BRIDGE_DIR.exists():
        BRIDGE_DIR.mkdir(mode=0o700, parents=True)


def load_auth() -> dict[str, Any] | None:
    """Load auth data from ~/.byfrost/auth.json. Returns None if missing."""
    if not AUTH_FILE.exists():
        return None
    try:
        return json.loads(AUTH_FILE.read_text())  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return None


def save_auth(data: dict[str, Any]) -> None:
    """Write auth data to ~/.byfrost/auth.json with mode 600."""
    ensure_byfrost_dir()
    AUTH_FILE.write_text(json.dumps(data, indent=2))
    AUTH_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600


# ---------------------------------------------------------------------------
# Server URL resolution
# ---------------------------------------------------------------------------


def get_server_url() -> str:
    """Resolve the server URL.

    Priority: BYFROST_SERVER env var > stored auth.json > default.
    """
    env_url = os.environ.get("BYFROST_SERVER")
    if env_url:
        return env_url.rstrip("/")

    auth = load_auth()
    if auth and auth.get("server_url"):
        url: str = auth["server_url"]
        return url.rstrip("/")

    return DEFAULT_SERVER_URL


# ---------------------------------------------------------------------------
# Platform and role detection
# ---------------------------------------------------------------------------


def detect_platform() -> str:
    """Return the platform string for device registration."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "linux"


def detect_role() -> str:
    """Auto-detect device role from platform. macOS = worker, else controller."""
    if sys.platform == "darwin":
        return "worker"
    return "controller"


def get_device_name() -> str:
    """Return a human-readable device name (hostname)."""
    return platform.node() or "unknown"


# ---------------------------------------------------------------------------
# Server API client
# ---------------------------------------------------------------------------


class ByfrostAPIClient:
    """HTTP client for the Byfrost coordination server.

    All methods are async. Each request creates a short-lived httpx
    client -- appropriate for CLI tools with low-volume API calls.
    """

    def __init__(self, server_url: str | None = None, timeout: float = 30.0):
        self._server_url = (server_url or get_server_url()).rstrip("/")
        self._timeout = timeout

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> httpx.Response:
        """Make an HTTP request to the server."""
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.request(
                method,
                f"{self._server_url}{path}",
                json=json_body,
                headers=headers,
            )
        return resp

    # -- Device flow --

    async def request_device_code(self) -> dict[str, Any]:
        """POST /auth/device/code - start device authorization flow."""
        resp = await self._request("POST", "/auth/device/code")
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def poll_device_token(self, device_code: str) -> dict[str, Any]:
        """POST /auth/device/token - poll for device flow completion.

        Returns the response dict. On HTTP 400 (expired/denied), returns
        an error dict instead of raising.
        """
        resp = await self._request(
            "POST", "/auth/device/token", json_body={"device_code": device_code}
        )
        if resp.status_code == 400:
            return {"error": resp.json().get("detail", "Device flow failed")}
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    # -- Device registration --

    async def register_device(
        self, token: str, name: str, role: str, plat: str
    ) -> dict[str, Any]:
        """POST /devices/register - register this device."""
        resp = await self._request(
            "POST",
            "/devices/register",
            json_body={"name": name, "role": role, "platform": plat},
            token=token,
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    # -- Token refresh --

    async def refresh_token(self, refresh_tok: str) -> dict[str, Any]:
        """POST /auth/refresh - exchange refresh token for new tokens."""
        resp = await self._request(
            "POST", "/auth/refresh", json_body={"refresh_token": refresh_tok}
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    # -- Device listing (for connect, account commands) --

    async def list_devices(self, token: str) -> list[dict[str, Any]]:
        """GET /devices/ - list user's registered devices."""
        resp = await self._request("GET", "/devices/", token=token)
        resp.raise_for_status()
        result: list[dict[str, Any]] = resp.json()
        return result

    # -- Pairing (for connect command) --

    async def initiate_pairing(
        self, token: str, worker_id: str, controller_id: str
    ) -> dict[str, Any]:
        """POST /pair/initiate - create pairing between devices.

        Returns {"pairing_id": str, "status": str}. On 409 (already paired),
        returns {"pairing_id": str, "already_exists": True}.
        """
        resp = await self._request(
            "POST",
            "/pair/initiate",
            json_body={"worker_id": worker_id, "controller_id": controller_id},
            token=token,
        )
        if resp.status_code == 409:
            detail = resp.json().get("detail", {})
            if isinstance(detail, dict) and "pairing_id" in detail:
                return {"pairing_id": detail["pairing_id"], "already_exists": True}
            return {"error": "Active pairing already exists", "already_exists": True}
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def get_controller_credentials(
        self, pairing_id: str, device_token: str
    ) -> dict[str, Any]:
        """GET /pair/{pairing_id}/credentials/controller.

        Uses device_token (not JWT) for authentication.
        Returns {ca_cert, cert, private_key, hmac_secret, prev_hmac_secret}.
        """
        resp = await self._request(
            "GET",
            f"/pair/{pairing_id}/credentials/controller",
            token=device_token,
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def get_pairing_addresses(
        self, pairing_id: str, device_token: str
    ) -> dict[str, Any]:
        """GET /pair/{pairing_id}/addresses.

        Uses device_token for authentication. Returns worker addresses.
        """
        resp = await self._request(
            "GET",
            f"/pair/{pairing_id}/addresses",
            token=device_token,
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    # -- Device deletion (for logout command) --

    async def delete_device(self, token: str, device_id: str) -> None:
        """DELETE /devices/{id} - unregister device."""
        resp = await self._request("DELETE", f"/devices/{device_id}", token=token)
        resp.raise_for_status()
