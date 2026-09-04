"""
tests/test_authz_surface.py — §55 server-side authorization audit regressions.

Three findings, each pinned by tests here:

  F1 (SEC-02 gap) — loopback spoofing behind the deploy guide's Caddy
      reverse_proxy: EVERY proxied visitor arrives with
      request.client.host == 127.0.0.1 (uvicorn runs without
      --proxy-headers). Loopback may therefore only be trusted when the
      request carries NO forwarding header; any X-Forwarded-* / Forwarded
      header means "the socket peer is a proxy" -> operator token required.

  F2 (critical) — SPA catch-all arbitrary file read: a raw `//etc/passwd`
      request reaches the route with a LEADING-SLASH path param and pathlib
      absolute-join DISCARDS the dist base. Containment is now enforced by
      _safe_dist_file (resolve + is_relative_to).

  F3 — /api/holdings and /api/stats leak real-wallet data (on-chain cash,
      positions, entry costs) that SEC-02 gates on /api/live/*; they get the
      same DIRECT-loopback-or-token gate.

All hermetic: tmp DBs, monkeypatched config, ASGI transport (no network).
`httpx.ASGITransport` presents client.host == 127.0.0.1 — exactly the
spoofed-loopback shape a real reverse proxy produces, so these tests
reproduce the production attack paths directly.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

import config
from api import db
from api.main import app, _safe_dist_file

TOKEN = "test-admin-token-xyz"


@pytest_asyncio.fixture
async def fresh_db(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "authz_test.db"
    monkeypatch.setattr(config, "INGESTED_KNOWLEDGE_DIR", tmp_path / "kb")
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(config, "LIVE_BOOK_PUBLIC", False)
    await db.init_db()
    yield


@pytest_asyncio.fixture
async def client(fresh_db):
    import httpx
    from data_providers.mock import MockProvider
    from llm.narrator import Narrator

    app.state.provider = MockProvider()
    app.state.narrator = Narrator()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1") as c:
        yield c


# ---------------------------------------------------------------------------
# F1 — proxy-aware loopback trust
# ---------------------------------------------------------------------------

async def test_proxied_loopback_is_not_trusted_on_live_book(client):
    """The deploy-guide shape: Caddy forwards -> client.host == 127.0.0.1.
    Without a token the request must be refused (403), not authenticated by
    the spoofed socket address."""
    r = await client.get(
        "/api/live/portfolio",
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert r.status_code == 403


async def test_proxied_loopback_with_token_is_allowed(client, monkeypatch):
    from live_execution import config as le_config
    monkeypatch.setattr(
        le_config, "LIVE_TRADING_ENABLED", False)   # reads as disarmed, gated
    r = await client.get(
        "/api/live/portfolio",
        headers={"X-Forwarded-For": "203.0.113.9",
                 "X-Admin-Token": TOKEN},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False   # disarmed, but AUTH passed


async def test_direct_loopback_without_token_still_allowed(client):
    """No forwarding headers + loopback socket = the local operator."""
    r = await client.get("/api/live/portfolio")
    assert r.status_code == 200


async def test_admin_token_unset_refuses_proxied_loopback(
        fresh_db, monkeypatch):
    """Fail-closed: no ADMIN_TOKEN configured -> the gate refuses proxied
    (and any non-direct) request outright, even on loopback sockets."""
    import httpx

    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    app.state.provider = None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1") as c:
        r = await c.get("/api/live/portfolio",
                        headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403


async def test_live_book_public_escape_hatch(client, monkeypatch):
    monkeypatch.setattr(config, "LIVE_BOOK_PUBLIC", True)
    r = await client.get(
        "/api/live/portfolio",
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert r.status_code == 200



# ---------------------------------------------------------------------------
# F3 — holdings/stats get the same gate
# ---------------------------------------------------------------------------

async def test_proxied_holdings_requires_token(client):
    r = await client.get(
        "/api/holdings", headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403


async def test_proxied_stats_requires_token(client):
    r = await client.get(
        "/api/stats", headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403


async def test_proxied_holdings_with_token_allowed(client):
    r = await client.get(
        "/api/holdings",
        headers={"X-Forwarded-For": "203.0.113.9", "X-Admin-Token": TOKEN},
    )
    assert r.status_code == 200


async def test_direct_loopback_holdings_allowed(client):
    r = await client.get("/api/holdings")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# F2 — SPA catch-all path containment
# ---------------------------------------------------------------------------

async def test_spa_leading_slash_absolute_path_serves_shell(client):
    """The confirmed arbitrary-file-read variant: a raw `//etc/passwd`
    request. Must serve the SPA shell, never /etc/passwd."""
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1") as c:
        r = await c.request("GET", "http://127.0.0.1//etc/passwd")
    assert r.status_code == 200
    assert "root:" not in r.text          # NEVER the passwd file


async def test_safe_dist_file_blocks_traversal_and_absolute():
    # Absolute join: pathlib would discard the dist base for a leading slash.
    assert _safe_dist_file("/etc/passwd") is None
    # Traversal.
    assert _safe_dist_file("../../etc/passwd") is None
    assert _safe_dist_file("assets/../../.env") is None
    # Empty / non-file / made-up names resolve to None (shell is served).
    assert _safe_dist_file("") is None
    assert _safe_dist_file("no-such-file.abc") is None


async def test_spa_serves_real_asset_when_contained(client):
    """A real dist asset still resolves (favicon etc.) — the guard is
    containment, not a blanket deny."""
    dist = config.BASE_DIR.parent / "frontend" / "dist"
    if not dist.exists():
        pytest.skip("frontend/dist not built in this checkout")
    files = [p for p in dist.rglob("*") if p.is_file()][:1]
    if not files:
        pytest.skip("dist empty")
    rel = files[0].relative_to(dist).as_posix()
    r = await client.get(f"/{rel}")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Sanity — the deliberate public research surface stays public
# ---------------------------------------------------------------------------

async def test_public_research_surface_stays_public(client):
    """Feed/proof/disclosure are the documented transparency model — the
    gate deliberately does NOT apply to them."""
    r = await client.get("/api/feed?limit=1",
                         headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 200
    r = await client.get("/api/system-status",
                         headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 200
