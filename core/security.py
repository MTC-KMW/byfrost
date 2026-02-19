#!/usr/bin/env python3
"""
Byfrost Security Module

Shared security primitives for the daemon and CLI:
- TLS certificate generation and loading (self-signed CA)
- HMAC-SHA256 message signing with timestamp-based replay protection
- Prompt sanitization against shell injection
- Rate limiting on authentication failures
- Structured audit logging
- Secret rotation

Both the daemon and byfrost CLI import from this module.
"""

import hashlib
import hmac
import json
import logging
import re
import secrets
import shlex
import subprocess
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Tuple

# Path constants (canonical definitions in core.config, re-exported here)
from core.config import BRIDGE_DIR, CERTS_DIR, LOG_DIR, SECRET_FILE, SECRET_HISTORY_FILE

# ---------------------------------------------------------------------------
# 1. TLS Certificate Management
# ---------------------------------------------------------------------------

class TLSManager:
    """
    Generates and manages a self-signed CA and server/client certificates
    for mutual TLS (mTLS) authentication on the WebSocket connection.

    Certificate layout:
      ~/.byfrost/certs/
        ca.key          CA private key
        ca.pem          CA certificate (copied to both machines)
        server.key      Server private key (Mac only)
        server.pem      Server certificate (Mac only)
        client.key      Client private key (Linux only)
        client.pem      Client certificate (Linux only)
    """

    CA_KEY = CERTS_DIR / "ca.key"
    CA_CERT = CERTS_DIR / "ca.pem"
    SERVER_KEY = CERTS_DIR / "server.key"
    SERVER_CERT = CERTS_DIR / "server.pem"
    CLIENT_KEY = CERTS_DIR / "client.key"
    CLIENT_CERT = CERTS_DIR / "client.pem"

    # Certificate validity in days
    CA_VALIDITY = 3650      # 10 years
    CERT_VALIDITY = 365     # 1 year

    @classmethod
    def ensure_dirs(cls):
        CERTS_DIR.mkdir(parents=True, exist_ok=True)
        CERTS_DIR.chmod(0o700)

    @classmethod
    def has_ca(cls) -> bool:
        return cls.CA_KEY.exists() and cls.CA_CERT.exists()

    @classmethod
    def has_server_certs(cls) -> bool:
        return cls.SERVER_KEY.exists() and cls.SERVER_CERT.exists()

    @classmethod
    def has_client_certs(cls) -> bool:
        return cls.CLIENT_KEY.exists() and cls.CLIENT_CERT.exists()

    @classmethod
    def generate_ca(cls) -> bool:
        """Generate a self-signed Certificate Authority."""
        cls.ensure_dirs()
        try:
            # Generate CA private key
            subprocess.run([
                "openssl", "genrsa", "-out", str(cls.CA_KEY), "4096"
            ], check=True, capture_output=True)
            cls.CA_KEY.chmod(0o600)

            # Generate CA certificate
            subprocess.run([
                "openssl", "req", "-new", "-x509",
                "-key", str(cls.CA_KEY),
                "-out", str(cls.CA_CERT),
                "-days", str(cls.CA_VALIDITY),
                "-subj", "/CN=Byfrost CA/O=Byfrost/OU=Security"
            ], check=True, capture_output=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    @classmethod
    def generate_server_cert(cls, hostname: str = "localhost") -> bool:
        """Generate server certificate signed by the CA."""
        if not cls.has_ca():
            return False
        cls.ensure_dirs()

        # Create SAN config for the server cert
        san_config = CERTS_DIR / "server_san.cnf"
        san_config.write_text(
            f"[req]\n"
            f"distinguished_name = req_distinguished_name\n"
            f"req_extensions = v3_req\n"
            f"prompt = no\n"
            f"\n"
            f"[req_distinguished_name]\n"
            f"CN = {hostname}\n"
            f"O = Byfrost\n"
            f"OU = Server\n"
            f"\n"
            f"[v3_req]\n"
            f"subjectAltName = DNS:{hostname},DNS:localhost,IP:127.0.0.1\n"
            f"keyUsage = digitalSignature, keyEncipherment\n"
            f"extendedKeyUsage = serverAuth\n"
        )

        try:
            # Generate server key
            subprocess.run([
                "openssl", "genrsa", "-out", str(cls.SERVER_KEY), "2048"
            ], check=True, capture_output=True)
            cls.SERVER_KEY.chmod(0o600)

            # Generate CSR
            csr = CERTS_DIR / "server.csr"
            subprocess.run([
                "openssl", "req", "-new",
                "-key", str(cls.SERVER_KEY),
                "-out", str(csr),
                "-config", str(san_config)
            ], check=True, capture_output=True)

            # Sign with CA
            subprocess.run([
                "openssl", "x509", "-req",
                "-in", str(csr),
                "-CA", str(cls.CA_CERT),
                "-CAkey", str(cls.CA_KEY),
                "-CAcreateserial",
                "-out", str(cls.SERVER_CERT),
                "-days", str(cls.CERT_VALIDITY),
                "-extensions", "v3_req",
                "-extfile", str(san_config)
            ], check=True, capture_output=True)

            # Cleanup temp files
            csr.unlink(missing_ok=True)
            san_config.unlink(missing_ok=True)
            (CERTS_DIR / "ca.srl").unlink(missing_ok=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    @classmethod
    def generate_client_cert(cls) -> bool:
        """Generate client certificate signed by the CA."""
        if not cls.has_ca():
            return False
        cls.ensure_dirs()

        try:
            # Generate client key
            subprocess.run([
                "openssl", "genrsa", "-out", str(cls.CLIENT_KEY), "2048"
            ], check=True, capture_output=True)
            cls.CLIENT_KEY.chmod(0o600)

            # Generate CSR
            csr = CERTS_DIR / "client.csr"
            subprocess.run([
                "openssl", "req", "-new",
                "-key", str(cls.CLIENT_KEY),
                "-out", str(csr),
                "-subj", "/CN=Byfrost Client/O=Byfrost/OU=Client"
            ], check=True, capture_output=True)

            # Sign with CA
            subprocess.run([
                "openssl", "x509", "-req",
                "-in", str(csr),
                "-CA", str(cls.CA_CERT),
                "-CAkey", str(cls.CA_KEY),
                "-CAcreateserial",
                "-out", str(cls.CLIENT_CERT),
                "-days", str(cls.CERT_VALIDITY),
                "-extfile", "/dev/stdin"
            ], input=b"keyUsage = digitalSignature\nextendedKeyUsage = clientAuth\n",
                check=True, capture_output=True)

            csr.unlink(missing_ok=True)
            (CERTS_DIR / "ca.srl").unlink(missing_ok=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    @classmethod
    def get_server_ssl_context(cls):
        """Create SSL context for the daemon (server side with client verification)."""
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(str(cls.SERVER_CERT), str(cls.SERVER_KEY))
        ctx.load_verify_locations(str(cls.CA_CERT))
        ctx.verify_mode = ssl.CERT_REQUIRED  # mTLS: require client cert
        ctx.check_hostname = False  # We verify via CA, not hostname
        return ctx

    @classmethod
    def get_client_ssl_context(cls):
        """Create SSL context for the CLI (client side)."""
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(str(cls.CLIENT_CERT), str(cls.CLIENT_KEY))
        ctx.load_verify_locations(str(cls.CA_CERT))
        ctx.check_hostname = False  # Self-signed, verify via CA trust
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    @classmethod
    def get_local_ssl_context(cls):
        """Create SSL context for local CLI on the worker machine.

        Uses the server cert as client identity. The daemon's mTLS only
        checks that the peer cert is signed by the pairing CA - it does
        not enforce Extended Key Usage, so the server cert works.
        """
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(str(cls.SERVER_CERT), str(cls.SERVER_KEY))
        ctx.load_verify_locations(str(cls.CA_CERT))
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    @classmethod
    def cert_info(cls) -> dict:
        """Return summary of certificate status."""
        info = {
            "ca_exists": cls.has_ca(),
            "server_exists": cls.has_server_certs(),
            "client_exists": cls.has_client_certs(),
            "certs_dir": str(CERTS_DIR),
        }
        # Check expiry of server cert
        if cls.has_server_certs():
            try:
                result = subprocess.run([
                    "openssl", "x509", "-in", str(cls.SERVER_CERT),
                    "-noout", "-enddate"
                ], capture_output=True, text=True)
                info["server_expires"] = result.stdout.strip().replace("notAfter=", "")
            except Exception:
                pass
        return info


# ---------------------------------------------------------------------------
# 2. HMAC Message Signing with Replay Protection
# ---------------------------------------------------------------------------

# Replay window: messages older than this are rejected
REPLAY_WINDOW_SECONDS = 60

# Maximum number of seen nonces to track (prevents memory leak)
MAX_NONCE_CACHE = 10000


class MessageSigner:
    """
    Signs and verifies WebSocket messages using HMAC-SHA256.

    Every message includes:
      - timestamp: Unix epoch (float)
      - nonce: Random 16-byte hex string (prevents replay within window)
      - hmac: HMAC-SHA256(secret, canonical_payload)

    Verification rejects:
      - Messages with timestamps outside the replay window
      - Messages with previously seen nonces (replay detection)
      - Messages with invalid HMAC signatures
    """

    def __init__(self, secret: str):
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._seen_nonces: dict[str, float] = {}  # nonce -> timestamp

    def sign(self, message: dict) -> dict:
        """Add timestamp, nonce, and HMAC to an outgoing message."""
        msg = dict(message)
        msg["timestamp"] = time.time()
        msg["nonce"] = secrets.token_hex(16)

        # Remove any existing hmac before signing
        msg.pop("hmac", None)

        # Canonical payload: sorted JSON of everything except hmac
        canonical = json.dumps(msg, sort_keys=True, separators=(",", ":"))
        sig = hmac.new(self._secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        msg["hmac"] = sig

        # Remove the old plaintext secret field if present
        msg.pop("secret", None)

        return msg

    def verify(self, message: dict) -> Tuple[bool, str]:
        """
        Verify an incoming message's HMAC, timestamp, and nonce.
        Returns (is_valid, reason).
        """
        # Extract and remove HMAC
        received_hmac = message.get("hmac")
        if not received_hmac:
            return False, "missing_hmac"

        # Check timestamp freshness
        msg_time = message.get("timestamp")
        if not msg_time:
            return False, "missing_timestamp"

        now = time.time()
        age = abs(now - msg_time)
        if age > REPLAY_WINDOW_SECONDS:
            return False, f"expired (age={age:.0f}s, window={REPLAY_WINDOW_SECONDS}s)"

        # Check nonce for replay
        nonce = message.get("nonce")
        if not nonce:
            return False, "missing_nonce"

        if nonce in self._seen_nonces:
            return False, "replayed_nonce"

        # Verify HMAC
        msg_copy = {k: v for k, v in message.items() if k != "hmac"}
        canonical = json.dumps(msg_copy, sort_keys=True, separators=(",", ":"))
        expected = hmac.new(self._secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(expected, received_hmac):
            return False, "invalid_hmac"

        # Record nonce
        self._seen_nonces[nonce] = now
        self._prune_nonces()

        return True, "ok"

    def _prune_nonces(self):
        """Remove expired nonces to prevent memory leak."""
        if len(self._seen_nonces) <= MAX_NONCE_CACHE:
            return
        cutoff = time.time() - REPLAY_WINDOW_SECONDS * 2
        self._seen_nonces = {
            n: t for n, t in self._seen_nonces.items() if t > cutoff
        }


# ---------------------------------------------------------------------------
# 3. Prompt Sanitization
# ---------------------------------------------------------------------------

# Characters that are never allowed in prompts
FORBIDDEN_PATTERNS = [
    r'\$\(',          # Command substitution $(...)
    r'`[^`]+`',       # Backtick command substitution
    r'\|\s*\w',       # Pipe to another command
    r';\s*\w',        # Command chaining with semicolon
    r'&&\s*\w',       # Command chaining with &&
    r'\|\|\s*\w',     # Command chaining with ||
    r'>\s*/',         # Redirect to absolute path
    r'>>\s*/',        # Append to absolute path
    r'<\s*/',         # Read from absolute path
    r'\$\{',          # Variable expansion ${...}
    r'\\x[0-9a-fA-F]',  # Hex escapes
    r'\\u[0-9a-fA-F]',  # Unicode escapes
]

# Compiled pattern for efficiency
_FORBIDDEN_RE = re.compile('|'.join(FORBIDDEN_PATTERNS))

# Maximum prompt length (prevents memory-based attacks)
MAX_PROMPT_LENGTH = 10000

# Allowed characters: printable ASCII + common unicode (letters, spaces, punctuation)
# Blocks control characters except newline/tab
_ALLOWED_CHARS_RE = re.compile(r'^[\x20-\x7e\n\t\r\u00a0-\uffff]*$')


class PromptSanitizer:
    """
    Validates and sanitizes prompts before they're embedded in shell commands.

    Defense in depth:
    1. Length check (prevents memory abuse)
    2. Character whitelist (blocks control characters)
    3. Pattern blacklist (blocks shell injection patterns)
    4. shlex.quote() wrapping (proper shell escaping for embedding)

    The sanitized prompt is safe to embed in:
      claude -p <sanitized_prompt>
    """

    @staticmethod
    def validate(prompt: str) -> Tuple[bool, str]:
        """
        Check if a prompt is safe. Returns (is_safe, reason).
        Does NOT modify the prompt - call sanitize() for the safe version.
        """
        if not prompt or not prompt.strip():
            return False, "empty_prompt"

        if len(prompt) > MAX_PROMPT_LENGTH:
            return False, f"prompt_too_long ({len(prompt)} > {MAX_PROMPT_LENGTH})"

        if not _ALLOWED_CHARS_RE.match(prompt):
            # Find the offending character for the error message
            for i, ch in enumerate(prompt):
                if ord(ch) < 0x20 and ch not in '\n\t\r':
                    return False, f"forbidden_char (0x{ord(ch):02x} at position {i})"
            return False, "forbidden_chars"

        match = _FORBIDDEN_RE.search(prompt)
        if match:
            return False, f"shell_injection_pattern: '{match.group()[:20]}'"

        return True, "ok"

    @staticmethod
    def sanitize(prompt: str) -> str:
        """
        Return a shell-safe version of the prompt using shlex.quote().
        This wraps the entire prompt in single quotes with proper escaping.

        Always call validate() first to reject clearly malicious prompts.
        sanitize() is the defense-in-depth layer for prompts that pass validation.
        """
        # shlex.quote wraps in single quotes and escapes any existing single quotes
        return shlex.quote(prompt.strip())

    @staticmethod
    def safe_embed(prompt: str) -> Tuple[bool, str, str]:
        """
        Validate and sanitize in one call.
        Returns (is_safe, reason, shell_safe_prompt).
        If not safe, shell_safe_prompt is empty string.
        """
        is_safe, reason = PromptSanitizer.validate(prompt)
        if not is_safe:
            return False, reason, ""
        return True, "ok", PromptSanitizer.sanitize(prompt)


# ---------------------------------------------------------------------------
# 4. Rate Limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """
    Tracks failed authentication attempts per source and enforces lockouts.

    After MAX_FAILURES failures within WINDOW_SECONDS, the source is locked
    out for LOCKOUT_SECONDS. Failed attempts are tracked by IP address.
    """

    MAX_FAILURES = 5
    WINDOW_SECONDS = 300      # 5 minute window
    LOCKOUT_SECONDS = 900     # 15 minute lockout

    def __init__(self):
        self._failures: dict[str, list[float]] = {}  # ip -> [timestamps]
        self._lockouts: dict[str, float] = {}  # ip -> lockout_until

    def is_locked(self, source: str) -> bool:
        """Check if a source is currently locked out."""
        lockout_until = self._lockouts.get(source)
        if lockout_until and time.time() < lockout_until:
            return True
        elif lockout_until:
            # Lockout expired, clean up
            del self._lockouts[source]
            self._failures.pop(source, None)
        return False

    def record_failure(self, source: str) -> bool:
        """
        Record a failed auth attempt. Returns True if source is now locked out.
        """
        now = time.time()
        cutoff = now - self.WINDOW_SECONDS

        if source not in self._failures:
            self._failures[source] = []

        # Prune old failures
        self._failures[source] = [
            t for t in self._failures[source] if t > cutoff
        ]
        self._failures[source].append(now)

        if len(self._failures[source]) >= self.MAX_FAILURES:
            self._lockouts[source] = now + self.LOCKOUT_SECONDS
            return True
        return False

    def record_success(self, source: str):
        """Clear failure history on successful auth."""
        self._failures.pop(source, None)
        self._lockouts.pop(source, None)

    def status(self) -> dict:
        now = time.time()
        return {
            "active_lockouts": {
                ip: round(until - now)
                for ip, until in self._lockouts.items()
                if until > now
            },
            "tracked_sources": len(self._failures),
        }


# ---------------------------------------------------------------------------
# 5. Audit Logger
# ---------------------------------------------------------------------------

class AuditLogger:
    """
    Structured audit trail for security-relevant events.
    Separate from the daemon's operational log.

    Events logged:
      - AUTH_SUCCESS / AUTH_FAILURE
      - TASK_SUBMIT / TASK_COMPLETE / TASK_CANCEL
      - LOCKOUT_TRIGGERED / LOCKOUT_EXPIRED
      - SECRET_ROTATED
      - CERT_GENERATED
      - PROMPT_REJECTED (sanitization failure)
      - DAEMON_START / DAEMON_STOP
    """

    def __init__(self, log_path: Optional[Path] = None):
        self._log_path = log_path or (LOG_DIR / "audit.log")
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        self._logger = logging.getLogger("byfrost.audit")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False  # Don't pollute the main log

        handler = RotatingFileHandler(
            self._log_path,
            maxBytes=5 * 1024 * 1024,  # 5MB
            backupCount=10  # Keep more history for audits
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z"
        ))
        # Avoid duplicate handlers on re-init
        if not self._logger.handlers:
            self._logger.addHandler(handler)

    def log(self, event: str, source: str = "-", details: str = ""):
        """Write a structured audit entry."""
        entry = json.dumps({
            "event": event,
            "source": source,
            "details": details,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        self._logger.info(entry)

    def auth_success(self, source: str):
        self.log("AUTH_SUCCESS", source)

    def auth_failure(self, source: str, reason: str):
        self.log("AUTH_FAILURE", source, reason)

    def lockout(self, source: str, duration: int):
        self.log("LOCKOUT_TRIGGERED", source, f"duration={duration}s")

    def task_submit(self, source: str, task_id: str, prompt_preview: str):
        self.log("TASK_SUBMIT", source, f"id={task_id} prompt={prompt_preview[:80]}")

    def task_complete(self, task_id: str, exit_code: int, duration: float):
        self.log("TASK_COMPLETE", "-", f"id={task_id} exit={exit_code} duration={duration:.1f}s")

    def task_cancel(self, source: str, task_id: str):
        self.log("TASK_CANCEL", source, f"id={task_id}")

    def prompt_rejected(self, source: str, reason: str):
        self.log("PROMPT_REJECTED", source, reason)

    def secret_rotated(self, source: str):
        self.log("SECRET_ROTATED", source)

    def cert_generated(self, cert_type: str):
        self.log("CERT_GENERATED", "-", cert_type)

    def daemon_start(self, port: int, tls: bool):
        self.log("DAEMON_START", "-", f"port={port} tls={tls}")

    def daemon_stop(self):
        self.log("DAEMON_STOP")


# ---------------------------------------------------------------------------
# 6. Secret Management
# ---------------------------------------------------------------------------

class SecretManager:
    """
    Manages the shared HMAC secret with rotation support.

    Secrets are NEVER stored in config.env (which is git-tracked).
    They live only in ~/.byfrost/secret on each machine.

    Rotation:
      1. New secret is generated
      2. Old secret is appended to secret.history (for grace period)
      3. During grace period, daemon accepts both old and new secrets
      4. After grace period, old secret is rejected
    """

    GRACE_PERIOD = 300  # 5 minutes: accept old secret after rotation

    @staticmethod
    def load() -> str:
        """Load the current secret."""
        if SECRET_FILE.exists():
            return SECRET_FILE.read_text().strip()
        return ""

    @staticmethod
    def generate() -> str:
        """Generate a new cryptographically secure secret."""
        return secrets.token_hex(32)  # 256-bit

    @staticmethod
    def save(secret: str):
        """Save a secret to the secret file."""
        BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_text(secret + "\n")
        SECRET_FILE.chmod(0o600)

    @staticmethod
    def rotate() -> str:
        """
        Rotate the secret. Returns the new secret.
        Old secret is preserved in history for the grace period.
        """
        old_secret = SecretManager.load()
        new_secret = SecretManager.generate()

        # Save old to history with timestamp
        if old_secret:
            BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
            with open(SECRET_HISTORY_FILE, "a") as f:
                f.write(f"{time.time():.0f}:{old_secret}\n")
            SECRET_HISTORY_FILE.chmod(0o600)

        SecretManager.save(new_secret)
        return new_secret

    @staticmethod
    def get_valid_secrets() -> list[str]:
        """
        Return list of currently valid secrets (current + grace period).
        Used by daemon to accept messages signed with recently-rotated secrets.
        """
        valid = []
        current = SecretManager.load()
        if current:
            valid.append(current)

        # Check history for secrets within grace period
        if SECRET_HISTORY_FILE.exists():
            now = time.time()
            try:
                for line in SECRET_HISTORY_FILE.read_text().strip().splitlines():
                    if ":" not in line:
                        continue
                    ts_str, old_secret = line.split(":", 1)
                    try:
                        ts = float(ts_str)
                    except ValueError:
                        continue
                    if now - ts < SecretManager.GRACE_PERIOD:
                        valid.append(old_secret)
            except Exception:
                pass

        return valid

    @staticmethod
    def prune_history():
        """Remove expired secrets from history."""
        if not SECRET_HISTORY_FILE.exists():
            return
        now = time.time()
        kept = []
        for line in SECRET_HISTORY_FILE.read_text().strip().splitlines():
            if ":" not in line:
                continue
            ts_str, _ = line.split(":", 1)
            try:
                ts = float(ts_str)
            except ValueError:
                continue
            # Keep for 24 hours for forensics, even though grace period is 5 min
            if now - ts < 86400:
                kept.append(line)
        SECRET_HISTORY_FILE.write_text("\n".join(kept) + "\n" if kept else "")
