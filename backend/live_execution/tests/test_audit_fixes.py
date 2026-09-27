"""
test_audit_fixes.py — A7 repo-audit regression tests.

Covers four fixes with no dedicated home elsewhere:

1. Daily ledger windows are UTC-day windows (deployed_today_usd feeds the
   "Rolling UTC-day" MAX_DAILY_DEPLOY_USD cap and realized_pnl_today feeds
   the daily-loss breaker) — a server-local midnight must not shift them.
2. mark_close is a FIFO PER-BUY close (pinned contract); whole-mint
   disposal with several open buys belongs to close_out_of_band.
3. tranches_taken uses strictly-greater-than (same-tick closes of an
   earlier position are not tranches of the current one).
4. confirm_signature asks once with searchTransactionHistory=True before
   declaring a send unconfirmed (short status caches on some RPCs).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pytest

from live_execution import solana
from live_execution.models import ExecutionLedger


class Clock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# 1. UTC-day windows
# ---------------------------------------------------------------------------

# Pacific/Kiritimati is UTC+14: 12:00Z on the 18th is already Sep 19 LOCAL,
# while 00:00Z on the 19th is still Sep 19 LOCAL — so under the OLD local-
# date logic both records share a "day"; under UTC semantics they do not.
_TZ = "Pacific/Kiritimati"
_T_SEP18_1200Z = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc).timestamp()
_T_SEP19_0000Z = datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc).timestamp()
_T_SEP19_1000Z = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc).timestamp()


def test_daily_windows_are_utc_not_local(tmp_path):
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset unavailable on this platform")

    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = _TZ
    time.tzset()
    try:
        ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=lambda: _T_SEP18_1200Z)
        ledger.record_buy("b1", "AAA", 40.0, 1.0, 40.0, "s1")   # UTC day: Sep 18
        ledger.mark_close("AAA", proceeds_usd=0.0)              # pnl -40, Sep 18 UTC

        ledger.now_fn = lambda: _T_SEP19_1000Z
        ledger.record_buy("b2", "BBB", 10.0, 1.0, 10.0, "s2")   # UTC day: Sep 19
        ledger.mark_close("BBB", proceeds_usd=5.0)              # pnl -5, Sep 19 UTC

        # "Now" is 00:00Z Sep 19 — LOCAL date is still Sep 19 (+14h), but the
        # UTC day has rolled. Only the Sep-19-UTC records may count.
        ledger.now_fn = lambda: _T_SEP19_0000Z
        assert ledger.deployed_today_usd() == pytest.approx(10.0)
        assert ledger.realized_pnl_today() == pytest.approx(-5.0)
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


# ---------------------------------------------------------------------------
# 2. mark_close: FIFO per-buy contract (whole-mint disposal = close_out_of_band)
# ---------------------------------------------------------------------------

def test_mark_close_is_fifo_per_buy_close(tmp_path):
    """A7 audit note pinned as a contract: mark_close closes the OLDEST buy
    only, against ITS cost. Whole-mint disposal with multiple open buys is
    close_out_of_band's job."""
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=Clock())
    ledger.record_buy("b1", "AAA", 50.0, 1.0, 50.0, "s1")
    ledger.record_buy("b2", "AAA", 30.0, 1.0, 30.0, "s2")

    rec = ledger.mark_close("AAA", proceeds_usd=55.0)

    assert rec.pnl_usd == pytest.approx(5.0)          # 55 - oldest cost 50
    assert ledger.open_positions() == {"AAA": pytest.approx(30.0)}
    assert ledger.total_open_exposure() == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# 3. tranches_taken: strictly newer than the open buy
# ---------------------------------------------------------------------------

def test_tranches_taken_requires_strictly_newer_close(tmp_path):
    frozen = Clock(1_000_000.0)
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=frozen)
    ledger.record_buy("b1", "AAA", 50.0, 1.0, 50.0, "s1")
    buy_ts = 1_000_000.0

    # A TP close stamped in the SAME tick as the buy (frozen clock — the
    # backfill/repair case) belongs to an EARLIER position, not this one.
    ledger.now_fn = lambda: buy_ts
    ledger.reduce_position("AAA", 0.33, 20.0, rule_id="exit_take_profit")
    assert ledger.tranches_taken("AAA") == 0

    # A later TP close IS a tranche.
    ledger.now_fn = lambda: buy_ts + 61.0
    ledger.reduce_position("AAA", 0.33, 18.0, rule_id="exit_take_profit")
    assert ledger.tranches_taken("AAA") == 1


# ---------------------------------------------------------------------------
# 4. confirm_signature history-search fallback
# ---------------------------------------------------------------------------

async def test_confirm_signature_history_fallback(monkeypatch):
    seen_modes: list[bool] = []

    async def fake_rpc(method, params, timeout=15.0, endpoints=None):
        assert method == "getSignatureStatuses"
        seen_modes.append(params[1]["searchTransactionHistory"])
        if not seen_modes[-1]:
            return {"value": []}                      # recent cache: empty
        return {"value": [{
            "err": None, "confirmationStatus": "confirmed", "slot": 7}]}

    async def fake_sleep(_s):
        pass

    monkeypatch.setattr(solana, "rpc", fake_rpc)
    monkeypatch.setattr(solana.asyncio, "sleep", fake_sleep)
    # Zero timeout: the polling loop is skipped, the fallback runs at once.
    monkeypatch.setattr(solana.config, "CONFIRM_TIMEOUT_SECONDS", 0.0)

    res = await solana.confirm_signature("SIG")

    assert res["confirmed"] is True
    assert res["slot"] == 7
    assert True in seen_modes                         # history search happened


async def test_confirm_signature_still_fails_when_history_also_empty(monkeypatch):
    async def fake_rpc(method, params, timeout=15.0, endpoints=None):
        return {"value": []}

    async def fake_sleep(_s):
        pass

    monkeypatch.setattr(solana, "rpc", fake_rpc)
    monkeypatch.setattr(solana.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(solana.config, "CONFIRM_TIMEOUT_SECONDS", 0.0)

    res = await solana.confirm_signature("SIG")

    assert res["confirmed"] is False
    assert res["err"] == "not confirmed before timeout"