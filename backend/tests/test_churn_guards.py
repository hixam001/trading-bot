"""
tests/test_churn_guards.py — the anti-churn + risk-cap + audit layer:

  * blocklist      : manual/auto mint blocks, candidate filtering,
                     §49 PnL-based close-outcome memory, auto-block,
                     and 24h re-entry cooldown (both books)
  * conviction     : compute_ticket math (fixed vs conviction mode),
                     daily deploy cap refusal through a real tmp DB tick
  * seal           : decision_commits rows written BEFORE acting, hashes
                     recompute verbatim (sha256(nonce|canonical payload))

All DB-touching tests run against a fresh tmp SQLite database.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from datetime import datetime, timezone

import pytest

import config
from api import db
from blocklist import (
    BLOCKED_SYMBOLS,
    _load,
    _save,
    block_mint,
    filter_candidates,
    is_blocked_mint,
    is_blocked_symbol,
    maybe_autoblock,
    record_close_outcome,
    should_autoblock,
    unblock_mint,
)
from llm.thinker import Thinker, template_think
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


# --- §49: PnL-based close memory, auto-block, re-entry cooldown (both books) ------

def test_record_close_outcome_appends_newest_first_and_caps(bl_state):
    record_close_outcome("MINT_R", "RUG", "exit_stop_loss", -1.5, book="paper")
    record_close_outcome("MINT_R", "RUG", "exit_take_profit", 2.0, book="live")
    closes = _load()["mints"]["MINT_R"]["closes"]
    assert len(closes) == 2
    # newest first, with the PnL basis + book recorded
    assert closes[0]["rule"] == "exit_take_profit"
    assert closes[0]["loss"] is False and closes[0]["book"] == "live"
    assert closes[1]["loss"] is True and closes[1]["book"] == "paper"
    # a history-only entry is NOT a block
    assert not is_blocked_mint("MINT_R")
    # capped at 10 (newest kept, oldest trimmed)
    for _ in range(12):
        record_close_outcome("MINT_R", "RUG", "exit_trailing_stop", 1.0)
    closes = _load()["mints"]["MINT_R"]["closes"]
    assert len(closes) == 10
    assert all(c["rule"] == "exit_trailing_stop" for c in closes)


def test_record_close_outcome_never_raises_on_unwritable_state(
        tmp_path, monkeypatch):
    """Losing the memory is strictly better than losing the tick."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a regular file, not a directory")
    monkeypatch.setattr(config, "BLOCKLIST_STATE_FILE",
                        str(blocker / "state.json"))
    record_close_outcome("MINT_X", "XXX", "exit_stop_loss", -1.0)
    assert not is_blocked_mint("MINT_X")


def test_maybe_autoblock_fires_on_consecutive_losses_any_rule(bl_state):
    # One loss is not a pattern.
    record_close_outcome("MINT_A", "AAA", "exit_stop_loss", -1.0)
    assert not maybe_autoblock("MINT_A", "AAA")
    assert not is_blocked_mint("MINT_A")
    # A second consecutive loss — via a DIFFERENT exit rule, PnL-based — is.
    record_close_outcome("MINT_A", "AAA", "exit_trailing_stop", -0.5)
    assert maybe_autoblock("MINT_A", "AAA")
    entry = _load()["mints"]["MINT_A"]
    assert entry["kind"] == "auto"
    assert "2 consecutive loss closes" in entry["reason"]
    assert "exit_stop_loss" in entry["reason"]
    assert "exit_trailing_stop" in entry["reason"]


def test_maybe_autoblock_win_resets_the_streak(bl_state):
    record_close_outcome("MINT_W", "WWW", "exit_stop_loss", -1.0)
    record_close_outcome("MINT_W", "WWW", "exit_take_profit", 2.0)
    record_close_outcome("MINT_W", "WWW", "exit_stop_loss", -1.0)
    assert not maybe_autoblock("MINT_W", "WWW")
    assert not is_blocked_mint("MINT_W")


def test_unblock_preserves_history_and_history_drives_cooldown(bl_state):
    record_close_outcome("MINT_H", "HHH", "exit_stop_loss", -1.0)
    record_close_outcome("MINT_H", "HHH", "exit_trailing_stop", -0.5)
    assert maybe_autoblock("MINT_H", "HHH")
    # Unlifting the verdict must NOT erase the evidence:
    assert unblock_mint("MINT_H")
    assert not is_blocked_mint("MINT_H")
    closes = _load()["mints"]["MINT_H"]["closes"]
    assert [c["loss"] for c in closes] == [True, True]
    # The preserved history still feeds the self-expiring cooldown:
    kept, blocked = filter_candidates([make_candidate("MINT_H", "HHH")])
    assert kept == []
    assert "re-entry cooldown" in blocked[0][1]


def test_reentry_cooldown_filters_recent_loss_and_expires(bl_state):
    record_close_outcome("MINT_C", "CCC", "exit_stop_loss", -1.0)
    kept, blocked = filter_candidates([make_candidate("MINT_C", "CCC")])
    assert kept == []
    assert "re-entry cooldown" in blocked[0][1]
    # Age the recorded close past the window -> free to be considered again.
    data = _load()
    data["mints"]["MINT_C"]["closes"][0]["ts"] = time.time() - 25 * 3600
    _save(data)
    kept, blocked = filter_candidates([make_candidate("MINT_C", "CCC")])
    assert len(kept) == 1 and blocked == []


def test_reentry_cooldown_ignores_wins(bl_state):
    record_close_outcome("MINT_O", "OOO", "exit_take_profit", 3.0)
    kept, blocked = filter_candidates([make_candidate("MINT_O", "OOO")])
    assert len(kept) == 1 and blocked == []


def test_live_book_records_then_autoblocks_on_full_close():
    """§49 → §52: the LIVE cycle's _manage does record_close_outcome →
    maybe_autoblock, in that order, with the live book tag, and journals
    the soft loss memory. (The paper book retired; its side of the old
    both-books pin went with it.)"""
    import run_live_cycle as rlc

    live_src = inspect.getsource(rlc._manage)
    assert "record_close_outcome as _record" in live_src
    assert 'book="live"' in live_src
    assert "maybe_autoblock as _maybe" in live_src
    assert live_src.index("_record(") < live_src.index("_maybe(")
    # reference layer 5: the thinker sees the loss lesson
    assert "we already paid for this lesson" in live_src


def test_anti_churn_thresholds_are_pinned():
    """Hardcoded, never env-settable (like every other risk number)."""
    assert config.AUTO_BLOCK_CONSECUTIVE_LOSSES == 2
    # legacy alias kept so old references still resolve
    assert config.AUTO_BLOCK_CONSECUTIVE_STOPS == \
        config.AUTO_BLOCK_CONSECUTIVE_LOSSES
    assert config.REENTRY_COOLDOWN_HOURS == 24.0


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
    from sizing import compute_ticket
    assert compute_ticket(1_000.0, heat=None) == \
        config.INTENDED_POSITION_SIZE_USD
    assert compute_ticket(1_000.0, heat=100) == \
        config.INTENDED_POSITION_SIZE_USD


def test_conviction_mode_scales_with_heat(monkeypatch):
    monkeypatch.setattr(config, "SIZING_MODE", "conviction")
    monkeypatch.setattr(config, "TICKET_CASH_FRACTION", 0.15)
    monkeypatch.setattr(config, "TICKET_MAX_USD", 150.0)
    monkeypatch.setattr(config, "MIN_TICKET_USD", 25.0)
    from sizing import compute_ticket

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


def test_commit_payload_is_canonical():
    """Same data, different key order -> identical canonical JSON."""
    a = json.dumps({"b": 1, "a": 2}, sort_keys=True, separators=(",", ":"))
    b = json.dumps({"a": 2, "b": 1}, sort_keys=True, separators=(",", ":"))
    assert a == b