"""Pairing router - certificate generation, credential distribution, rotation.

Endpoints (implemented in Tasks 1.8-1.10):
    POST /pair/initiate                            - Create pairing
    GET  /pair/{pairing_id}/credentials/worker     - Fetch worker certs
    GET  /pair/{pairing_id}/credentials/controller - Fetch controller certs
    GET  /pair/{pairing_id}/addresses              - Get paired device addresses
    POST /pair/{pairing_id}/rotate                 - Rotate HMAC secret
    POST /pair/{pairing_id}/revoke                 - Revoke pairing
"""

import base64
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import get_current_user
from app.config import Settings, get_settings
from app.database import get_db
from app.models import Device, Pairing, User
from app.pairing.crypto import (
    decrypt_secret,
    encrypt_secret,
    generate_hmac_secret,
    generate_pairing_certs,
)
from app.rate_limit import rate_limit

router = APIRouter()
_device_security = HTTPBearer()

_HMAC_GRACE_PERIOD = timedelta(minutes=5)


# -- Pydantic schemas --


class PairingInitiateRequest(BaseModel):
    """Request body for pairing initiation."""

    worker_id: uuid.UUID
    controller_id: uuid.UUID


class PairingResponse(BaseModel):
    """Response for pairing initiation."""

    pairing_id: uuid.UUID
    status: str


class CredentialsResponse(BaseModel):
    """Credentials returned to a worker or controller device."""

    ca_cert: str
    cert: str
    private_key: str
    hmac_secret: str  # base64-encoded
    prev_hmac_secret: str | None = None  # base64-encoded, during rotation grace


class AddressesResponse(BaseModel):
    """Network addresses of the paired device."""

    addresses: dict | None


# -- Shared helpers --


async def _get_pairing_for_device(
    pairing_id: uuid.UUID,
    role: str,
    credentials: HTTPAuthorizationCredentials,
    db: AsyncSession,
) -> Pairing:
    """Load an active pairing and verify the device token for the given role."""
    result = await db.execute(
        select(Pairing).where(Pairing.id == pairing_id, Pairing.status == "active")
    )
    pairing = result.scalar_one_or_none()
    if not pairing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pairing not found",
        )

    device_id = pairing.worker_id if role == "worker" else pairing.controller_id
    device = await db.get(Device, device_id)
    if not device or not bcrypt.checkpw(
        credentials.credentials.encode(), device.device_token.encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device token",
        )

    return pairing


async def _get_pairing_and_role(
    pairing_id: uuid.UUID,
    credentials: HTTPAuthorizationCredentials,
    db: AsyncSession,
) -> tuple[Pairing, str]:
    """Load active pairing and determine caller role from device token.

    Tries worker first, then controller. Returns (pairing, role).
    """
    result = await db.execute(
        select(Pairing).where(Pairing.id == pairing_id, Pairing.status == "active")
    )
    pairing = result.scalar_one_or_none()
    if not pairing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pairing not found",
        )

    token = credentials.credentials.encode()
    for role, device_id in [
        ("worker", pairing.worker_id),
        ("controller", pairing.controller_id),
    ]:
        device = await db.get(Device, device_id)
        if device and bcrypt.checkpw(token, device.device_token.encode()):
            return pairing, role

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid device token",
    )


async def _get_user_pairing(
    pairing_id: uuid.UUID,
    user: User,
    db: AsyncSession,
) -> Pairing:
    """Load an active pairing and verify ownership."""
    result = await db.execute(
        select(Pairing).where(Pairing.id == pairing_id, Pairing.status == "active")
    )
    pairing = result.scalar_one_or_none()
    if not pairing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pairing not found",
        )
    if pairing.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Pairing does not belong to you",
        )
    return pairing


def _prev_hmac_if_active(pairing: Pairing, settings_obj: Settings) -> str | None:
    """Return base64-encoded previous HMAC secret if within grace period."""
    if (
        pairing.prev_hmac_secret
        and pairing.hmac_rotated_at
        and datetime.now(timezone.utc) - pairing.hmac_rotated_at < _HMAC_GRACE_PERIOD
    ):
        return base64.b64encode(
            decrypt_secret(pairing.prev_hmac_secret, settings_obj.encryption_key)  # type: ignore[union-attr]
        ).decode()
    return None


# -- Endpoints --


@router.post(
    "/initiate",
    response_model=PairingResponse,
    dependencies=[rate_limit(5, 3600, by="user")],
)
async def initiate_pairing(
    body: PairingInitiateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PairingResponse:
    """Create a new pairing between a worker and controller device."""
    # Load both devices
    worker = await db.get(Device, body.worker_id)
    controller = await db.get(Device, body.controller_id)

    if not worker or not controller:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )

    # Verify ownership
    if worker.user_id != user.id or controller.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Device does not belong to you",
        )

    # Verify roles
    if worker.role != "worker" or controller.role != "controller":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="First device must be a worker and second must be a controller",
        )

    # Check for existing active pairing
    existing = await db.execute(
        select(Pairing).where(
            Pairing.worker_id == body.worker_id,
            Pairing.controller_id == body.controller_id,
            Pairing.status == "active",
        )
    )
    existing_pairing = existing.scalar_one_or_none()
    if existing_pairing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Active pairing already exists for this device pair",
                "pairing_id": str(existing_pairing.id),
            },
        )

    # Create pairing record (flush to get ID for cert CN)
    pairing = Pairing(user_id=user.id, worker_id=body.worker_id, controller_id=body.controller_id)
    db.add(pairing)
    await db.flush()

    # Extract worker addresses from device heartbeat data
    addrs = worker.addresses or {}
    worker_addresses = [
        addr
        for key in ("local_ip", "tailscale_ip", "public_ip")
        if (addr := addrs.get(key))
    ]

    # Generate certs and HMAC secret
    certs = generate_pairing_certs(pairing.id, worker_addresses)
    settings = get_settings()
    hmac_secret = generate_hmac_secret()

    # Store certs and encrypted secrets
    pairing.ca_cert = certs.ca_cert_pem
    pairing.worker_cert = certs.worker_cert_pem
    pairing.worker_key = encrypt_secret(certs.worker_key_pem.encode(), settings.encryption_key)
    pairing.controller_cert = certs.controller_cert_pem
    pairing.controller_key = encrypt_secret(
        certs.controller_key_pem.encode(), settings.encryption_key
    )
    pairing.hmac_secret = encrypt_secret(hmac_secret, settings.encryption_key)

    await db.commit()

    return PairingResponse(pairing_id=pairing.id, status="active")


@router.get("/{pairing_id}/credentials/worker", response_model=CredentialsResponse)
async def get_worker_credentials(
    pairing_id: uuid.UUID,
    credentials: HTTPAuthorizationCredentials = Depends(_device_security),
    db: AsyncSession = Depends(get_db),
) -> CredentialsResponse:
    """Fetch worker credentials for a pairing (device token auth)."""
    pairing = await _get_pairing_for_device(pairing_id, "worker", credentials, db)
    settings = get_settings()

    return CredentialsResponse(
        ca_cert=pairing.ca_cert,  # type: ignore[arg-type]
        cert=pairing.worker_cert,  # type: ignore[arg-type]
        private_key=decrypt_secret(pairing.worker_key, settings.encryption_key).decode(),  # type: ignore[arg-type]
        hmac_secret=base64.b64encode(
            decrypt_secret(pairing.hmac_secret, settings.encryption_key)  # type: ignore[arg-type]
        ).decode(),
        prev_hmac_secret=_prev_hmac_if_active(pairing, settings),
    )


@router.get("/{pairing_id}/credentials/controller", response_model=CredentialsResponse)
async def get_controller_credentials(
    pairing_id: uuid.UUID,
    credentials: HTTPAuthorizationCredentials = Depends(_device_security),
    db: AsyncSession = Depends(get_db),
) -> CredentialsResponse:
    """Fetch controller credentials for a pairing (device token auth)."""
    pairing = await _get_pairing_for_device(pairing_id, "controller", credentials, db)
    settings = get_settings()

    return CredentialsResponse(
        ca_cert=pairing.ca_cert,  # type: ignore[arg-type]
        cert=pairing.controller_cert,  # type: ignore[arg-type]
        private_key=decrypt_secret(pairing.controller_key, settings.encryption_key).decode(),  # type: ignore[arg-type]
        hmac_secret=base64.b64encode(
            decrypt_secret(pairing.hmac_secret, settings.encryption_key)  # type: ignore[arg-type]
        ).decode(),
        prev_hmac_secret=_prev_hmac_if_active(pairing, settings),
    )


@router.get("/{pairing_id}/addresses", response_model=AddressesResponse)
async def get_addresses(
    pairing_id: uuid.UUID,
    credentials: HTTPAuthorizationCredentials = Depends(_device_security),
    db: AsyncSession = Depends(get_db),
) -> AddressesResponse:
    """Get the paired device's addresses (device token auth)."""
    pairing, role = await _get_pairing_and_role(pairing_id, credentials, db)

    # Return the OTHER device's addresses
    other_id = pairing.controller_id if role == "worker" else pairing.worker_id
    other_device = await db.get(Device, other_id)

    return AddressesResponse(
        addresses=other_device.addresses if other_device else None,
    )


@router.post(
    "/{pairing_id}/rotate",
    dependencies=[rate_limit(10, 3600)],
)
async def rotate_hmac(
    pairing_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Rotate the HMAC secret (JWT auth). Old secret valid for 5 minutes."""
    pairing = await _get_user_pairing(pairing_id, user, db)
    settings = get_settings()

    # Move current secret to previous
    pairing.prev_hmac_secret = pairing.hmac_secret

    # Generate and store new secret
    new_secret = generate_hmac_secret()
    pairing.hmac_secret = encrypt_secret(new_secret, settings.encryption_key)
    pairing.hmac_rotated_at = datetime.now(timezone.utc)

    await db.commit()
    return {"status": "rotated"}


@router.post("/{pairing_id}/revoke")
async def revoke_pairing(
    pairing_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Revoke a pairing, invalidating all credentials (JWT auth)."""
    pairing = await _get_user_pairing(pairing_id, user, db)

    pairing.status = "revoked"
    # Clear sensitive material
    pairing.worker_key = None
    pairing.controller_key = None
    pairing.hmac_secret = None
    pairing.prev_hmac_secret = None

    await db.commit()
    return {"status": "revoked"}
