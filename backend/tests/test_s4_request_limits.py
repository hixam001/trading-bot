"""
backend/tests/test_s4_request_limits.py — S4: bounded request bodies and params.

The API is public for read feeds and operator-only for mutation. Even an
authenticated operator body must be bounded before it is parsed into
Pydantic models or written to disk.
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

import config
from api import db
from api.main import app


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "request-limits.db")
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", False)
    await db.init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_oversized_body_is_rejected_before_parsing(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUEST_BODY_BYTES", 1024)
    body = b"x" * 2048
    response = await client.post(
        "/api/knowledge-base/ingest", content=body,
        headers={"Content-Type": "application/json"})

    assert response.status_code == 413, response.text
    assert "too large" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_body_at_the_cap_is_still_parsed(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUEST_BODY_BYTES", 1024)
    # Small valid JSON below the cap reaches the auth gate (403), not 413.
    response = await client.post(
        "/api/knowledge-base/ingest", json={"documents": []})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_chunked_body_without_content_length_is_capped(client, monkeypatch):
    """A client can omit Content-Length by streaming; count bytes as they
    arrive and stop at the cap."""
    monkeypatch.setattr(config, "MAX_REQUEST_BODY_BYTES", 512)

    async def _chunks():
        for _ in range(4):
            yield b"x" * 256

    response = await client.post(
        "/api/knowledge-base/ingest", content=_chunks(),
        headers={"Content-Type": "application/json"})
    assert response.status_code == 413, response.text


@pytest.mark.asyncio
async def test_query_and_path_bounds_are_enforced(client):
    assert (await client.get("/api/feed", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/feed", params={"limit": 501})).status_code == 422
    assert (await client.get("/api/feed", params={"offset": -1})).status_code == 422
    bad_mint = "not-a-solana-mint"
    response = await client.get(f"/api/mint/{bad_mint}/history")
    assert response.status_code == 422
    assert "valid Solana" in response.json()["detail"]
