"""Tests for device registration, listing, heartbeat, and deletion."""

import random
import uuid
from unittest.mock import patch

import fakeredis.aioredis
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.auth.jwt import create_access_token
from app.config import get_settings
from app.database import get_db
from app.main import create_app
from app.models import User


@pytest.fixture()
async def async_client():
    """Async test client with transaction rollback for isolation."""
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    test_engine = create_async_engine(get_settings().database_url)
    conn = await test_engine.connect()
    txn = await conn.begin()

    async def override_get_db():
        # Bind session to the existing connection so all ops share the txn
        async with AsyncSession(bind=conn, expire_on_commit=False) as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    with patch("app.rate_limit.get_redis", return_value=fake_redis):
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
    username: str = "testdeviceuser",
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
    """Helper to register a device and return the response JSON."""
    resp = await ac.post(
        "/devices/register",
        json={"name": name, "role": role, "platform": platform},
        headers=headers,
    )
    assert resp.status_code == 200
    return resp.json()


class TestRegister:
    """Device registration."""

    async def test_register_device(self, async_client: httpx.AsyncClient) -> None:
        """POST /devices/register returns device_id and device_token."""
        _, headers = await _seed_user(async_client)
        data = await _register_device(async_client, headers)
        assert "device_id" in data
        assert "device_token" in data
        assert len(data["device_token"]) > 20

    async def test_register_requires_auth(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """POST /devices/register without JWT returns 401."""
        resp = await async_client.post(
            "/devices/register",
            json={"name": "test", "role": "worker", "platform": "macos"},
        )
        assert resp.status_code == 401

    async def test_register_invalid_role(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Invalid role returns 422."""
        _, headers = await _seed_user(async_client)
        resp = await async_client.post(
            "/devices/register",
            json={"name": "test", "role": "invalid", "platform": "macos"},
            headers=headers,
        )
        assert resp.status_code == 422

    async def test_register_invalid_platform(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Invalid platform returns 422."""
        _, headers = await _seed_user(async_client)
        resp = await async_client.post(
            "/devices/register",
            json={"name": "test", "role": "worker", "platform": "bsd"},
            headers=headers,
        )
        assert resp.status_code == 422


class TestListDevices:
    """Device listing."""

    async def test_list_devices(self, async_client: httpx.AsyncClient) -> None:
        """GET /devices returns registered devices."""
        _, headers = await _seed_user(async_client)
        await _register_device(async_client, headers, name="mac-1")
        await _register_device(
            async_client, headers, name="linux-1", role="controller", platform="linux"
        )

        resp = await async_client.get("/devices/", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        names = {d["name"] for d in data}
        assert names == {"mac-1", "linux-1"}

    async def test_list_devices_empty(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """GET /devices with no devices returns empty list."""
        _, headers = await _seed_user(async_client)
        resp = await async_client.get("/devices/", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == []


class TestDeleteDevice:
    """Device deletion."""

    async def test_delete_device(self, async_client: httpx.AsyncClient) -> None:
        """DELETE /devices/{id} removes the device."""
        _, headers = await _seed_user(async_client)
        reg = await _register_device(async_client, headers)
        device_id = reg["device_id"]

        resp = await async_client.delete(f"/devices/{device_id}", headers=headers)
        assert resp.status_code == 204

        # Verify it's gone
        resp = await async_client.get("/devices/", headers=headers)
        assert resp.json() == []

    async def test_delete_other_users_device(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Cannot delete a device owned by another user."""
        _, headers_a = await _seed_user(async_client)
        reg = await _register_device(async_client, headers_a)
        device_id = reg["device_id"]

        # Create a second user
        _, headers_b = await _seed_user(
            async_client, github_id=88888, username="otheruser"
        )

        resp = await async_client.delete(f"/devices/{device_id}", headers=headers_b)
        assert resp.status_code == 404


class TestHeartbeat:
    """Device heartbeat."""

    async def test_heartbeat(self, async_client: httpx.AsyncClient) -> None:
        """POST /devices/{id}/heartbeat updates addresses."""
        _, headers = await _seed_user(async_client)
        reg = await _register_device(async_client, headers)
        device_id = reg["device_id"]
        device_token = reg["device_token"]

        addresses = {
            "local_ip": "192.168.1.10",
            "tailscale_ip": "100.64.0.1",
            "public_ip": "1.2.3.4",
            "port": 9090,
        }
        resp = await async_client.post(
            f"/devices/{device_id}/heartbeat",
            json={"addresses": addresses},
            headers={"Authorization": f"Bearer {device_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Verify addresses were stored
        resp = await async_client.get("/devices/", headers=headers)
        devices = resp.json()
        assert len(devices) == 1
        assert devices[0]["addresses"] == addresses
        assert devices[0]["last_heartbeat"] is not None

    async def test_heartbeat_wrong_token(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Wrong device token returns 401."""
        _, headers = await _seed_user(async_client)
        reg = await _register_device(async_client, headers)
        device_id = reg["device_id"]

        resp = await async_client.post(
            f"/devices/{device_id}/heartbeat",
            json={"addresses": {"local_ip": "10.0.0.1"}},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401
