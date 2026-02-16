"""Per-pairing crypto: CA generation, HMAC secret encryption.

CA generation: creates an ephemeral Certificate Authority, signs worker
and controller certificates, then destroys the CA private key.

HMAC secrets: generates 256-bit secrets and encrypts them with
AES-256-GCM before database storage.
"""

import base64
import gc
import ipaddress
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# Validity periods
_CA_VALIDITY_DAYS = 3650  # ~10 years
_CERT_VALIDITY_DAYS = 365  # 1 year


@dataclass(frozen=True)
class PairingCerts:
    """Certificates generated for a device pairing.

    The CA private key is NOT included - it was destroyed after signing.
    """

    ca_cert_pem: str
    worker_cert_pem: str
    worker_key_pem: str
    controller_cert_pem: str
    controller_key_pem: str


def _parse_san_entries(addresses: list[str]) -> list[x509.GeneralName]:
    """Convert address strings to x509 SAN entries.

    IP addresses become x509.IPAddress, everything else becomes
    x509.DNSName. Always includes localhost and 127.0.0.1.
    """
    seen_ips: set[str] = {"127.0.0.1"}
    seen_dns: set[str] = {"localhost"}
    entries: list[x509.GeneralName] = []

    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
            if str(ip) not in seen_ips:
                seen_ips.add(str(ip))
        except ValueError:
            if addr not in seen_dns:
                seen_dns.add(addr)

    # Build entries in a stable order: IPs then DNS names
    for ip_str in sorted(seen_ips):
        entries.append(x509.IPAddress(ipaddress.ip_address(ip_str)))
    for dns in sorted(seen_dns):
        entries.append(x509.DNSName(dns))

    return entries


def generate_pairing_certs(
    pairing_id: uuid.UUID,
    worker_addresses: list[str],
) -> PairingCerts:
    """Generate per-pairing CA and signed certificates for mTLS.

    Creates a 4096-bit RSA CA, signs a server cert for the worker and
    a client cert for the controller, then destroys the CA private key.

    Args:
        pairing_id: UUID of the pairing record (used in CA CN).
        worker_addresses: Network addresses for worker SAN entries.
            IP strings become IPAddress SANs; others become DNSName SANs.

    Returns:
        PairingCerts with CA public cert, worker cert+key, controller cert+key.
    """
    now = datetime.now(timezone.utc)

    # -- 1. Generate and self-sign CA --
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"Byfrost Pairing CA {pairing_id}"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Byfrost"),
    ])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=_CA_VALIDITY_DAYS))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                key_cert_sign=True,
                crl_sign=True,
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # -- 2. Generate worker cert (server role) --
    worker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san_entries = _parse_san_entries(worker_addresses)
    worker_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "byfrost-worker"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Byfrost"),
        ]))
        .issuer_name(ca_name)
        .public_key(worker_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=_CERT_VALIDITY_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(san_entries), critical=False
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                key_cert_sign=False,
                crl_sign=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # -- 3. Generate controller cert (client role) --
    controller_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    )
    controller_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "byfrost-controller"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Byfrost"),
        ]))
        .issuer_name(ca_name)
        .public_key(controller_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=_CERT_VALIDITY_DAYS))
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                key_cert_sign=False,
                crl_sign=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # -- 4. Destroy CA private key --
    del ca_key
    gc.collect()

    # -- 5. Serialize and return --
    return PairingCerts(
        ca_cert_pem=ca_cert.public_bytes(serialization.Encoding.PEM).decode(),
        worker_cert_pem=worker_cert.public_bytes(
            serialization.Encoding.PEM
        ).decode(),
        worker_key_pem=worker_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        controller_cert_pem=controller_cert.public_bytes(
            serialization.Encoding.PEM
        ).decode(),
        controller_key_pem=controller_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )


# ---------------------------------------------------------------------------
# HMAC secret generation and AES-256-GCM encryption
# ---------------------------------------------------------------------------


def generate_hmac_secret() -> bytes:
    """Generate a 256-bit (32-byte) random HMAC secret."""
    return secrets.token_bytes(32)


def encrypt_secret(plaintext: bytes, key_b64: str) -> str:
    """Encrypt bytes with AES-256-GCM, return JSON string for DB storage.

    Args:
        plaintext: The secret bytes to encrypt.
        key_b64: Base64-encoded 32-byte encryption key.

    Returns:
        JSON string containing version, nonce, and ciphertext (all b64).
    """
    key = base64.b64decode(key_b64)
    if len(key) != 32:
        raise ValueError("Encryption key must be exactly 32 bytes")

    nonce = os.urandom(12)  # 96-bit nonce for GCM
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)

    return json.dumps({
        "v": 1,
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    })


def decrypt_secret(encrypted_json: str, key_b64: str) -> bytes:
    """Decrypt an AES-256-GCM encrypted secret from its JSON representation.

    Args:
        encrypted_json: JSON string produced by encrypt_secret().
        key_b64: Base64-encoded 32-byte encryption key.

    Returns:
        Original plaintext bytes.
    """
    key = base64.b64decode(key_b64)
    data = json.loads(encrypted_json)

    nonce = base64.b64decode(data["nonce"])
    ciphertext = base64.b64decode(data["ciphertext"])

    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None)
