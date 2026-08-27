"""
rule_engine/fake_chart.py — A7 wash-trade "fake chart" filter (omo audit §28).

Verbatim port of the reference `isFakeChart` (omotrades/omo,
src/lib/market.server.ts). A wash-traded pair prints a headline hour of volume
that its own depth could never absorb, and it does it in a handful of enormous
round-trips instead of a crowd of small ones. Nothing said about a tape like
that is worth saying, so these rows never reach the reasoning layer at all.

This is a PRE-FILTER, not the entry verdict: it only removes manufactured /
dead / distribution-corpse tapes before enrichment and think/gate so they never
burn scrape or LLM credits. The deterministic gate + model still authorize every
real entry.

Missing-data policy (defense-first, "unknown != zero" — the decimals lesson):
  * This filter only flags tapes it can actually SEE. If `vol_1h` is unknown
    (None) the tape is unevaluable -> return NOT fake and let the deterministic
    gate fail the candidate closed with an accurate missing-data reason
    (liquidity_floor / volume_alive). A chart with no data is "unevaluable",
    not "fake" — we never mislabel it.
  * Any threshold whose CONCLUSION would be a false positive on missing data
    (fees-vs-fdv, fresh-launch, vol-without-crowd, dead-tape, no-recent-trades,
    headline-day) only fires when the field it reasons about is PRESENT. An
    unknown 6h window is not "an empty present"; an unknown fdv is not "a small
    float"; unknown buys/sells are not "nobody behind it".
  * Thresholds where coercing None -> 0 is the SAFE direction (the negative-
    change bleed/corpse tests: 0 can never be < -25) keep reference `?? 0`
    parity, so behavior is identical for all real (data-present) cases.

Each threshold returns a short stable reason string so every rejection is
auditable (defense-first rule 6: log every rejection with a reason).
"""
from __future__ import annotations

from typing import Optional, Tuple

# Standard swap-fee estimate used to derive "fees paid" from traded volume
# (reference parity: lifeVol * 0.005).
_FEE_RATE = 0.005


def _n(v: Optional[float]) -> float:
    """None -> 0.0 (reference `?? 0` parity); pass through real numbers."""
    return float(v) if v is not None else 0.0


def is_fake_chart(
    *,
    liq_usd: Optional[float],
    vol_1h: Optional[float],
    vol_5m: Optional[float] = None,
    vol_6h: Optional[float] = None,
    vol_24h: Optional[float] = None,
    buys_1h: Optional[int] = None,
    sells_1h: Optional[int] = None,
    chg_1h: Optional[float] = None,
    chg_6h: Optional[float] = None,
    chg_24h: Optional[float] = None,
    fdv_usd: Optional[float] = None,
    age_hours: Optional[float] = None,
) -> Tuple[bool, str]:
    """
    Return (is_fake, reason). reason names the tripped threshold ('' if real).

    The 13 thresholds are evaluated in reference order; the FIRST to trip wins.
    """
    # Guard: an unknown 1h tape is unevaluable here — the gate fails it closed
    # with an accurate missing-data reason. Never mislabel "no data" as "fake".
    if vol_1h is None:
        return False, ""

    liq = _n(liq_usd)
    vol1h = _n(vol_1h)
    vol5m = _n(vol_5m)
    vol6h = _n(vol_6h)
    vol24h = _n(vol_24h)
    buys = int(_n(buys_1h))
    sells = int(_n(sells_1h))
    trades = buys + sells
    chg1h = _n(chg_1h)
    chg6h = _n(chg_6h)
    chg24h = _n(chg_24h)
    fdv = _n(fdv_usd)
    age = _n(age_hours)

    # Fees-based thresholds (1, 2) measure lifetime turnover, so they need the
    # 24h volume to be VISIBLE. A missing vol_24h is "unevaluable", not "zero
    # fees" — assuming zero would mislabel a data-poor tape as manufactured
    # (unknown != zero, the decimals lesson).
    if vol_24h is not None:
        # Fees paid is the honest receipt on a chart. Real turnover leaves fees
        # behind, so a float that has barely generated any fee over its whole
        # life was never traded by a crowd: the price was walked, not bought.
        life_vol = vol24h if (age > 0 and age < 24) else max(vol24h, vol6h * 4)
        fees_usd = life_vol * _FEE_RATE
        # 1. young tape whose lifetime fees can't justify its valuation
        if age > 0 and age < 72 and fdv > 0 and fees_usd < fdv * 0.03:
            return True, "fees-too-low-for-fdv"
        # 2. a fresh launch carrying a small float and almost no fees. Needs a
        # REAL fdv — an unknown fdv is not "a small float" (unknown != zero).
        if (age > 0 and age < 24 and fdv_usd is not None
                and fdv < 150_000 and fees_usd < 2_000):
            return True, "fresh-launch-no-fees"

    # 3/4. an hour (or a day) cannot really turn over its own depth this much
    if liq > 0 and vol1h > liq * 20:
        return True, "1h-vol-20x-depth"
    if liq > 0 and vol24h > liq * 150:
        return True, "24h-vol-150x-depth"

    # 5. volume with almost nobody behind it. Needs REAL trade counts — unknown
    # buys/sells are not "nobody" (unknown != zero).
    if buys_1h is not None and sells_1h is not None:
        if vol1h > 50_000 and trades < 60:
            return True, "vol-without-crowd"
    # 6. an average ticket no real participant in a pool this thin would send
    if trades > 0 and vol1h / trades > 2_500 and liq < 150_000:
        return True, "avg-ticket-too-big"
    # 7. one-sided by construction: bots loop, crowds do not
    if trades > 40 and (buys == 0 or sells == 0):
        return True, "one-sided-tape"

    # 8/9. charts nothing intelligent can be said about: a straight bleed off
    # the top / a distribution corpse already down hard on every window. An
    # unknown change window coerces to 0, which can never satisfy a < -25 /
    # < -40 test, so missing data safely BLOCKS these flags (reference ?? 0
    # parity where zero is the safe direction).
    if chg1h < -25 and chg6h < -40:
        return True, "straight-bleed"
    if chg24h < -55 and chg6h < -20:
        return True, "distribution-corpse"

    # 10. dead tape: the pool is still there but nobody is trading it any more.
    # Needs a REAL vol_24h — an unknown day-volume is not "nobody trades it".
    if (liq > 0 and vol_24h is not None
            and vol1h < liq * 0.15 and vol24h < liq * 3):
        return True, "dead-tape"
    # 11. no recent trades and a tiny hour. Needs a REAL vol_5m — an unknown
    # 5m window is not "no recent trades" (unknown != zero).
    if vol_5m is not None and vol5m == 0 and vol1h < 5_000:
        return True, "no-recent-trades"
    # 12. headline day, empty present: whatever happened here is over. Needs a
    # REAL vol_6h — an unknown 6h window is not "an empty present".
    if vol_6h is not None and vol24h > 0 and vol6h / vol24h < 0.06:
        return True, "headline-day-over"
    # 13. paper float on a sliver of real depth
    if liq > 0 and fdv > 0 and fdv / liq > 30:
        return True, "paper-float"

    return False, ""


def is_fake_candidate(c) -> Tuple[bool, str]:
    """Candidate-level wrapper: pull the tape fields off a models.Candidate."""
    return is_fake_chart(
        liq_usd=getattr(c, "liquidity_usd", None),
        vol_1h=getattr(c, "volume_1h_usd", None),
        vol_5m=getattr(c, "volume_5m_usd", None),
        vol_6h=getattr(c, "volume_6h_usd", None),
        vol_24h=getattr(c, "volume_24h_usd", None),
        buys_1h=getattr(c, "buys_1h", None),
        sells_1h=getattr(c, "sells_1h", None),
        chg_1h=getattr(c, "price_change_1h_pct", None),
        chg_6h=getattr(c, "price_change_6h_pct", None),
        chg_24h=getattr(c, "price_change_24h_pct", None),
        fdv_usd=getattr(c, "fdv_usd", None),
        age_hours=getattr(c, "age_hours", None),
    )
