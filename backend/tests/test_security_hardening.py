"""
tests/test_security_hardening.py — §38 security audit regressions.

Covers:
  F3  operator-token guard on the mutating endpoints (admin reset + KB
      ingest): missing/wrong token -> 403; UNSET token -> endpoint fully
      disabled (fail closed); correct token -> allowed.
  F4  knowledge-ingest size cap (oversized document refused before it
      touches disk/DB).
  F5  baseline security headers on every response; no-store on /api/*.
  F6  CORS narrowed to GET/POST + Content-Type.

All hermetic: tmp DBs, monkeypatched config, ASGI transport (no network).
"""
from __future__ import annotations

import pytest
import pytest_asyncio

import config
from api import db
from api.main import app


@pytest_asyncio.fixture
async def fresh_db(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "sec_test.db"
    monkeypatch.setattr(config, "INGESTED_KNOWLEDGE_DIR", tmp_path / "ingested")
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ------------------------------------------------------------------ F3: admin

async def test_admin_reset_refused_without_token(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit-token")
    r = await client.post("/api/admin/reset", params={"confirm": "yes"})
    assert r.status_code == 403


async def test_admin_reset_refused_with_wrong_token(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit-token")
    r = await client.post("/api/admin/reset", params={"confirm": "yes"},
                          headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 403


async def test_admin_reset_disabled_when_token_unset(client, monkeypatch):
    # FAIL CLOSED: no token configured -> even a supplied header is refused.
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    r = await client.post("/api/admin/reset", params={"confirm": "yes"},
                          headers={"X-Admin-Token": "anything"})
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"]


async def test_admin_reset_prune_works_with_token(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit-token")
    r = await client.post(
        "/api/admin/reset",
        params={"confirm": "yes", "mode": "prune_only"},
        headers={"X-Admin-Token": "sekrit-token"},
    )
    assert r.status_code == 200
    assert r.json()["prune_only"] is True


async def test_admin_reset_token_ok_but_missing_confirm(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit-token")
    r = await client.post("/api/admin/reset",
                          headers={"X-Admin-Token": "sekrit-token"})
    assert r.status_code == 400          # token passed, confirm still required


# ------------------------------------------------------------- F3: kb ingest

async def test_kb_ingest_refused_without_token(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit-token")
    r = await client.post("/api/knowledge-base/ingest",
                          json={"documents": [{"filename": "a.md", "content": "x"}]})
    assert r.status_code == 403


# ------------------------------------------------------------------ F4: size

async def test_ingest_size_cap_refuses_oversized(tmp_path, monkeypatch):
    from knowledge_base import loader

    monkeypatch.setattr(config, "MAX_INGEST_CHARS", 100)
    monkeypatch.setattr(config, "INGESTED_KNOWLEDGE_DIR", tmp_path / "ingested")
    with pytest.raises(ValueError, match="too large"):
        await loader.ingest_file("big.md", "x" * 101)
    # Nothing was written to disk.
    assert not (tmp_path / "ingested").exists() or not list(
        (tmp_path / "ingested").iterdir())


async def test_ingest_empty_still_refused(tmp_path, monkeypatch):
    from knowledge_base import loader

    monkeypatch.setattr(config, "INGESTED_KNOWLEDGE_DIR", tmp_path / "ingested")
    with pytest.raises(ValueError, match="empty"):
        await loader.ingest_file("empty.md", "   ")


# ------------------------------------------------------------- F5/F6: headers

async def test_security_headers_present(client):
    r = await client.get("/api/system-status")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"


async def test_api_responses_are_no_store(client):
    r = await client.get("/api/system-status")
    assert r.headers["cache-control"] == "no-store"


def test_cors_narrowed():
    # The middleware stack must not allow arbitrary methods/headers anymore.
    from fastapi.middleware.cors import CORSMiddleware

    cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    assert cors.kwargs["allow_methods"] == ["GET", "POST"]
    assert cors.kwargs["allow_headers"] == ["Content-Type"]
    assert cors.kwargs["allow_origins"] == [config.FRONTEND_ORIGIN]
