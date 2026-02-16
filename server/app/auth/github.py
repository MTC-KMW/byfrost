"""GitHub OAuth API client for browser and device flows."""

import httpx

from app.config import get_settings

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_USER_URL = "https://api.github.com/user"


async def exchange_code_for_token(code: str) -> str:
    """Exchange an OAuth authorization code for a GitHub access token."""
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

    if "error" in data:
        raise ValueError(f"GitHub OAuth error: {data['error_description']}")

    result: str = data["access_token"]
    return result


async def get_github_user(access_token: str) -> dict:
    """Fetch the authenticated user's profile from GitHub."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            GITHUB_USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        result: dict = resp.json()
        return result


async def request_device_code() -> dict:
    """Start the device authorization flow. Returns device_code, user_code, etc."""
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GITHUB_DEVICE_CODE_URL,
            data={
                "client_id": settings.github_client_id,
                "scope": "read:user user:email",
            },
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        result: dict = resp.json()
        return result


async def poll_device_token(device_code: str) -> dict:
    """Poll GitHub for device flow completion.

    Returns the full response dict. Check for:
    - "access_token" key: authorization complete
    - "error" == "authorization_pending": user hasn't authorized yet
    - "error" == "slow_down": polling too fast
    - "error" == "expired_token": device code expired
    - "error" == "access_denied": user denied
    """
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": settings.github_client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        result: dict = resp.json()
        return result
