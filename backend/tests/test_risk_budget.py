"""
tests/test_risk_budget.py - REF-R8 drawdown-adaptive risk budget + sizing.

Reference parity (computeBudget() from omotrades/omo, ported verbatim):

    drawdown_factor = clamp(1 + min(0, unrealized)/equity * 2.5, 0.5, 1.0)
    max_order_usd   = round(clamp(equity * 0.035 * df, 25, 3000))
    max_daily_usd   = round(clamp(max_order * 4, 25, 12000))

Every expectation below is hand-computed from those formulas with the
hardcoded constants PER_ORDER_FRACTION=0.035, DAY_MULTIPLE=4,
HARD_ORDER_CEILING_USD=3000, HARD_DAILY_CEILING_USD=12000 and
MIN_TICKET_USD=25. DB-touching tests run against a fresh tmp SQLite DB.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import config
from api import db
from llm.thinker import Thinker
from main import run_tick
from models import Candidate, PortfolioState, Trade
from paper_trading_engine import (
    _round_half_up,
    compute_risk_budget,
    compute_ticket,
    portfolio_equity_and_unrealized,
)


# ------------------------------------------------------- pure budget math ----

def test_flat_book_full_size():
    # equity 1000, no open risk: df 1.0 -> order = 1000 * 0.035 = 35
    b = compute_risk_budget(1000.0, 0.0)
    assert b.drawdown_factor == 1.0
    assert b.max_order_usd == 35.0
    assert b.max_daily_usd == 140.0          # 35 * DAY_MULTIPLE(4)
    assert b.derived is True
    assert "0.035" in b.formula
    assert "equity 1000" in b.formula


def test_green_book_never_grows_ticket():
    # unrealized gains are clipped: min(0, +50) = 0 -> df stays 1.0
    b = compute_risk_budget(1000.0, 50.0)
    assert b.drawdown_factor == 1.0
    assert b.max_order_usd == 35.0


def test_minus_20pct_open_loss_halves_ticket():
    # dd = -400/2000 = -0.2 -> df = 1 - 0.2*2.5 = 0.5
    # order = round(2000 * 0.035 * 0.5) = 35 (full size would be 70)
    b = compute_risk_budget(2000.0, -400.0)
    assert b.drawdown_factor == 0.5
    assert b.max_order_usd == 35.0
    assert b.max_daily_usd == 140.0          # 35 * 4


def test_drawdown_never_sizes_below_min_ticket():
    # reference parity: the clamp low bound IS MIN_TICKET_USD. At equity
    # 1000 and df 0.5 the raw formula gives 17.5 -> floored at 25, exactly
    # as the reference computeBudget does.
    b = compute_risk_budget(1000.0, -200.0)
    assert b.drawdown_factor == 0.5
    assert b.max_order_usd == 25.0
    assert b.max_daily_usd == 100.0          # 25 * 4


def test_drawdown_factor_floors_at_half():
    # dd = -20000/2000 = -10 -> 1 - 25 clamps to the 0.5 floor
    # order = round(2000 * 0.035 * 0.5) = 35
    b = compute_risk_budget(2000.0, -20_000.0)
    assert b.drawdown_factor == 0.5
    assert b.max_order_usd == 35.0


def test_order_ceiling_caps_large_books():
    # 100000 * 0.035 = 3500 -> capped at 3000; daily hits the 12000 ceiling
    b = compute_risk_budget(100_000.0, 0.0)
    assert b.max_order_usd == 3000.0
    assert b.max_daily_usd == 12_000.0


def test_tiny_book_floors_at_min_ticket():
    # 100 * 0.035 = 3.5 -> floored at 25; daily = 25 * 4 = 100
    b = compute_risk_budget(100.0, 0.0)
    assert b.max_order_usd == 25.0
    assert b.max_daily_usd == 100.0


@pytest.mark.parametrize("equity", [0.0, -5.0, float("nan"), float("inf")])
def test_unreadable_equity_fails_closed(equity):
    b = compute_risk_budget(equity, 0.0)
    assert b.derived is False
    assert b.max_order_usd == config.MIN_TICKET_USD
    assert b.drawdown_factor == 1.0


def test_unreadable_unrealized_fails_closed():
    # a NaN open-pnl mark is unreadable -> refuse to guess -> min ticket
    b = compute_risk_budget(1000.0, float("nan"))
    assert b.derived is False
    assert b.max_order_usd == config.MIN_TICKET_USD


def test_round_half_up_matches_js_math_round():
    # Python builtin round() is bankers: round(32.5) == 32. Math.round -> 33.
    assert _round_half_up(32.5) == 33
    assert _round_half_up(2.5) == 3
    assert _round_half_up(17.5) == 18
    assert _round_half_up(17.4999) == 17
    assert _round_half_up(-0.4) == 0


# --------------------------------------------------- portfolio equity marks ----

MINT = "MintRisk1111111111111111111111111111111111"


def _trade(mint: str = MINT, qty: float = 100_000.0) -> Trade:
    return Trade(
        symbol="R",
        mint_address=mint,
        entry_price_usd=0.001,
        position_size_usd=100.0,
        quantity=qty,
        candidate_snapshot={"decimals": 6},
    )


def test_equity_marks_to_market(monkeypatch):
    monkeypatch.setattr(config, "SLIPPAGE_PCT", 0.0)
    monkeypatch.setattr(config, "FEE_PCT", 0.0)
    book = PortfolioState(cash_usd=500.0, open_positions=[_trade()])
    equity, unrealized = portfolio_equity_and_unrealized(book, {MINT: 0.002})
    assert unrealized == pytest.approx(100.0)   # (0.002 - 0.001) * 100k
    assert equity == pytest.approx(700.0)       # 500 cash + 100 cost + 100 pnl


def test_equity_missing_mark_held_at_cost():
    book = PortfolioState(cash_usd=500.0, open_positions=[_trade()])
    equity, unrealized = portfolio_equity_and_unrealized(book, {})
    assert unrealized == 0.0
    assert equity == pytest.approx(600.0)


def test_equity_degenerate_mark_held_at_cost():
    book = PortfolioState(cash_usd=500.0, open_positions=[_trade()])
    equity, unrealized = portfolio_equity_and_unrealized(book, {MINT: -1.0})
    assert unrealized == 0.0
    assert equity == pytest.approx(600.0)


def test_equity_degenerate_position_held_at_cost():
    # qty 0 makes compute_unrealized_pnl raise -> mark at cost, never crash
    book = PortfolioState(cash_usd=500.0, open_positions=[_trade(qty=0.0)])
    equity, unrealized = portfolio_equity_and_unrealized(book, {MINT: 0.002})
    assert unrealized == 0.0
    assert equity == pytest.approx(600.0)


# ------------------------------------------------- ticket: risk_budget mode ----

def test_ticket_risk_budget_flat(monkeypatch):
    monkeypatch.setattr(config, "SIZING_MODE", "risk_budget")
    assert compute_ticket(0.0, None, equity_usd=1000.0,
                          unrealized_usd=0.0) == 35.0


def test_ticket_risk_budget_drawdown(monkeypatch):
    monkeypatch.setattr(config, "SIZING_MODE", "risk_budget")
    # budget at equity 2000, -20% open: df 0.5 -> 35; conviction 1.0 -> 35
    assert compute_ticket(0.0, None, equity_usd=2000.0,
                          unrealized_usd=-400.0, conviction_factor=1.0) == 35.0


def test_ticket_risk_budget_conviction_scales(monkeypatch):
    monkeypatch.setattr(config, "SIZING_MODE", "risk_budget")
    # 35 * 1.2 = 42
    assert compute_ticket(0.0, None, equity_usd=1000.0, unrealized_usd=0.0,
                          conviction_factor=1.2) == 42.0


def test_ticket_risk_budget_conviction_floors_at_min(monkeypatch):
    monkeypatch.setattr(config, "SIZING_MODE", "risk_budget")
    # 35 * 0.6 = 21 -> below MIN_TICKET -> floored at 25
    assert compute_ticket(0.0, None, equity_usd=1000.0, unrealized_usd=0.0,
                          conviction_factor=0.6) == 25.0


def test_ticket_risk_budget_ceiling(monkeypatch):
    monkeypatch.setattr(config, "SIZING_MODE", "risk_budget")
    # budget 3000 * 1.2 = 3600 -> capped at HARD_ORDER_CEILING_USD
    assert compute_ticket(0.0, None, equity_usd=100_000.0, unrealized_usd=0.0,
                          conviction_factor=1.2) == 3000.0


@pytest.mark.parametrize("cf", [None, float("nan"), 0.0, -0.5])
def test_ticket_risk_budget_malformed_conviction_neutral(monkeypatch, cf):
    monkeypatch.setattr(config, "SIZING_MODE", "risk_budget")
    assert compute_ticket(0.0, None, equity_usd=1000.0, unrealized_usd=0.0,
                          conviction_factor=cf) == 35.0


def test_ticket_risk_budget_missing_inputs_fail_closed(monkeypatch):
    monkeypatch.setattr(config, "SIZING_MODE", "risk_budget")
    # no equity/unrealized -> budget fails closed -> minimum ticket
    assert compute_ticket(0.0, None) == config.MIN_TICKET_USD


# -------------------------------------- daily ceiling through a real tick ----

def make_candidate(mint: str, symbol: str) -> Candidate:
    """Passes every reference-parity rule by default (churn-suite shape)."""
    return Candidate(
        symbol=symbol,
        mint_address=mint,
        price_usd=0.001,
        liquidity_usd=60_000.0,
        volume_24h_usd=250_000.0,
        market_cap_usd=180_000.0,
        volume_1h_usd=25_000.0,
        buys_1h=320, sells_1h=210,
        price_change_1h_pct=6.0,
        age_hours=30.0,
        has_twitter=True, has_telegram=True, has_website=True,
    )


class OneCandidateProvider:
    def __init__(self, candidates: list[Candidate]):
        self.candidates = candidates

    async def get_candidates(self, limit: int) -> list[Candidate]:
        return self.candidates[:limit]

    async def get_current_price(self, mint_address: str, decimals=None):
        return 0.001

    async def aclose(self):
        pass


@pytest.fixture
def tick_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "rb.db")
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    monkeypatch.setattr(config, "BLOCKLIST_STATE_FILE", tmp_path / "bl.json")

    async def _ready():
        await db.init_db()
    return _ready


async def test_risk_budget_daily_ceiling_refuses_second_entry(tick_env,
                                                              monkeypatch):
    """DAY_MULTIPLE=1 -> max_daily = max_order = $35 at equity $1000. The
    first $35 entry fills; the second would take the day to $70 > $35 and
    must be refused with a journal line."""
    await tick_env()
    monkeypatch.setattr(config, "SIZING_MODE", "risk_budget")
    monkeypatch.setattr(config, "DAY_MULTIPLE", 1)

    c1 = make_candidate("MintAAAA1111111111111111111111111111111111", "AAA")
    c2 = make_candidate("MintBBBB2222222222222222222222222222222222", "BBB")
    provider = OneCandidateProvider([c1, c2])

    s1 = await run_tick(provider, Thinker(), state={})
    assert s1["opened"] == 1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with db.get_db() as conn:
        events = await db.get_feed_events(conn, limit=10)
        deployed = await db.deployed_today(conn)
        row = await db.get_daily_stats(conn, today)

    capped = [e for e in events if "daily deploy cap" in e["thesis"]]
    assert capped, "derived-ceiling refusal must be visible in the journal"
    assert deployed == pytest.approx(35.0)

    # REF-R8/R9 truths persisted for the public surfaces
    assert row is not None
    rb = row["stats_json"]["risk_budget"]
    assert rb["max_order_usd"] == 35.0
    assert rb["max_daily_usd"] == 35.0       # DAY_MULTIPLE=1 for this test
    assert rb["derived"] is True
    cal = row["stats_json"]["calibration"]
    assert cal["conviction_factor"] == 1.0   # no closed trades -> FLAT
    assert cal["samples"] == 0


async def test_patch_daily_stats_merges_without_clobber(tick_env):
    await tick_env()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with db.get_db() as conn:
        assert await db.get_daily_stats(conn, today) is None
        await db.patch_daily_stats(
            conn, today, {"risk_budget": {"max_order_usd": 35.0}})
        await db.patch_daily_stats(
            conn, today, {"calibration": {"conviction_factor": 1.05}})
        row = await db.get_daily_stats(conn, today)
    assert row["stats_json"]["risk_budget"] == {"max_order_usd": 35.0}
    assert row["stats_json"]["calibration"] == {"conviction_factor": 1.05}


async def test_disclosure_surfaces_risk_budget_and_calibration(tick_env):
    """Fresh book: disclosure recomputes at cost-basis equity (no external
    calls) -> full-size budget and FLAT conviction, fail-closed shape."""
    await tick_env()
    from api.routes.disclosure import get_disclosure
    result = await get_disclosure()

    rb = result["risk_budget"]
    for k in ("equity_usd", "drawdown_factor", "max_order_usd",
              "max_daily_usd", "formula", "derived"):
        assert k in rb
    cal = result["calibration"]
    for k in ("samples", "wins", "win_rate", "avg_win_pct", "avg_loss_pct",
              "expectancy_pct", "conviction_factor", "formula"):
        assert k in cal

    # equity = INITIAL_CASH_USD (1000) at cost -> 35 / 140, FLAT conviction
    assert rb["max_order_usd"] == 35.0
    assert rb["max_daily_usd"] == 140.0
    assert cal["conviction_factor"] == 1.0
    assert cal["samples"] == 0
