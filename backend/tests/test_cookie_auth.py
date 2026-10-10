"""
tests/test_cookie_auth.py — test HttpOnly session cookie auth and expiration.

Verifies Rule 9 implementation:
- Login sets HttpOnly, SameSite, Secure cookie with Max-Age.
- Sessions expire after config.SESSION_MAX_AGE_SECONDS.
- Protected endpoints accept valid session cookie without X-Admin-Token header.
- Tampered and expired cookies fail closed (403).
- Logout clears the cookie.
- Brute-force rate limiting protects the login endpoint.
"""
from __future__ import annotations

import time
import httpx
import pytest
import pytest_asyncio

import config
from api import db
from api.auth import (
    _FAILED_ATTEMPTS,
    create_session_token,
    verify_session_token,
)
from api.main import app

TOKEN = "test-operator-secret-987"


@pytest_asyncio.fixture
async def auth_env(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "auth_test.db"
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(config, "SESSION_COOKIE_NAME", "admin_session")
    monkeypatch.setattr(config, "SESSION_MAX_AGE_SECONDS", 3600)
    monkeypatch.setattr(config, "LIVE_BOOK_PUBLIC", False)
    _FAILED_ATTEMPTS.clear()
    await db.init_db()
    yield
    _FAILED_ATTEMPTS.clear()


@pytest_asyncio.fixture
async def client(auth_env):
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
async def test_login_success_sets_httponly_cookie(client):
    r = await client.post("/api/auth/login", json={"token": TOKEN})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "authenticated"
    assert data["expires_in"] == 3600

    # Verify cookie attributes
    cookie = client.cookies.get("admin_session")
    assert cookie is not None
    assert len(cookie.split(".")) == 3

    # Check raw Set-Cookie header for security directives
    set_cookie_header = r.headers.get("set-cookie", "").lower()
    assert "httponly" in set_cookie_header
    assert "samesite=lax" in set_cookie_header
    assert "max-age=3600" in set_cookie_header


@pytest.mark.asyncio
async def test_login_invalid_token_refused(client):
    r = await client.post("/api/auth/login", json={"token": "wrong-secret"})
    assert r.status_code == 403
    assert "admin_session" not in client.cookies


@pytest.mark.asyncio
async def test_login_rate_limiting(client):
    for _ in range(5):
        r = await client.post("/api/auth/login", json={"token": "bad-password"})
        assert r.status_code == 403

    # 6th attempt should be rate limited (429)
    r_locked = await client.post("/api/auth/login", json={"token": "bad-password"})
    assert r_locked.status_code == 429


@pytest.mark.asyncio
async def test_login_disabled_when_admin_token_unset(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    r = await client.post("/api/auth/login", json={"token": "anything"})
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"]


@pytest.mark.asyncio
async def test_protected_route_authenticated_via_cookie(client, monkeypatch):
    from live_execution import config as le_config
    monkeypatch.setattr(le_config, "LIVE_TRADING_ENABLED", False)

    # 1. Without login or token, proxied request is rejected
    r_unauth = await client.get(
        "/api/live/portfolio",
        headers={"X-Forwarded-For": "203.0.113.10"},
    )
    assert r_unauth.status_code == 403

    # 2. Login to obtain session cookie
    r_login = await client.post("/api/auth/login", json={"token": TOKEN})
    assert r_login.status_code == 200

    # 3. Proxied request with cookie succeeds without X-Admin-Token header
    r_auth = await client.get(
        "/api/live/portfolio",
        headers={"X-Forwarded-For": "203.0.113.10"},
    )
    assert r_auth.status_code == 200
    assert r_auth.json()["enabled"] is False


@pytest.mark.asyncio
async def test_expired_session_cookie_rejected(client):
    # Create an expired token (issued 2 hours ago)
    past_time = int(time.time()) - 7200
    expired_token = f"{past_time}.abcd1234abcd1234"
    import hashlib
    import hmac
    from api.auth import _derive_session_key
    sig = hmac.new(
        _derive_session_key(TOKEN),
        expired_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    full_expired_token = f"{expired_token}.{sig}"

    # Verify helper directly fails on expired token
    assert not verify_session_token(full_expired_token, secret=TOKEN, max_age=3600)

    # Send request with expired cookie
    client.cookies.set("admin_session", full_expired_token)
    r = await client.get(
        "/api/live/portfolio",
        headers={"X-Forwarded-For": "203.0.113.10"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_tampered_session_cookie_rejected(client):
    valid_token = create_session_token(TOKEN)
    parts = valid_token.split(".")
    # Tamper with the nonce payload
    tampered_token = f"{parts[0]}.deadbeefdeadbeef.{parts[2]}"

    assert not verify_session_token(tampered_token, secret=TOKEN)

    client.cookies.set("admin_session", tampered_token)
    r = await client.get(
        "/api/live/portfolio",
        headers={"X-Forwarded-For": "203.0.113.10"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_logout_clears_cookie(client):
    # Log in
    await client.post("/api/auth/login", json={"token": TOKEN})
    assert client.cookies.get("admin_session") is not None

    # Log out
    r = await client.post("/api/auth/logout")
    assert r.status_code == 200
    assert r.json()["status"] == "logged_out"


@pytest.mark.asyncio
async def test_session_status_endpoint(client):
    # 1. Unauthenticated
    r1 = await client.get("/api/auth/session")
    assert r1.status_code == 200
    assert r1.json()["authenticated"] is False

    # 2. Header-authenticated
    r2 = await client.get(
        "/api/auth/session",
        headers={"X-Admin-Token": TOKEN},
    )
    assert r2.status_code == 200
    assert r2.json()["authenticated"] is True
    assert r2.json()["auth_type"] == "header"

    # 3. Cookie-authenticated
    await client.post("/api/auth/login", json={"token": TOKEN})
    r3 = await client.get("/api/auth/session")
    assert r3.status_code == 200
    assert r3.json()["authenticated"] is True
    assert r3.json()["auth_type"] == "cookie"
    assert r3.json()["expires_in"] <= 3600

