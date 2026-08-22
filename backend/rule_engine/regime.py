"""
rule_engine/regime.py — market regime gate (§3).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

import config
from models import Candidate


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MarketRegime:
    computed_at: str
    pct_candidates_green_1h: float
    median_volume_1h_usd: float
    avg_buy_sell_ratio: float
    regime_ok: bool
    regime_detail: str


def compute_market_regime(candidates: list[Candidate]) -> MarketRegime:
    """Computed ONCE per tick from the full candidate batch, before any
    per-candidate evaluation (§3.2)."""
    if not candidates:
        return MarketRegime(
            computed_at=_now_iso(), pct_candidates_green_1h=0.0,
            median_volume_1h_usd=0.0, avg_buy_sell_ratio=0.0,
            regime_ok=False, regime_detail="no candidates this tick",
        )

    pct_green = sum(1 for c in candidates if (c.price_change_1h_pct or 0.0) > 0) / len(candidates)
    median_vol = statistics.median(c.volume_1h_usd or 0.0 for c in candidates)
    avg_ratio = statistics.mean(
        (c.buys_1h / max(c.sells_1h, 1)) if c.buys_1h is not None and c.sells_1h is not None else 0.0
        for c in candidates
    )

    # NOTE: thresholds below are explicit placeholders pending calibration
    # against real data in the 10-day window (config.py §3.3 note).
    regime_ok = (
        config.REGIME_MIN_PCT_GREEN <= pct_green <= config.REGIME_MAX_PCT_GREEN
        and median_vol >= config.REGIME_MIN_MEDIAN_VOLUME_USD
    )
    detail = f"{pct_green:.0%} green, median 1h vol ${median_vol:,.0f}, avg buy/sell ratio {avg_ratio:.2f}"

    return MarketRegime(
        computed_at=_now_iso(), pct_candidates_green_1h=pct_green,
        median_volume_1h_usd=median_vol, avg_buy_sell_ratio=avg_ratio,
        regime_ok=regime_ok, regime_detail=detail,
    )
