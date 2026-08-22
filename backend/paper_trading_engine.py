"""
paper_trading_engine.py — Simulated portfolio management.

PAPER TRADING ONLY. This module manages simulated positions in SQLite. It
never constructs, signs, or broadcasts any real transaction; there is no
wallet interaction of any kind in this file, under any framing.

Every state-changing function (open_position, close_position,
scale_into_position):
  1. asserts config.PAPER_TRADING_ONLY at runtime (E7, belt-and-suspenders),
  2. performs the conditional state write FIRST (§5.1) and reads the
     affected row count,
  3. treats rowcount == 0 as "already happened" — logs and touches NOTHING,
  4. only after rowcount == 1 is confirmed, adjusts cash (guarded).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

import config
from api import db
from models import Candidate, GateDecision, PortfolioState, Trade
from rule_engine.regime import MarketRegime
from rule_engine.rules import ACTIVE_RULES
from rule_engine.gate import evaluate_gate

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure money-math functions (tested in tests/test_money_math.py, E5)
# ---------------------------------------------------------------------------

def compute_unrealized_pnl(trade: Trade, current_price: float) -> tuple[float, float]:
    """
    Unrealized P&L at current_price, net of simulated exit costs
    (slippage + fee) so it reflects what an exit would actually receive.
    Returns (pnl_usd, pnl_pct). Raises ValueError on invalid input —
    never silently returns zero.
    """
    if current_price <= 0:
        raise ValueError(f"current_price must be > 0, got {current_price!r}")
    if trade.quantity <= 0:
        raise ValueError(f"trade.quantity must be > 0, got {trade.quantity!r}")
    if trade.position_size_usd <= 0:
        raise ValueError(f"position_size_usd must be > 0, got {trade.position_size_usd!r}")
    gross = trade.quantity * current_price
    net = gross * (1.0 - config.SLIPPAGE_PCT) * (1.0 - config.FEE_PCT)
    pnl_usd = net - trade.position_size_usd
    pnl_pct = (pnl_usd / trade.position_size_usd) * 100.0
    return pnl_usd, pnl_pct


def compute_realized_pnl(trade: Trade, exit_price: float) -> tuple[float, float]:
    """
    Realized P&L on closing at exit_price:
      gross_proceeds = quantity * exit_price
      net_proceeds   = gross * (1 - SLIPPAGE) * (1 - FEE)
      realized_pnl   = net_proceeds - position_size_usd
    Raises ValueError on invalid input.
    """
    if exit_price <= 0:
        raise ValueError(f"exit_price must be > 0, got {exit_price!r}")
    if trade.quantity <= 0:
        raise ValueError(f"trade.quantity must be > 0, got {trade.quantity!r}")
    if trade.position_size_usd <= 0:
        raise ValueError(f"position_size_usd must be > 0, got {trade.position_size_usd!r}")
    gross = trade.quantity * exit_price
    net = gross * (1.0 - config.SLIPPAGE_PCT) * (1.0 - config.FEE_PCT)
    pnl_usd = net - trade.position_size_usd
    pnl_pct = (pnl_usd / trade.position_size_usd) * 100.0
    return pnl_usd, pnl_pct


def compute_position_size(price_usd: float) -> tuple[float, float]:
    """
    Entry sizing: fixed intended USD size, adjusted for simulated entry
    costs. Returns (position_size_usd, quantity) where
      cost_basis = size * (1 + FEE) * (1 + SLIPPAGE)  [what the buyer pays]
      quantity   = size / price
    Raises ValueError on non-positive price.
    """
    if price_usd <= 0:
        raise ValueError(f"price_usd must be > 0, got {price_usd!r}")
    size = config.INTENDED_POSITION_SIZE_USD
    quantity = size / price_usd
    return size, quantity


def compute_entry_cost(size_usd: float) -> float:
    """Total USD debited on entry, including simulated entry costs."""
    return size_usd * (1.0 + config.FEE_PCT) * (1.0 + config.SLIPPAGE_PCT)


# ---------------------------------------------------------------------------
# Result objects — thread the applied/not-applied outcome to callers
# ---------------------------------------------------------------------------

@dataclass
class OpenResult:
    applied: bool
    trade: Optional[Trade]
    reason: str   # "opened" | "duplicate_open_position" | "cash_refused"


@dataclass
class CloseResult:
    applied: bool
    trade: Optional[Trade]
    reason: str   # "closed" | "already_closed" | "cash_refused"


@dataclass
class ScaleResult:
    applied: bool
    trade: Optional[Trade]
    reason: str   # "scaled" | "position_closed" | "exposure_cap" | "cash_refused"


# ---------------------------------------------------------------------------
# State-changing functions — atomicity pattern per §5.1
# ---------------------------------------------------------------------------

async def open_position(
    conn: aiosqlite.Connection,
    candidate: Candidate,
    gate: GateDecision,
) -> OpenResult:
    """Open a new simulated position. Idempotent per mint (§5.1)."""
    config.assert_paper_trading_only()

    price = candidate.price_usd
    if price <= 0:
        return OpenResult(False, None, "invalid_price")
    size, quantity = compute_position_size(price)
    cost_basis = compute_entry_cost(size)

    trade = Trade(
        symbol=candidate.symbol,
        mint_address=candidate.mint_address,
        entry_price_usd=price,
        position_size_usd=size,
        quantity=quantity,
        candidate_snapshot=candidate.to_dict(),
        thesis="",
    )

    # 1. Conditional state write FIRST — rowcount is the sole authority.
    rows = await db.try_insert_open_trade(conn, trade)
    if rows == 0:
        log.warning(
            "open_position: %s already has an open position — no-op, cash NOT debited",
            candidate.symbol,
        )
        existing = await db.get_open_trade_for_mint(conn, candidate.mint_address)
        return OpenResult(False, existing, "duplicate_open_position")

    # 2. State write confirmed (rows == 1). Only now touch cash.
    moved = await db.adjust_cash(conn, -cost_basis)
    if moved == 0:
        # Defensive: cash guard refused (should be impossible — cash_available
        # rule gates this upstream). Roll the trade row back rather than leave
        # an unfunded position.
        log.error("open_position: cash adjustment refused for %s — rolling back", candidate.symbol)
        await conn.execute(
            "DELETE FROM trades WHERE trade_id = ? AND is_open = 1", (trade.trade_id,)
        )
        await conn.commit()
        return OpenResult(False, None, "cash_refused")

    log.info(
        "OPENED %s: size $%.2f, qty %.4f @ $%.8f | cost basis $%.2f",
        trade.symbol, size, quantity, price, cost_basis,
    )
    return OpenResult(True, trade, "opened")


async def close_position(
    conn: aiosqlite.Connection,
    trade: Trade,
    exit_price: float,
    exit_reason: str,
) -> CloseResult:
    """Close a simulated position. Double-close is a safe no-op (§5.1)."""
    config.assert_paper_trading_only()

    if exit_price <= 0:
        return CloseResult(False, trade, "invalid_price")

    realized_usd, realized_pct = compute_realized_pnl(trade, exit_price)
    closed_at = datetime.now(timezone.utc).isoformat()

    # 1. Conditional close write FIRST.
    rows = await db.close_trade_row(
        conn, trade.trade_id, closed_at, exit_price, exit_reason,
        realized_usd, realized_pct,
    )
    if rows == 0:
        log.warning(
            "close_position: %s (%s) already closed — cash NOT credited twice",
            trade.symbol, trade.trade_id,
        )
        persisted = await db.get_trade_by_id(conn, trade.trade_id)
        return CloseResult(False, persisted, "already_closed")

    # 2. Confirmed close. Credit cash now: cost basis + realized P&L.
    proceeds = trade.position_size_usd + realized_usd
    moved = await db.adjust_cash(conn, proceeds)
    if moved == 0:
        # Cannot happen with positive proceeds on a healthy portfolio, but
        # never fabricate state: surface loudly.
        log.critical(
            "close_position: cash credit REFUSED for %s after confirmed close — "
            "portfolio state inconsistent, investigate immediately",
            trade.trade_id,
        )
        return CloseResult(False, await db.get_trade_by_id(conn, trade.trade_id), "cash_refused")

    log.info(
        "CLOSED %s [%s]: pnl $%+.4f (%+.1f%%) | proceeds $%.2f",
        trade.symbol, exit_reason, realized_usd, realized_pct, proceeds,
    )
    trade.closed_at = closed_at
    trade.exit_price_usd = exit_price
    trade.exit_reason = exit_reason
    trade.realized_pnl_usd = realized_usd
    trade.realized_pnl_pct = realized_pct
    trade.is_open = False
    return CloseResult(True, trade, "closed")


async def scale_into_position(
    conn: aiosqlite.Connection,
    existing: Trade,
    candidate: Candidate,
) -> ScaleResult:
    """
    Add to an existing open position (§5.1). The exposure cap is enforced
    atomically inside the UPDATE's WHERE clause. A repeat that would breach
    the cap, or that targets an already-closed position, affects zero rows.
    """
    config.assert_paper_trading_only()

    price = candidate.price_usd
    if price <= 0:
        return ScaleResult(False, existing, "invalid_price")
    size, quantity = compute_position_size(price)
    cost_basis = compute_entry_cost(size)

    # 1. Conditional scale-in write FIRST (cap enforced atomically).
    rows = await db.add_to_position_row(
        conn, existing.trade_id, size, quantity, config.MAX_EXPOSURE_PER_MINT_USD
    )
    if rows == 0:
        current = await db.get_trade_by_id(conn, existing.trade_id)
        if current is None or not current.is_open:
            log.warning("scale_into_position: %s no longer open — no-op", existing.trade_id)
            return ScaleResult(False, current, "position_closed")
        log.info(
            "scale_into_position: %s at exposure cap ($%.2f) — no-op, cash NOT debited",
            candidate.symbol, current.position_size_usd,
        )
        return ScaleResult(False, current, "exposure_cap")

    # 2. Confirmed. Debit cash.
    moved = await db.adjust_cash(conn, -cost_basis)
    if moved == 0:
        log.error("scale_into_position: cash refused for %s — investigate", existing.trade_id)
        return ScaleResult(False, await db.get_trade_by_id(conn, existing.trade_id), "cash_refused")

    updated = await db.get_trade_by_id(conn, existing.trade_id)
    log.info(
        "SCALED INTO %s: +$%.2f -> total $%.2f exposure",
        candidate.symbol, size, updated.position_size_usd if updated else -1,
    )
    return ScaleResult(True, updated, "scaled")


# ---------------------------------------------------------------------------
# Exit conditions (§5.2) and unified entry point (§5 / E4)
# ---------------------------------------------------------------------------

def check_exit_conditions(trade: Trade, current_price: float) -> Optional[str]:
    """
    Fixed numeric exit conditions, checked in order. Returns an exit reason
    or None. These are the SOLE decision-makers for exits — no LLM involved.
    Uses compute_unrealized_pnl() so slippage/fees are included.
    """
    pnl_usd, pnl_frac_of_cost = _unrealized_fraction(trade, current_price)
    if pnl_frac_of_cost >= config.TAKE_PROFIT_PCT:
        return "take_profit"
    if pnl_frac_of_cost <= -config.STOP_LOSS_PCT:
        return "stop_loss"
    opened = datetime.fromisoformat(trade.opened_at)
    held_hours = (
        datetime.now(timezone.utc) - opened
    ).total_seconds() / 3600.0
    if held_hours >= config.MAX_HOLD_HOURS:
        return "timeout"
    return None


def _unrealized_fraction(trade: Trade, current_price: float) -> tuple[float, float]:
    """(pnl_usd, pnl as a FRACTION of cost basis). Raises on invalid input."""
    pnl_usd, pnl_pct = compute_unrealized_pnl(trade, current_price)
    return pnl_usd, pnl_pct / 100.0


async def load_portfolio_state(conn: aiosqlite.Connection) -> PortfolioState:
    cash = await db.get_cash_balance(conn)
    positions = await db.get_open_trades(conn)
    return PortfolioState(cash_usd=cash, open_positions=positions)


async def decide_and_act(
    candidate: Candidate,
    portfolio: PortfolioState,
    regime: MarketRegime,
    conn: aiosqlite.Connection,
) -> GateDecision:
    """
    Unified entry point (§5): evaluate the gate; on pass, route to
    open_position (no existing position) or scale_into_position (existing).
    The gate decision is ALWAYS returned so the caller logs it either way.
    """
    gate = evaluate_gate(candidate, portfolio, regime, ACTIVE_RULES)

    if gate.all_passed:
        existing = portfolio.get_open_trade_for_mint(candidate.mint_address)
        if existing is None:
            await open_position(conn, candidate, gate)
        else:
            await scale_into_position(conn, existing, candidate)

    return gate


