"""
tests/test_fake_chart.py — A7 wash-trade "fake chart" filter (omo audit §28).

Verbatim-port tests for rule_engine/fake_chart.is_fake_chart. Every expectation
is hand-computed from the 13 reference thresholds (omotrades/omo
market.server.ts isFakeChart). A clean baseline that trips NOTHING is perturbed
one field at a time so each test isolates exactly one threshold (the first to
trip wins, matching reference order).

Missing-data policy under test: an unknown vol_1h returns NOT fake (the gate
fails it closed with an accurate reason), and any threshold that would false-
positive on a missing window only fires when that field is present
(unknown != zero). Only the negative-change rules keep `?? 0` parity, where
zero is the safe direction.
"""
from __future__ import annotations

import pytest

from models import Candidate
from rule_engine.fake_chart import is_fake_chart, is_fake_candidate


def _base() -> dict:
    """A healthy tape that trips none of the 13 thresholds (hand-verified)."""
    return dict(
        liq_usd=100_000, vol_1h=100_000, vol_5m=10_000, vol_6h=400_000,
        vol_24h=800_000, buys_1h=500, sells_1h=400, chg_1h=5.0, chg_6h=10.0,
        chg_24h=20.0, fdv_usd=1_000_000, age_hours=100.0,
    )


def _run(**over) -> tuple:
    kw = _base()
    kw.update(over)
    return is_fake_chart(**kw)


# --- clean baseline ---------------------------------------------------------

def test_clean_baseline_is_real():
    fake, reason = _run()
    assert fake is False
    assert reason == ""


# --- threshold 1: fees too low for fdv -------------------------------------

def test_t1_fees_too_low_for_fdv():
    # age 50 (in 0..72), fdv 1m > 0, life_vol = max(0, 0*4) = 0 -> fees 0
    # fees 0 < fdv*0.03 (30_000) -> fake. First threshold, so it wins.
    fake, reason = _run(age_hours=50.0, vol_6h=0.0, vol_24h=0.0)
    assert fake is True
    assert reason == "fees-too-low-for-fdv"


# --- threshold 2: fresh launch, small float, almost no fees ----------------

def test_t2_fresh_launch_no_fees():
    # age 10 (in 0..24) -> life_vol = vol24h = 350_000 -> fees 1_750.
    # T1 avoided: fees 1_750 >= fdv*0.03 (50_000*0.03 = 1_500).
    # T2: age<24, fdv 50_000 < 150_000, fees 1_750 < 2_000 -> fake.
    fake, reason = _run(age_hours=10.0, fdv_usd=50_000, vol_24h=350_000)
    assert fake is True
    assert reason == "fresh-launch-no-fees"


# --- threshold 3: 1h volume > 20x depth ------------------------------------

def test_t3_1h_vol_20x_depth():
    # vol1h 3_000_000 > liq*20 (2_000_000). age 100 skips T1/T2. T3 is checked
    # before T6, so it wins even though the avg-ticket ratio also rises.
    fake, reason = _run(vol_1h=3_000_000)
    assert fake is True
    assert reason == "1h-vol-20x-depth"


# --- threshold 4: 24h volume > 150x depth ----------------------------------

def test_t4_24h_vol_150x_depth():
    # vol24h 20_000_000 > liq*150 (15_000_000); vol1h stays 100_000 so T3 is
    # quiet. T4 is checked before T12, so it wins.
    fake, reason = _run(vol_24h=20_000_000)
    assert fake is True
    assert reason == "24h-vol-150x-depth"


# --- threshold 5: volume with almost nobody behind it ----------------------

def test_t5_vol_without_crowd():
    # vol1h 60_000 > 50_000 and trades 40 < 60. vol1h <= liq*20 keeps T3 quiet;
    # vol24h <= liq*150 keeps T4 quiet.
    fake, reason = _run(vol_1h=60_000, buys_1h=20, sells_1h=20)
    assert fake is True
    assert reason == "vol-without-crowd"


# --- threshold 6: average ticket too big for a thin pool -------------------

def test_t6_avg_ticket_too_big():
    # trades 60 (>= 60 keeps T5 quiet), vol1h/trades = 160_000/60 = 2666 > 2500,
    # liq 140_000 < 150_000. vol1h <= liq*20 keeps T3 quiet.
    fake, reason = _run(vol_1h=160_000, buys_1h=30, sells_1h=30, liq_usd=140_000)
    assert fake is True
    assert reason == "avg-ticket-too-big"


# --- threshold 7: one-sided tape -------------------------------------------

def test_t7_one_sided_tape():
    # trades 60 > 40 and sells == 0. trades >= 60 keeps T5 quiet; avg ticket
    # 100_000/60 = 1666 < 2500 keeps T6 quiet.
    fake, reason = _run(buys_1h=60, sells_1h=0)
    assert fake is True
    assert reason == "one-sided-tape"


# --- threshold 8: straight bleed -------------------------------------------

def test_t8_straight_bleed():
    # chg1h -30 < -25 and chg6h -45 < -40. Normal volumes keep T3-T7 quiet.
    fake, reason = _run(chg_1h=-30.0, chg_6h=-45.0)
    assert fake is True
    assert reason == "straight-bleed"


# --- threshold 9: distribution corpse --------------------------------------

def test_t9_distribution_corpse():
    # chg24h -60 < -55 and chg6h -25 < -20. chg1h stays +5 so T8 is quiet.
    fake, reason = _run(chg_24h=-60.0, chg_6h=-25.0)
    assert fake is True
    assert reason == "distribution-corpse"


# --- threshold 10: dead tape ------------------------------------------------

def test_t10_dead_tape():
    # liq>0, vol1h 10_000 < liq*0.15 (15_000), vol24h 200_000 < liq*3 (300_000).
    # vol1h >= 5_000 keeps T11 quiet; vol6h/vol24h = 2.0 keeps T12 quiet.
    fake, reason = _run(vol_1h=10_000, vol_24h=200_000)
    assert fake is True
    assert reason == "dead-tape"


# --- threshold 11: no recent trades ----------------------------------------

def test_t11_no_recent_trades():
    # vol5m == 0 and vol1h 4_000 < 5_000. vol24h 800_000 > liq*3 keeps T10 quiet.
    fake, reason = _run(vol_5m=0.0, vol_1h=4_000)
    assert fake is True
    assert reason == "no-recent-trades"


# --- threshold 12: headline day, empty present -----------------------------

def test_t12_headline_day_over():
    # vol24h > 0 and vol6h/vol24h = 50_000/1_000_000 = 0.05 < 0.06. vol1h stays
    # above liq*0.15 so T10 is quiet; vol5m > 0 so T11 is quiet.
    fake, reason = _run(vol_24h=1_000_000, vol_6h=50_000)
    assert fake is True
    assert reason == "headline-day-over"


# --- threshold 13: paper float on a sliver of depth ------------------------

def test_t13_paper_float():
    # liq>0, fdv>0, fdv/liq = 5_000_000/100_000 = 50 > 30. Baseline volumes and
    # positive changes keep T3-T12 quiet; age 100 skips T1/T2.
    fake, reason = _run(fdv_usd=5_000_000)
    assert fake is True
    assert reason == "paper-float"


# --- missing-data policy ----------------------------------------------------

def test_unknown_vol_1h_is_not_fake_gate_handles_it():
    # An unknown 1h tape is unevaluable -> NOT fake here; the deterministic
    # gate fails it closed (volume_alive) with an accurate reason. We never
    # mislabel "no data" as a manufactured chart.
    fake, reason = is_fake_chart(liq_usd=100_000, vol_1h=None)
    assert fake is False
    assert reason == ""


def test_missing_optional_fields_default_to_zero_parity():
    # Only the required tape fields present; every optional window None. The
    # filter must NOT assume zero for the missing windows (unknown != zero):
    # no threshold may fire on data it cannot see. The deterministic gate
    # fails this tape closed with an accurate reason instead.
    fake, reason = is_fake_chart(liq_usd=100_000, vol_1h=4_000)
    assert fake is False
    assert reason == ""


def test_unknown_change_windows_safely_block_bleed_rules():
    # chg_6h / chg_24h omitted -> None -> 0, which can never satisfy the
    # straight-bleed (< -40) or corpse (< -55 / < -20) tests. So even with a
    # steep negative 1h change, missing deeper windows keep those flags quiet
    # (reference ?? 0 parity where zero is the safe direction).
    fake, reason = is_fake_chart(
        liq_usd=100_000, vol_1h=100_000, vol_5m=10_000, vol_6h=400_000,
        vol_24h=800_000, buys_1h=500, sells_1h=400, chg_1h=-30.0,
        fdv_usd=1_000_000, age_hours=100.0)
    assert fake is False
    assert reason == ""


# --- Candidate wrapper ------------------------------------------------------

def _candidate(**over) -> Candidate:
    kw = _base()
    kw.update(over)
    return Candidate(
        symbol="TEST", mint_address="MintAAA111111111111111111111111111111111",
        price_usd=0.01,
        liquidity_usd=kw["liq_usd"], volume_24h_usd=kw["vol_24h"],
        market_cap_usd=kw["fdv_usd"],
        volume_1h_usd=kw["vol_1h"], volume_5m_usd=kw["vol_5m"],
        volume_6h_usd=kw["vol_6h"], buys_1h=kw["buys_1h"], sells_1h=kw["sells_1h"],
        price_change_1h_pct=kw["chg_1h"], price_change_6h_pct=kw["chg_6h"],
        price_change_24h_pct=kw["chg_24h"], fdv_usd=kw["fdv_usd"],
        age_hours=kw["age_hours"],
    )


def test_wrapper_clean_candidate_is_real():
    fake, reason = is_fake_candidate(_candidate())
    assert fake is False and reason == ""


def test_wrapper_flags_wash_traded_candidate():
    fake, reason = is_fake_candidate(_candidate(vol_1h=3_000_000))
    assert fake is True and reason == "1h-vol-20x-depth"


def test_wrapper_missing_vol_1h_is_not_fake():
    fake, reason = is_fake_candidate(_candidate(vol_1h=None))
    assert fake is False and reason == ""


# --- volume_5m plumbing -----------------------------------------------------

def test_candidate_model_has_volume_5m_field():
    c = _candidate()
    assert c.volume_5m_usd == 10_000
    assert c.to_dict()["volume_5m_usd"] == 10_000


def test_dexscreener_extracts_volume_m5():
    from data_providers.dexscreener import _extract_pair_fields
    pair = {
        "priceUsd": "0.01",
        "liquidity": {"usd": 100_000},
        "volume": {"h24": 800_000, "h1": 100_000, "m5": 12_345},
        "priceChange": {"h1": 5.0},
        "txns": {"h1": {"buys": 500, "sells": 400}},
        "fdv": 1_000_000,
    }
    fields = _extract_pair_fields(pair)
    assert fields["volume_5m_usd"] == 12_345
