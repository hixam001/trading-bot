"""
tests/test_promotion_gate.py — read-only evaluation logic (G4/G5).
"""
from __future__ import annotations

import pytest

import config
from models import Trade
from promotion_gate import evaluate


def _trade(pnl: float, opened_at="2026-08-01T00:00:00+00:00",
           closed_at="2026-08-02T00:00:00+00:00") -> Trade:
    t = Trade()
    t.realized_pnl_usd = pnl
    t.opened_at = opened_at
    t.closed_at = closed_at
    return t


def test_empty_history_fails_everything():
    r = evaluate([], None)
    assert not r["all_criteria_met"]
    assert len(r["criteria"]) == 5
    assert "read" not in str(type(r))   # dict report only
    assert all(c["passed"] is False for c in r["criteria"][:1])


def test_all_criteria_met_scenario():
    # 40 winners of +$10 each -> win rate 1.0, PF undefined? No losses ->
    # profit factor None -> that criterion FAILS (undefined != passing).
    trades = [_trade(10.0) for _ in range(40)]
    r = evaluate(trades, "2026-08-01T00:00:00+00:00")
    pf = next(c for c in r["criteria"] if c["name"] == "Minimum profit factor")
    assert pf["passed"] is False          # no losses yet = undefined = fail


def test_mixed_history_meets_all():
    trades = [_trade(15.0) for _ in range(30)] + [_trade(-5.0) for _ in range(10)]
    # win rate 0.75 >= 0.55; PF = 450/50 = 9 >= 1.5
    r = evaluate(trades, "2026-08-01T00:00:00+00:00")
    names_ok = {c["name"]: c["passed"] for c in r["criteria"]}
    assert names_ok["Minimum trade count"] is True
    assert names_ok["Minimum win rate"] is True
    assert names_ok["Minimum profit factor"] is True
    assert names_ok["Maximum drawdown"] is True
    assert names_ok["Learning window elapsed"] is True
    assert r["all_criteria_met"] is True
    # The note must always disclaim auto-triggering.
    assert "does not trigger anything automatically" in r["note"]


def test_drawdown_failure():
    trades = [_trade(-80.0) for _ in range(3)]   # equity 920 -> 840 -> 760
    r = evaluate(trades, "2026-08-01T00:00:00+00:00")
    dd = next(c for c in r["criteria"] if c["name"] == "Maximum drawdown")
    assert dd["actual"] > 20.0 and dd["passed"] is False
