"""Tests for per-pairing CA certificate generation."""

import ipaddress
import uuid

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.pairing.crypto import PairingCerts, _parse_san_entries, generate_pairing_certs


@pytest.fixture()
def sample_certs() -> PairingCerts:
    """Generate a set of pairing certs for testing."""
    return generate_pairing_certs(
        pairing_id=uuid.uuid4(),
        worker_addresses=["192.168.1.10", "100.64.0.1", "myhost.local"],
    )


def _load_cert(pem: str) -> x509.Certificate:
    """Load a PEM certificate string."""
    return x509.load_pem_x509_certificate(pem.encode())


class TestGeneratePairingCerts:
    """Certificate generation and chain validation."""

    def test_returns_all_pem_fields(self, sample_certs: PairingCerts) -> None:
        """All five PEM fields are present and correctly formatted."""
        assert sample_certs.ca_cert_pem.startswith("-----BEGIN CERTIFICATE-----")
        assert sample_certs.worker_cert_pem.startswith("-----BEGIN CERTIFICATE-----")
        assert sample_certs.controller_cert_pem.startswith("-----BEGIN CERTIFICATE-----")
        assert sample_certs.worker_key_pem.startswith("-----BEGIN PRIVATE KEY-----")
        assert sample_certs.controller_key_pem.startswith("-----BEGIN PRIVATE KEY-----")

    def test_ca_is_self_signed(self, sample_certs: PairingCerts) -> None:
        """CA certificate is self-signed with BasicConstraints ca=True."""
        ca = _load_cert(sample_certs.ca_cert_pem)
        assert ca.issuer == ca.subject

        bc = ca.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.value.ca is True
        assert bc.value.path_length == 0

    def test_worker_cert_chains_to_ca(self, sample_certs: PairingCerts) -> None:
        """Worker cert issuer matches the CA subject."""
        ca = _load_cert(sample_certs.ca_cert_pem)
        worker = _load_cert(sample_certs.worker_cert_pem)
        assert worker.issuer == ca.subject

        # Verify signature using CA public key
        ca.public_key().verify(  # type: ignore[union-attr]
            worker.signature,
            worker.tbs_certificate_bytes,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

    def test_controller_cert_chains_to_ca(
        self, sample_certs: PairingCerts
    ) -> None:
        """Controller cert issuer matches the CA subject."""
        ca = _load_cert(sample_certs.ca_cert_pem)
        controller = _load_cert(sample_certs.controller_cert_pem)
        assert controller.issuer == ca.subject

        # Verify signature using CA public key
        ca.public_key().verify(  # type: ignore[union-attr]
            controller.signature,
            controller.tbs_certificate_bytes,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

    def test_worker_cert_has_correct_sans(
        self, sample_certs: PairingCerts
    ) -> None:
        """Worker cert SANs include input addresses plus localhost/127.0.0.1."""
        worker = _load_cert(sample_certs.worker_cert_pem)
        san = worker.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        ips = san.value.get_values_for_type(x509.IPAddress)
        dns_names = san.value.get_values_for_type(x509.DNSName)

        # Input IPs + default 127.0.0.1
        assert ipaddress.ip_address("192.168.1.10") in ips
        assert ipaddress.ip_address("100.64.0.1") in ips
        assert ipaddress.ip_address("127.0.0.1") in ips

        # Input DNS + default localhost
        assert "myhost.local" in dns_names
        assert "localhost" in dns_names

    def test_worker_has_server_auth_eku(
        self, sample_certs: PairingCerts
    ) -> None:
        """Worker cert has ExtendedKeyUsage with serverAuth."""
        worker = _load_cert(sample_certs.worker_cert_pem)
        eku = worker.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        assert ExtendedKeyUsageOID.SERVER_AUTH in eku.value

    def test_controller_has_client_auth_eku(
        self, sample_certs: PairingCerts
    ) -> None:
        """Controller cert has ExtendedKeyUsage with clientAuth."""
        controller = _load_cert(sample_certs.controller_cert_pem)
        eku = controller.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage
        )
        assert ExtendedKeyUsageOID.CLIENT_AUTH in eku.value

    def test_cert_validity_periods(self, sample_certs: PairingCerts) -> None:
        """CA has ~10yr validity, worker/controller have ~1yr validity."""
        ca = _load_cert(sample_certs.ca_cert_pem)
        worker = _load_cert(sample_certs.worker_cert_pem)
        controller = _load_cert(sample_certs.controller_cert_pem)

        ca_days = (
            ca.not_valid_after_utc - ca.not_valid_before_utc
        ).days
        assert 3649 <= ca_days <= 3651

        worker_days = (
            worker.not_valid_after_utc - worker.not_valid_before_utc
        ).days
        assert 364 <= worker_days <= 366

        controller_days = (
            controller.not_valid_after_utc - controller.not_valid_before_utc
        ).days
        assert 364 <= controller_days <= 366

    def test_ca_key_not_in_result(self, sample_certs: PairingCerts) -> None:
        """Result contains exactly 2 private keys (worker + controller), not CA."""
        pem_fields = [
            sample_certs.ca_cert_pem,
            sample_certs.worker_cert_pem,
            sample_certs.worker_key_pem,
            sample_certs.controller_cert_pem,
            sample_certs.controller_key_pem,
        ]
        private_key_count = sum(
            1 for f in pem_fields if "BEGIN PRIVATE KEY" in f
        )
        assert private_key_count == 2

        # CA field is a certificate, not a private key
        assert "BEGIN PRIVATE KEY" not in sample_certs.ca_cert_pem

    def test_empty_addresses(self) -> None:
        """Empty address list still generates valid certs with localhost SANs."""
        certs = generate_pairing_certs(
            pairing_id=uuid.uuid4(), worker_addresses=[]
        )
        worker = _load_cert(certs.worker_cert_pem)
        san = worker.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        ips = san.value.get_values_for_type(x509.IPAddress)
        dns_names = san.value.get_values_for_type(x509.DNSName)

        assert ipaddress.ip_address("127.0.0.1") in ips
        assert "localhost" in dns_names

    def test_pairing_id_in_ca_subject(self) -> None:
        """Pairing UUID appears in the CA's Common Name."""
        pid = uuid.uuid4()
        certs = generate_pairing_certs(pairing_id=pid, worker_addresses=[])
        ca = _load_cert(certs.ca_cert_pem)
        cn = ca.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        assert str(pid) in cn

    def test_worker_key_matches_cert(self, sample_certs: PairingCerts) -> None:
        """Worker private key corresponds to the worker cert's public key."""
        worker = _load_cert(sample_certs.worker_cert_pem)
        key = load_pem_private_key(sample_certs.worker_key_pem.encode(), None)
        assert key.public_key().public_bytes(  # type: ignore[union-attr]
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ) == worker.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def test_controller_key_matches_cert(
        self, sample_certs: PairingCerts
    ) -> None:
        """Controller private key corresponds to the controller cert's public key."""
        controller = _load_cert(sample_certs.controller_cert_pem)
        key = load_pem_private_key(
            sample_certs.controller_key_pem.encode(), None
        )
        assert key.public_key().public_bytes(  # type: ignore[union-attr]
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ) == controller.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )


class TestParseSanEntries:
    """SAN entry parsing from address strings."""

    def test_ipv4_address(self) -> None:
        """IPv4 string becomes x509.IPAddress."""
        entries = _parse_san_entries(["10.0.0.1"])
        ip_entries = [
            e for e in entries if isinstance(e, x509.IPAddress)
        ]
        ip_values = [e.value for e in ip_entries]
        assert ipaddress.ip_address("10.0.0.1") in ip_values

    def test_dns_name(self) -> None:
        """Hostname string becomes x509.DNSName."""
        entries = _parse_san_entries(["myhost.local"])
        dns_entries = [
            e for e in entries if isinstance(e, x509.DNSName)
        ]
        dns_values = [e.value for e in dns_entries]
        assert "myhost.local" in dns_values

    def test_always_includes_localhost(self) -> None:
        """localhost and 127.0.0.1 are always present regardless of input."""
        entries = _parse_san_entries([])
        ip_entries = [
            e for e in entries if isinstance(e, x509.IPAddress)
        ]
        dns_entries = [
            e for e in entries if isinstance(e, x509.DNSName)
        ]
        ip_values = [e.value for e in ip_entries]
        dns_values = [e.value for e in dns_entries]

        assert ipaddress.ip_address("127.0.0.1") in ip_values
        assert "localhost" in dns_values
