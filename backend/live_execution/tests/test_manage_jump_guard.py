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
def manage_env(monkeypatch, tmp_path):
    """Stub chain reads + order routing; record any sell attempt. §50: _manage
    is ledger-aware now (tranches_taken + sell-gate inputs), so the fixture
    supplies a real tmp ledger — never the operator's live one. §57: also
    clears the process-wide decimals memo so each test re-resolves decimals
    through THIS fixture's stub (a value cached by an earlier test must not
    leak in)."""
    calls = []

    async def fake_decimals(mint):
        return 6

    async def fake_place_order(**kw):
        calls.append(kw)
        return SimpleNamespace(status="unarmed", reason="test stub",
                               usd_value=0.0)

    from live_execution.models import ExecutionLedger
    ledger = ExecutionLedger(tmp_path / "manage_exec.json")

    monkeypatch.setattr(rlc.solana, "get_mint_decimals", fake_decimals)
    monkeypatch.setattr(rlc, "place_order", fake_place_order)
    rlc._DECIMALS_CACHE.clear()
    yield calls, ledger
    rlc._DECIMALS_CACHE.clear()


async def test_manage_skips_bad_quote_and_does_not_ratchet_hwm(manage_env):
    calls, ledger = manage_env
    hwm: dict = {}
    meta = _meta()
    # The exact incident shape: entry $0.04022, poisoned quote $119.0648.
    await rlc._manage(StubJupiter(119.0648), ledger, hwm, meta)
    assert MINT not in hwm                     # peak NOT poisoned
    assert calls == []                         # no sell attempted
    assert "last_price_usd" not in meta[MINT]  # risk-budget mark untouched


async def test_manage_ratchets_on_legitimate_move(manage_env):
    calls, ledger = manage_env
    hwm: dict = {}
    meta = _meta()
    # +24% in one cycle: real moves do this; far below the 50x jump cap.
    await rlc._manage(StubJupiter(0.05), ledger, hwm, meta)
    assert hwm[MINT] == pytest.approx(0.05)
    assert calls == []                         # no rule fires at +24%


async def test_manage_genuine_collapse_still_exits(manage_env):
    calls, ledger = manage_env
    hwm = {MINT: 0.08}                          # established peak
    meta = _meta()
    # -50% vs entry: the guard is upward-only, the stop must still fire.
    await rlc._manage(StubJupiter(0.02), ledger, hwm, meta)
    assert len(calls) == 1
    assert calls[0]["side"] == "sell"


# ---------------------------------------------------------------------------
# §57 — the fast exit scanner (restored): lock serialization + fresh book
# ---------------------------------------------------------------------------

async def test_manage_takes_the_exit_lock(manage_env):
    """One manage pass at a time, cycle OR fast scanner — the §57 contract.
    A second _manage while the first is in flight must wait, never overlap
    (two concurrent passes could double-sell one position)."""
    import asyncio

    calls, ledger = manage_env
    hwm: dict = {}
    release = asyncio.Event()
    entered = asyncio.Event()

    started = asyncio.Event()

    async def slow_price(mint, decimals=None):
        if not entered.is_set():
            entered.set()
            started.set()
            await release.wait()          # hold the first pass inside pricing
        return 0.04

    class SlowJupiter:
        async def get_current_price(self, mint, decimals=None):
            return await slow_price(mint, decimals)

    import run_live_cycle as rlc_mod
    # Give the ledger an open position so the pass has something to scan.
    ledger.record_buy("idem-1", MINT, 67.0, 1665.0, 0.04022, "sig", "confirmed")
    meta = _meta()

    t1 = asyncio.create_task(rlc._manage(SlowJupiter(), ledger, hwm, meta))
    await started.wait()                  # first pass holds the lock mid-pricing
    assert rlc_mod._EXIT_LOCK.locked()
    t2 = asyncio.create_task(rlc._manage(StubJupiter(0.02), ledger, hwm, meta))
    await asyncio.sleep(0.05)             # let t2 actually reach the lock
    assert rlc_mod._EXIT_LOCK.locked()    # still held by t1
    release.set()                         # let the first pass finish
    await asyncio.gather(t1, t2)
    # t2 ran after t1 — serialized, both completed, no exception escaped.


def test_journal_meta_overlays_chain_flags_and_skips_closed(tmp_path):
    """§57: the scanner's book view is a FRESH ledger read — a position whose
    close already landed is never re-priced — with the cycle's chain flags
    (chain_excluded / chain_tokens) overlaid on what is still open."""
    from run_live_cycle import _journal_meta
    from live_execution.models import ExecutionLedger

    other = "MINTBBB111111111111111111111111111111111111"
    ledger = ExecutionLedger(tmp_path / "scan_exec.json")
    ledger.record_buy("idem-a", MINT, 67.0, 1665.0, 0.04022, "sig", "confirmed")
    ledger.record_buy("idem-b", other, 5.0, 100.0, 0.05, "sig", "confirmed")
    # close the second position outright
    ledger.reduce_position(other, 1.0, 4.0, full_close=True,
                           rule_id="exit_stop_loss")
    flags = {MINT: {"chain_excluded": False, "chain_tokens": 100.0}}
    meta = _journal_meta(ledger, flags)
    assert set(meta) == {MINT}                    # closed mint is GONE
    assert meta[MINT]["chain_tokens"] == 100.0     # flag overlaid
    assert meta[MINT]["chain_excluded"] is False
    assert meta[MINT]["cost"] == 67.0
    assert meta[MINT]["tokens"] == 1665.0
    # an excluded position is carried but skipped by the scanner body
    flags2 = {MINT: {"chain_excluded": True}}
    meta2 = _journal_meta(ledger, flags2)
    assert meta2[MINT]["chain_excluded"] is True
