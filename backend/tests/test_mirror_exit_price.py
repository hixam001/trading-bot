"""
tests/test_mirror_exit_price.py — A3 (repo audit): the mirrored exit price
must be the FILL price (actual proceeds / tokens actually sold), not the
journal's token count.

When reconcile clamps a held fraction (dust / fee rounding), the old
computation divided the actual proceeds by the journal's token count,
understating the exit price and skewing the mirrored realized_pnl_pct
that calibration / learning / perf_report consume. pnl_usd keeps the §50
full-cost write-off (dust); only the price must be the fill price.

Hermetic: tmp SQLite, tmp ledger, stubbed jupiter/place_order/mirror.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

import config as live_config
from live_execution.models import ExecutionLedger
import run_live_cycle as rlc

MINT = "MINTCCC1111111111111111111111111111111111111"



class StubJupiter:
    def __init__(self, price: float):
        self.price = price

    async def get_current_price(self, mint: str, decimals=None) -> float:
        return self.price


@pytest_asyncio.fixture
async def _db(tmp_path, monkeypatch):
    """Hermetic temp SQLite (repo convention) + mock backend."""
    monkeypatch.setattr(live_config, "DB_PATH", tmp_path / "mirror_exit.db")
    monkeypatch.setattr(live_config, "DATA_BACKEND", "mock")
    from api import db
    await db.init_db()
    yield db

@pytest.fixture
def manage_env(monkeypatch, tmp_path):
    calls = []

    async def fake_decimals(mint):
        return 6


    async def fake_place_order(**kw):
        calls.append(kw)
        # A reconcile-clamped fill: the chain sold 1500 of the 1665
        # journal tokens for $30.00 actual proceeds.
        return SimpleNamespace(
            status="filled", reason="", usd_value=30.0, tokens=1500.0,
            signature="SIGSELL", price_impact_pct=0.1,
        )


    async def fake_mirror_close(mint, exit_price_usd, pnl_usd, rule_id):
        calls.append({"mirror_close": {
            "mint": mint, "exit_price_usd": exit_price_usd,
            "pnl_usd": pnl_usd, "rule_id": rule_id}})


    async def noop_reflection(*a, **kw):
        return None

    ledger = ExecutionLedger(tmp_path / "mirror_exit_exec.json")
    monkeypatch.setattr(rlc.solana, "get_mint_decimals", fake_decimals)
    monkeypatch.setattr(rlc, "place_order", fake_place_order)
    monkeypatch.setattr(rlc, "_mirror_live_close", fake_mirror_close)
    monkeypatch.setattr(rlc, "_store_live_reflection", noop_reflection)
    rlc._DECIMALS_CACHE.clear()
    yield calls, ledger
    rlc._DECIMALS_CACHE.clear()



async def test_exit_price_is_the_fill_price_not_the_journal_count(_db,
                                                                  manage_env):
    calls, ledger = manage_env
    ledger.record_buy("idem-1", MINT, 67.0, 1665.0, 0.04022, "sig",
                      "confirmed")
    meta = {MINT: {"price_usd": 0.04022, "tokens": 1665.0, "cost": 67.0,
                   "opened_ts": 1_700_000_000.0}}
    hwm: dict = {}
    # -50% vs entry: the stop fires; the stub fill is the clamped one.
    await rlc._manage(StubJupiter(0.02), ledger, hwm, meta)
    mirror = [c["mirror_close"] for c in calls if "mirror_close" in c]
    assert len(mirror) == 1
    mc = mirror[0]
    assert mc["rule_id"] == "exit_stop_loss"
    assert mc["exit_price_usd"] == pytest.approx(30.0 / 1500.0)
    # the exact regression shape: the old code divided by the journal's
    # 1665 tokens (0.018018...) — never again.
    assert mc["exit_price_usd"] != pytest.approx(30.0 / 1665.0)
    # pnl keeps the §50 full-cost write-off: proceeds minus full journal cost
    assert mc["pnl_usd"] == pytest.approx(30.0 - 67.0)


async def test_exit_price_falls_back_to_journal_tokens_when_unclamped(
        _db, manage_env, monkeypatch):
    calls, ledger = manage_env
    ledger.record_buy("idem-1", MINT, 67.0, 1665.0, 0.04022, "sig",
                      "confirmed")
    meta = {MINT: {"price_usd": 0.04022, "tokens": 1665.0, "cost": 67.0,
                   "opened_ts": 1_700_000_000.0}}
    hwm: dict = {}

    async def unclamped(**kw):
        calls.append(kw)
        return SimpleNamespace(
            status="filled", reason="", usd_value=33.0, tokens=None,
            signature="SIGSELL", price_impact_pct=0.1,
        )

    monkeypatch.setattr(rlc, "place_order", unclamped)
    await rlc._manage(StubJupiter(0.02), ledger, hwm, meta)
    mirror = [c["mirror_close"] for c in calls if "mirror_close" in c]
    assert len(mirror) == 1
    # no clamped token count on the result -> the journal count (1665) is
    # the only honest denominator: 33.0 / 1665.
    assert mirror[0]["exit_price_usd"] == pytest.approx(33.0 / 1665.0)