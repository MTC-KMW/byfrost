"""Auth router - GitHub OAuth (browser + device flow) and JWT management."""

import secrets
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.github import (
    exchange_code_for_token,
    get_github_user,
    poll_device_token,
    request_device_code,
)
from app.auth.jwt import create_access_token, create_refresh_token, decode_token
from app.config import get_settings
from app.database import get_db
from app.models import User
from app.rate_limit import rate_limit

router = APIRouter()

# In-memory state store for CSRF protection (browser flow).
# In production with multiple server instances, use Redis instead.
_pending_states: set[str] = set()


# -- Pydantic schemas --

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class DeviceCodeResponse(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class DeviceTokenRequest(BaseModel):
    device_code: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


# -- Helpers --

async def _upsert_user(db: AsyncSession, github_user: dict) -> User:
    """Create or update a user from GitHub profile data."""
    result = await db.execute(
        select(User).where(User.github_id == github_user["id"])
    )
    user = result.scalar_one_or_none()

    if user:
        user.github_username = github_user["login"]
        user.email = github_user.get("email")
    else:
        user = User(
            github_id=github_user["id"],
            github_username=github_user["login"],
            email=github_user.get("email"),
        )
        db.add(user)

    await db.commit()
    await db.refresh(user)
    return user


def _issue_tokens(user: User) -> TokenResponse:
    """Create access + refresh tokens for a user."""
    return TokenResponse(
        access_token=create_access_token(user.id, user.github_username),
        refresh_token=create_refresh_token(user.id),
    )


# -- Browser flow --

@router.get("/github", dependencies=[rate_limit(20, 3600)])
async def github_redirect() -> RedirectResponse:
    """Redirect to GitHub OAuth authorization page."""
    settings = get_settings()
    state = secrets.token_urlsafe(32)
    _pending_states.add(state)

    params = urlencode({
        "client_id": settings.github_client_id,
        "redirect_uri": f"{settings.server_url}/auth/github/callback",
        "scope": "read:user user:email",
        "state": state,
    })
    return RedirectResponse(url=f"https://github.com/login/oauth/authorize?{params}")


@router.get("/github/callback", dependencies=[rate_limit(20, 3600)])
async def github_callback(
    code: str,
    state: str,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Handle GitHub OAuth callback, create/update user, issue JWT."""
    if state not in _pending_states:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid state parameter",
        )
    _pending_states.discard(state)

    # Exchange code for GitHub token, then fetch user profile
    github_token = await exchange_code_for_token(code)
    github_user = await get_github_user(github_token)

    user = await _upsert_user(db, github_user)
    return _issue_tokens(user)


# -- Device flow --

@router.post("/device/code", dependencies=[rate_limit(20, 3600)])
async def device_code() -> DeviceCodeResponse:
    """Start the device authorization flow. Returns a user code to display."""
    data = await request_device_code()
    return DeviceCodeResponse(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=data["verification_uri"],
        expires_in=data["expires_in"],
        interval=data.get("interval", 5),
    )


@router.post("/device/token", dependencies=[rate_limit(200, 3600)])
async def device_token(
    body: DeviceTokenRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Poll for device flow completion. Returns tokens when authorized."""
    data = await poll_device_token(body.device_code)

    if "access_token" in data:
        # Authorization complete - fetch user and issue our JWT
        github_user = await get_github_user(data["access_token"])
        user = await _upsert_user(db, github_user)
        return _issue_tokens(user).model_dump()

    error = data.get("error", "unknown_error")
    if error == "authorization_pending":
        return {"status": "pending"}
    if error == "slow_down":
        return {"status": "slow_down", "interval": data.get("interval", 10)}

    # expired_token, access_denied, or other errors
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Device flow failed: {error}",
    )


# -- Token refresh --

@router.post("/refresh")
async def refresh(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Exchange a valid refresh token for new access + refresh tokens."""
    payload = decode_token(body.refresh_token)

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type - expected refresh token",
        )

    user_id = payload.get("sub")
    result = await db.execute(
        select(User).where(User.id == uuid.UUID(user_id))
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    return _issue_tokens(user)
