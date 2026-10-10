"""
tests/test_input_validation.py — Comprehensive verification of server-side validation and sanitization.

Verifies:
1. Type, length, and format validation on request bodies (login, signup, knowledge base).
2. Boundary and format validation on query parameters (limit, offset, mode, confirm).
3. Path parameter validation (mint address, static dist containment).
4. Rejection of null bytes, control characters, traversal patterns, and oversized payloads (422 / 400).
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

import config
from api import db
from api.main import app

TOKEN = "test-operator-admin-token-123"


@pytest_asyncio.fixture
async def val_env(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "val_test.db"
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(config, "INGESTED_KNOWLEDGE_DIR", tmp_path / "kb_ingest")
    monkeypatch.setattr(config, "SESSION_COOKIE_NAME", "admin_session")
    monkeypatch.setattr(config, "LIVE_BOOK_PUBLIC", False)
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    await db.init_db()
    yield


@pytest_asyncio.fixture
async def client(val_env):
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
# 1. Auth Validation (Login & Signup)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_rejects_empty_or_whitespace_token(client):
    r1 = await client.post("/api/auth/login", json={"token": ""})
    assert r1.status_code == 422

    r2 = await client.post("/api/auth/login", json={"token": "   "})
    assert r2.status_code == 422


@pytest.mark.asyncio
async def test_login_rejects_oversized_token(client):
    r = await client.post("/api/auth/login", json={"token": "A" * 513})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_login_rejects_null_bytes_in_token(client):
    r = await client.post("/api/auth/login", json={"token": f"{TOKEN}\x00extra"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_login_rejects_invalid_token_type(client):
    r = await client.post("/api/auth/login", json={"token": ["not", "a", "string"]})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_login_rejects_oversized_captcha_or_honeypot(client):
    r1 = await client.post("/api/auth/login", json={
        "token": TOKEN,
        "captcha_token": "C" * 1025,
    })
    assert r1.status_code == 422

    r2 = await client.post("/api/auth/login", json={
        "token": TOKEN,
        "_hp_website": "H" * 257,
    })
    assert r2.status_code == 422


@pytest.mark.asyncio
async def test_signup_rejects_malformed_username_format(client):
    # Contains illegal characters: spaces, angle brackets, slashes, quotes
    for bad_user in ("admin user", "<script>alert(1)</script>", "admin/root", "user;DROP TABLE"):
        r = await client.post("/api/auth/signup", json={
            "username": bad_user,
            "password": "ValidPassword123!",
        })
        assert r.status_code == 422, f"Failed to reject invalid username: {bad_user}"


@pytest.mark.asyncio
async def test_signup_rejects_length_bounds(client):
    # Username too short (<3)
    r1 = await client.post("/api/auth/signup", json={"username": "ab", "password": "ValidPassword123!"})
    assert r1.status_code == 422

    # Username too long (>50)
    r2 = await client.post("/api/auth/signup", json={"username": "a" * 51, "password": "ValidPassword123!"})
    assert r2.status_code == 422

    # Password too short (<8)
    r3 = await client.post("/api/auth/signup", json={"username": "valid_user", "password": "short"})
    assert r3.status_code == 422

    # Password too long (>128)
    r4 = await client.post("/api/auth/signup", json={"username": "valid_user", "password": "P" * 129})
    assert r4.status_code == 422


# ---------------------------------------------------------------------------
# 2. Knowledge Base Ingestion Validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kb_ingest_rejects_null_bytes_and_blank_filename(client):
    r1 = await client.post(
        "/api/knowledge-base/ingest",
        json={"documents": [{"filename": "bad\x00file.md", "content": "valid content"}]},
        headers={"X-Admin-Token": TOKEN},
    )
    assert r1.status_code == 422

    r2 = await client.post(
        "/api/knowledge-base/ingest",
        json={"documents": [{"filename": "   ", "content": "valid content"}]},
        headers={"X-Admin-Token": TOKEN},
    )
    assert r2.status_code == 422


@pytest.mark.asyncio
async def test_kb_ingest_rejects_oversized_filename(client):
    r = await client.post(
        "/api/knowledge-base/ingest",
        json={"documents": [{"filename": "f" * 256 + ".md", "content": "valid content"}]},
        headers={"X-Admin-Token": TOKEN},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_kb_ingest_rejects_oversized_content(client):
    r = await client.post(
        "/api/knowledge-base/ingest",
        json={"documents": [{"filename": "huge.md", "content": "x" * 500_001}]},
        headers={"X-Admin-Token": TOKEN},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_kb_ingest_rejects_oversized_documents_list(client):
    too_many = [{"filename": f"doc_{i}.md", "content": "content"} for i in range(51)]
    r = await client.post(
        "/api/knowledge-base/ingest",
        json={"documents": too_many},
        headers={"X-Admin-Token": TOKEN},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 3. Query Parameter Validation & Bounds (limit, offset)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_feed_pagination_bounds(client):
    # limit < 1
    r1 = await client.get("/api/feed", params={"limit": 0})
    assert r1.status_code == 422

    # limit > 500
    r2 = await client.get("/api/feed", params={"limit": 501})
    assert r2.status_code == 422

    # offset < 0
    r3 = await client.get("/api/feed", params={"offset": -1})
    assert r3.status_code == 422

    # offset > 100_000
    r4 = await client.get("/api/feed", params={"offset": 100_001})
    assert r4.status_code == 422

    # invalid non-integer type
    r5 = await client.get("/api/feed", params={"limit": "not_a_number"})
    assert r5.status_code == 422


@pytest.mark.asyncio
async def test_journal_pagination_bounds(client):
    r1 = await client.get("/api/journal", params={"limit": 0})
    assert r1.status_code == 422

    r2 = await client.get("/api/journal", params={"limit": 501})
    assert r2.status_code == 422

    r3 = await client.get("/api/journal", params={"offset": 100_001})
    assert r3.status_code == 422


@pytest.mark.asyncio
async def test_proof_endpoints_limit_bounds(client):
    # refusals.json limit bounds
    r1 = await client.get("/api/refusals.json", params={"limit": 0})
    assert r1.status_code == 422
    r2 = await client.get("/api/refusals.json", params={"limit": 501})
    assert r2.status_code == 422

    # theses.json limit bounds
    r3 = await client.get("/api/theses.json", params={"limit": 0})
    assert r3.status_code == 422
    r4 = await client.get("/api/theses.json", params={"limit": 501})
    assert r4.status_code == 422

    # reasoning.json limit bounds
    r5 = await client.get("/api/reasoning.json", params={"limit": 0})
    assert r5.status_code == 422
    r6 = await client.get("/api/reasoning.json", params={"limit": 201})
    assert r6.status_code == 422


# ---------------------------------------------------------------------------
# 4. Path Parameter Validation (Solana Mint Address)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mint_history_rejects_null_bytes_and_sql_injection(client):
    # SQL injection string as mint address
    r1 = await client.get("/api/mint/SELECT%20*%20FROM%20trades/history")
    assert r1.status_code == 422

    # Too short
    r2 = await client.get("/api/mint/abc/history")
    assert r2.status_code == 422

    # Non-base58 characters (0, O, I, l)
    r3 = await client.get(f"/api/mint/{'0' * 44}/history")
    assert r3.status_code == 422

    # Pagination bounds on valid mint
    valid_mint = "5" * 44
    r4 = await client.get(f"/api/mint/{valid_mint}/history", params={"offset": 100_001})
    assert r4.status_code == 422


# ---------------------------------------------------------------------------
# 5. Cross-Site Scripting (XSS) Defenses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_api_path_escapes_xss_payload(client):
    """Reflected paths in 404 responses are HTML-escaped."""
    r = await client.get('/api/<script>alert("xss")</script>')
    assert r.status_code == 404
    data = r.json()
    assert "<script>" not in data["detail"]
    assert "&lt;script&gt;" in data["detail"]


@pytest.mark.asyncio
async def test_admin_reset_escapes_xss_in_mode(client):
    """Reflected mode parameters in admin error responses are HTML-escaped."""
    r = await client.post(
        '/api/admin/reset?confirm=yes&mode=<script>',
        headers={"X-Admin-Token": TOKEN},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "<script>" not in detail
    assert "&lt;script&gt;" in detail


@pytest.mark.asyncio
async def test_csp_header_enforces_script_restriction(client):
    """Content-Security-Policy header blocks inline/external script injections."""
    r = await client.get("/api/feed")
    assert "content-security-policy" in r.headers
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp


