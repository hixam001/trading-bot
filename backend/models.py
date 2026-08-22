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
    # Discovery provenance ONLY — observability field. The rule engine never
    # reads or branches on this. "trending" | "new_listing" | "both" |
    # "unknown" (explicit unknown; never silently defaulted to trending).
    discovery_source: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "mint_address": self.mint_address,
            "price_usd": self.price_usd,
            "liquidity_usd": self.liquidity_usd,
            "volume_24h_usd": self.volume_24h_usd,
            "volume_1h_usd": self.volume_1h_usd,
            "buys_1h": self.buys_1h,
            "sells_1h": self.sells_1h,
            "price_change_1h_pct": self.price_change_1h_pct,
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


@dataclass(frozen=True)
class GateDecision:
    candidate: Candidate
    rules: list[RuleResult]
    all_passed: bool
    failed_rule_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "rules": [
                {"rule_id": r.rule_id, "passed": r.passed, "detail": r.detail, "value": r.value}
                for r in self.rules
            ],
            "all_passed": self.all_passed,
            "failed_rule_ids": self.failed_rule_ids,
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
    exit_reason: Optional[str] = None  # take_profit | stop_loss | timeout
    realized_pnl_usd: Optional[float] = None
    realized_pnl_pct: Optional[float] = None
    is_open: bool = True
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
