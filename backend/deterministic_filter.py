"""
deterministic_filter.py — Two-tier pre-filter applied before any LLM call.

TIER 1 — Hard fails (instant rejection, no LLM call):
  Checks backed by near-unambiguous on-chain facts when the data is actually
  known (non-None). A None value always means "not checked" and is never
  treated as a failing value.

  Hard-fail conditions:
    • price_usd <= 0              — invalid/corrupt data
    • is_likely_honeypot is True  — confirmed: can't sell
    • mint_authority_revoked is False — confirmed: creator can mint unlimited supply
    • freeze_authority_revoked is False — confirmed: creator can freeze all holders
    • transfer_fee_enable is True — confirmed: Token-2022 hidden sell tax active

TIER 2 — Soft signals (candidate proceeds to LLM with flags injected into prompt):
  Probabilistic checks whose thresholds come from config. The LLM weighs
  multiple stacking flags together — a single flag is not a rejection.
  Per memecoin_evaluation_notes.md §2: "treat as a risk flag that lowers
  confidence, not an automatic hard fail."

  Soft checks: liquidity, holder count, holder concentration, age bounds,
  24h volume, market cap, volume/mcap ratio, dying 1h/6h volume ratio.

Return type:
    (hard_fail: bool, hard_fail_reason: str | None, soft_flags: list[str])
"""
from __future__ import annotations

import logging
from typing import Optional

import config
from models import Candidate

log = logging.getLogger(__name__)

# Type alias for the new return shape
FilterResult = tuple[bool, Optional[str], list[str]]


def apply_filters(candidate: Candidate) -> FilterResult:
    """
    Apply two-tier deterministic pre-filter rules to a candidate.

    Returns:
        (hard_fail, hard_fail_reason, soft_flags)

        hard_fail        — True if candidate is rejected before reaching the LLM
        hard_fail_reason — Single descriptive string when hard_fail is True, else None
        soft_flags       — List of risk flag strings to pass through to the LLM prompt.
                           Non-empty even when hard_fail is False; empty when clean.

    Every decision path is logged for auditability (defense-first rule 6).
    None is NEVER treated as a failing value — it means the data was unavailable.
    """

    # ── TIER 1: HARD FAILS ───────────────────────────────────────────────────
    # Check each hard-fail condition. Return immediately on first match so the
    # reason is precise (not a combined string — hard fails are singular facts).

    # Invalid price — data quality gate, not a trading signal
    if candidate.price_usd <= 0:
        reason = f"invalid_price: {candidate.price_usd} (must be > 0)"
        log.info(
            "HARD FAIL %s (%s): %s",
            candidate.symbol, candidate.mint_address, reason,
        )
        return True, reason, []

    # Confirmed honeypot — token transfer is impossible for buyers
    if candidate.is_likely_honeypot is True:
        reason = "confirmed_honeypot: token flagged as honeypot (holders cannot sell)"
        log.info(
            "HARD FAIL %s (%s): %s",
            candidate.symbol, candidate.mint_address, reason,
        )
        return True, reason, []

    # Confirmed mint authority active — creator can inflate supply at will
    if candidate.mint_authority_revoked is False:
        reason = "mint_authority_active: creator retains unlimited minting capability"
        log.info(
            "HARD FAIL %s (%s): %s",
            candidate.symbol, candidate.mint_address, reason,
        )
        return True, reason, []

    # Confirmed freeze authority active — creator can freeze all holder accounts
    if candidate.freeze_authority_revoked is False:
        reason = "freeze_authority_active: creator can freeze all holder accounts"
        log.info(
            "HARD FAIL %s (%s): %s",
            candidate.symbol, candidate.mint_address, reason,
        )
        return True, reason, []

    # Confirmed Token-2022 transfer fee active — hidden sell tax
    if candidate.transfer_fee_enable is True:
        reason = "transfer_fee_active: Token-2022 sell tax is enabled (hidden exit cost)"
        log.info(
            "HARD FAIL %s (%s): %s",
            candidate.symbol, candidate.mint_address, reason,
        )
        return True, reason, []

    # ── TIER 2: SOFT SIGNALS ─────────────────────────────────────────────────
    # Collect all failed soft checks. The candidate still proceeds to the LLM.
    soft_flags: list[str] = []

    # ── Liquidity ────────────────────────────────────────────────────────────
    if candidate.liquidity_usd < config.MIN_LIQUIDITY_USD:
        soft_flags.append(
            f"low_liquidity: ${candidate.liquidity_usd:,.0f} < "
            f"${config.MIN_LIQUIDITY_USD:,.0f} minimum"
        )

    # ── Holder concentration ─────────────────────────────────────────────────
    # Per memecoin_evaluation_notes.md §2: bundling/concentration is a stacking
    # signal, not an individually disqualifying fact.
    if candidate.top_holder_pct >= config.MAX_TOP_HOLDER_PCT:
        soft_flags.append(
            f"high_holder_concentration: top holder owns "
            f"{candidate.top_holder_pct:.1f}% >= {config.MAX_TOP_HOLDER_PCT:.1f}% limit"
        )

    # ── Holder count ─────────────────────────────────────────────────────────
    if candidate.holder_count < config.MIN_HOLDER_COUNT:
        soft_flags.append(
            f"low_holder_count: {candidate.holder_count} < "
            f"{config.MIN_HOLDER_COUNT} minimum"
        )

    # ── Token age — too new ───────────────────────────────────────────────────
    if candidate.age_hours < config.MIN_AGE_HOURS:
        soft_flags.append(
            f"too_new: {candidate.age_hours:.2f}h old < "
            f"{config.MIN_AGE_HOURS}h minimum"
        )

    # ── Token age — too old ───────────────────────────────────────────────────
    if candidate.age_hours > config.MAX_AGE_HOURS:
        soft_flags.append(
            f"too_old: {candidate.age_hours:.1f}h old > "
            f"{config.MAX_AGE_HOURS}h maximum"
        )

    # ── 24h volume ───────────────────────────────────────────────────────────
    if candidate.volume_24h_usd < config.MIN_VOLUME_24H_USD:
        soft_flags.append(
            f"low_volume: ${candidate.volume_24h_usd:,.0f}/24h < "
            f"${config.MIN_VOLUME_24H_USD:,.0f} minimum"
        )

    # ── Market cap ───────────────────────────────────────────────────────────
    if candidate.market_cap_usd < config.MIN_MARKET_CAP_USD:
        soft_flags.append(
            f"low_market_cap: ${candidate.market_cap_usd:,.0f} < "
            f"${config.MIN_MARKET_CAP_USD:,.0f} minimum"
        )

    # ── Volume-to-market-cap ratio ────────────────────────────────────────────
    # Per memecoin_evaluation_notes.md §3: 24h vol < 80% of mcap = manipulation signal.
    if candidate.market_cap_usd > 0:
        vol_mcap_ratio = candidate.volume_24h_usd / candidate.market_cap_usd
        if vol_mcap_ratio < config.MIN_VOLUME_TO_MCAP_RATIO:
            soft_flags.append(
                f"low_vol_mcap_ratio: 24h vol ${candidate.volume_24h_usd:,.0f} is "
                f"{vol_mcap_ratio:.1%} of mcap ${candidate.market_cap_usd:,.0f} "
                f"(< {config.MIN_VOLUME_TO_MCAP_RATIO:.0%} threshold — possible manipulation/bundling)"
            )

    # ── Mutable metadata ─────────────────────────────────────────────────────
    # True means name/symbol/URI can be swapped post-launch (classic rug setup).
    # Not a hard fail because some legitimate projects update metadata legitimately.
    if candidate.mutable_metadata is True:
        soft_flags.append(
            "mutable_metadata: token metadata is not locked "
            "(name/symbol/image can be changed by creator)"
        )

    # ── Dying volume (1h vs 6h momentum collapse) ─────────────────────────────
    # Only fires when both trend fields are present (None = data unavailable, skip).
    if (
        candidate.volume_1h_usd is not None
        and candidate.volume_6h_usd is not None
        and candidate.volume_6h_usd > 0
    ):
        vol_ratio = candidate.volume_1h_usd / candidate.volume_6h_usd
        if vol_ratio < config.MIN_VOLUME_1H_TO_6H_RATIO:
            soft_flags.append(
                f"dying_volume: 1h vol ${candidate.volume_1h_usd:,.0f} is only "
                f"{vol_ratio:.1%} of 6h vol ${candidate.volume_6h_usd:,.0f} "
                f"(< {config.MIN_VOLUME_1H_TO_6H_RATIO:.0%} threshold — momentum collapsing)"
            )

    # ── Log outcome ───────────────────────────────────────────────────────────
    if soft_flags:
        log.debug(
            "SOFT FLAGS %s (%s): %s — proceeding to LLM",
            candidate.symbol,
            candidate.mint_address,
            "; ".join(soft_flags),
        )
    else:
        log.debug(
            "FILTER CLEAN %s (%s) — no flags raised",
            candidate.symbol,
            candidate.mint_address,
        )

    return False, None, soft_flags
