"""
tests/test_funnel_snapshots.py — §64: persisted funnel snapshots.

Properties verified:
  1. compute_funnel() classifies EXACTLY like /api/funnel and
     scripts/perf_report.py §57 (verdict=fail + empty failed_rule_ids =
     MODEL refusal; non-empty = GATE refusal; filled = bound commits)
  2. the rate stays None when there are no gate-passers (null-never-zero,
     in the PERSISTED series too, not just the endpoint)
  3. _snapshot_funnel() writes one throttled row per window; an immediate
     second call is a no-op (an idle engine must not pad the trend)
  4. snapshots read back OLDEST first (a trend series reads left-to-right)
  5. GET /api/funnel/snapshots serves the stored rows verbatim — the UI
     trend renders stored data, never re-derives it
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

import config
from api import db
from api.main import app
from models import FeedEvent
from run_live_cycle import _snapshot_funnel


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ev(symbol: str, verdict: str, failed: list) -> FeedEvent:
    return FeedEvent(
        symbol=symbol, mint_address="Mint" + symbol + "x" * 30,
        verdict=verdict, thesis="thesis " + symbol,
        rule_breakdown=[{"rule_id": "liquidity_floor", "passed": not failed,
                         "detail": "ok", "value": 1}],
        failed_rule_ids=failed, narration_source="deepseek")


@asynccontextmanager
async def conn():
    async with db.get_db() as c:
        yield c


async def _rows(limit: int = 10) -> list[dict]:
    async with conn() as c:
        return await db.get_funnel_snapshots(c, limit=limit)


@pytest_asyncio.fixture
async def funnel_db(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "snap_test.db"
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    await db.init_db()
    async with db.get_db() as c:
        for i in range(2):  # pass rows -> model approved
            await db.insert_feed_event(c, _ev("P" + str(i), "pass", []))
        for i in range(2):  # fail + empty failed_rule_ids -> MODEL refusal
            await db.insert_feed_event(c, _ev("M" + str(i), "fail", []))
        for i in range(2):  # fail + rules -> GATE refusal
            await db.insert_feed_event(
                c, _ev("G" + str(i), "fail", ["liquidity_floor"]))
    yield


@pytest_asyncio.fixture
async def client():
    from data_providers.mock import MockProvider
    from llm.narrator import Narrator

    app.state.provider = MockProvider()
    app.state.narrator = Narrator()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as c:
        yield c


async def test_compute_funnel_matches_the_57_classification(funnel_db):
    from api.routes.funnel import _funnel_window, compute_funnel
    feed, commits, total = await _funnel_window(1000)
    counts = compute_funnel(feed, commits, total, 1000)
    assert counts["candidates_seen"] == 6
    assert counts["gate_refused"] == 2
    assert counts["model_refused"] == 2
    assert counts["gate_passed"] == 4
    assert counts["model_approved"] == 2
    assert counts["filled"] == 0
    assert counts["model_refusal_rate_of_gate_passers"] == 0.5


async def test_snapshot_written_then_throttled(funnel_db):
    first = await _snapshot_funnel()
    assert first is not None
    rows = await _rows()
    assert len(rows) == 1
    snap = rows[0]
    assert snap["candidates_seen"] == 6
    assert snap["gate_refused"] == 2
    assert snap["model_refused"] == 2
    assert snap["gate_passed"] == 4
    assert snap["model_refusal_rate"] == 0.5
    # An immediate second write is throttled — no identical-row padding.
    assert await _snapshot_funnel() is None
    assert len(await _rows()) == 1


async def test_snapshot_written_again_after_interval(funnel_db):
    first = await _snapshot_funnel()
    assert first is not None
    # Backdate the stored row beyond the interval window.
    old_ts = (_now() - timedelta(seconds=400)).isoformat()
    async with conn() as c:
        await c.execute("UPDATE funnel_snapshots SET ts = ? WHERE id = ?",
                        (old_ts, first))
        await c.commit()
    second = await _snapshot_funnel()
    assert second is not None
    rows = await _rows()
    assert [r["id"] for r in rows] == [first, second]  # oldest first


async def test_snapshot_rate_is_null_without_gate_passers(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "snap_empty.db"
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    await db.init_db()
    row_id = await _snapshot_funnel()
    assert row_id is not None
    snap = (await _rows())[0]
    assert snap["gate_passed"] == 0
    assert snap["model_refusal_rate"] is None   # null-never-zero, persisted


async def test_snapshots_endpoint_serves_stored_rows(funnel_db, client):
    await _snapshot_funnel()
    r = await client.get("/api/funnel/snapshots")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    snap = body["snapshots"][0]
    assert snap["model_refused"] == 2
    # The live /api/funnel and the stored snapshot agree line-by-line.
    live = (await client.get("/api/funnel")).json()
    for field in ("candidates_seen", "gate_refused", "model_refused",
                  "gate_passed", "model_approved", "filled"):
        assert snap[field] == live[field], field
    assert (snap["model_refusal_rate"]
            == live["model_refusal_rate_of_gate_passers"])

