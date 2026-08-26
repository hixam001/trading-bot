"""Offline tests for the omo-style executor guards and ledger sell math."""
import asyncio
import time
from pathlib import Path

from live_execution import config as le_config
from live_execution.executor import OrderResult, place_order, quote_impact_pct
from live_execution.models import ExecutionLedger


def _tmp_ledger(tmp_path):
    return ExecutionLedger(tmp_path / "exec.json")


def test_quote_impact_fraction_to_percent():
    assert quote_impact_pct({"priceImpactPct": "0.012"}) == 1.2
    assert quote_impact_pct({}) == 0.0
    assert quote_impact_pct({"priceImpactPct": "junk"}) == 0.0


def test_place_order_unarmed_by_default():
    r = asyncio.run(place_order(side="buy", mint="Mint1111", symbol="T", usd=25))
    assert r.status == "unarmed"
    assert "disarmed" in r.reason
    r2 = asyncio.run(place_order(side="sell", mint="Mint1111", symbol="T", fraction=1.0))
    assert r2.status == "unarmed"


def test_order_result_defaults_and_json():
    r = OrderResult()
    j = r.to_json()
    assert j["status"] == "" and j["signature"] == ""


def test_reduce_position_partial_then_full(tmp_path):
    led = _tmp_ledger(tmp_path)
    led.record_buy(idempotency_key="k1", mint="MINT", usd_size=100.0,
                   tokens_out=50.0, price_usd=2.0, signature="sig1")
    # trim 20% of the position: proceeds 30 for a 20-cost slice
    rec = led.reduce_position("MINT", fraction=0.2, proceeds_usd=30.0)
    assert rec.pnl_usd == 10.0
    assert led.open_token_amounts()["MINT"] == 40.0
    open_pos = led.open_positions()
    assert abs(open_pos["MINT"] - 80.0) < 1e-9
    # close the rest
    rec2 = led.reduce_position("MINT", fraction=1.0, proceeds_usd=90.0)
    assert rec2.pnl_usd == 10.0
    assert "MINT" not in led.open_token_amounts()
    assert led.realized_pnl_today() == 20.0


def test_deployed_today_and_cap_math(tmp_path, monkeypatch):
    led = _tmp_ledger(tmp_path)
    led.record_buy(idempotency_key="a", mint="A", usd_size=120.0,
                   tokens_out=1.0, price_usd=120.0, signature="s")
    assert led.deployed_today_usd() == 120.0
    remaining = le_config.MAX_DAILY_DEPLOY_USD - led.deployed_today_usd()
    assert remaining == 180.0   # 300 default cap


def test_commit_log_seal_bind_roundtrip(tmp_path):
    from live_execution.commit_log import CommitLog
    cl = CommitLog(tmp_path / "commits.json")
    rec = cl.seal("buy", {"mint": "M", "usd": 25})
    assert rec["status"] == "sealed" and rec["hash"]
    assert cl.bind(rec["hash"], "sig123") is True
    rows = cl.recent(5)
    assert rows[0]["status"] == "bound" and rows[0]["signature"] == "sig123"
    cl2 = CommitLog(tmp_path / "commits.json")
    assert len(cl2.all()) == 1   # persisted across instances
    assert cl2.bind(rec["hash"], "again") is False  # already bound
