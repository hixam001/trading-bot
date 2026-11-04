"""
tests/test_mass_assignment.py — Verify mass assignment protections on mutating endpoints.

Tests Rule 14:
- Endpoints only accept specific whitelisted fields.
- Arbitrary injected fields (e.g. role, is_admin, account_status, user_id) are ignored.
- Missing required fields are strictly validated (422).
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

import config
from api import db
from api.main import app

TOKEN = "test-token-operator-secret-32-chars-long"


@pytest_asyncio.fixture
async def app_env(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "mass_assign_test.db"
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(config, "INGESTED_KNOWLEDGE_DIR", tmp_path / "kb_ingested")
    await db.init_db()
    yield


@pytest_asyncio.fixture
async def client(app_env):
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
async def test_kb_ingest_rejects_mass_assignment_fields(client):
    """S2: extra fields like role/is_admin/user_id are rejected, not ignored."""
    payload = {
        "documents": [
            {
                "filename": "whitelisted.md",
                "content": "This is legitimate content for strategy.",
                "role": "admin",
                "is_admin": True,
                "account_status": "superuser",
                "user_id": "00000000-0000-0000-0000-000000000000",
                "permissions": ["all"],
            }
        ],
        "role": "superadmin",
        "is_admin": True,
    }
    r = await client.post(
        "/api/knowledge-base/ingest",
        json=payload,
        headers={"X-Admin-Token": TOKEN},
    )
    assert r.status_code == 422
    assert "extra_forbidden" in r.text

    # Nothing was written.
    async with db.get_db() as conn:
        docs = await db.get_kb_documents(conn)
        assert not any(d["filename"] == "whitelisted.md" for d in docs)


@pytest.mark.asyncio
async def test_kb_ingest_rejects_missing_required_fields(client):
    """Missing required schema fields (e.g. filename or content) triggers 422 validation error."""
    # Missing content
    r1 = await client.post(
        "/api/knowledge-base/ingest",
        json={"documents": [{"filename": "doc.md"}]},
        headers={"X-Admin-Token": TOKEN},
    )
    assert r1.status_code == 422

    # Missing filename
    r2 = await client.post(
        "/api/knowledge-base/ingest",
        json={"documents": [{"content": "hello"}]},
        headers={"X-Admin-Token": TOKEN},
    )
    assert r2.status_code == 422


@pytest.mark.asyncio
async def test_login_rejects_mass_assignment_fields(client):
    """S2: extra attributes in the login body (role, is_admin, ...) are rejected."""
    payload = {
        "token": TOKEN,
        "role": "admin",
        "is_admin": True,
        "account_status": "active",
        "user_id": "00000000-0000-0000-0000-000000000000",
    }
    r = await client.post("/api/auth/login", json=payload)
    assert r.status_code == 422
    assert "extra_forbidden" in r.text


@pytest.mark.asyncio
async def test_admin_reset_strictly_validates_mode(client):
    """Confirm admin reset strictly whitelists mode parameter."""
    # Invalid mode rejected
    r_bad = await client.post(
        "/api/admin/reset?confirm=yes&mode=drop_database",
        headers={"X-Admin-Token": TOKEN},
    )
    assert r_bad.status_code == 400
    assert "Unknown mode" in r_bad.json()["detail"]

    # Valid mode accepted
    r_good = await client.post(
        "/api/admin/reset?confirm=yes&mode=prune_only",
        headers={"X-Admin-Token": TOKEN},
    )
    assert r_good.status_code == 200
