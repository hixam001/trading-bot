"""
tests/test_funnel.py — §63: /api/funnel serves the §57 refusal funnel with
the EXACT classification scripts/perf_report.py uses: verdict=fail with
EMPTY failed_rule_ids = the MODEL declined among gate-passers;
non-empty = the GATE refused. The UI funnel must reconcile with the
operator report line-by-line (§63 verification requirement).
"""
from __future__ import annotations

import pytest_asyncio

import config
from api import db
from api.main import app
from models import FeedEvent


def _ev(symbol: str, verdict: str, failed: list) -> FeedEvent:
    return FeedEvent(
        symbol=symbol, mint_address="Mint" + symbol + "x" * 30,
        verdict=verdict, thesis="thesis " + symbol,
        rule_breakdown=[{"rule_id": "liquidity_floor", "passed": not failed,
                         "detail": "ok", "value": 1}],
        failed_rule_ids=failed, narration_source="deepseek")


@pytest_asyncio.fixture
async def funnel_db(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "funnel_test.db"
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    await db.init_db()
    async with db.get_db() as conn:
        for i in range(2):  # pass rows -> model approved
            await db.insert_feed_event(conn, _ev("P" + str(i), "pass", []))
        for i in range(2):  # fail + empty failed_rule_ids -> MODEL refusal
            await db.insert_feed_event(conn, _ev("M" + str(i), "fail", []))
        for i in range(2):  # fail + rules -> GATE refusal
            await db.insert_feed_event(
                conn, _ev("G" + str(i), "fail", ["liquidity_floor"]))
    yield

@pytest_asyncio.fixture
async def client(funnel_db):
    import httpx
    from data_providers.mock import MockProvider
    from llm.narrator import Narrator

    app.state.provider = MockProvider()
    app.state.narrator = Narrator()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_funnel_counts_match_the_57_classification(client):
    r = await client.get("/api/funnel")
    assert r.status_code == 200
    body = r.json()
    assert body["candidates_seen"] == 6
    assert body["gate_refused"] == 2
    assert body["model_refused"] == 2
    assert body["gate_passed"] == 4  # model_refused + model_approved
    assert body["model_approved"] == 2
    assert body["filled"] == 0  # no sealed commits in this fixture
    assert body["model_refusal_rate_of_gate_passers"] == 0.5


async def test_funnel_null_rate_when_no_decisions(funnel_db, client):
    # A fresh empty database: the rate must stay None, never a zero.
    config.DB_PATH = config.DB_PATH.parent / "funnel_empty.db"
    await db.init_db()
    r = await client.get("/api/funnel")
    body = r.json()
    assert body["candidates_seen"] == 0
    assert body["gate_passed"] == 0
    assert body["model_refusal_rate_of_gate_passers"] is None