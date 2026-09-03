"""
rule_engine/exits.py — the the reference bot-model exit engine (§5.2 rebuild).

Ported from the reference bot' documented exit set (PROCESS.md §5), adapted to the
paper engine's money math. Evaluation order is fixed and RISK-OFF BEATS
PROFIT: a stop, trail, liquidity break, invalidation or stale timer closes
the position FULLY; only the take-profit ladder trims.

  exit_stop_loss            unrealized loss at/below the hard stop
  exit_trail_give_back      up ACTIVATION or better, then gave back
                            GIVE_BACK percentage points off the high-water
  exit_liquidity_break      pool below the floor -> size can't leave cleanly
  exit_thesis_invalidated   6h dump AND sellers leading by the multiple
  exit_stale_thesis         held long, going nowhere, tape drying up
  exit_take_profit          ladder trims: +100%/+300%/+900% -> 33%/33%/50%

Pure functions only: no I/O, no clock reads (time is injected), so every
decision is replayable and unit-testable. The caller (run_live_cycle._manage,
the §52 single-book exit scanner) supplies market data and applies the
SELL RISK GATE before routing any order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import config
from models import Trade


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExitInput:
    """Everything the engine may look at. Optional market fields the caller
    could not fetch are None and their rules report not-evaluable."""
    trade: Trade
    price_usd: float
    high_water_usd: Optional[float] = None
    tranches_taken: int = 0
    liquidity_usd: Optional[float] = None
    chg6h_pct: Optional[float] = None
    buys6h: Optional[int] = None
    sells6h: Optional[int] = None
    vol6h_usd: Optional[float] = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class ExitDecision:
    rule_id: str        # "" when holding
    action: str         # "hold" | "close_full" | "trim"
    fraction: float     # 1.0 for close_full; trim fraction otherwise
    detail: str


_HOLD = ExitDecision("", "hold", 0.0, "no exit condition met")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check_exit_conditions(trade: Trade, current_price: float) -> Optional[str]:
    """
    §52: single-price exit probe (moved from the retired paper engine —
    its only consumers were tests). Delegates to the shared engine with
    price-only inputs — rules needing richer market data (liquidity break,
    invalidation, stale volume) evaluate only in the scanners where that
    data exists. Returns the fired rule_id or None. The SOLE decision-maker
    for exits is the rule engine — no LLM involved.
    """
    decision = evaluate_exits(ExitInput(trade=trade, price_usd=current_price))
    return decision.rule_id or None


def _net_gain_frac(trade: Trade, price_usd: float) -> float:
    """Unrealized P&L as a fraction of cost basis, net of exit costs."""
    # §52: sizing.py is the neutral home of the money math — no circularity
    # with the rule engine anymore (the paper module is gone).
    from sizing import compute_unrealized_pnl
    _, pnl_pct = compute_unrealized_pnl(trade, price_usd)
    return pnl_pct / 100.0


def _gross_gain_frac(entry_price: float, price: float) -> float:
    """Raw price gain vs entry (no cost model) — the trail's basis, matching
    the high-water mark which is also a raw price."""
    if entry_price <= 0:
        return 0.0
    return (price / entry_price) - 1.0


def _held_days(trade: Trade, now: datetime) -> float:
    opened = datetime.fromisoformat(trade.opened_at)
    return (now - opened).total_seconds() / 86400.0


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

def evaluate_exits(inp: ExitInput) -> ExitDecision:
    """Fixed order; first fired rule wins. Pure."""
    trade = inp.trade
    price = inp.price_usd
    if price <= 0:
        return _HOLD

    net = _net_gain_frac(trade, price)

    # 1. Hard stop ----------------------------------------------------------
    if net <= -config.STOP_LOSS_PCT:
        return ExitDecision(
            "exit_stop_loss", "close_full", 1.0,
            f"net {net:+.1%} at/below the hard stop "
            f"(-{config.STOP_LOSS_PCT:.0%})",
        )

    # 2. Trailing give-back ---------------------------------------------------
    entry = trade.entry_price_usd
    hwm = inp.high_water_usd if inp.high_water_usd is not None else entry
    hwm_gain = _gross_gain_frac(entry, hwm)
    cur_gain = _gross_gain_frac(entry, price)
    give_back_pp = (hwm_gain - cur_gain) * 100.0
    if (
        hwm_gain >= config.EXIT_TRAIL_ACTIVATION_PCT
        and give_back_pp >= config.EXIT_TRAIL_GIVE_BACK_PP
    ):
        return ExitDecision(
            "exit_trail_give_back", "close_full", 1.0,
            f"peaked {hwm_gain:+.1%}, now {cur_gain:+.1%} — gave back "
            f"{give_back_pp:.0f} points off the high",
        )

    # 3. Liquidity break ------------------------------------------------------
    if (
        inp.liquidity_usd is not None
        and inp.liquidity_usd < config.EXIT_LIQUIDITY_FLOOR_USD
    ):
        return ExitDecision(
            "exit_liquidity_break", "close_full", 1.0,
            f"pool ${inp.liquidity_usd:,.0f} below floor "
            f"${config.EXIT_LIQUIDITY_FLOOR_USD:,.0f} — size can't leave cleanly",
        )

    # 4. Thesis invalidated ---------------------------------------------------
    if (
        inp.chg6h_pct is not None
        and inp.buys6h is not None
        and inp.sells6h is not None
        and inp.chg6h_pct <= config.EXIT_INVALIDATION_CHG6H_PCT
        and inp.sells6h > config.EXIT_INVALIDATION_SELL_MULT * inp.buys6h
    ):
        return ExitDecision(
            "exit_thesis_invalidated", "close_full", 1.0,
            f"6h {inp.chg6h_pct:+.1f}% with sellers leading "
            f"({inp.sells6h} vs {inp.buys6h} buys)",
        )

    # 5. Stale thesis -----------------------------------------------------------
    held_days = _held_days(trade, inp.now)
    if (
        held_days >= config.EXIT_STALE_DAYS
        and abs(net) <= config.EXIT_STALE_BAND_PCT
        and inp.vol6h_usd is not None
        and inp.vol6h_usd < config.EXIT_STALE_VOL6H_USD
    ):
        return ExitDecision(
            "exit_stale_thesis", "close_full", 1.0,
            f"held {held_days:.0f}d, net {net:+.1%}, 6h vol "
            f"${inp.vol6h_usd:,.0f} — thesis went stale",
        )

    # 6. Take-profit ladder (the only partial action) ---------------------------
    for idx, (threshold, trim_fraction) in enumerate(config.EXIT_TP_LADDER):
        if inp.tranches_taken <= idx and net >= threshold:
            return ExitDecision(
                "exit_take_profit", "trim", trim_fraction,
                f"net {net:+.1%} >= tranche {idx + 1} (+{threshold:.0%}) — "
                f"trimming {trim_fraction:.0%} of remaining",
            )

    return _HOLD


# ---------------------------------------------------------------------------
# Sell risk gate — narrow on purpose (a refused sell leaves risk on)
# ---------------------------------------------------------------------------

RISK_OFF_RULES = frozenset({
    "exit_stop_loss",
    "exit_trail_give_back",
    "exit_liquidity_break",
    "exit_thesis_invalidated",
    "exit_stale_thesis",
})


def sell_risk_gate(
    decision: ExitDecision,
    trim_value_usd: float,
    last_exit_for_mint: Optional[datetime],
    closes_last_24h: int,
    now: datetime,
    min_clip_usd: Optional[float] = None,
) -> tuple[ExitDecision, str]:
    """
    Applies the reference's narrow sell gate: minimum $25 clip, 30-minute per-mint
    cooldown, tranche-taken-once (enforced upstream by the counter), and a
    ceiling of 8 exits per rolling 24h.

    Documented deviation from a literal port: RISK-OFF rules bypass the
    cooldown and daily ceiling. Blocking a stop during a cascade is exactly
    how a book bleeds out; profit trims — the only optional sells — carry
    the gate in full. Returns (possibly-downgraded decision, gate_note).

    min_clip_usd (§50): the LIVE book overrides the paper $25 clip with the
    §45 equity-proportional live ticket floor (33% of a $0.50 ticket is
    $0.17 — a $25 clip would structurally refuse every trim on this book).
    None keeps config.SELL_MIN_CLIP_USD so every paper expectation stays
    bit-identical.
    """
    min_clip = (config.SELL_MIN_CLIP_USD if min_clip_usd is None
                else float(min_clip_usd))
    if decision.action == "hold":
        return decision, ""

    if decision.rule_id in RISK_OFF_RULES:
        return decision, "risk-off exit bypasses the sell gate"

    # Gated path (take-profit trims):
    if (
        last_exit_for_mint is not None
        and (now - last_exit_for_mint).total_seconds()
        < config.SELL_COOLDOWN_MINUTES * 60.0
    ):
        return (
            ExitDecision(decision.rule_id, "hold", 0.0,
                         "sell cooldown active for this mint"),
            f"cooldown: last exit {last_exit_for_mint.isoformat()}",
        )
    if closes_last_24h >= config.MAX_EXITS_PER_24H:
        return (
            ExitDecision(decision.rule_id, "hold", 0.0,
                         "24h exit ceiling reached"),
            f"daily cap: {closes_last_24h} exits in rolling 24h",
        )
    if trim_value_usd < min_clip:
        return (
            ExitDecision(decision.rule_id, "hold", 0.0,
                         f"trim ${trim_value_usd:.2f} below "
                         f"${min_clip:.2f} minimum clip"),
            "min clip",
        )
    return decision, ""