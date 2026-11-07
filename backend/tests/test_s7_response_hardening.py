"""
backend/tests/test_s7_response_hardening.py — §92 S7 response-surface proofs.

Security headers are only useful if they are present on the responses an
attacker actually sees. These tests pin every surface that bypasses the normal
route handler:

  * 404 / 405 (catch-all + method mismatch)
  * 413 (body-cap middleware)
  * 429 (rate-limit middleware)
  * 500 (unhandled exception / ServerErrorMiddleware)
  * static assets and the SPA shell

They also pin the two classic "test configuration leaked into production"
holes: test hostnames must not bypass HTTPS outside a test runtime, and DEBUG
must not disable auth, CSRF, or rate limits, nor echo a secret in the debug
exception text.
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi.routing import APIRoute

import config
from api import db, rate_limiter
from api.main import app

SENTINEL_OPENAI_KEY = "sk-" + ("A" * 40)


@pytest_asyncio.fixture
async def app_env(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "test_s7.db"
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    await db.init_db()
    yield


@pytest_asyncio.fixture
async def client(app_env):
    from data_providers.mock import MockProvider
    from llm.narrator import Narrator

    app.state.provider = MockProvider()
    app.state.narrator = Narrator()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as c:
        yield c


def _assert_security_headers(response, *, api: bool = False) -> None:
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["x-permitted-cross-domain-policies"] == "none"

    csp = response.headers["content-security-policy"]
    for directive in (
        "default-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ):
        assert directive in csp, f"missing CSP directive: {directive}"

    if api:
        assert response.headers["cache-control"] == "no-store"


async def test_security_headers_on_404_and_405(client):
    not_found = await client.get("/api/does-not-exist")
    assert not_found.status_code == 404
    _assert_security_headers(not_found, api=True)

    wrong_method = await client.post("/api/feed")
    assert wrong_method.status_code == 405
    _assert_security_headers(wrong_method, api=True)


async def test_security_headers_on_413(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUEST_BODY_BYTES", 1024)
    response = await client.post(
        "/api/feed",
        content=b"x" * 2048,
        headers={"content-length": "2048"},
    )
    assert response.status_code == 413
    _assert_security_headers(response, api=True)


async def test_security_headers_on_429(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_GENERAL_PER_MINUTE", 1)
    rate_limiter.limiter.clear()

    first = await client.get("/api/feed")
    second = await client.get("/api/feed")

    assert first.status_code == 200
    assert second.status_code == 429
    assert int(second.headers["retry-after"]) >= 1
    _assert_security_headers(second, api=True)
    rate_limiter.limiter.clear()


async def test_security_headers_on_500(client, monkeypatch):
    monkeypatch.setattr(config, "DEBUG", False)

    async def boom():
        raise RuntimeError("s7 synthetic server failure")

    route = APIRoute("/api/__s7_boom", boom, methods=["GET"])
    app.router.routes.insert(0, route)
    try:
        response = await client.get("/api/__s7_boom")
    finally:
        app.router.routes.remove(route)

    assert response.status_code == 500
    assert response.json()["detail"] == "Internal server error"
    _assert_security_headers(response, api=True)


@pytest.mark.parametrize("path", ["/", "/assets/does-not-exist.js"])
async def test_security_headers_on_static_and_spa_surfaces(client, path):
    response = await client.get(path)
    assert response.status_code in (200, 404)
    _assert_security_headers(response)


async def test_csp_forbids_base_tag_hijack(client):
    response = await client.get("/api/system-status")
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "base-uri 'none'" in csp
    assert "base-uri 'self'" not in csp


async def test_test_hostnames_only_bypass_https_in_test_runtime(monkeypatch):
    monkeypatch.setattr(config, "FORCE_HTTPS", True)
    transport = httpx.ASGITransport(app=app)

    # Under pytest the synthetic host is allowed, so the suite stays hermetic.
    monkeypatch.setattr(config, "TESTING", True, raising=False)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        response = await client.get("/api/system-status",
                                    follow_redirects=False)
    assert response.status_code == 200

    # Outside a test runtime a forged test Host must not disable HTTPS.
    monkeypatch.setattr(config, "TESTING", False, raising=False)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        response = await client.get("/api/system-status",
                                    follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"].startswith("https://")

    # A genuine loopback host still works without HTTPS.
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://127.0.0.1:8000") as client:
        response = await client.get("/api/system-status",
                                    follow_redirects=False)
    assert response.status_code == 200


async def test_debug_mode_does_not_bypass_operator_auth(client, monkeypatch):
    monkeypatch.setattr(config, "DEBUG", True)
    response = await client.get("/api/admin/monitoring/security-events")
    assert response.status_code == 403


async def test_debug_mode_does_not_bypass_csrf(client, monkeypatch):
    monkeypatch.setattr(config, "DEBUG", True)
    client.cookies.set(config.SESSION_COOKIE_NAME, "synthetic-session")
    response = await client.post("/api/admin/reset?confirm=yes")
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]


async def test_debug_mode_does_not_disable_rate_limits(client, monkeypatch):
    monkeypatch.setattr(config, "DEBUG", True)
    monkeypatch.setattr(config, "RATE_LIMIT_GENERAL_PER_MINUTE", 1)
    rate_limiter.limiter.clear()

    first = await client.get("/api/feed")
    second = await client.get("/api/feed")

    assert first.status_code == 200
    assert second.status_code == 429
    rate_limiter.limiter.clear()


async def test_debug_exception_text_is_redacted(client, monkeypatch):
    monkeypatch.setattr(config, "DEBUG", True)

    async def boom():
        raise RuntimeError(
            f"provider rejected key {SENTINEL_OPENAI_KEY} while calling upstream")

    route = APIRoute("/api/__s7_boom_redact", boom, methods=["GET"])
    app.router.routes.insert(0, route)
    try:
        response = await client.get("/api/__s7_boom_redact")
    finally:
        app.router.routes.remove(route)

    assert response.status_code == 500
    body = response.json()
    assert body["exception_type"] == "RuntimeError"
    assert SENTINEL_OPENAI_KEY not in response.text
    assert "REDACTED" in body["debug_message"]
    _assert_security_headers(response, api=True)
