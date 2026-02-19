"""Tests for core.security module."""

import time

from core.security import (
    MessageSigner,
    PromptSanitizer,
    RateLimiter,
    SecretManager,
)


class TestMessageSigner:
    """HMAC signing and verification."""

    def test_sign_and_verify(self):
        secret = "test-secret-256bit-hex-value"
        signer = MessageSigner(secret)
        msg = {"type": "ping"}
        signed = signer.sign(msg)
        assert "hmac" in signed
        assert "timestamp" in signed
        assert "nonce" in signed
        is_valid, reason = signer.verify(signed)
        assert is_valid
        assert reason == "ok"

    def test_reject_tampered_message(self):
        signer = MessageSigner("secret")
        signed = signer.sign({"type": "ping"})
        signed["type"] = "task.submit"
        is_valid, reason = signer.verify(signed)
        assert not is_valid
        assert reason == "invalid_hmac"

    def test_reject_replay(self):
        signer = MessageSigner("secret")
        signed = signer.sign({"type": "ping"})
        signer.verify(signed)  # first use
        is_valid, reason = signer.verify(signed)
        assert not is_valid
        assert reason == "replayed_nonce"

    def test_reject_missing_hmac(self):
        signer = MessageSigner("secret")
        is_valid, reason = signer.verify({"type": "ping", "timestamp": time.time()})
        assert not is_valid
        assert reason == "missing_hmac"

    def test_reject_missing_timestamp(self):
        signer = MessageSigner("secret")
        is_valid, reason = signer.verify({"type": "ping", "hmac": "fake"})
        assert not is_valid
        assert reason == "missing_timestamp"


class TestPromptSanitizer:
    """Prompt validation and sanitization."""

    def test_valid_prompt(self):
        is_safe, reason = PromptSanitizer.validate("Fix the bug in main.swift")
        assert is_safe
        assert reason == "ok"

    def test_empty_prompt(self):
        is_safe, reason = PromptSanitizer.validate("")
        assert not is_safe

    def test_whitespace_only(self):
        is_safe, reason = PromptSanitizer.validate("   ")
        assert not is_safe

    def test_shell_injection_backtick(self):
        is_safe, reason = PromptSanitizer.validate("Run `rm -rf /`")
        assert not is_safe
        assert "shell_injection" in reason

    def test_shell_injection_dollar_paren(self):
        is_safe, reason = PromptSanitizer.validate("$(cat /etc/passwd)")
        assert not is_safe

    def test_shell_injection_variable_expansion(self):
        is_safe, reason = PromptSanitizer.validate("echo ${HOME}")
        assert not is_safe

    def test_allows_pipes_and_chaining(self):
        """Pipes, &&, ||, ; are allowed - shlex.quote() handles shell safety."""
        for prompt in [
            "hello | rm",
            "cd mac-app && xcodebuild build",
            "run tests; if failing fix them",
            "try this || try that",
            "write output > /tmp/out.txt",
        ]:
            is_safe, reason = PromptSanitizer.validate(prompt)
            assert is_safe, f"Prompt should be allowed: {prompt!r} (got: {reason})"

    def test_too_long(self):
        is_safe, reason = PromptSanitizer.validate("x" * 20000)
        assert not is_safe
        assert "too_long" in reason

    def test_sanitize_wraps_in_quotes(self):
        result = PromptSanitizer.sanitize("hello world")
        assert result.startswith("'") or result.startswith('"')

    def test_safe_embed(self):
        is_safe, reason, sanitized = PromptSanitizer.safe_embed("Fix the bug")
        assert is_safe
        assert reason == "ok"
        assert sanitized  # non-empty

    def test_safe_embed_rejects_unsafe(self):
        is_safe, reason, sanitized = PromptSanitizer.safe_embed("$(evil)")
        assert not is_safe
        assert sanitized == ""


class TestRateLimiter:
    """Rate limiting on auth failures."""

    def test_not_locked_initially(self):
        rl = RateLimiter()
        assert not rl.is_locked("192.168.1.1")

    def test_lockout_after_max_failures(self):
        rl = RateLimiter()
        for _ in range(RateLimiter.MAX_FAILURES):
            rl.record_failure("192.168.1.1")
        assert rl.is_locked("192.168.1.1")

    def test_success_clears_failures(self):
        rl = RateLimiter()
        for _ in range(3):
            rl.record_failure("192.168.1.1")
        rl.record_success("192.168.1.1")
        assert not rl.is_locked("192.168.1.1")

    def test_different_sources_independent(self):
        rl = RateLimiter()
        for _ in range(RateLimiter.MAX_FAILURES):
            rl.record_failure("10.0.0.1")
        assert rl.is_locked("10.0.0.1")
        assert not rl.is_locked("10.0.0.2")

    def test_status_reports_lockouts(self):
        rl = RateLimiter()
        for _ in range(RateLimiter.MAX_FAILURES):
            rl.record_failure("10.0.0.1")
        status = rl.status()
        assert "10.0.0.1" in status["active_lockouts"]


class TestSecretManager:
    """Secret generation (not file I/O)."""

    def test_generate_returns_hex_string(self):
        secret = SecretManager.generate()
        assert len(secret) == 64  # 32 bytes = 64 hex chars
        int(secret, 16)  # should not raise

    def test_generate_is_unique(self):
        s1 = SecretManager.generate()
        s2 = SecretManager.generate()
        assert s1 != s2
