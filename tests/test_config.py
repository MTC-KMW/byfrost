"""Tests for core.config module."""

from pathlib import Path

from core.config import BRIDGE_DIR, CERTS_DIR, DEFAULT_PORT, LOG_DIR


def test_bridge_dir_is_under_home():
    assert str(BRIDGE_DIR).endswith(".byfrost")
    assert BRIDGE_DIR.parent == Path.home()


def test_log_dir_is_under_bridge_dir():
    assert LOG_DIR.parent == BRIDGE_DIR


def test_certs_dir_is_under_bridge_dir():
    assert CERTS_DIR.parent == BRIDGE_DIR


def test_default_port():
    assert DEFAULT_PORT == 9784
