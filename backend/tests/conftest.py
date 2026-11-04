"""
tests/conftest.py — Test fixtures and state isolation for API tests.
"""
from __future__ import annotations

import pytest
from api.rate_limiter import limiter
from api.auth import (
    _FAILED_ATTEMPTS,
    _GLOBAL_FAILED_ATTEMPTS,
    reset_session_revocations,
)
from api import bot_protection


import config

TEST_ADMIN_TOKEN = "test-operator-admin-token-32-chars-long-conftest"


@pytest.fixture(autouse=True)
def _isolate_rate_limits(tmp_path, monkeypatch):
    """Ensure in-memory rate limits, revocations, and PoW nonces are isolated per test."""
    monkeypatch.setattr(
        config, "SESSION_REVOCATION_FILE", str(tmp_path / "session_revocations.json")
    )
    limiter.clear()
    _FAILED_ATTEMPTS.clear()
    _GLOBAL_FAILED_ATTEMPTS.clear()
    reset_session_revocations(delete_file=True)
    bot_protection._USED_POW_NONCES.clear()
    yield
    limiter.clear()
    _FAILED_ATTEMPTS.clear()
    _GLOBAL_FAILED_ATTEMPTS.clear()
    reset_session_revocations(delete_file=True)
    bot_protection._USED_POW_NONCES.clear()


@pytest.fixture(autouse=True)
def _ensure_long_admin_token(monkeypatch):
    """Ensure config.ADMIN_TOKEN has a valid 32+ character test token by default for hermetic runs."""
    if not config.ADMIN_TOKEN or len(config.ADMIN_TOKEN) < 32:
        monkeypatch.setattr(config, "ADMIN_TOKEN", TEST_ADMIN_TOKEN)


