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


# --- out-of-band close (2026-08-29, the vanished-position repair) --------------
# The operator sells a held coin from their own wallet; reconcile flags it
# chain_excluded + "operator review needed" every cycle but never mutates the
# ledger. close_out_of_band IS the operator decision, recorded as such.

def test_out_of_band_close_with_proceeds(tmp_path):
    """Known proceeds: PnL realized against summed cost of ALL open buys."""
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=Clock())
    ledger.record_buy("b1", "MINT", 0.5370006, 4015.376685, 0.0001337, "sig",
                      status="confirmed")
    closed = ledger.close_out_of_band("MINT", proceeds_usd=1.11715,
                                      note="manual sell")
    assert _open_buys(ledger, "MINT") == []
    close = [r for r in ledger._load() if r["kind"] == "close"][-1]
    assert close["pnl_usd"] == pytest.approx(1.11715 - 0.5370006)
    assert close["usd_size"] == pytest.approx(1.11715)
    assert close["tokens_out"] == pytest.approx(4015.376685)
    assert "outofband" in close["idempotency_key"]
    assert close["note"] == "out-of-band: manual sell"
    # realized_pnl_today counts it (pnl is not None).
    assert ledger.realized_pnl_today() == pytest.approx(1.11715 - 0.5370006)


def test_out_of_band_close_without_proceeds_is_honest(tmp_path):
    """Unknown proceeds: pnl stays None — never fabricated; the daily-loss
    breaker skips None rows so it can never trip on a made-up number."""
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=Clock())
    ledger.record_buy("b1", "MINT", 0.75, 100.0, 0.0075, "sig",
                      status="confirmed")
    ledger.close_out_of_band("MINT")
    assert _open_buys(ledger, "MINT") == []
    close = [r for r in ledger._load() if r["kind"] == "close"][-1]
    assert close["pnl_usd"] is None
    assert close["usd_size"] == 0.0
    assert ledger.realized_pnl_today() == 0.0   # None row skipped


def test_out_of_band_close_multiple_buys_sums_cost(tmp_path):
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=Clock())
    ledger.record_buy("b1", "MINT", 0.30, 100.0, 0.003, "sig",
                      status="confirmed")
    ledger.record_buy("b2", "MINT", 0.20, 200.0, 0.001, "sig",
                      status="confirmed")
    closed = ledger.close_out_of_band("MINT", proceeds_usd=0.55)
    assert _open_buys(ledger, "MINT") == []
    close = [r for r in ledger._load() if r["kind"] == "close"][-1]
    assert close["pnl_usd"] == pytest.approx(0.55 - 0.50)   # vs SUMMED cost
    assert close["tokens_out"] == pytest.approx(300.0)


def test_out_of_band_close_refuses_unknown_mint(tmp_path):
    """A typo'd mint must not be able to invent a close."""
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=Clock())
    ledger.record_buy("b1", "REAL", 0.50, 100.0, 0.005, "sig",
                      status="confirmed")
    with pytest.raises(ValueError):
        ledger.close_out_of_band("TYPO")
    assert len(_open_buys(ledger, "REAL")) == 1   # untouched


def test_out_of_band_close_leaves_other_mints_alone(tmp_path):
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=Clock())
    ledger.record_buy("b1", "GONE", 0.50, 100.0, 0.005, "sig",
                      status="confirmed")
    ledger.record_buy("b2", "HELD", 0.60, 200.0, 0.003, "sig",
                      status="confirmed")
    ledger.close_out_of_band("GONE")
    assert _open_buys(ledger, "GONE") == []
    assert len(_open_buys(ledger, "HELD")) == 1


def test_out_of_band_idempotent_second_call_refuses(tmp_path):
    """Running the repair twice must not double-close or crash the book."""
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=Clock())
    ledger.record_buy("b1", "MINT", 0.50, 100.0, 0.005, "sig",
                      status="confirmed")
    ledger.close_out_of_band("MINT", proceeds_usd=1.0)
    with pytest.raises(ValueError):
        ledger.close_out_of_band("MINT", proceeds_usd=1.0)
    closes = [r for r in ledger._load() if r["kind"] == "close"]
    assert len(closes) == 1   # no second close record
    assert ledger.realized_pnl_today() == pytest.approx(0.5)   # counted once
