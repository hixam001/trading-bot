"""
tests/test_safety_endpoint.py — §64: /api/safety, the safety-state surface
for the SystemStatus safety section and the persistent alert strip.

Properties verified:
  1. required fields with correct shapes (kill_switch / daily_loss_breaker /
     blocklist / break / llm / crowd_chain / alerts / thresholds)
  2. fail-closed kill switch: tripped file -> engaged + a fail alert; corrupt
     file -> engaged (never "assume fine"); no file -> clear, no alert
  3. breaker view: today's realized P&L from the ledger + headroom computed
     server-side (no client money math)
  4. blocklist summary: manual/auto counts + most recent addition
  5. break state pass-through (fail-closed like the gate's not_on_break)
  6. alerts: LLM down, crowd chain exhausted, no-fills-while-flowing; and
     NOTHING when all clear (an empty strip is the feature working)
  7. authz: live-book posture — direct loopback allowed, proxied without a
     token refused (same rule as /api/live/portfolio, SEC-02)
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

import config
from api import db
from api.main import app
from models import FeedEvent


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_kill_switch(tripped: bool, reason: str = "") -> None:
    path = Path(str(config.KILL_SWITCH_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"tripped": tripped, "reason": reason, "ts": time.time()}))


def _ev(symbol: str, verdict: str, failed: list, ts: str | None = None) -> FeedEvent:
    return FeedEvent(
        symbol=symbol, mint_address="Mint" + symbol + "x" * 30,
        verdict=verdict, thesis="thesis " + symbol,
        rule_breakdown=[{"rule_id": "liquidity_floor", "passed": not failed,
                         "detail": "ok", "value": 1}],
        failed_rule_ids=failed, narration_source="deepseek", ts=ts or _now_iso())


async def _seed_flow(n: int) -> None:
    async with db.get_db() as conn:
        for i in range(n):
            await db.insert_feed_event(conn, _ev(f"F{i}", "fail", []))


async def _seed_bound_commit() -> None:
    async with db.get_db() as conn:
        await db.insert_decision_commit(
            conn, created_at=_now_iso(), tick_ts=_now_iso(),
            symbol="AAA", mint_address="Mint" + "A" * 30, verdict="buy",
            entry_allowed=True, nonce="n1", payload_json="{}",
            payload_hash="h1")
        commit_id = await db.get_commit_id_by_hash(conn, "h1")
        await db.bind_commit_signature(
            conn, commit_id, "sig123", "filled", "exact")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    # Kill switch: conftest already retargets the path to a throwaway dir;
    # make sure it starts CLEAR for each test.
    ks = Path(str(config.KILL_SWITCH_FILE))
    if ks.is_file():
        ks.unlink()
    # Break state: clear (conftest path is already throwaway).
    bs = Path(str(config.BREAK_STATE_FILE))
    if bs.is_file():
        bs.unlink()
    # Blocklist: throwaway sidecar per test.
    monkeypatch.setattr(config, "BLOCKLIST_STATE_FILE",
                        str(tmp_path / "blocklist_state.json"))
    from data_providers import crowd
    monkeypatch.setattr(crowd, "_chain_status_path", lambda: tmp_path / "chain.json")
    # Endpoint tests publish the configured fixture state before each read.
    read_observation = crowd.observed_chain_status
    def observed():
        crowd.publish_chain_status()
        return read_observation()
    monkeypatch.setattr(crowd, "observed_chain_status", observed)
    # LLM health: default to a healthy cached verdict (no network anywhere).
    from api.routes import system_status as ss
    ss._health_cache.update(ok=True, at=time.monotonic())
    yield
    ss._health_cache.update(ok=None, at=0.0)


@pytest_asyncio.fixture
async def fresh_db(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "safety_test.db"
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
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://127.0.0.1") as c:
        yield c


# ---------------------------------------------------------------------------
# Shape + fail-closed safety state
# ---------------------------------------------------------------------------

async def test_blocklist_corrupt_read_does_not_mutate(client):
    path = Path(config.BLOCKLIST_STATE_FILE)
    path.write_text("{broken")
    body = (await client.get("/api/safety")).json()
    assert body["blocklist"]["blocks"] is None
    assert path.read_text() == "{broken"
    assert not path.with_suffix(".corrupt").exists()


async def test_safety_required_fields(client):
    r = await client.get("/api/safety")
    assert r.status_code == 200
    body = r.json()
    for field in ("generated_at_utc", "armed", "kill_switch",
                  "daily_loss_breaker", "blocklist", "break", "llm",
                  "crowd_chain", "alerts", "thresholds"):
        assert field in body, f"missing field: {field}"
    assert body["kill_switch"]["engaged"] is False
    assert body["break"]["on_break"] is False
    assert body["blocklist"]["blocks"] == 0
    assert body["alerts"] == []          # all clear -> empty list, never noise


async def test_kill_switch_tripped_surfaces_and_alerts(client):
    _write_kill_switch(True, "AUTO: realized daily loss breached breaker")
    r = await client.get("/api/safety")
    body = r.json()
    assert body["kill_switch"]["engaged"] is True
    assert body["daily_loss_breaker"]["engaged"] is True
    kill_alerts = [a for a in body["alerts"] if a["id"] == "kill_switch"]
    assert len(kill_alerts) == 1
    assert kill_alerts[0]["severity"] == "fail"
    assert "AUTO" in kill_alerts[0]["message"]


@pytest.mark.parametrize("reason", ["operator maintenance", "AUTO: unrelated halt"])
async def test_manual_kill_is_not_daily_loss_trip(client, reason):
    _write_kill_switch(True, reason)
    body = (await client.get("/api/safety")).json()
    assert body["kill_switch"]["engaged"] is True
    assert body["daily_loss_breaker"]["engaged"] is False
    assert "kill_switch" in [a["id"] for a in body["alerts"]]


async def test_kill_switch_corrupt_file_fails_closed(client):
    path = Path(str(config.KILL_SWITCH_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    r = await client.get("/api/safety")
    body = r.json()
    assert body["kill_switch"]["engaged"] is True  # never "assume fine"


async def test_break_state_pass_through(client):
    from rule_engine import liveness
    liveness.set_break(True, minutes=30, reason="manual pause")
    r = await client.get("/api/safety")
    body = r.json()
    assert body["break"]["on_break"] is True
    assert body["break"]["reason"] == "manual pause"
    assert body["break"]["break_until_epoch"] > time.time()
    # A break is routine and self-expiring: visible in the panel, NOT an alert.
    assert body["alerts"] == []
    liveness.set_break(False)


async def test_blocklist_summary_counts_and_latest(client):
    import blocklist
    blocklist.block_mint("MintAAA" + "a" * 24, "AAA", "rugged", kind="manual")
    blocklist.block_mint("MintBBB" + "b" * 24, "BBB",
                         "3 consecutive loss closes", kind="auto")
    # A closes-history-only entry is memory, not a block — never counted.
    blocklist.record_close_outcome("MintCCC" + "c" * 24, "CCC",
                                   "exit_stop_loss", -1.0)
    r = await client.get("/api/safety")
    bl = r.json()["blocklist"]
    assert bl["blocks"] == 2
    assert bl["manual"] == 1
    assert bl["auto"] == 1
    assert bl["latest"]["symbol"] in ("AAA", "BBB")
    assert bl["latest"]["reason"] != ""


# ---------------------------------------------------------------------------
# Breaker view (ledger numbers, headroom computed server-side)
# ---------------------------------------------------------------------------

async def test_breaker_pnl_and_headroom_from_ledger(client, monkeypatch, tmp_path):
    from live_execution import config as le_config
    from live_execution.models import ExecutionLedger
    state = tmp_path / "le_state"
    state.mkdir()
    monkeypatch.setattr(le_config, "STATE_DIR", state)
    ledger = ExecutionLedger(state / "executions.json")
    # $100 cost closed for $80 proceeds -> realized -20 (pnl = proceeds - cost).
    ledger.record_buy("b1", "AAA", 100.0, 1.0, 100.0, "sig")
    ledger.mark_close("AAA", proceeds_usd=80.0)   # realized -20 today
    r = await client.get("/api/safety")
    breaker = r.json()["daily_loss_breaker"]
    assert breaker["limit_usd"] == 75.0
    assert breaker["realized_pnl_today_usd"] == pytest.approx(-20.0)
    # Headroom: how much further the realized loss can go before the breaker.
    assert breaker["headroom_usd"] == pytest.approx(55.0)
    assert breaker["engaged"] is False


async def test_breaker_missing_ledger_is_zero_not_none(client, monkeypatch, tmp_path):
    """No ledger file = an empty book. Today's realized P&L is then genuinely
    0.0 (a fact about an empty book, not a fabrication), and headroom is the
    full limit — the operator has not lost anything yet."""
    from live_execution import config as le_config
    monkeypatch.setattr(le_config, "STATE_DIR", tmp_path / "fresh")
    r = await client.get("/api/safety")
    breaker = r.json()["daily_loss_breaker"]
    assert breaker["realized_pnl_today_usd"] == 0.0
    assert breaker["headroom_usd"] == pytest.approx(breaker["limit_usd"])
    assert breaker["limit_usd"] == 75.0


async def test_breaker_corrupt_ledger_degrades_to_none(client, monkeypatch, tmp_path):
    """A CORRUPT ledger is UNREADABLE, not empty — the P&L must be None
    (null-never-zero), never a fabricated 0.0 that would read as \"flat day\"
    while real positions are unaccounted for."""
    from live_execution import config as le_config
    state = tmp_path / "corrupt"
    state.mkdir()
    (state / "executions.json").write_text("{not json")
    monkeypatch.setattr(le_config, "STATE_DIR", state)
    r = await client.get("/api/safety")
    breaker = r.json()["daily_loss_breaker"]
    assert breaker["realized_pnl_today_usd"] is None   # never fabricated
    assert breaker["headroom_usd"] is None
    assert breaker["limit_usd"] == 75.0                # the limit is still truth


# ---------------------------------------------------------------------------
# Alert conditions
# ---------------------------------------------------------------------------

async def test_llm_down_alert(client, monkeypatch):
    from api.routes import system_status as ss
    ss._health_cache.update(ok=False, at=time.monotonic())
    r = await client.get("/api/safety")
    body = r.json()
    assert body["llm"]["main_reachable"] is False
    ids = [a["id"] for a in body["alerts"]]
    assert "llm_down" in ids
    assert [a for a in body["alerts"] if a["id"] == "llm_down"][0]["severity"] == "fail"


async def test_crowd_chain_exhausted_alert(client, monkeypatch):
    from data_providers import crowd
    monkeypatch.setattr(crowd, "_configured_scrapers",
                        lambda: [("scrapling", None), ("firecrawl", None)])
    monkeypatch.setattr(crowd, "_is_benched", lambda name: True)
    r = await client.get("/api/safety")
    body = r.json()
    assert body["crowd_chain"]["exhausted"] is True
    ids = [a["id"] for a in body["alerts"]]
    assert "data_chain" in ids


async def test_chain_not_exhausted_when_a_scraper_is_live(client, monkeypatch):
    from data_providers import crowd
    monkeypatch.setattr(crowd, "_configured_scrapers",
                        lambda: [("scrapling", None), ("firecrawl", None)])
    monkeypatch.setattr(crowd, "_is_benched", lambda name: name == "firecrawl")
    r = await client.get("/api/safety")
    body = r.json()
    assert body["crowd_chain"]["exhausted"] is False
    assert "data_chain" not in [a["id"] for a in body["alerts"]]


async def test_no_fills_alert_when_candidates_flow_without_fills(client):
    await _seed_flow(10)
    r = await client.get("/api/safety")
    ids = [a["id"] for a in r.json()["alerts"]]
    assert "no_fills" in ids


async def test_no_fills_alert_cleared_by_a_recent_fill(client):
    await _seed_flow(10)
    await _seed_bound_commit()
    r = await client.get("/api/safety")
    assert "no_fills" not in [a["id"] for a in r.json()["alerts"]]


async def test_no_fills_alert_requires_candidates_flowing(client):
    # An empty window is quiet hours, not an alert (threshold gate).
    await _seed_flow(3)
    r = await client.get("/api/safety")
    assert "no_fills" not in [a["id"] for a in r.json()["alerts"]]


# ---------------------------------------------------------------------------
# Authz — live-book posture
# ---------------------------------------------------------------------------

async def test_safety_proxied_without_token_refused(client):
    r = await client.get("/api/safety",
                         headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403


async def test_safety_direct_loopback_allowed(client):
    # ASGITransport presents 127.0.0.1 with no forwarding headers: the
    # local-dashboard shape. Allowed without a token.
    r = await client.get("/api/safety")
    assert r.status_code == 200


