"""Tests for SQLAlchemy models (pure Python, no database required)."""

import uuid

from app.models import Device, Pairing, User


class TestUserModel:
    """User model instantiation."""

    def test_create_user(self) -> None:
        user = User(
            id=uuid.uuid4(),
            github_id=12345,
            github_username="testuser",
            email="test@example.com",
        )
        assert user.github_id == 12345
        assert user.github_username == "testuser"
        assert user.email == "test@example.com"

    def test_user_email_optional(self) -> None:
        user = User(
            id=uuid.uuid4(),
            github_id=12345,
            github_username="testuser",
        )
        assert user.email is None


class TestDeviceModel:
    """Device model instantiation."""

    def test_create_worker(self) -> None:
        device = Device(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            name="macbook-pro",
            role="worker",
            platform="macos",
            device_token="$2b$12$hashed",
        )
        assert device.role == "worker"
        assert device.platform == "macos"

    def test_create_controller(self) -> None:
        device = Device(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            name="linux-desktop",
            role="controller",
            platform="linux",
            device_token="$2b$12$hashed",
        )
        assert device.role == "controller"
        assert device.platform == "linux"

    def test_addresses_json(self) -> None:
        device = Device(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            name="test-host",
            role="worker",
            platform="macos",
            device_token="$2b$12$hashed",
            addresses={"local_ip": "192.168.1.10", "port": 9784},
        )
        assert device.addresses["local_ip"] == "192.168.1.10"

    def test_last_heartbeat_optional(self) -> None:
        device = Device(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            name="test-host",
            role="worker",
            platform="macos",
            device_token="$2b$12$hashed",
        )
        assert device.last_heartbeat is None


class TestPairingModel:
    """Pairing model instantiation."""

    def test_create_pairing(self) -> None:
        pairing = Pairing(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            worker_id=uuid.uuid4(),
            controller_id=uuid.uuid4(),
            ca_cert="-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----",
            hmac_secret="encrypted-secret-data",
            status="active",
        )
        assert pairing.status == "active"
        assert pairing.ca_cert is not None

    def test_status_defaults_at_db_level(self) -> None:
        """Status server_default is 'active'; pre-flush it is None."""
        pairing = Pairing(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            worker_id=uuid.uuid4(),
            controller_id=uuid.uuid4(),
        )
        # server_default applies at INSERT time, not in Python
        assert pairing.status is None

    def test_revoked_status(self) -> None:
        pairing = Pairing(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            worker_id=uuid.uuid4(),
            controller_id=uuid.uuid4(),
            status="revoked",
        )
        assert pairing.status == "revoked"
