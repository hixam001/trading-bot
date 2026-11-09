"""§93 R7: test-host trust and signup concealment are consistent."""
from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException, Request

import config
from api import auth
from api.main import _is_loopback_host, app
from api.rate_limiter import get_client_ip

SYNTHETIC_HOSTS = ("testclient", "testserver", "test", "app")


def _request(peer: str, host: str, forwarded: str | None = None) -> Request:
    headers = [(b"host", host.encode())]
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return Request({"type": "http", "method": "GET", "scheme": "http",
                    "path": "/api/holdings", "query_string": b"",
                    "headers": headers, "client": (peer, 54321),
                    "server": (host, 80)})


@pytest.mark.parametrize("host", SYNTHETIC_HOSTS)
def test_synthetic_hosts_are_never_trusted_outside_tests(monkeypatch, host):
    monkeypatch.setattr(config, "TESTING", False)
    monkeypatch.setattr(config, "FORCE_HTTPS", True)
    direct = _request(host, host)
    assert _is_loopback_host(host) is False
    assert auth._is_request_https(direct) is True
    with pytest.raises(HTTPException) as denied:
        auth.require_local_or_admin(direct)
    assert denied.value.status_code == 403
    # A synthetic socket peer cannot impersonate a trusted forwarding proxy.
    assert get_client_ip(_request(host, host, "127.0.0.1")) == host
    # Even a real trusted proxy cannot turn a forged synthetic XFF into
    # loopback/admin trust or the insecure-cookie exemption.
    proxied = _request("127.0.0.1", host, host)
    assert auth._is_request_https(proxied) is True
    with pytest.raises(HTTPException) as denied:
        auth.require_local_or_admin(proxied)
    assert denied.value.status_code == 403


@pytest.mark.parametrize("host", SYNTHETIC_HOSTS)
def test_shared_test_host_helper_is_gated_by_pytest(monkeypatch, host):
    monkeypatch.setattr(config, "TESTING", False)
    assert config.is_test_host(host) is False
    monkeypatch.setattr(config, "TESTING", True)
    assert config.is_test_host(host) is True
    assert _is_loopback_host(host) is True
    assert auth._is_request_https(_request(host, host)) is False
    auth.require_local_or_admin(_request(host, host))
    assert get_client_ip(_request(host, host, "198.51.100.15")) == "198.51.100.15"
    assert config.is_test_host("public.example") is False


SIGNUP_BODIES = [
    pytest.param(b"", "application/json", id="empty"),
    pytest.param(b'{"username":', "application/json", id="malformed-json"),
    pytest.param(b"username=user&password=password", "text/plain", id="wrong-content-type"),
    pytest.param(b'{"username":"valid_user","password":"ValidPassword123!"}',
                 "application/json", id="valid"),
]


@pytest.mark.parametrize("body,content_type", SIGNUP_BODIES)
@pytest.mark.asyncio
async def test_signup_is_concealed_before_body_validation(monkeypatch, body, content_type):
    monkeypatch.setattr(config, "ALLOW_SIGNUP", False)
    transport = httpx.ASGITransport(app=app, client=("198.51.100.9", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="https://bot.example") as client:
        response = await client.post("/api/auth/signup", content=body,
                                     headers={"Content-Type": content_type})
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("body,content_type", SIGNUP_BODIES)
@pytest.mark.asyncio
async def test_signup_opt_in_preserves_body_validation(monkeypatch, body, content_type):
    monkeypatch.setattr(config, "ALLOW_SIGNUP", True)
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", False)
    transport = httpx.ASGITransport(app=app, client=("198.51.100.9", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="https://bot.example") as client:
        response = await client.post("/api/auth/signup", content=body,
                                     headers={"Content-Type": content_type})
    assert response.status_code == (403 if b"valid_user" in body else 422)
