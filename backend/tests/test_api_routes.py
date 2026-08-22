"""
tests/test_api_routes.py — endpoint shape/pagination against a seeded DB (J4/H4).
"""
from __future__ import annotations

import pytest
import pytest_asyncio

import config
from api import db
from api.main import app


@pytest_asyncio.fixture
async def seeded_db(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "api_test.db"
    # Keep test ingests out of the real knowledge_base/ingested archive, and
    # keep tests hermetic regardless of the operator's real .env settings.
    kb_dir = tmp_path / "ingested"
    monkeypatch.setattr(config, "INGESTED_KNOWLEDGE_DIR", kb_dir)
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    await db.init_db()
    async with db.get_db() as conn:
        for i in range(7):
            await db.insert_feed_event(conn, FeedEventFactory(i))
        # One opened then closed trade -> journal entry.
        from paper_trading_engine import close_position, open_position
        from tests.test_rules import make_candidate
        c = make_candidate()
        await open_position(conn, c, None)
        trade = await db.get_open_trade_for_mint(conn, c.mint_address)
        await close_position(conn, trade, exit_price=c.price_usd * 2,
                             exit_reason="take_profit")
    yield


def FeedEventFactory(i: int):
    from models import FeedEvent
    return FeedEvent(symbol=f"T{i}", mint_address=f"Mint{i:02d}{'x' * 34}",
                     verdict="pass" if i % 2 == 0 else "fail",
                     thesis=f"thesis {i}",
                     rule_breakdown=[{"rule_id": "liquidity_floor", "passed": True,
                                      "detail": "ok", "value": 1}])


@pytest_asyncio.fixture
async def client(seeded_db):
    import httpx
    from data_providers.mock import MockProvider
    from llm.narrator import Narrator

    app.state.provider = MockProvider()
    app.state.narrator = Narrator()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_feed_shape_and_pagination(client):
    r = await client.get("/api/feed", params={"limit": 3, "offset": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 7 and len(body["events"]) == 3
    ids = [e["id"] for e in body["events"]]
    assert ids == sorted(ids, reverse=True)          # newest first
    ev = body["events"][0]
    for key in ("symbol", "verdict", "thesis", "rule_breakdown",
                "failed_rule_ids", "grounding_flags"):
        assert key in ev
    # Page 2 continues without overlap.
    r2 = await client.get("/api/feed", params={"limit": 3, "offset": 3})
    ids2 = [e["id"] for e in r2.json()["events"]]
    assert not set(ids) & set(ids2)


async def test_journal_contains_closed_trade(client):
    r = await client.get("/api/journal")
    body = r.json()
    assert body["total"] == 1
    t = body["trades"][0]
    assert t["is_open"] is False
    assert t["exit_reason"] == "take_profit"
    assert t["realized_pnl_usd"] is not None


async def test_stats_summary(client):
    r = await client.get("/api/stats")
    body = r.json()
    for key in ("cash_usd", "equity_usd", "win_rate", "closed_trades",
                "equity_curve", "paper_trading_only"):
        assert key in body
    assert body["closed_trades"] == 1
    assert body["paper_trading_only"] is True


async def test_market_regime_endpoint(client):
    from rule_engine.regime import compute_market_regime
    from tests.test_rules import make_candidate
    batch = [make_candidate()]
    reg = compute_market_regime(batch)
    async with db.get_db() as conn:
        await db.insert_market_regime(
            conn, computed_at=reg.computed_at, candidate_count=1,
            pct_green=reg.pct_candidates_green_1h, median_vol=reg.median_volume_1h_usd,
            avg_ratio=reg.avg_buy_sell_ratio, regime_ok=reg.regime_ok,
            detail=reg.regime_detail)
    r = await client.get("/api/market-regime")
    body = r.json()
    assert body["count"] >= 1
    assert {"computed_at", "candidate_count", "regime_ok"} <= set(body["regimes"][0])


async def test_promotion_gate_endpoint(client):
    r = await client.get("/api/promotion-gate")
    body = r.json()
    assert len(body["criteria"]) == 5
    assert "does not trigger anything automatically" in body["note"]


async def test_system_status_endpoint(client):
    r = await client.get("/api/system-status")
    body = r.json()
    assert body["paper_trading_only"] is True
    assert body["data_backend"] == "mock"
    assert "ollama_reachable" in body and "provider_calls_today" in body


async def test_knowledge_base_endpoints(client):
    r = await client.post("/api/knowledge-base/ingest", json={
        "documents": [
            {"filename": "notes.md", "content": "First note about liquidity. Second sentence."},
            {"filename": "../evil.md", "content": "Should be sanitized."},
            {"filename": "empty.md", "content": "   "},
        ]
    })
    body = r.json()
    assert len(body["ingested"]) == 2          # empty one rejected
    assert any(e["filename"] == "evil.md" for e in body["ingested"])  # path stripped
    r2 = await client.get("/api/knowledge-base")
    kb = r2.json()
    assert "static_knowledge" in kb and len(kb["static_knowledge"]) > 0
    names = [d["filename"] for d in kb["ingested"]]
    assert "notes.md" in names and "evil.md" in names
