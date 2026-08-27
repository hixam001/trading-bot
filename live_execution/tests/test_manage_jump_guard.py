"""
tests/test_manage_jump_guard.py — §32 parity: the live manage loop's
bad-quote guard (mirror of the paper scanner's EXIT_PRICE_JUMP_MAX guard).

A transient bad quote must not ratchet the live high-water mark (a
poisoned peak can force a premature trail exit). A live sell can never
fabricate money — it is a real swap — but an early exit on a phantom spike
is still real harm, so the guard matches the paper side exactly:
upward-only, skip the cycle, high-water untouched.

Hermetic: jupiter/solana/place_order are stubbed; no network, no ledger
writes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import run_live_cycle as rlc

MINT = "MINTAAA1111111111111111111111111111111111111"


class StubJupiter:
    def __init__(self, price: float):
        self.price = price

    async def get_current_price(self, mint: str, decimals=None) -> float:
        return self.price


def _meta(price_usd: float = 0.04022) -> dict:
    return {MINT: {"price_usd": price_usd, "tokens": 1665.0, "cost": 67.0,
                   "opened_ts": 1_700_000_000.0}}


@pytest.fixture
def manage_env(monkeypatch):
    """Stub chain reads + order routing; record any sell attempt."""
    calls = []

    async def fake_decimals(mint):
        return 6

    async def fake_place_order(**kw):
        calls.append(kw)
        return SimpleNamespace(status="unarmed", reason="test stub",
                               usd_value=0.0)

    monkeypatch.setattr(rlc.solana, "get_mint_decimals", fake_decimals)
    monkeypatch.setattr(rlc, "place_order", fake_place_order)
    return calls


async def test_manage_skips_bad_quote_and_does_not_ratchet_hwm(manage_env):
    hwm: dict = {}
    meta = _meta()
    # The exact incident shape: entry $0.04022, poisoned quote $119.0648.
    await rlc._manage(StubJupiter(119.0648), None, hwm, meta)
    assert MINT not in hwm                     # peak NOT poisoned
    assert manage_env == []                    # no sell attempted
    assert "last_price_usd" not in meta[MINT]  # risk-budget mark untouched


async def test_manage_ratchets_on_legitimate_move(manage_env):
    hwm: dict = {}
    meta = _meta()
    # +24% in one cycle: real moves do this; far below the 50x jump cap.
    await rlc._manage(StubJupiter(0.05), None, hwm, meta)
    assert hwm[MINT] == pytest.approx(0.05)
    assert manage_env == []                    # no rule fires at +24%


async def test_manage_genuine_collapse_still_exits(manage_env):
    hwm = {MINT: 0.08}                          # established peak
    meta = _meta()
    # -50% vs entry: the guard is upward-only, the stop must still fire.
    await rlc._manage(StubJupiter(0.02), None, hwm, meta)
    assert len(manage_env) == 1
    assert manage_env[0]["side"] == "sell"
