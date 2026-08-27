"""
tests for live_execution.reconcile — A2 chain-vs-journal reconciliation.

The chain is the sole authority on HOW MANY tokens the wallet holds; the
journal is the sole authority for cost basis. reconcile() must never mutate
cost, never silently drop a position, and treat an unreadable chain read as
"unknown" — never as "empty". Hermetic: pure logic, no network.
"""
from __future__ import annotations

from live_execution.reconcile import reconcile

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _meta(mint="MINTAAA", tokens=100.0, cost=25.0):
    return {mint: {"price_usd": 0.25, "tokens": tokens, "cost": cost,
                   "opened_ts": 1_700_000_000.0}}


def test_unreadable_chain_is_unchecked_not_empty():
    meta = _meta()
    report = reconcile(meta, None)
    assert report["checked"] is False
    assert report["discrepancies"] == []
    # Nothing may be flagged or mutated on an unreadable read:
    assert "chain_excluded" not in meta["MINTAAA"]
    assert "chain_tokens" not in meta["MINTAAA"]


def test_matching_balances_produce_no_discrepancy():
    meta = _meta(tokens=100.0)
    report = reconcile(meta, {"MINTAAA": 100.0})
    assert report["checked"] is True
    assert report["discrepancies"] == []
    assert "chain_excluded" not in meta["MINTAAA"]
    assert "chain_tokens" not in meta["MINTAAA"]


def test_float_dust_within_tolerance_is_not_a_discrepancy():
    meta = _meta(tokens=100.0)
    report = reconcile(meta, {"MINTAAA": 100.0 + 1e-9})
    assert report["discrepancies"] == []


def test_chain_below_journal_clamps_exit_sizing():
    meta = _meta(tokens=100.0)
    report = reconcile(meta, {"MINTAAA": 40.0})
    assert meta["MINTAAA"]["chain_tokens"] == 40.0
    assert "chain_excluded" not in meta["MINTAAA"]
    [d] = report["discrepancies"]
    assert d["kind"] == "chain_below_journal"
    assert d["journal"] == 100.0 and d["chain"] == 40.0
    # Cost basis is NEVER touched by reconciliation:
    assert meta["MINTAAA"]["cost"] == 25.0


def test_vanished_position_is_excluded_and_flagged():
    meta = _meta(tokens=100.0)
    report = reconcile(meta, {})   # chain answered, but holds nothing
    assert meta["MINTAAA"]["chain_excluded"] is True
    [d] = report["discrepancies"]
    assert d["kind"] == "vanished"
    # The journal row's numbers stay intact for operator review:
    assert meta["MINTAAA"]["tokens"] == 100.0
    assert meta["MINTAAA"]["cost"] == 25.0


def test_chain_above_journal_keeps_journal_amount():
    meta = _meta(tokens=100.0)
    report = reconcile(meta, {"MINTAAA": 150.0})
    assert "chain_tokens" not in meta["MINTAAA"]     # no clamp needed
    assert "chain_excluded" not in meta["MINTAAA"]
    [d] = report["discrepancies"]
    assert d["kind"] == "chain_above_journal"


def test_unjournaled_holding_is_flagged_never_added():
    meta = _meta()
    report = reconcile(meta, {"MINTAAA": 100.0, "MINTBBB": 7.0})
    kinds = {d["kind"] for d in report["discrepancies"]}
    assert kinds == {"unjournaled"}
    [d] = report["discrepancies"]
    assert d["mint"] == "MINTBBB" and d["chain"] == 7.0
    # Never added to the book:
    assert "MINTBBB" not in meta


def test_excluded_mints_are_dry_powder_not_positions():
    meta = _meta()
    report = reconcile(meta, {"MINTAAA": 100.0, USDC: 4.2},
                       exclude_mints=frozenset({USDC}))
    assert report["discrepancies"] == []


def test_zero_token_meta_rows_are_skipped():
    meta = _meta(tokens=0.0)
    report = reconcile(meta, {})
    assert report["discrepancies"] == []
    assert "chain_excluded" not in meta["MINTAAA"]


def test_multiple_positions_each_get_their_own_verdict():
    meta = {
        "MINTAAA": {"tokens": 100.0, "cost": 25.0},
        "MINTBBB": {"tokens": 50.0, "cost": 10.0},
        "MINTCCC": {"tokens": 10.0, "cost": 2.0},
    }
    report = reconcile(meta, {"MINTAAA": 100.0, "MINTBBB": 0.0,
                              "MINTCCC": 5.0})
    kinds = {d["mint"]: d["kind"] for d in report["discrepancies"]}
    assert kinds == {"MINTBBB": "vanished", "MINTCCC": "chain_below_journal"}
    assert meta["MINTBBB"]["chain_excluded"] is True
    assert meta["MINTCCC"]["chain_tokens"] == 5.0
    assert "chain_excluded" not in meta["MINTAAA"]
