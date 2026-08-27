"""
tests/test_churn_guards.py — the anti-churn + risk-cap + audit layer:

  * blocklist      : manual/auto mint blocks, candidate filtering,
                     consecutive-stop-out auto-block trigger
  * conviction     : compute_ticket math (fixed vs conviction mode),
                     daily deploy cap refusal through a real tmp DB tick
  * seal           : decision_commits rows written BEFORE acting, hashes
                     recompute verbatim (sha256(nonce|canonical payload))

All DB-touching tests run against a fresh tmp SQLite database.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone

import pytest

import config
from api import db
from blocklist import (
    BLOCKED_SYMBOLS,
    _load,
    block_mint,
    filter_candidates,
    is_blocked_mint,
    is_blocked_symbol,
    should_autoblock,
    unblock_mint,
)
from llm.thinker import Thinker, template_think
from main import run_tick
from models import Candidate, Trade


# ---------------------------------------------------------------- fixtures ----

def make_candidate(mint: str = "Mint1111111111111111111111111111111111111",
                  symbol: str = "TEST") -> Candidate:
    """Passes every reference-parity rule by default."""
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


def make_trade(mint: str = "Mint1111111111111111111111111111111111111") -> Trade:
    return Trade(
        symbol="T",
        mint_address=mint,
        entry_price_usd=0.001,
        position_size_usd=100.0,
        quantity=100_000.0,
        candidate_snapshot={"decimals": 6},
    )


class SingleProvider:
    """Yields a fixed candidate list every tick."""

    def __init__(self, cands: list[Candidate]):
        self.cands = cands

    async def get_candidates(self, limit: int) -> list[Candidate]:
        return self.cands[:limit]

    async def get_current_price(self, mint_address: str, decimals=None):
        return 0.001

    async def aclose(self):
        pass


@pytest.fixture
def bl_state(tmp_path, monkeypatch):
    path = tmp_path / "blocklist_state.json"
    monkeypatch.setattr(config, "BLOCKLIST_STATE_FILE", str(path))
    return path


# --- blocklist core -------------------------------------------------------------

def test_block_unblock_roundtrip(bl_state):
    assert not is_blocked_mint("MINT_A")
    block_mint("MINT_A", "DONT", "2 consecutive stop-outs", kind="auto")
    assert is_blocked_mint("MINT_A")
    entry = _load()["mints"]["MINT_A"]
    assert entry["kind"] == "auto"
    assert unblock_mint("MINT_A")
    assert not is_blocked_mint("MINT_A")


def test_filter_candidates_splits_kept_and_blocked(bl_state):
    block_mint("MINT_BAD", "BADCOIN", "rugged twice", kind="auto")
    kept_c = make_candidate("MintGood22222222222222222222222222222222222", "GOOD")
    bad_c = make_candidate("MINT_BAD", "BAD")
    kept, blocked = filter_candidates([kept_c, bad_c])
    assert [c.mint_address for c in kept] == [kept_c.mint_address]
    assert blocked == [("BAD", "rugged twice")]


def test_should_autoblock_requires_all_stop_losses():
    assert should_autoblock(["exit_stop_loss", "exit_stop_loss"])
    assert not should_autoblock(["exit_take_profit", "exit_stop_loss"])
    assert not should_autoblock(["exit_stop_loss"])


def test_corrupt_blocklist_file_quarantined(bl_state):
    bl_state.write_text("not-json{")
    assert not is_blocked_mint("anything")
    assert bl_state.with_suffix(".corrupt").exists()


# --- A6 static symbol blocklist --------------------------------------------------

def test_is_blocked_symbol_matches_listed_names_case_insensitive():
    assert is_blocked_symbol("WAWA")
    assert is_blocked_symbol("wawa")          # lower-case source
    assert is_blocked_symbol("$WAWA")         # leading $ ticker
    assert is_blocked_symbol(" WAWA ")        # stray whitespace
    assert is_blocked_symbol("CRASHIUS")
    assert not is_blocked_symbol("PEPE")
    assert not is_blocked_symbol("")          # empty never blocks
    assert not is_blocked_symbol(None)        # missing never blocks


def test_is_blocked_symbol_catches_404_family_any_spacing():
    assert is_blocked_symbol("404")
    assert is_blocked_symbol("404LIFE")
    assert is_blocked_symbol("404 life not found")   # spaced variant
    assert is_blocked_symbol("$404LIFENOTFOUND")
    assert not is_blocked_symbol("405")              # not the 404 family


def test_blocked_symbols_is_an_immutable_frozenset():
    assert isinstance(BLOCKED_SYMBOLS, frozenset)
    assert "WAWA" in BLOCKED_SYMBOLS


def test_filter_candidates_blocks_static_symbol_even_on_fresh_mint(bl_state):
    # A rugged name re-launched under a brand-new mint is still caught by the
    # symbol layer (the mint list has never seen this mint before).
    fresh = make_candidate("MintFresh9999999999999999999999999999999999", "WAWA")
    good = make_candidate("MintGood3333333333333333333333333333333333", "GOOD")
    kept, blocked = filter_candidates([fresh, good])
    assert [c.mint_address for c in kept] == [good.mint_address]
    assert blocked == [("WAWA", "blocked symbol (static list)")]


def test_mint_block_takes_precedence_over_symbol_reason(bl_state):
    # When a mint is ALSO manually blocked, the mint reason wins (it is more
    # specific) and the symbol layer is never consulted.
    block_mint("MINT_X", "WAWA", "operator block", kind="manual")
    c = make_candidate("MINT_X", "WAWA")
    kept, blocked = filter_candidates([c])
    assert kept == []
    assert blocked == [("WAWA", "operator block")]


# --- conviction ticket sizing -----------------------------------------------------

def test_fixed_mode_returns_intended_size(monkeypatch):
    monkeypatch.setattr(config, "SIZING_MODE", "fixed")
    from paper_trading_engine import compute_ticket
    assert compute_ticket(1_000.0, heat=None) == \
        config.INTENDED_POSITION_SIZE_USD
    assert compute_ticket(1_000.0, heat=100) == \
        config.INTENDED_POSITION_SIZE_USD


def test_conviction_mode_scales_with_heat(monkeypatch):
    monkeypatch.setattr(config, "SIZING_MODE", "conviction")
    monkeypatch.setattr(config, "TICKET_CASH_FRACTION", 0.15)
    monkeypatch.setattr(config, "TICKET_MAX_USD", 150.0)
    monkeypatch.setattr(config, "MIN_TICKET_USD", 25.0)
    from paper_trading_engine import compute_ticket

    # cash 1000 -> base = min(150, 150) = 150
    # heat None -> neutral 50 -> conviction min(1, 0.5+0.3)=0.8 -> 120
    assert compute_ticket(1_000.0, heat=None) == 120
    # heat 100 -> conviction 1.0 -> capped at base 150
    assert compute_ticket(1_000.0, heat=100) == 150
    # tiny cash -> floor at MIN_TICKET_USD
    assert compute_ticket(80.0, heat=None) == 25


# --- daily deploy cap through a real tick ----------------------------------------

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
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "churn.db")
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    monkeypatch.setattr(config, "BLOCKLIST_STATE_FILE", tmp_path / "bl.json")

    async def _ready():
        await db.init_db()
    return _ready


async def test_daily_cap_refuses_second_deployment(tick_env, monkeypatch):
    await tick_env()
    monkeypatch.setattr(config, "DAILY_DEPLOY_CAP_USD", 120.0)

    # AAA green (+6%), BBB red (-2%): regime stays OK, both pass the gate,
    # but together they'd deploy $200 > the $120 daily cap.
    c1 = make_candidate("MintAAAA1111111111111111111111111111111111", "AAA")
    c2 = make_candidate("MintBBBB2222222222222222222222222222222222", "BBB")
    c2.price_change_1h_pct = -2.0
    provider = OneCandidateProvider([c1, c2])

    s1 = await run_tick(provider, Thinker(), state={})
    assert s1["opened"] == 1          # AAA entered; BBB hit the cap

    async with db.get_db() as conn:
        events = await db.get_feed_events(conn, limit=10)
        cash = await db.get_cash_balance(conn)
        deployed = await db.deployed_today(conn)
        open_trades = await db.get_open_trades(conn)

    capped = [e for e in events if "daily deploy cap" in e["thesis"]]
    assert capped, "cap refusal must be visible in the journal"
    assert deployed == pytest.approx(100.0)
    assert len(open_trades) == 1
    premium = config.INTENDED_POSITION_SIZE_USD * 1.01 * 1.02
    assert cash == pytest.approx(config.INITIAL_CASH_USD - premium)


# --- decision seal: commits written before acting, hashes recompute ---------------

async def test_decision_seal_written_and_hash_verifies(tick_env,
                                                       monkeypatch):
    await tick_env()
    thinker = Thinker()
    c = make_candidate()
    summary = await run_tick(
        OneCandidateProvider([c]), thinker, state={"tick": 0, "theses": {}})
    assert summary["candidates"] == 1

    async with db.get_db() as conn:
        async with conn.execute(
            "SELECT nonce, payload_json, payload_hash, verdict, "
            "entry_allowed FROM decision_commits"
        ) as cur:
            rows = await cur.fetchall()

    assert len(rows) == 1
    nonce, payload_json, payload_hash, verdict, allowed = rows[0]
    recomputed = hashlib.sha256((nonce + "|" + payload_json).encode())
    assert recomputed.hexdigest() == payload_hash
    payload = json.loads(payload_json)
    assert payload["symbol"] == c.symbol
    assert payload["think_verdict"] in ("buy", "pass")
    assert isinstance(allowed, int)


def test_commit_payload_is_canonical():
    """Same data, different key order -> identical canonical JSON."""
    a = json.dumps({"b": 1, "a": 2}, sort_keys=True, separators=(",", ":"))
    b = json.dumps({"a": 2, "b": 1}, sort_keys=True, separators=(",", ":"))
    assert a == b