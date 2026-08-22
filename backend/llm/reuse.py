"""
llm/reuse.py — short-term thesis reuse (Task A.5).

If a mint was narrated within the last N ticks and its rule-relevant stats
haven't moved meaningfully, reuse the prior thesis instead of spending
another Ollama call. The RULE ENGINE always re-evaluates every candidate
every tick — this only suppresses duplicate narration of an unchanged
rejection/acceptance.

"Meaningfully changed" = any of:
  - verdict (all_passed) or failed_rule_ids set differs, OR
  - liquidity/volume_1h: >5% relative AND >$250 absolute change, OR
  - market_cap: >5% relative change, OR
  - buys_1h/sells_1h: >10% relative AND >25 txns absolute change, OR
  - price_change_1h_pct: >3 percentage points

(The relative+absolute pairs mean tiny tokens aren't flagged by rounding
noise; large tokens need a real proportional move.)
"""
from __future__ import annotations

import config


# Ticks a prior thesis stays reusable (config-overridable for calibration).
REUSE_TICK_WINDOW = getattr(config, "THESIS_REUSE_TICKS", 3)

# (relative fraction, absolute floor): a field counts as changed only if it
# exceeds the relative bound while ALSO exceeding the absolute noise floor,
# so small-token rounding jitter never counts as meaningful movement.
_REL_ABS = {
    "liquidity_usd": (0.05, 250.0),
    "volume_1h_usd": (0.05, 250.0),
    "market_cap_usd": (0.05, None),
    "buys_1h": (0.10, 25),
    "sells_1h": (0.10, 25),
}
_CHG_PP = 3.0   # price_change_1h_pct percentage points
_FIELD_ORDER = ("liquidity_usd", "volume_1h_usd", "market_cap_usd",
                "buys_1h", "sells_1h", "price_change_1h_pct")


def stats_signature(c) -> tuple:
    """The rule-relevant numerics compared between ticks (pure)."""
    return (
        c.liquidity_usd,
        c.volume_1h_usd,
        c.market_cap_usd,
        c.buys_1h,
        c.sells_1h,
        c.price_change_1h_pct,
    )


def reused_if_stable(prior: dict | None,
                     all_passed: bool,
                     failed_rule_ids: list[str],
                     stats: tuple) -> bool:
    """
    Pure decision used by main.py. prior=None -> False (never narrated).

    prior: {"all_passed": bool, "failed_rule_ids": [str], "stats": tuple}
    stats: stats_signature(candidate) for THIS tick.
    """
    if not prior:
        return False
    if prior["all_passed"] != all_passed:
        return False
    if set(prior["failed_rule_ids"]) != set(failed_rule_ids):
        return False

    for name, old, new in zip(_FIELD_ORDER, prior["stats"], stats):
        if name == "price_change_1h_pct":
            if old is None or new is None:
                if old != new:
                    return False
            elif abs(new - old) > _CHG_PP:
                return False
            continue
        rel, abs_floor = _REL_ABS[name]
        if old is None or new is None:
            if old != new:
                return False
            continue
        delta = abs(new - old)
        if abs_floor is not None and delta <= abs_floor:
            continue
        base = max(abs(old), 1e-9)
        if (delta / base) > rel:
            return False
    return True

