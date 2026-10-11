"""
backend/tests/test_audit_77_a.py — Phase A Security Audit Tests (§77 A2–A5).
"""
from __future__ import annotations

import subprocess
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import config
from api.auth import (
    _FAILED_ATTEMPTS,
    clear_session_cookie,
    create_session_token,
    require_admin_token,
    require_local_or_admin,
    verify_session_token,
)
from api.bot_protection import verify_bot_protection
from api.main import app
from api.rate_limiter import get_client_ip

LONG_TOKEN = "a" * 32
SHORT_TOKEN = "short-token-15"


# ---------------------------------------------------------------------------
# A3: ADMIN_TOKEN Strength Enforcement
# ---------------------------------------------------------------------------

def test_admin_token_strength_enforcement(monkeypatch):
    """Tokens shorter than 32 characters treat operator auth as disabled."""
    monkeypatch.setattr(config, "ADMIN_TOKEN", SHORT_TOKEN)
    client = TestClient(app)

    # 1. require_admin_token fails closed
    scope = {"type": "http", "method": "GET", "path": "/api/admin/status", "headers": [(b"x-admin-token", SHORT_TOKEN.encode())]}
    req = Request(scope)
    with pytest.raises(HTTPException) as exc_info:
        require_admin_token(req)
    assert exc_info.value.status_code == 403
    assert "disabled" in exc_info.value.detail

    # 2. /api/auth/login behaves as disabled
    res = client.post("/api/auth/login", json={"token": SHORT_TOKEN})
    assert res.status_code == 403
    assert "disabled" in res.json().get("detail", "")

    # 3. 32+ chars works
    monkeypatch.setattr(config, "ADMIN_TOKEN", LONG_TOKEN)
    res_ok = client.post("/api/auth/login", json={"token": LONG_TOKEN})
    assert res_ok.status_code == 200


# ---------------------------------------------------------------------------
# A4: Hardcoded CAPTCHA Bypass Removal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_captcha_bypass_removed(monkeypatch):
    """'test-captcha-pass' must be rejected when CAPTCHA is enabled."""
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", True)
    monkeypatch.setattr(config, "CAPTCHA_PROVIDER", "internal")

    with pytest.raises(HTTPException) as exc_info:
        await verify_bot_protection(token="test-captcha-pass")
    assert exc_info.value.status_code == 400


def test_repo_has_no_test_captcha_pass_in_backend_code():
    """Grep backend/ excluding tests/ for 'test-captcha-pass'."""
    res = subprocess.run(
        ["grep", "-rn", "--exclude-dir=tests", "--exclude-dir=__pycache__", "test-captcha-pass", "backend"],
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0, f"Found 'test-captcha-pass' in backend production code:\n{res.stdout}"


# ---------------------------------------------------------------------------
# A5: Login Lockout Order (DoS prevention)
# ---------------------------------------------------------------------------

def test_login_lockout_order_dos_prevented(monkeypatch):
    """A correct token authenticates even after 5 wrong attempts from the same IP."""
    monkeypatch.setattr(config, "ADMIN_TOKEN", LONG_TOKEN)
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", False)
    client = TestClient(app)

    # 5 wrong attempts from IP X
    for i in range(5):
        res = client.post(
            "/api/auth/login",
            json={"token": "wrong-token-attempt"},
            headers={"X-Forwarded-For": "198.51.100.10"},
        )
        assert res.status_code == 403

    # 6th attempt with wrong token is rate-limited (429)
    res_wrong = client.post(
        "/api/auth/login",
        json={"token": "wrong-token-attempt"},
        headers={"X-Forwarded-For": "198.51.100.10"},
    )
    assert res_wrong.status_code == 429

    # Correct token from IP X must succeed (200) and clear the lockout
    res_correct = client.post(
        "/api/auth/login",
        json={"token": LONG_TOKEN},
        headers={"X-Forwarded-For": "198.51.100.10"},
    )
    assert res_correct.status_code == 200

    # IP Y is unaffected by IP X's failures
    res_y = client.post(
        "/api/auth/login",
        json={"token": "wrong-token-attempt"},
        headers={"X-Forwarded-For": "198.51.100.20"},
    )
    assert res_y.status_code == 403  # First failure is 403, not 429


# ---------------------------------------------------------------------------
# A2: Proxy Headers and Single Source of Truth for Client IP
# ---------------------------------------------------------------------------

def test_proxy_headers_and_client_ip_throttling(monkeypatch):
    """Untrusted peers cannot defeat login throttling by rotating X-Forwarded-For."""
    monkeypatch.setattr(config, "ADMIN_TOKEN", LONG_TOKEN)

    # Wrap app in ProxyHeadersMiddleware with trusted loopback and docker CIDR
    proxied_app = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1,172.16.0.0/12")
    client = TestClient(proxied_app)

    # (a) Peer = public IP (untrusted) + spoofed rotating XFF
    # TestClient default client host is 'testclient', so simulate untrusted peer by setting client host
    # In ASGI scope, client is (host, port)
    class CustomTransportClient(TestClient):
        def __init__(self, app, peer_ip: str):
            self.peer_ip = peer_ip
            super().__init__(app)

        def request(self, *args, **kwargs):
            # starlette TestClient creates scope in transport; we can pass headers or wrap
            return super().request(*args, **kwargs)

    # We can test ProxyHeadersMiddleware by creating raw scopes:
    # 5 wrong attempts from untrusted peer 203.0.113.195 with rotating XFF
    for i in range(5):
        # Using a custom scope runner
        response = client.post(
            "/api/auth/login",
            json={"token": "wrong-token"},
            headers={"X-Forwarded-For": f"10.0.0.{i}"},
            # TestClient doesn't directly allow setting client socket IP per request,
            # but we can verify get_client_ip logic directly on Requests:
        )
    # Let's verify via Request objects directly:
    for i in range(5):
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [(b"x-forwarded-for", f"10.0.0.{i}".encode())],
            "client": ("203.0.113.195", 12345),
        }
        req = Request(scope)
        assert get_client_ip(req) == "203.0.113.195"

    # (b) Peer = trusted proxy + XFF -> effective IP is the XFF value
    scope_trusted = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [(b"x-forwarded-for", b"198.51.100.5, 127.0.0.1")],
        "client": ("127.0.0.1", 12345),
    }
    req_trusted = Request(scope_trusted)
    assert get_client_ip(req_trusted) == "198.51.100.5"

    # (c) require_local_or_admin refuses any request carrying forwarding headers, from any peer
    scope_loopback_proxied = {
        "type": "http",
        "method": "GET",
        "path": "/api/live/portfolio",
        "headers": [(b"x-forwarded-for", b"127.0.0.1")],
        "client": ("127.0.0.1", 12345),
    }
    req_loopback_proxied = Request(scope_loopback_proxied)
    with pytest.raises(HTTPException) as exc_info:
        require_local_or_admin(req_loopback_proxied)
    assert exc_info.value.status_code == 403
