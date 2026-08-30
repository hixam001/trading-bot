"""
models.py — Core domain dataclasses for trading-bot.

Plain Python dataclasses; SQLite persistence lives entirely in api/db.py.
No file I/O happens here.

None semantics (non-negotiable): a field a data source did not actually
return is None. None means UNKNOWN — it is never coerced to False/0, which
would fabricate a safety claim ("checked and fine") that was never made.

PAPER TRADING ONLY: nothing in this file moves real funds. Trade represents
a simulated position exclusively.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Candidate — a token presented to the gate for evaluation (B13)
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """
    A memecoin candidate assembled from one or more providers.

    Numeric fields the gate rules depend on (volume_1h_usd, buys_1h,
    sells_1h, price_change_1h_pct) are Optional: None = provider did not
    return them. Rules treat missing values explicitly as unevaluable and
    fail closed (skip the candidate) rather than guessing.

    Security fields default None (= not checked), never False (= checked
    and safe).
    """
    symbol: str
    mint_address: str
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    market_cap_usd: float
    volume_1h_usd: Optional[float] = None
    buys_1h: Optional[int] = None
    sells_1h: Optional[int] = None
    price_change_1h_pct: Optional[float] = None   # percent, e.g. -12.5
    # --- reference-parity breadth fields ---------------------------------------
    price_change_5m_pct: Optional[float] = None
    price_change_6h_pct: Optional[float] = None
    price_change_24h_pct: Optional[float] = None
    volume_5m_usd: Optional[float] = None         # A7 fake-chart filter input
    fdv_usd: Optional[float] = None               # fully-diluted valuation
    buys_6h: Optional[int] = None
    sells_6h: Optional[int] = None
    volume_6h_usd: Optional[float] = None
    pool_count: Optional[int] = None              # number of Solana pools
    total_liquidity_usd: Optional[float] = None   # summed across ALL pools
    top_pool_share: Optional[float] = None        # deepest/total, 0..1
    boosted: Optional[bool] = None                # paid Dexscreener boost active
    # Realtime social read (llm/social.py) - EVIDENCE ONLY, never a verdict:
    # interest in {organic, peaked, unclear}; note is one grounded sentence.
    social_interest: Optional[str] = None
    web_summary: Optional[str] = None            # condensed web evidence lines
    social_note: Optional[str] = None
    age_hours: Optional[float] = None
    holder_count: Optional[int] = None
    top_holder_pct: Optional[float] = None
    # Token mint decimals — REQUIRED for correct execution-price quoting
    # (a wrong decimals value scales prices by 10^(9-decimals)). None = unknown.
    decimals: Optional[int] = None
    # Public presence channels — None = unknown, True = present
    has_twitter: Optional[bool] = None
    has_telegram: Optional[bool] = None
    has_website: Optional[bool] = None
    # Security fields (Birdeye token_security) — None = not checked
    mint_authority_revoked: Optional[bool] = None
    freeze_authority_revoked: Optional[bool] = None
    is_likely_honeypot: Optional[bool] = None
    mutable_metadata: Optional[bool] = None
    transfer_fee_enable: Optional[bool] = None
    name: str = ""
    source: str = "mock"   # which provider stack produced this candidate
    # Crowd conviction (crowd_heat): REAL heat from the fomo.fun board /
    # pump.fun comments when a feed answered, filled during the read stage.
    # None = no feed answered -> the rule falls back to the presence proxy.
    fomo_heat: Optional[int] = None
    crowd_heat_source: str = ""            # "fomo" | "pumpfun" | "" (= proxy)
    fomo_theses: Optional[list[dict]] = None
    # §43 (2026-08-30): True when the pipeline DELIBERATELY did not spend a
    # fomo.fun lookup on this candidate because it already failed one of the
    # cheap local rules (§44 sequencing: cheap rules → scrape → crowd rules).
    # The crowd_heat rule then reports evaluated=False ("not evaluated")
    # instead of falling back to the presence proxy — an honest "we didn't
    # look" beats a number nobody measured. Never set in mock mode (no feed
    # runs there at all).
    crowd_lookup_deferred: bool = False
    # Discovery provenance ONLY — observability field. The rule engine never
    # reads or branches on this. "trending" | "new_listing" | "both" |
    # "unknown" (explicit unknown; never silently defaulted to trending).
    discovery_source: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "mint_address": self.mint_address,
            "fomo_heat": self.fomo_heat,
            "crowd_heat_source": self.crowd_heat_source,
            "fomo_theses": self.fomo_theses,
            "crowd_lookup_deferred": self.crowd_lookup_deferred,
            "price_usd": self.price_usd,
            "liquidity_usd": self.liquidity_usd,
            "volume_24h_usd": self.volume_24h_usd,
            "volume_1h_usd": self.volume_1h_usd,
            "buys_1h": self.buys_1h,
            "sells_1h": self.sells_1h,
            "price_change_1h_pct": self.price_change_1h_pct,
            "price_change_5m_pct": self.price_change_5m_pct,
            "price_change_6h_pct": self.price_change_6h_pct,
            "price_change_24h_pct": self.price_change_24h_pct,
            "volume_5m_usd": self.volume_5m_usd,
            "fdv_usd": self.fdv_usd,
            "buys_6h": self.buys_6h,
            "sells_6h": self.sells_6h,
            "volume_6h_usd": self.volume_6h_usd,
            "pool_count": self.pool_count,
            "total_liquidity_usd": self.total_liquidity_usd,
            "top_pool_share": self.top_pool_share,
            "boosted": self.boosted,
            "social_interest": self.social_interest,
            "web_summary": self.web_summary,
            "social_note": self.social_note,
            "age_hours": self.age_hours,
            "market_cap_usd": self.market_cap_usd,
            "holder_count": self.holder_count,
            "decimals": self.decimals,
            "top_holder_pct": self.top_holder_pct,
            "has_twitter": self.has_twitter,
            "has_telegram": self.has_telegram,
            "has_website": self.has_website,
            "mint_authority_revoked": self.mint_authority_revoked,
            "freeze_authority_revoked": self.freeze_authority_revoked,
            "is_likely_honeypot": self.is_likely_honeypot,
            "mutable_metadata": self.mutable_metadata,
            "transfer_fee_enable": self.transfer_fee_enable,
            "discovery_source": self.discovery_source,
            "source": self.source,
        }


@dataclass(frozen=True)
class SecurityInfo:
    """Security check results. None ALWAYS means unknown — never False."""
    mint_authority_revoked: Optional[bool] = None
    freeze_authority_revoked: Optional[bool] = None
    is_likely_honeypot: Optional[bool] = None


# ---------------------------------------------------------------------------
# Rule engine contracts (§2.1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuleResult:
    rule_id: str                                # stable snake_case identifier
    passed: bool
    detail: str                                 # human-readable, includes real numbers
    value: float | int | bool | None = None     # raw underlying value for logging/calibration
    # evaluated=False means this rule was NOT actually run for this candidate
    # (§43: the crowd_heat feed is only queried for candidates that already
    # cleared every other rule, so a reject's crowd number is "not evaluated"
    # rather than a fabricated proxy). A non-evaluated rule ALWAYS carries
    # passed=False and value=None — it can never contribute a pass claim.
    evaluated: bool = True


@dataclass(frozen=True)
class GateDecision:
    candidate: Candidate
    rules: list[RuleResult]
    all_passed: bool
    failed_rule_ids: list[str]
    # Rules deliberately not evaluated for this candidate (§43). Kept apart
    # from failed_rule_ids so the learning loop's rejection breakdown and the
    # thesis-reuse signature never read a skipped rule as a real rejection —
    # while the full rule list (incl. the skipped one) stays in `rules`.
    not_evaluated_rule_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "rules": [
                {"rule_id": r.rule_id, "passed": r.passed, "detail": r.detail,
                 "value": r.value, "evaluated": r.evaluated}
                for r in self.rules
            ],
            "all_passed": self.all_passed,
            "failed_rule_ids": self.failed_rule_ids,
            "not_evaluated_rule_ids": self.not_evaluated_rule_ids,
        }


# ---------------------------------------------------------------------------
# Trade — a simulated (paper) position
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    trade_id: str = field(default_factory=lambda: _new_id("trade"))
    symbol: str = ""
    mint_address: str = ""
    opened_at: str = field(default_factory=_now_iso)
    entry_price_usd: float = 0.0
    position_size_usd: float = 0.0     # USD deployed (post-slippage, post-fee); grows on scale-in
    quantity: float = 0.0              # tokens held; grows on scale-in
    candidate_snapshot: dict = field(default_factory=dict)
    thesis: str = ""
    # Populated on close
    closed_at: Optional[str] = None
    exit_price_usd: Optional[float] = None
    exit_reason: Optional[str] = None  # exit_stop_loss | exit_trail_give_back |
    # exit_liquidity_break | exit_thesis_invalidated | exit_stale_thesis
    realized_pnl_usd: Optional[float] = None
    realized_pnl_pct: Optional[float] = None
    is_open: bool = True
    # reference-style exit machinery:
    high_water_usd: Optional[float] = None  # peak price since entry (trail memory)
    tranches_taken: int = 0                 # how many TP-ladder trims fired
    # Populated asynchronously after close (D5/D6) — never blocks the tick loop
    reflection_text: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "mint_address": self.mint_address,
            "opened_at": self.opened_at,
            "entry_price_usd": self.entry_price_usd,
            "position_size_usd": self.position_size_usd,
            "quantity": self.quantity,
            "candidate_snapshot": self.candidate_snapshot,
            "thesis": self.thesis,
            "closed_at": self.closed_at,
            "exit_price_usd": self.exit_price_usd,
            "exit_reason": self.exit_reason,
            "realized_pnl_usd": self.realized_pnl_usd,
            "realized_pnl_pct": self.realized_pnl_pct,
            "is_open": self.is_open,
            "reflection_text": self.reflection_text,
        }


# ---------------------------------------------------------------------------
# FeedEvent — every gate decision logged, pass OR fail (§2.4)
# ---------------------------------------------------------------------------

@dataclass
class FeedEvent:
    ts: str = field(default_factory=_now_iso)
    symbol: str = ""
    mint_address: str = ""
    candidate_snapshot: dict = field(default_factory=dict)
    verdict: str = "fail"                      # "pass" | "fail"
    thesis: str = ""
    rule_breakdown: list[dict] = field(default_factory=list)   # full RuleResults, always complete
    failed_rule_ids: list[str] = field(default_factory=list)
    regime_ok: bool = False
    grounding_flags: list[str] = field(default_factory=list)   # ungrounded-term flags (D2) — flagged, never dropped
    narration_source: str = ""                 # "ollama:<model>" | "template" | ""
    led_to_trade_id: Optional[str] = None
    model_version: Optional[str] = None
    prompt_version: Optional[str] = None
    id: Optional[int] = None                   # set after DB insert

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "symbol": self.symbol,
            "mint_address": self.mint_address,
            "candidate_snapshot": self.candidate_snapshot,
            "verdict": self.verdict,
            "thesis": self.thesis,
            "rule_breakdown": self.rule_breakdown,
            "failed_rule_ids": self.failed_rule_ids,
            "regime_ok": self.regime_ok,
            "grounding_flags": self.grounding_flags,
            "narration_source": self.narration_source,
            "led_to_trade_id": self.led_to_trade_id,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
        }


# ---------------------------------------------------------------------------
# PortfolioState — what the rules evaluate against (§2.1 RuleFn signature)
# ---------------------------------------------------------------------------

@dataclass
class PortfolioState:
    cash_usd: float
    open_positions: list[Trade] = field(default_factory=list)

    def held_usd_in_mint(self, mint_address: str) -> float:
        return sum(
            t.position_size_usd for t in self.open_positions
            if t.mint_address == mint_address
        )

    def get_open_trade_for_mint(self, mint_address: str) -> Optional[Trade]:
        return next(
            (t for t in self.open_positions if t.mint_address == mint_address),
            None,
        )


# ---------------------------------------------------------------------------
# DailyStats — aggregate stats for one calendar day (learning loop G1/G2)
# ---------------------------------------------------------------------------

@dataclass
class DailyStats:
    date: str                                   # YYYY-MM-DD UTC
    open_positions: int = 0
    closed_trades: int = 0
    stats_json: dict = field(default_factory=dict)  # win rate, PF, DD, pnl, rejection breakdown
