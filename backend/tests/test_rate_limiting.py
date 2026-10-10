"""
tests/test_rate_limiting.py — Test server-enforced tiered rate limiting.

Verifies Rule 11:
- Stricter limits on auth endpoints (login, password reset).
- Stricter limits on paid service / AI model endpoints.
- Server-side enforcement with anti-spoofing protection.
- Standard rate limit response headers (Retry-After, X-RateLimit-*).
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

import config
from api import db
from api.main import app
from api.rate_limiter import limiter

TOKEN = "test-token-rate-limit"


@pytest_asyncio.fixture
async def rate_env(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "rate_test.db"
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_AUTH_PER_MINUTE", 5)
    monkeypatch.setattr(config, "RATE_LIMIT_PAID_PER_MINUTE", 10)
    monkeypatch.setattr(config, "RATE_LIMIT_GENERAL_PER_MINUTE", 30)
    limiter.clear()
    await db.init_db()
    yield
    limiter.clear()


@pytest_asyncio.fixture
async def client(rate_env):
    from data_providers.mock import MockProvider
    from llm.narrator import Narrator

    app.state.provider = MockProvider()
    app.state.narrator = Narrator()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_auth_tier_strict_rate_limit(client):
    """Auth/login endpoints are strictly rate limited to 5 requests per minute."""
    for _ in range(5):
        r = await client.post(
            "/api/auth/login",
            json={"token": "wrong"},
            headers={"X-Forwarded-For": "198.51.100.1"},
        )
        assert r.status_code == 403

    # 6th request is throttled by rate limiter middleware with 429
    r6 = await client.post(
        "/api/auth/login",
        json={"token": "wrong"},
        headers={"X-Forwarded-For": "198.51.100.1"},
    )
    assert r6.status_code == 429
    assert "rate limited" in r6.json()["detail"].lower()
    assert "retry-after" in r6.headers
    assert r6.headers["x-ratelimit-remaining"] == "0"
    assert r6.headers["x-ratelimit-limit"] == "5"


@pytest.mark.asyncio
async def test_paid_service_ai_rate_limit(client):
    """Paid service / AI endpoints (e.g. system-status LLM health) are strictly limited."""
    for _ in range(10):
        r = await client.get(
            "/api/system-status",
            headers={"X-Forwarded-For": "198.51.100.2"},
        )
        assert r.status_code == 200
        assert "x-ratelimit-limit" in r.headers
        assert r.headers["x-ratelimit-limit"] == "10"

    # 11th request exceeds the paid_service quota
    r11 = await client.get(
        "/api/system-status",
        headers={"X-Forwarded-For": "198.51.100.2"},
    )
    assert r11.status_code == 429
    assert "paid_service" in r11.json()["detail"]
    assert "retry-after" in r11.headers


@pytest.mark.asyncio
async def test_general_api_rate_limit(client, monkeypatch):
    """General API endpoints enforce configured quota."""
    monkeypatch.setattr(config, "RATE_LIMIT_GENERAL_PER_MINUTE", 5)

    for i in range(5):
        r = await client.get(
            "/api/feed?limit=1",
            headers={"X-Forwarded-For": "198.51.100.3"},
        )
        assert r.status_code == 200
        assert r.headers["x-ratelimit-remaining"] == str(4 - i)

    # 6th request is rate limited
    r6 = await client.get(
        "/api/feed?limit=1",
        headers={"X-Forwarded-For": "198.51.100.3"},
    )
    assert r6.status_code == 429
    assert "general" in r6.json()["detail"]


@pytest.mark.asyncio
async def test_anti_spoofing_cannot_bypass_limit(rate_env):
    """Direct connections cannot bypass rate limits by forging X-Forwarded-For."""
    import httpx
    from data_providers.mock import MockProvider
    from llm.narrator import Narrator

    app.state.provider = MockProvider()
    app.state.narrator = Narrator()

    # Simulate direct socket peer from external IP (not loopback)
    transport = httpx.ASGITransport(app=app, client=("198.51.100.99", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://app") as ext_client:
        for i in range(5):
            # Attempt to bypass by rotating fake X-Forwarded-For on every request
            r = await ext_client.post(
                "/api/auth/login",
                json={"token": "wrong"},
                headers={"X-Forwarded-For": f"10.0.0.{i}"},
            )
            assert r.status_code == 403

        # 6th request with yet another fake IP is STILL throttled because
        # the server tracks the real socket IP 198.51.100.99
        r_bypass = await ext_client.post(
            "/api/auth/login",
            json={"token": "wrong"},
            headers={"X-Forwarded-For": "10.0.0.99"},
        )
        assert r_bypass.status_code == 429


@pytest.mark.asyncio
async def test_non_api_routes_exempt(client):
    """Root and static assets are exempt from API rate limits."""
    for _ in range(40):
        r = await client.get("/")
        assert r.status_code in (200, 308)
        assert "x-ratelimit-limit" not in r.headers

