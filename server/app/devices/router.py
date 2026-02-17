"""Devices router - registration, listing, heartbeat."""

import secrets
import uuid
from datetime import datetime, timezone
from typing import Literal

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import get_current_user
from app.database import get_db
from app.models import Device, Pairing, User
from app.rate_limit import rate_limit

router = APIRouter()
device_security = HTTPBearer()


# -- Pydantic schemas --


class DeviceRegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    role: Literal["worker", "controller"]
    platform: Literal["macos", "linux", "windows"]


class DeviceRegisterResponse(BaseModel):
    device_id: uuid.UUID
    device_token: str  # plaintext, shown once only


class DeviceInfo(BaseModel):
    id: uuid.UUID
    name: str
    role: str
    platform: str
    addresses: dict | None
    last_heartbeat: datetime | None
    created_at: datetime


class HeartbeatRequest(BaseModel):
    addresses: dict  # {local_ip, tailscale_ip, public_ip, port}


class DevicePairingResponse(BaseModel):
    pairing_id: uuid.UUID
    status: str
    role: str  # "worker" or "controller"


# -- Device token auth dependency --


async def get_device_by_token(
    device_id: uuid.UUID,
    credentials: HTTPAuthorizationCredentials = Depends(device_security),
    db: AsyncSession = Depends(get_db),
) -> Device:
    """Verify a device token against the bcrypt hash for a specific device."""
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()

    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )

    if not bcrypt.checkpw(
        credentials.credentials.encode(), device.device_token.encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device token",
        )

    return device


# -- Endpoints --


@router.post("/register", dependencies=[rate_limit(10, 3600)])
async def register_device(
    body: DeviceRegisterRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeviceRegisterResponse:
    """Register or re-register a device. Returns a device token (shown once).

    If a device with the same (user, name, platform) already exists, its
    token is rotated and the existing device is returned. This prevents
    duplicate entries when a user runs ``byfrost login`` multiple times.
    """
    raw_token = secrets.token_urlsafe(32)
    hashed_token = bcrypt.hashpw(raw_token.encode(), bcrypt.gensalt()).decode()

    # Check for existing device with same (user, name, platform)
    result = await db.execute(
        select(Device).where(
            Device.user_id == current_user.id,
            Device.name == body.name,
            Device.platform == body.platform,
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        # Re-register: rotate token, update role
        existing.device_token = hashed_token
        existing.role = body.role
        device = existing
    else:
        device = Device(
            user_id=current_user.id,
            name=body.name,
            role=body.role,
            platform=body.platform,
            device_token=hashed_token,
        )
        db.add(device)

    await db.commit()
    await db.refresh(device)

    return DeviceRegisterResponse(device_id=device.id, device_token=raw_token)


@router.get("/")
async def list_devices(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DeviceInfo]:
    """List all devices belonging to the current user."""
    result = await db.execute(
        select(Device).where(Device.user_id == current_user.id)
    )
    devices = result.scalars().all()

    return [
        DeviceInfo(
            id=d.id,
            name=d.name,
            role=d.role,
            platform=d.platform,
            addresses=d.addresses,
            last_heartbeat=d.last_heartbeat,
            created_at=d.created_at,
        )
        for d in devices
    ]


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(
    device_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Unregister a device. Must belong to the current user."""
    result = await db.execute(
        select(Device).where(
            Device.id == device_id, Device.user_id == current_user.id
        )
    )
    device = result.scalar_one_or_none()

    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )

    await db.delete(device)
    await db.commit()


@router.post("/{device_id}/heartbeat")
async def heartbeat(
    body: HeartbeatRequest,
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update device addresses and last_heartbeat timestamp."""
    device.addresses = body.addresses
    device.last_heartbeat = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "ok"}


@router.get("/{device_id}/pairing", response_model=DevicePairingResponse)
async def get_device_pairing(
    device: Device = Depends(get_device_by_token),
    db: AsyncSession = Depends(get_db),
) -> DevicePairingResponse:
    """Return the active pairing for this device (device token auth)."""
    # Check as worker first (most common use case for this endpoint)
    result = await db.execute(
        select(Pairing).where(
            Pairing.worker_id == device.id, Pairing.status == "active"
        )
    )
    pairing = result.scalar_one_or_none()
    if pairing:
        return DevicePairingResponse(
            pairing_id=pairing.id, status=pairing.status, role="worker"
        )

    # Check as controller
    result = await db.execute(
        select(Pairing).where(
            Pairing.controller_id == device.id, Pairing.status == "active"
        )
    )
    pairing = result.scalar_one_or_none()
    if pairing:
        return DevicePairingResponse(
            pairing_id=pairing.id, status=pairing.status, role="controller"
        )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="No active pairing for this device",
    )
