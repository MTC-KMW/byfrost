"""Tests for pairing initiation, credential distribution, addresses, rotation, revocation."""

import base64
import random
import secrets
import uuid
from unittest.mock import patch

import fakeredis.aioredis
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.auth.jwt import create_access_token
from app.config import get_settings
from app.database import get_db
from app.main import create_app
from app.models import Pairing, User

# Valid base64-encoded 32-byte key for tests
_TEST_ENCRYPTION_KEY = base64.b64encode(secrets.token_bytes(32)).decode()


@pytest.fixture()
async def async_client():
    """Async test client with transaction rollback for isolation."""
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    test_engine = create_async_engine(get_settings().database_url)
    conn = await test_engine.connect()
    txn = await conn.begin()

    async def override_get_db():
        async with AsyncSession(bind=conn, expire_on_commit=False) as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    with (
        patch("app.rate_limit.get_redis", return_value=fake_redis),
        patch("app.pairing.router.get_settings") as mock_settings,
    ):
        real_settings = get_settings()
        mock_settings.return_value = real_settings.model_copy(
            update={"encryption_key": _TEST_ENCRYPTION_KEY}
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            yield ac

    await txn.rollback()
    await conn.close()
    await test_engine.dispose()
    await fake_redis.aclose()


async def _seed_user(
    async_client: httpx.AsyncClient,
    github_id: int | None = None,
    username: str = "testpairinguser",
) -> tuple[uuid.UUID, dict]:
    """Create a user in the DB and return (user_id, auth_headers)."""
    if github_id is None:
        github_id = random.randint(100000, 999999999)

    app = async_client._transport.app  # type: ignore[union-attr]
    override_get_db = app.dependency_overrides[get_db]

    session_gen = override_get_db()
    db = await session_gen.__anext__()

    user = User(github_id=github_id, github_username=username, email=None)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    try:
        await session_gen.__anext__()
    except StopAsyncIteration:
        pass

    token = create_access_token(user.id, user.github_username)
    headers = {"Authorization": f"Bearer {token}"}
    return user.id, headers


async def _register_device(
    ac: httpx.AsyncClient,
    headers: dict,
    name: str = "my-mac",
    role: str = "worker",
    platform: str = "macos",
) -> dict:
    """Register a device and return the response JSON."""
    resp = await ac.post(
        "/devices/register",
        json={"name": name, "role": role, "platform": platform},
        headers=headers,
    )
    assert resp.status_code == 200
    return resp.json()


class TestInitiatePairing:
    """POST /pair/initiate tests."""

    async def test_initiate_pairing(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Happy path: create worker + controller, initiate pairing."""
        _, headers = await _seed_user(async_client)
        worker = await _register_device(async_client, headers, name="worker-1", role="worker")
        controller = await _register_device(
            async_client, headers, name="ctrl-1", role="controller", platform="linux"
        )

        resp = await async_client.post(
            "/pair/initiate",
            json={
                "worker_id": worker["device_id"],
                "controller_id": controller["device_id"],
            },
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "pairing_id" in data
        assert data["status"] == "active"

    async def test_initiate_requires_auth(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """No JWT returns 401."""
        resp = await async_client.post(
            "/pair/initiate",
            json={
                "worker_id": str(uuid.uuid4()),
                "controller_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code in (401, 403)

    async def test_initiate_device_not_found(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Nonexistent device ID returns 404."""
        _, headers = await _seed_user(async_client)
        resp = await async_client.post(
            "/pair/initiate",
            json={
                "worker_id": str(uuid.uuid4()),
                "controller_id": str(uuid.uuid4()),
            },
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_initiate_wrong_owner(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Devices belonging to different user returns 403."""
        _, headers_a = await _seed_user(async_client, username="user_a")
        _, headers_b = await _seed_user(
            async_client, github_id=88888, username="user_b"
        )

        worker = await _register_device(async_client, headers_a, name="w", role="worker")
        controller = await _register_device(
            async_client, headers_b, name="c", role="controller", platform="linux"
        )

        # User A tries to pair their worker with user B's controller
        resp = await async_client.post(
            "/pair/initiate",
            json={
                "worker_id": worker["device_id"],
                "controller_id": controller["device_id"],
            },
            headers=headers_a,
        )
        assert resp.status_code == 403

    async def test_initiate_wrong_roles(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Two workers (wrong roles) returns 400."""
        _, headers = await _seed_user(async_client)
        w1 = await _register_device(async_client, headers, name="w1", role="worker")
        w2 = await _register_device(async_client, headers, name="w2", role="worker")

        resp = await async_client.post(
            "/pair/initiate",
            json={
                "worker_id": w1["device_id"],
                "controller_id": w2["device_id"],
            },
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_initiate_duplicate(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Same worker+controller pair already active returns 409."""
        _, headers = await _seed_user(async_client)
        worker = await _register_device(async_client, headers, name="w", role="worker")
        controller = await _register_device(
            async_client, headers, name="c", role="controller", platform="linux"
        )

        payload = {
            "worker_id": worker["device_id"],
            "controller_id": controller["device_id"],
        }

        resp1 = await async_client.post("/pair/initiate", json=payload, headers=headers)
        assert resp1.status_code == 200

        resp2 = await async_client.post("/pair/initiate", json=payload, headers=headers)
        assert resp2.status_code == 409

    async def test_pairing_stores_certs(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Verify DB record contains CA cert, worker cert, controller cert, and encrypted fields."""
        _, headers = await _seed_user(async_client)
        worker = await _register_device(async_client, headers, name="w", role="worker")
        controller = await _register_device(
            async_client, headers, name="c", role="controller", platform="linux"
        )

        resp = await async_client.post(
            "/pair/initiate",
            json={
                "worker_id": worker["device_id"],
                "controller_id": controller["device_id"],
            },
            headers=headers,
        )
        assert resp.status_code == 200
        pairing_id = resp.json()["pairing_id"]

        # Query the DB for the pairing record
        app = async_client._transport.app  # type: ignore[union-attr]
        override_get_db = app.dependency_overrides[get_db]
        session_gen = override_get_db()
        db = await session_gen.__anext__()

        result = await db.execute(
            select(Pairing).where(Pairing.id == uuid.UUID(pairing_id))
        )
        pairing = result.scalar_one()

        assert pairing.ca_cert is not None
        assert pairing.ca_cert.startswith("-----BEGIN CERTIFICATE-----")
        assert pairing.worker_cert is not None
        assert pairing.worker_cert.startswith("-----BEGIN CERTIFICATE-----")
        assert pairing.controller_cert is not None
        assert pairing.controller_cert.startswith("-----BEGIN CERTIFICATE-----")
        # Private keys are encrypted (JSON), not raw PEM
        assert pairing.worker_key is not None
        assert '"v"' in pairing.worker_key
        assert pairing.controller_key is not None
        assert '"v"' in pairing.controller_key
        # HMAC secret is also encrypted
        assert pairing.hmac_secret is not None
        assert '"v"' in pairing.hmac_secret

        try:
            await session_gen.__anext__()
        except StopAsyncIteration:
            pass


async def _create_pairing(
    ac: httpx.AsyncClient,
    headers: dict,
) -> tuple[str, str, str]:
    """Create a worker+controller and initiate pairing.

    Returns (pairing_id, worker_device_token, controller_device_token).
    """
    worker = await _register_device(ac, headers, name="w", role="worker")
    controller = await _register_device(
        ac, headers, name="c", role="controller", platform="linux"
    )
    resp = await ac.post(
        "/pair/initiate",
        json={
            "worker_id": worker["device_id"],
            "controller_id": controller["device_id"],
        },
        headers=headers,
    )
    assert resp.status_code == 200
    return resp.json()["pairing_id"], worker["device_token"], controller["device_token"]


class TestCredentialDistribution:
    """GET /pair/{pairing_id}/credentials/worker and /controller tests."""

    async def test_worker_credentials(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Worker fetches its credentials successfully."""
        _, headers = await _seed_user(async_client)
        pairing_id, worker_token, _ = await _create_pairing(async_client, headers)

        resp = await async_client.get(
            f"/pair/{pairing_id}/credentials/worker",
            headers={"Authorization": f"Bearer {worker_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ca_cert"].startswith("-----BEGIN CERTIFICATE-----")
        assert data["cert"].startswith("-----BEGIN CERTIFICATE-----")
        assert data["private_key"].startswith("-----BEGIN PRIVATE KEY-----")
        # HMAC secret is base64-encoded 32 bytes
        hmac_bytes = base64.b64decode(data["hmac_secret"])
        assert len(hmac_bytes) == 32

    async def test_controller_credentials(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Controller fetches its credentials successfully."""
        _, headers = await _seed_user(async_client)
        pairing_id, _, controller_token = await _create_pairing(async_client, headers)

        resp = await async_client.get(
            f"/pair/{pairing_id}/credentials/controller",
            headers={"Authorization": f"Bearer {controller_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ca_cert"].startswith("-----BEGIN CERTIFICATE-----")
        assert data["cert"].startswith("-----BEGIN CERTIFICATE-----")
        assert data["private_key"].startswith("-----BEGIN PRIVATE KEY-----")

    async def test_credentials_wrong_token(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Invalid device token returns 401."""
        _, headers = await _seed_user(async_client)
        pairing_id, _, _ = await _create_pairing(async_client, headers)

        resp = await async_client.get(
            f"/pair/{pairing_id}/credentials/worker",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    async def test_credentials_wrong_role(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Controller token on worker endpoint returns 401."""
        _, headers = await _seed_user(async_client)
        pairing_id, _, controller_token = await _create_pairing(async_client, headers)

        resp = await async_client.get(
            f"/pair/{pairing_id}/credentials/worker",
            headers={"Authorization": f"Bearer {controller_token}"},
        )
        assert resp.status_code == 401

    async def test_credentials_pairing_not_found(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Nonexistent pairing_id returns 404."""
        _, headers = await _seed_user(async_client)
        # Register a device to get a valid token
        worker = await _register_device(async_client, headers, name="w", role="worker")

        resp = await async_client.get(
            f"/pair/{uuid.uuid4()}/credentials/worker",
            headers={"Authorization": f"Bearer {worker['device_token']}"},
        )
        assert resp.status_code == 404


async def _create_pairing_with_addresses(
    ac: httpx.AsyncClient,
    headers: dict,
) -> tuple[str, str, str, str]:
    """Create a paired worker+controller where worker has sent a heartbeat.

    Returns (pairing_id, worker_token, controller_token, worker_device_id).
    """
    worker = await _register_device(ac, headers, name="w", role="worker")
    controller = await _register_device(
        ac, headers, name="c", role="controller", platform="linux"
    )

    # Send heartbeat to set worker addresses
    await ac.post(
        f"/devices/{worker['device_id']}/heartbeat",
        json={"addresses": {"local_ip": "192.168.1.10", "tailscale_ip": "100.64.0.1"}},
        headers={"Authorization": f"Bearer {worker['device_token']}"},
    )
    # Send heartbeat to set controller addresses
    await ac.post(
        f"/devices/{controller['device_id']}/heartbeat",
        json={"addresses": {"local_ip": "10.0.0.5"}},
        headers={"Authorization": f"Bearer {controller['device_token']}"},
    )

    resp = await ac.post(
        "/pair/initiate",
        json={
            "worker_id": worker["device_id"],
            "controller_id": controller["device_id"],
        },
        headers=headers,
    )
    assert resp.status_code == 200
    return (
        resp.json()["pairing_id"],
        worker["device_token"],
        controller["device_token"],
        worker["device_id"],
    )


class TestAddressExchange:
    """GET /pair/{pairing_id}/addresses tests."""

    async def test_worker_gets_controller_addresses(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Worker gets the controller's addresses."""
        _, headers = await _seed_user(async_client)
        pairing_id, worker_token, _, _ = await _create_pairing_with_addresses(
            async_client, headers
        )

        resp = await async_client.get(
            f"/pair/{pairing_id}/addresses",
            headers={"Authorization": f"Bearer {worker_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["addresses"]["local_ip"] == "10.0.0.5"

    async def test_controller_gets_worker_addresses(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Controller gets the worker's addresses."""
        _, headers = await _seed_user(async_client)
        pairing_id, _, controller_token, _ = await _create_pairing_with_addresses(
            async_client, headers
        )

        resp = await async_client.get(
            f"/pair/{pairing_id}/addresses",
            headers={"Authorization": f"Bearer {controller_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["addresses"]["local_ip"] == "192.168.1.10"
        assert data["addresses"]["tailscale_ip"] == "100.64.0.1"

    async def test_addresses_wrong_token(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Invalid device token returns 401."""
        _, headers = await _seed_user(async_client)
        pairing_id, _, _, _ = await _create_pairing_with_addresses(
            async_client, headers
        )

        resp = await async_client.get(
            f"/pair/{pairing_id}/addresses",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401


class TestRotation:
    """POST /pair/{pairing_id}/rotate tests."""

    async def test_rotate_hmac(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Happy path: rotate HMAC secret, new differs from old."""
        _, headers = await _seed_user(async_client)
        pairing_id, worker_token, _ = await _create_pairing(async_client, headers)

        # Get original secret
        creds_before = await async_client.get(
            f"/pair/{pairing_id}/credentials/worker",
            headers={"Authorization": f"Bearer {worker_token}"},
        )
        original_secret = creds_before.json()["hmac_secret"]

        # Rotate
        resp = await async_client.post(
            f"/pair/{pairing_id}/rotate", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rotated"

        # Get new secret
        creds_after = await async_client.get(
            f"/pair/{pairing_id}/credentials/worker",
            headers={"Authorization": f"Bearer {worker_token}"},
        )
        new_secret = creds_after.json()["hmac_secret"]
        assert new_secret != original_secret

    async def test_rotate_grace_period(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """After rotation, credential endpoint returns both secrets."""
        _, headers = await _seed_user(async_client)
        pairing_id, worker_token, _ = await _create_pairing(async_client, headers)

        # Get original secret
        creds_before = await async_client.get(
            f"/pair/{pairing_id}/credentials/worker",
            headers={"Authorization": f"Bearer {worker_token}"},
        )
        original_secret = creds_before.json()["hmac_secret"]
        assert creds_before.json()["prev_hmac_secret"] is None

        # Rotate
        await async_client.post(f"/pair/{pairing_id}/rotate", headers=headers)

        # During grace period, prev_hmac_secret should be the original
        creds_after = await async_client.get(
            f"/pair/{pairing_id}/credentials/worker",
            headers={"Authorization": f"Bearer {worker_token}"},
        )
        data = creds_after.json()
        assert data["prev_hmac_secret"] == original_secret
        assert data["hmac_secret"] != original_secret

    async def test_rotate_requires_auth(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """No JWT returns 401."""
        _, headers = await _seed_user(async_client)
        pairing_id, _, _ = await _create_pairing(async_client, headers)

        resp = await async_client.post(f"/pair/{pairing_id}/rotate")
        assert resp.status_code in (401, 403)

    async def test_rotate_wrong_owner(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Different user cannot rotate another user's pairing."""
        _, headers_a = await _seed_user(async_client, username="owner")
        pairing_id, _, _ = await _create_pairing(async_client, headers_a)

        _, headers_b = await _seed_user(
            async_client, github_id=77777, username="intruder"
        )
        resp = await async_client.post(
            f"/pair/{pairing_id}/rotate", headers=headers_b
        )
        assert resp.status_code == 403


class TestRevocation:
    """POST /pair/{pairing_id}/revoke tests."""

    async def test_revoke_pairing(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Happy path: revoke sets status to 'revoked'."""
        _, headers = await _seed_user(async_client)
        pairing_id, _, _ = await _create_pairing(async_client, headers)

        resp = await async_client.post(
            f"/pair/{pairing_id}/revoke", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "revoked"

    async def test_revoked_pairing_credentials_fail(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Credential fetch on revoked pairing returns 404."""
        _, headers = await _seed_user(async_client)
        pairing_id, worker_token, _ = await _create_pairing(async_client, headers)

        # Revoke
        await async_client.post(f"/pair/{pairing_id}/revoke", headers=headers)

        # Try to fetch credentials
        resp = await async_client.get(
            f"/pair/{pairing_id}/credentials/worker",
            headers={"Authorization": f"Bearer {worker_token}"},
        )
        assert resp.status_code == 404

    async def test_revoke_requires_auth(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """No JWT returns 401."""
        _, headers = await _seed_user(async_client)
        pairing_id, _, _ = await _create_pairing(async_client, headers)

        resp = await async_client.post(f"/pair/{pairing_id}/revoke")
        assert resp.status_code in (401, 403)
