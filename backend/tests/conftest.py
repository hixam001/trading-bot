"""
tests/conftest.py — Test fixtures and state isolation for API tests.
"""
from __future__ import annotations

import pytest
from api.rate_limiter import limiter
from api.auth import _FAILED_ATTEMPTS


@pytest.fixture(autouse=True)
def _isolate_rate_limits():
    """Ensure in-memory sliding window rate limits are isolated per test."""
    limiter.clear()
    _FAILED_ATTEMPTS.clear()
    yield
    limiter.clear()
    _FAILED_ATTEMPTS.clear()

