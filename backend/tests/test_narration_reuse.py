"""
tests/test_narration_reuse.py — Task A.5 "stats changed meaningfully" logic.
"""
from __future__ import annotations

from llm.reuse import reused_if_stable, stats_signature
from tests.test_rules import make_candidate

BASE = {
    "all_passed": False,
    "failed_rule_ids": ["buy_pressure", "volume_alive"],
}


def prior(stats, all_passed=False, failed=None):
    return {"all_passed": all_passed or BASE["all_passed"],
            "failed_rule_ids": failed or BASE["failed_rule_ids"],
            "stats": stats}


def sig(liq=50_000.0, v1h=20_000.0, mc=100_000.0, buys=300, sells=200,
        chg=5.0):
    return (liq, v1h, mc, buys, sells, chg)


def test_identical_stats_reuse():
    assert reused_if_stable(prior(sig()), False, BASE["failed_rule_ids"], sig())


def test_verdict_flip_blocks_reuse():
    assert not reused_if_stable(
        prior(sig(), all_passed=True), True, [], sig())


def test_failed_set_change_blocks_reuse():
    s = sig()
    assert not reused_if_stable(prior(s), False,
                                BASE["failed_rule_ids"] + ["liquidity_floor"], s)


def test_small_volume_jitter_within_noise_reuses():
    # $40 change on $20k = 0.2% (<5%) and <$250 floor -> unchanged
    assert reused_if_stable(prior(sig(v1h=20_000.0)), False,
                            BASE["failed_rule_ids"], sig(v1h=20_040.0))


def test_meaningful_volume_move_blocks_reuse():
    # +$1,500 on $20k = 7.5% (>5%) and >$250 -> meaningful
    assert not reused_if_stable(prior(sig(v1h=20_000.0)), False,
                                BASE["failed_rule_ids"], sig(v1h=21_500.0))


def test_tiny_token_absolute_floor_protects():
    # micro-cap: $120 -> $160 is 33% relative but under the $250 floor ->
    # NOT meaningful (jitter), reuse allowed.
    assert reused_if_stable(prior(sig(v1h=120.0)), False,
                            BASE["failed_rule_ids"], sig(v1h=160.0))


def test_tx_count_move_beyond_both_bounds_blocks():
    # buys 300 -> 340: 13% (>10%) and >25 txns -> meaningful
    assert not reused_if_stable(prior(sig(buys=300)), False,
                                BASE["failed_rule_ids"], sig(buys=340))


def test_price_change_shift_over_3pp_blocks():
    assert not reused_if_stable(prior(sig(chg=2.0)), False,
                                BASE["failed_rule_ids"], sig(chg=6.0))


def test_none_appearing_is_meaningful():
    assert not reused_if_stable(prior(sig(v1h=None)), False,
                                BASE["failed_rule_ids"], sig(v1h=9_999.0))


def test_stats_signature_matches_field_order():
    c = make_candidate()
    assert stats_signature(c) == (
        c.liquidity_usd, c.volume_1h_usd, c.market_cap_usd,
        c.buys_1h, c.sells_1h, c.price_change_1h_pct)


# --- malformed / legacy prior must fail CLOSED (never raise) ----------------
# Regression for the pre-2026-08-27 writer that stored the decision dict without
# a "stats" key: reused_if_stable used to KeyError and kill the whole tick.

def test_prior_missing_stats_fails_closed_not_raises():
    legacy = {"all_passed": False, "failed_rule_ids": BASE["failed_rule_ids"]}
    assert reused_if_stable(legacy, False, BASE["failed_rule_ids"], sig()) is False


def test_prior_missing_all_passed_fails_closed():
    bad = {"failed_rule_ids": BASE["failed_rule_ids"], "stats": sig()}
    assert reused_if_stable(bad, False, BASE["failed_rule_ids"], sig()) is False


def test_prior_missing_failed_rule_ids_fails_closed():
    bad = {"all_passed": False, "stats": sig()}
    assert reused_if_stable(bad, False, BASE["failed_rule_ids"], sig()) is False


def test_prior_not_a_dict_fails_closed():
    assert reused_if_stable("garbage", False, BASE["failed_rule_ids"], sig()) is False
    assert reused_if_stable(["a", "b"], False, BASE["failed_rule_ids"], sig()) is False

