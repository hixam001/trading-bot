"""
§87 D1: per-mint exit retry backoff.

A failed/unresolved sell must not be retried every 15s scan. Consecutive
misses back off 15/30/60/120/300s. Risk-off exits (stop/trail/liquidity/
invalidation) are capped at 60s so a cascade is never starved. A filled exit
or a position-size change resets the backoff.
"""
from __future__ import annotations

import types

import pytest

import run_live_cycle as rlc


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(rlc, "_exit_retry_state", {})


def test_schedule_is_exponential_and_capped():
    assert [rlc._exit_backoff_delay(n, False) for n in range(1, 7)] == [
        15.0, 30.0, 60.0, 120.0, 300.0, 300.0]


def test_risk_off_is_capped_at_60s():
    assert [rlc._exit_backoff_delay(n, True) for n in range(1, 7)] == [
        15.0, 30.0, 60.0, 60.0, 60.0, 60.0]


def test_failure_blocks_until_delay_elapses():
    mint = "MINT"
    assert rlc._exit_backoff_allows(mint, 100.0, False, now=1000.0) is True
    rlc._note_exit_failure(mint, 100.0, now=1000.0)

    assert rlc._exit_backoff_allows(mint, 100.0, False, now=1014.9) is False
    assert rlc._exit_backoff_allows(mint, 100.0, False, now=1015.0) is True
    # Second consecutive miss doubles the window.
    rlc._note_exit_failure(mint, 100.0, now=1015.0)
    assert rlc._exit_backoff_allows(mint, 100.0, False, now=1044.9) is False
    assert rlc._exit_backoff_allows(mint, 100.0, False, now=1045.0) is True


def test_risk_off_overrides_a_long_trim_backoff_at_60s():
    mint = "MINT"
    # Earn a 300s backoff through repeated profit-trim misses.
    for i in range(5):
        rlc._note_exit_failure(mint, 100.0, now=1000.0 + i)
    assert rlc._exit_backoff_allows(mint, 100.0, False, now=1100.0) is False

    # A risk-off decision cannot wait the trim's 300s: 60s from the last
    # failure is enough.
    assert rlc._exit_backoff_allows(mint, 100.0, True, now=1064.0) is True


def test_size_change_resets_backoff():
    mint = "MINT"
    rlc._note_exit_failure(mint, 100.0, now=1000.0)
    assert rlc._exit_backoff_allows(mint, 100.0, False, now=1001.0) is False
    # A partial fill / chain clamp changed the position: new situation.
    assert rlc._exit_backoff_allows(mint, 67.0, False, now=1001.0) is True
    assert mint not in rlc._exit_retry_state


def test_fill_clears_backoff():
    mint = "MINT"
    rlc._note_exit_failure(mint, 100.0, now=1000.0)
    assert mint in rlc._exit_retry_state
    rlc._clear_exit_backoff(mint)
    assert mint not in rlc._exit_retry_state
    assert rlc._exit_backoff_allows(mint, 100.0, False, now=1000.0) is True


async def test_scan_skips_the_order_while_backed_off(monkeypatch):
    """The exit decision is still evaluated, but no second sell is sent."""
    mint = "MINTMINTMINTMINTMINTMINTMINTMINT"
    calls: list = []

    async def decimals(m):
        return 6

    class Jup:
        async def get_current_price(self, m, decimals=None):
            return 0.02

    async def fake_order(**kw):
        calls.append(kw)
        return types.SimpleNamespace(status="failed", reason="unresolved")

    monkeypatch.setattr(rlc, "_cached_decimals", decimals)
    monkeypatch.setattr(rlc, "place_order", fake_order)
    meta = {mint: {"price_usd": 0.04, "tokens": 1000.0, "cost": 40.0,
                   "opened_ts": 0.0}}
    hwm = {mint: 0.04}
    ledger = types.SimpleNamespace(tranches_taken=lambda m: 0,
                                   last_close_ts=lambda m: None,
                                   closes_since=lambda since: 0)

    await rlc._scan_exits_for_position(Jup(), ledger, hwm, mint, meta[mint])
    assert len(calls) == 1
    assert mint in rlc._exit_retry_state

    await rlc._scan_exits_for_position(Jup(), ledger, hwm, mint, meta[mint])
    assert len(calls) == 1, "a backed-off position must not be retried early"
