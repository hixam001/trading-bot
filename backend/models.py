"""
models.py — Core domain dataclasses for trading-bot.

These are plain Python dataclasses. SQLite persistence is handled entirely
by api/db.py. No file I/O happens here — models are pure data containers.

PAPER TRADING ONLY: no model, field, or method in this file has anything to
do with real fund movement. The Trade class represents simulated positions.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _new_trade_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Candidate — a token presented for evaluation
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """
    A token candidate fetched from the data ingestion layer.

    All numeric fields are strictly typed. Any ingestion backend that cannot
    provide a value for a required field must raise an error rather than
    substituting a default (defense-first rule 1).
    """
    symbol: str
    mint_address: str
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    holder_count: int
    top_holder_pct: float       # percentage held by the single largest holder
    age_hours: float
    market_cap_usd: float
    # Optional metadata (used for logging / display only, not filter logic)
    name: str = ""
    description: str = ""
    website: str = ""
    twitter: str = ""
    source: str = "mock"        # which backend produced this candidate

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "mint_address": self.mint_address,
            "price_usd": self.price_usd,
            "liquidity_usd": self.liquidity_usd,
            "volume_24h_usd": self.volume_24h_usd,
            "holder_count": self.holder_count,
            "top_holder_pct": self.top_holder_pct,
            "age_hours": self.age_hours,
            "market_cap_usd": self.market_cap_usd,
            "name": self.name,
            "description": self.description,
            "website": self.website,
            "twitter": self.twitter,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Verdict — LLM scoring result for one candidate
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    """
    The LLM's verdict on a candidate token.

    All fields are validated by llm_scorer before this object is created.
    A Verdict is never created from raw LLM output without passing through
    explicit schema/type/range validation (defense-first rule 1).
    """
    candidate: Candidate
    verdict: str                    # "pass" | "fail"  (validated)
    confidence: float               # 0.0 – 1.0        (validated)
    risk_flags: list[str]           # free-form flag strings from the LLM
    thesis: str
    entry_condition: str
    invalidation_condition: str
    # Raw LLM response preserved for auditability
    raw_llm_response: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "risk_flags": self.risk_flags,
            "thesis": self.thesis,
            "entry_condition": self.entry_condition,
            "invalidation_condition": self.invalidation_condition,
        }


# ---------------------------------------------------------------------------
# Trade — a simulated (paper) position
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    """
    A simulated paper-trading position. Never represents real fund movement.

    created via paper_trading_engine.open_position() only.
    P&L fields are populated by paper_trading_engine.close_position() only.
    """
    trade_id: str = field(default_factory=_new_trade_id)
    symbol: str = ""
    mint_address: str = ""
    opened_at: str = field(default_factory=_now_iso)
    entry_price_usd: float = 0.0
    position_size_usd: float = 0.0     # USD deployed (post-slippage, post-fee)
    quantity: float = 0.0              # tokens acquired
    candidate_snapshot: dict = field(default_factory=dict)
    verdict_snapshot: dict = field(default_factory=dict)
    invalidation_condition: str = ""
    # Populated on close
    closed_at: Optional[str] = None
    exit_price_usd: Optional[float] = None
    exit_reason: Optional[str] = None         # "take_profit" | "stop_loss" | "timeout" | "manual"
    realized_pnl_usd: Optional[float] = None
    realized_pnl_pct: Optional[float] = None
    is_open: bool = True
    # FR-26: per-trade reflection text from LLM (populated async after close)
    reflection_text: Optional[str] = None


# ---------------------------------------------------------------------------
# FeedEvent — every decision the tick loop makes (pass OR fail)
# ---------------------------------------------------------------------------

@dataclass
class FeedEvent:
    """
    A single decision event logged by the tick loop.

    Both pass and fail decisions produce FeedEvents (FR-3). This is what
    the live feed panel displays — not just successful entries.
    """
    ts: str = field(default_factory=_now_iso)
    symbol: str = ""
    mint_address: str = ""
    candidate_snapshot: dict = field(default_factory=dict)
    verdict: str = "fail"              # "pass" | "fail"
    confidence: Optional[float] = None
    risk_flags: list[str] = field(default_factory=list)
    entry_condition: Optional[str] = None
    invalidation_condition: Optional[str] = None
    thesis: Optional[str] = None
    led_to_trade_id: Optional[str] = None
    # Set after DB insert
    id: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "symbol": self.symbol,
            "mint_address": self.mint_address,
            "candidate_snapshot": self.candidate_snapshot,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "risk_flags": self.risk_flags,
            "entry_condition": self.entry_condition,
            "invalidation_condition": self.invalidation_condition,
            "thesis": self.thesis,
            "led_to_trade_id": self.led_to_trade_id,
        }


# ---------------------------------------------------------------------------
# DailyStats — aggregate stats for one calendar day
# ---------------------------------------------------------------------------

@dataclass
class DailyStats:
    date: str                           # YYYY-MM-DD
    open_positions: int = 0
    closed_trades: int = 0
    recommendations: dict = field(default_factory=dict)
