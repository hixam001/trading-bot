"""
deterministic_filter.py — Hard pre-filter rules applied before any LLM call.

Every candidate must pass ALL checks before being sent to the LLM for scoring.
This keeps expensive LLM calls reserved for candidates that are at least
structurally valid. All thresholds come from config — never hardcoded inline.

Failure behavior: return (False, [list_of_flags]) — never substitute a default
and silently continue (defense-first rule 2).
"""
from __future__ import annotations

import logging

import config
from models import Candidate

log = logging.getLogger(__name__)


def apply_filters(candidate: Candidate) -> tuple[bool, list[str]]:
    """
    Apply all deterministic pre-filter rules to a candidate.

    Returns:
        (passed, risk_flags)
        passed     — True if the candidate passes every check
        risk_flags — list of descriptive strings for each failed check

    Every rejected candidate is logged with its specific failure reason(s)
    so the decision log is auditable (defense-first rule 6).
    """
    flags: list[str] = []

    # ── Liquidity ────────────────────────────────────────────────────────────
    if candidate.liquidity_usd < config.MIN_LIQUIDITY_USD:
        flags.append(
            f"low_liquidity: ${candidate.liquidity_usd:,.0f} < "
            f"${config.MIN_LIQUIDITY_USD:,.0f} minimum"
        )

    # ── Holder concentration ─────────────────────────────────────────────────
    if candidate.top_holder_pct >= config.MAX_TOP_HOLDER_PCT:
        flags.append(
            f"high_holder_concentration: top holder owns "
            f"{candidate.top_holder_pct:.1f}% >= {config.MAX_TOP_HOLDER_PCT:.1f}% limit"
        )

    # ── Holder count ─────────────────────────────────────────────────────────
    if candidate.holder_count < config.MIN_HOLDER_COUNT:
        flags.append(
            f"low_holder_count: {candidate.holder_count} < "
            f"{config.MIN_HOLDER_COUNT} minimum"
        )

    # ── Token age — too new ───────────────────────────────────────────────────
    if candidate.age_hours < config.MIN_AGE_HOURS:
        flags.append(
            f"too_new: {candidate.age_hours:.2f}h old < "
            f"{config.MIN_AGE_HOURS}h minimum"
        )

    # ── Token age — too old ───────────────────────────────────────────────────
    if candidate.age_hours > config.MAX_AGE_HOURS:
        flags.append(
            f"too_old: {candidate.age_hours:.1f}h old > "
            f"{config.MAX_AGE_HOURS}h maximum"
        )

    # ── Volume ───────────────────────────────────────────────────────────────
    if candidate.volume_24h_usd < config.MIN_VOLUME_24H_USD:
        flags.append(
            f"low_volume: ${candidate.volume_24h_usd:,.0f}/24h < "
            f"${config.MIN_VOLUME_24H_USD:,.0f} minimum"
        )

    # ── Market cap ───────────────────────────────────────────────────────────
    if candidate.market_cap_usd < config.MIN_MARKET_CAP_USD:
        flags.append(
            f"low_market_cap: ${candidate.market_cap_usd:,.0f} < "
            f"${config.MIN_MARKET_CAP_USD:,.0f} minimum"
        )

    # ── Zero / negative price guard ──────────────────────────────────────────
    if candidate.price_usd <= 0:
        flags.append(f"invalid_price: {candidate.price_usd} (must be > 0)")

    passed = len(flags) == 0

    if not passed:
        log.debug(
            "FILTER REJECT %s (%s): %s",
            candidate.symbol,
            candidate.mint_address,
            "; ".join(flags),
        )
    else:
        log.debug(
            "FILTER PASS %s (%s)",
            candidate.symbol,
            candidate.mint_address,
        )

    return passed, flags
