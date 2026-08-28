# Regression tests for ExecutionLedger.reduce_position full_close intent.
#
# Stale-holdings bug: a reconcile-clamped FULL exit produced a fraction just
# under the 0.999 close threshold (chain/journal, e.g. 0.99889), so the sell
# was booked as a trim and left a dust row open forever, polluting holdings
# and counting against MAX_OPEN_POSITIONS. full_close=True threads the
# exit-engine close intent so the position closes outright.
from __future__ import annotations

import pytest

from live_execution.models import ExecutionLedger


class Clock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def _open_buys(ledger, mint):
    return [r for r in ledger._load()
            if r["kind"] == "buy" and r["mint"] == mint
            and r["status"] in ledger._OPEN]


def test_full_close_closes_despite_sub_threshold_fraction(tmp_path):
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=Clock())
    ledger.record_buy("b1", "MINT", 0.90, 2121.660130, 0.000423, "sig",
                      status="confirmed")
    rec = ledger.reduce_position("MINT", 0.998891270582532, 0.705312,
                                 full_close=True)
    assert _open_buys(ledger, "MINT") == []
    assert rec.pnl_usd == pytest.approx(0.705312 - 0.90)


def test_trim_without_full_close_leaves_remainder(tmp_path):
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=Clock())
    ledger.record_buy("b1", "MINT", 100.0, 10.0, 10.0, "sig",
                      status="confirmed")
    ledger.reduce_position("MINT", 0.5, 55.0)
    opens = _open_buys(ledger, "MINT")
    assert len(opens) == 1
    assert opens[0]["usd_size"] == pytest.approx(50.0)
    assert opens[0]["tokens_out"] == pytest.approx(5.0)


def test_full_close_realizes_pnl_on_full_cost(tmp_path):
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=Clock())
    ledger.record_buy("b1", "MINT", 10.0, 5.0, 2.0, "sig", status="confirmed")
    rec = ledger.reduce_position("MINT", 1.0, 12.0, full_close=True)
    assert _open_buys(ledger, "MINT") == []
    assert rec.pnl_usd == pytest.approx(2.0)
