"""
tests/conftest.py — Test fixtures and state isolation for API tests.
"""
from __future__ import annotations

import pytest
from api.rate_limiter import limiter
from api.auth import _FAILED_ATTEMPTS, _REVOKED_SESSION_SIGS


import config

TEST_ADMIN_TOKEN = "test-operator-admin-token-32-chars-long-conftest"


@pytest.fixture(autouse=True)
def _isolate_rate_limits():
    """Ensure in-memory sliding window rate limits and session revocations are isolated per test."""
    limiter.clear()
    _FAILED_ATTEMPTS.clear()
    _REVOKED_SESSION_SIGS.clear()
    yield
    limiter.clear()
    _FAILED_ATTEMPTS.clear()
    _REVOKED_SESSION_SIGS.clear()


@pytest.fixture(autouse=True)
def _ensure_long_admin_token(monkeypatch):
    """Ensure config.ADMIN_TOKEN has a valid 32+ character test token by default for hermetic runs."""
    if not config.ADMIN_TOKEN or len(config.ADMIN_TOKEN) < 32:
        monkeypatch.setattr(config, "ADMIN_TOKEN", TEST_ADMIN_TOKEN)


