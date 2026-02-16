"""Devices router - registration, listing, heartbeat."""

import secrets
import uuid
from datetime import datetime, timezone
from typing import Literal

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import get_current_user
from app.database import get_db
from app.models import Device, User

router = APIRouter()
device_security = HTTPBearer()


# -- Pydantic schemas --


class DeviceRegisterRequest(BaseModel):
    name: str
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


@router.post("/register")
async def register_device(
    body: DeviceRegisterRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeviceRegisterResponse:
    """Register a new device. Returns a device token (shown once)."""
    raw_token = secrets.token_urlsafe(32)
    hashed_token = bcrypt.hashpw(raw_token.encode(), bcrypt.gensalt()).decode()

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
