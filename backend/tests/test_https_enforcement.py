"""
tests/test_https_enforcement.py — Rule 19: HTTPS enforcement and secure transport.

Verifies:
1. Automatic 308 Permanent Redirect for insecure HTTP requests:
   - Non-loopback / public clients hitting HTTP.
   - Proxied clients with X-Forwarded-Proto: http.
   - RFC 7239 Forwarded: proto=http.
   - X-Forwarded-Ssl: off.
2. Preservation of HTTP method, path, and query strings on 308 redirect.
3. Proper canonical host resolution without internal backend ports (:80, :8000).
4. Exemption of local loopback (127.0.0.1, localhost) and hermetic test runners.
5. Strict-Transport-Security (HSTS) headers with max-age=63072000, includeSubDomains, preload.
6. Content-Security-Policy upgrade-insecure-requests directive.
7. Session cookie Secure flag enforcement on HTTPS / proxied requests.
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import Response

import config
from api import auth, db
from api.main import app

TOKEN = "test-operator-token-https-32-chars-long"


@pytest_asyncio.fixture
async def https_env(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "test_https.db"
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(config, "FORCE_HTTPS", True)
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    await db.init_db()
    yield


@pytest_asyncio.fixture
async def client(https_env):
    from data_providers.mock import MockProvider
    from llm.narrator import Narrator

    app.state.provider = MockProvider()
    app.state.narrator = Narrator()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Local Loopback and Hermetic Test Exemption
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_loopback_traffic_allowed_without_redirect(client):
    """Local development and automated test suites on 127.0.0.1 do not trigger 308 redirect."""
    r = await client.get("/api/system-status")
    assert r.status_code == 200
    assert "main_llm_provider" in r.json() or "paper_trading_only" in r.json()


# ---------------------------------------------------------------------------
# 2. Reverse Proxy Insecure Protocol Detection & 308 Redirect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_x_forwarded_proto_http_redirected_to_https(client):
    """Reverse proxy signaling HTTP via X-Forwarded-Proto triggers 308 redirect to HTTPS."""
    r = await client.get(
        "/api/system-status",
        headers={"x-forwarded-proto": "http", "host": "bot.example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 308
    assert r.headers["location"] == "https://bot.example.com/api/system-status"


@pytest.mark.asyncio
async def test_rfc_7239_forwarded_proto_http_redirected_to_https(client):
    """Reverse proxy signaling HTTP via RFC 7239 Forwarded header triggers 308 redirect."""
    r = await client.get(
        "/api/stats",
        headers={"forwarded": "for=203.0.113.195;proto=http", "host": "bot.example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 308
    assert r.headers["location"] == "https://bot.example.com/api/stats"


@pytest.mark.asyncio
async def test_x_forwarded_ssl_off_redirected_to_https(client):
    """Legacy proxy signaling X-Forwarded-Ssl: off triggers 308 redirect to HTTPS."""
    r = await client.get(
        "/api/feed",
        headers={"x-forwarded-ssl": "off", "host": "bot.example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 308
    assert r.headers["location"] == "https://bot.example.com/api/feed"


# ---------------------------------------------------------------------------
# 3. Method, Query Parameters, and Host Port Preservation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redirect_preserves_post_method_and_query_string(client):
    """308 redirect preserves POST method (unlike 301/302) and preserves query strings."""
    r = await client.post(
        "/api/admin/reset?confirm=yes&mode=prune_only",
        headers={
            "x-forwarded-proto": "http",
            "host": "bot.example.com",
            "X-Admin-Token": TOKEN,
        },
        follow_redirects=False,
    )
    assert r.status_code == 308
    assert r.headers["location"] == "https://bot.example.com/api/admin/reset?confirm=yes&mode=prune_only"


@pytest.mark.asyncio
async def test_redirect_uses_x_forwarded_host_and_strips_internal_ports(client):
    """Redirect uses X-Forwarded-Host if provided, and strips internal proxy ports (8000/8080/80)."""
    r = await client.get(
        "/api/system-status",
        headers={
            "x-forwarded-proto": "http",
            "x-forwarded-host": "bot.example.com:8000",
            "host": "127.0.0.1:8000",
        },
        follow_redirects=False,
    )
    assert r.status_code == 308
    assert r.headers["location"] == "https://bot.example.com/api/system-status"


# ---------------------------------------------------------------------------
# 4. Non-Loopback External Client Direct HTTP Connection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_external_non_loopback_client_redirected_to_https(https_env):
    """A direct connection from a non-loopback host (e.g. public IP or domain) redirects to HTTPS."""
    from data_providers.mock import MockProvider
    from llm.narrator import Narrator

    app.state.provider = MockProvider()
    app.state.narrator = Narrator()

    # Emulate an external client connecting to a public domain
    transport = httpx.ASGITransport(
        app=app,
        client=("198.51.100.42", 54321),  # Public client IP
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://bot.example.com"
    ) as ext_client:
        r = await ext_client.get("/api/system-status", follow_redirects=False)
        assert r.status_code == 308
        assert r.headers["location"] == "https://bot.example.com/api/system-status"


@pytest.mark.asyncio
async def test_force_https_can_be_disabled_via_config(client, monkeypatch):
    """When FORCE_HTTPS is explicitly disabled, HTTP requests pass through without redirection."""
    monkeypatch.setattr(config, "FORCE_HTTPS", False)
    r = await client.get(
        "/api/system-status",
        headers={"x-forwarded-proto": "http"},
        follow_redirects=False,
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 5. HSTS and Upgrade-Insecure-Requests Security Headers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hsts_and_upgrade_insecure_requests_emitted_on_https(client):
    """Responses on HTTPS carry HSTS preload headers and CSP upgrade-insecure-requests."""
    r = await client.get(
        "/api/system-status",
        headers={"x-forwarded-proto": "https"},
    )
    assert r.status_code == 200

    # HSTS must be present on HTTPS responses
    assert "strict-transport-security" in r.headers
    hsts = r.headers["strict-transport-security"]
    assert "max-age=63072000" in hsts
    assert "includeSubDomains" in hsts
    assert "preload" in hsts

    # CSP must contain upgrade-insecure-requests
    assert "content-security-policy" in r.headers
    csp = r.headers["content-security-policy"]
    assert "upgrade-insecure-requests" in csp


@pytest.mark.asyncio
async def test_hsts_not_emitted_on_plain_http_per_rfc6797(client, monkeypatch):
    """RFC 6797 specifies that HSTS must only be emitted over TLS / HTTPS."""
    monkeypatch.setattr(config, "FORCE_HTTPS", False)
    r = await client.get("/api/system-status")
    assert r.status_code == 200
    assert "strict-transport-security" not in r.headers


# ---------------------------------------------------------------------------
# 6. Session Cookie Secure Flag Alignment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_cookie_sets_secure_flag_on_https(client):
    """Session cookie set over HTTPS or proxied HTTPS carries the Secure attribute."""
    resp = Response()
    # Emulate request with x-forwarded-proto: https
    from starlette.requests import Request
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [(b"x-forwarded-proto", b"https"), (b"host", b"bot.example.com")],
    }
    req = Request(scope)
    token = auth.create_session_token(secret=TOKEN)
    auth.set_session_cookie(resp, token, request=req)

    raw_cookie = resp.headers.get("set-cookie", "")
    assert config.SESSION_COOKIE_NAME in raw_cookie
    assert "HttpOnly" in raw_cookie
    assert "Secure" in raw_cookie
    assert "SameSite=lax" in raw_cookie


@pytest.mark.asyncio
async def test_clear_session_cookie_matches_secure_attribute_on_https():
    """Logout / clear cookie matches the Secure attribute when on HTTPS."""
    resp = Response()
    from starlette.requests import Request
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/logout",
        "headers": [(b"x-forwarded-proto", b"https"), (b"host", b"bot.example.com")],
    }
    req = Request(scope)
    auth.clear_session_cookie(resp, request=req)

    raw_cookie = resp.headers.get("set-cookie", "")
    assert config.SESSION_COOKIE_NAME in raw_cookie
    assert "HttpOnly" in raw_cookie
    assert "Secure" in raw_cookie
