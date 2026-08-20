"""
paper_trading_engine.py — Simulated portfolio management.

PAPER TRADING ONLY. This module manages simulated positions against a SQLite
database. It never constructs, signs, or broadcasts any real transaction.
There is no wallet interaction of any kind in this file.

Key functions:
  open_position()  — Open a new simulated position.
  close_position() — Close an existing simulated position.
  compute_unrealized_pnl() — Pure function. Tested independently.
  compute_realized_pnl()   — Pure function. Tested independently.
  check_exit_conditions()  — Determine if an open position should close.

Defense-first rules applied throughout:
  - Idempotency guard in open_position() prevents double-opening (rule 7).
  - Cash balance validated before any position is opened (rule 1).
  - P&L functions raise on invalid inputs rather than returning 0 (rule 2).
  - All monetary values are float, not Decimal — sufficient for paper
    trading simulation where exact rounding to the cent isn't required,
    but we document this assumption explicitly.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

import config
from api import db
from models import Candidate, Trade, Verdict

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure money-math functions (tested in tests/test_money_math.py)
# ---------------------------------------------------------------------------

def compute_unrealized_pnl(trade: Trade, current_price: float) -> tuple[float, float]:
    """
    Compute unrealized P&L for an open position at the given current price.

    Accounts for hypothetical exit costs (slippage + fee) so the displayed
    unrealized P&L is a realistic estimate of what we'd actually receive on
    exit, not a naive mark-to-market.

    Args:
        trade: An open Trade object with entry_price_usd, quantity, and
               position_size_usd populated.
        current_price: The current market price in USD.

    Returns:
        (pnl_usd, pnl_pct)
        pnl_usd — unrealized P&L in USD (negative = loss)
        pnl_pct — unrealized P&L as a percentage of cost basis

    Raises:
        ValueError: if current_price <= 0 or trade is in an invalid state.
    """
    if current_price <= 0:
        raise ValueError(f"current_price must be > 0, got {current_price!r}")
    if trade.quantity <= 0:
        raise ValueError(f"trade.quantity must be > 0, got {trade.quantity!r}")
    if trade.position_size_usd <= 0:
        raise ValueError(f"trade.position_size_usd must be > 0, got {trade.position_size_usd!r}")

    gross_current_value = trade.quantity * current_price
    # Apply hypothetical exit slippage then fee
    net_exit_value = (
        gross_current_value
        * (1.0 - config.SLIPPAGE_PCT)
        * (1.0 - config.FEE_PCT)
    )
    pnl_usd = net_exit_value - trade.position_size_usd
    pnl_pct = (pnl_usd / trade.position_size_usd) * 100.0
    return pnl_usd, pnl_pct


def compute_realized_pnl(trade: Trade, exit_price: float) -> tuple[float, float]:
    """
    Compute realized P&L when closing a position at exit_price.

    Formula:
      gross_proceeds = quantity * exit_price
      net_proceeds   = gross_proceeds * (1 - SLIPPAGE_PCT) * (1 - FEE_PCT)
      realized_pnl   = net_proceeds - position_size_usd

    The position_size_usd already incorporates entry-side slippage/fees
    (deducted at open_position time), so this correctly accounts for
    round-trip costs.

    Args:
        trade: The Trade being closed. Must have quantity and position_size_usd.
        exit_price: The exit price in USD.

    Returns:
        (realized_pnl_usd, realized_pnl_pct)

    Raises:
        ValueError: if exit_price <= 0 or trade fields are invalid.
    """
    if exit_price <= 0:
        raise ValueError(f"exit_price must be > 0, got {exit_price!r}")
    if trade.quantity <= 0:
        raise ValueError(f"trade.quantity must be > 0, got {trade.quantity!r}")
    if trade.position_size_usd <= 0:
        raise ValueError(f"trade.position_size_usd must be > 0, got {trade.position_size_usd!r}")

    gross_proceeds = trade.quantity * exit_price
    net_proceeds = (
        gross_proceeds
        * (1.0 - config.SLIPPAGE_PCT)
        * (1.0 - config.FEE_PCT)
    )
    realized_pnl_usd = net_proceeds - trade.position_size_usd
    realized_pnl_pct = (realized_pnl_usd / trade.position_size_usd) * 100.0
    return realized_pnl_usd, realized_pnl_pct


def _compute_position_size(cash_balance: float) -> float:
    """
    Compute the USD amount to deploy in a new position.

    Uses config.POSITION_SIZE_PCT of current cash, capped so the position
    cannot exceed the full cash balance (no leverage, no negative cash).

    Args:
        cash_balance: Current paper cash balance in USD.

    Returns:
        Position size in USD to allocate.

    Raises:
        ValueError: if cash_balance <= 0.
    """
    if cash_balance <= 0:
        raise ValueError(f"cash_balance must be > 0, got {cash_balance!r}")
    return min(cash_balance * config.POSITION_SIZE_PCT, cash_balance)


def _compute_quantity(gross_position_usd: float, entry_price: float) -> tuple[float, float]:
    """
    Compute the number of tokens purchased and the net cost basis,
    after entry-side slippage and fee are applied.

    Simulated execution:
      gross_usd      = the amount we intend to spend
      net_usd_in     = gross_usd * (1 - SLIPPAGE_PCT) * (1 - FEE_PCT)
                       (this is what actually buys tokens after slippage/fee)
      quantity       = net_usd_in / entry_price
      cost_basis     = gross_usd  (cash deducted from portfolio)

    Note: position_size_usd stored on the Trade is the gross amount (cash
    actually removed from the portfolio), not the net tokens' value.
    This matches how real DEX execution works: you commit X USD and receive
    slightly less than X USD worth of tokens.

    Returns:
        (quantity, cost_basis_usd)
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be > 0, got {entry_price!r}")
    if gross_position_usd <= 0:
        raise ValueError(f"gross_position_usd must be > 0, got {gross_position_usd!r}")

    net_usd_in = gross_position_usd * (1.0 - config.SLIPPAGE_PCT) * (1.0 - config.FEE_PCT)
    quantity = net_usd_in / entry_price
    return quantity, gross_position_usd  # cost_basis = gross spent


def check_exit_conditions(
    trade: Trade,
    current_price: float,
) -> Optional[tuple[str, float]]:
    """
    Determine whether an open position should be closed.

    Checks take-profit, stop-loss, and timeout conditions in that order.
    Returns (exit_reason, current_price) if any condition triggers,
    None if the position should remain open.

    Args:
        trade: An open Trade object.
        current_price: Current market price in USD.

    Returns:
        (exit_reason, exit_price) or None.
    """
    try:
        _pnl_usd, pnl_pct = compute_unrealized_pnl(trade, current_price)
    except ValueError as exc:
        log.error("check_exit_conditions: invalid state for trade %s: %s", trade.trade_id, exc)
        return None

    # Take-profit
    if pnl_pct >= config.TAKE_PROFIT_PCT * 100:
        log.info(
            "EXIT take_profit %s: pnl=%.1f%% >= %.1f%%",
            trade.symbol, pnl_pct, config.TAKE_PROFIT_PCT * 100,
        )
        return "take_profit", current_price

    # Stop-loss
    if pnl_pct <= -(config.STOP_LOSS_PCT * 100):
        log.info(
            "EXIT stop_loss %s: pnl=%.1f%% <= -%.1f%%",
            trade.symbol, pnl_pct, config.STOP_LOSS_PCT * 100,
        )
        return "stop_loss", current_price

    # Timeout
    try:
        opened_dt = datetime.fromisoformat(trade.opened_at)
        now_dt = datetime.now(timezone.utc)
        hold_hours = (now_dt - opened_dt).total_seconds() / 3600
        if hold_hours >= config.MAX_HOLD_HOURS:
            log.info(
                "EXIT timeout %s: held %.1fh >= %dh max",
                trade.symbol, hold_hours, config.MAX_HOLD_HOURS,
            )
            return "timeout", current_price
    except (ValueError, TypeError) as exc:
        log.error("check_exit_conditions: cannot parse opened_at for trade %s: %s", trade.trade_id, exc)

    return None  # position stays open


# ---------------------------------------------------------------------------
# Position lifecycle
# ---------------------------------------------------------------------------

async def open_position(
    conn: aiosqlite.Connection,
    candidate: Candidate,
    verdict: Verdict,
) -> Optional[Trade]:
    """
    Open a new simulated paper-trading position.

    Returns the created Trade, or None if the position cannot be opened
    (e.g. insufficient cash, already open, too many positions).

    Defense-first:
      - Idempotency guard: checks for an existing open position on this
        mint address before opening (rule 7).
      - Cash balance validated before deduction (rule 1).
      - PAPER_TRADING_ONLY asserted at runtime (belt-and-suspenders).
    """
    # Belt-and-suspenders safety check — this should always be True
    if not config.PAPER_TRADING_ONLY:
        raise RuntimeError(
            "PAPER_TRADING_ONLY is False — real trading is not implemented "
            "in this build and must not be enabled."
        )

    # Idempotency guard: don't double-open a position on the same token
    if await db.is_position_open(conn, candidate.mint_address):
        log.info(
            "open_position: skipping %s — position already open for mint %s",
            candidate.symbol,
            candidate.mint_address,
        )
        return None

    # Check open position count
    open_trades = await db.get_open_trades(conn)
    if len(open_trades) >= config.MAX_OPEN_POSITIONS:
        log.info(
            "open_position: skipping %s — at max open positions (%d)",
            candidate.symbol,
            config.MAX_OPEN_POSITIONS,
        )
        return None

    # Validate cash balance
    cash = await db.get_cash_balance(conn)
    if cash < 10.0:  # hard floor: $10 minimum to open any position
        log.warning("open_position: insufficient cash (%.4f) — skipping %s", cash, candidate.symbol)
        return None

    # Compute position sizing
    gross_position_usd = _compute_position_size(cash)
    entry_price = candidate.price_usd

    if entry_price <= 0:
        log.error(
            "open_position: candidate %s has invalid price %r — skipping",
            candidate.symbol, entry_price,
        )
        return None

    quantity, cost_basis = _compute_quantity(gross_position_usd, entry_price)

    # Create trade record
    trade = Trade(
        symbol=candidate.symbol,
        mint_address=candidate.mint_address,
        entry_price_usd=entry_price,
        position_size_usd=cost_basis,
        quantity=quantity,
        candidate_snapshot=candidate.to_dict(),
        verdict_snapshot=verdict.to_dict(),
        invalidation_condition=verdict.invalidation_condition,
        is_open=True,
    )

    # Deduct cash and persist — both in same logical operation
    new_cash = cash - cost_basis
    await db.update_cash_balance(conn, new_cash)
    await db.insert_trade(conn, trade)

    log.info(
        "OPENED %s: size=$%.2f, qty=%.4f @ $%.8f | cash remaining: $%.2f",
        trade.symbol,
        cost_basis,
        quantity,
        entry_price,
        new_cash,
    )
    return trade


async def close_position(
    conn: aiosqlite.Connection,
    trade: Trade,
    exit_price: float,
    exit_reason: str,
) -> Trade:
    """
    Close a simulated position and update the portfolio cash balance.

    The trade record is updated atomically. Cash is credited before the
    trade is marked closed — if anything fails between these two writes,
    the trade remains open in the DB (recoverable state, defense-first rule 7).

    Returns the updated Trade object with P&L fields populated.
    """
    if not trade.is_open:
        raise ValueError(f"Trade {trade.trade_id} is already closed.")

    if exit_price <= 0:
        raise ValueError(f"exit_price must be > 0, got {exit_price!r}")

    realized_pnl_usd, realized_pnl_pct = compute_realized_pnl(trade, exit_price)

    # Compute proceeds returned to cash
    proceeds = trade.position_size_usd + realized_pnl_usd

    # Credit cash first
    current_cash = await db.get_cash_balance(conn)
    new_cash = current_cash + proceeds
    await db.update_cash_balance(conn, new_cash)

    # Then mark trade as closed
    closed_at = datetime.now(timezone.utc).isoformat()
    await db.close_trade_in_db(
        conn,
        trade_id=trade.trade_id,
        closed_at=closed_at,
        exit_price_usd=exit_price,
        exit_reason=exit_reason,
        realized_pnl_usd=realized_pnl_usd,
        realized_pnl_pct=realized_pnl_pct,
    )

    log.info(
        "CLOSED %s [%s]: pnl=$%+.4f (%+.1f%%) | cash: $%.2f -> $%.2f",
        trade.symbol,
        exit_reason,
        realized_pnl_usd,
        realized_pnl_pct,
        current_cash,
        new_cash,
    )

    # Return updated trade object (callers use this for feed event + reflection)
    trade.closed_at = closed_at
    trade.exit_price_usd = exit_price
    trade.exit_reason = exit_reason
    trade.realized_pnl_usd = realized_pnl_usd
    trade.realized_pnl_pct = realized_pnl_pct
    trade.is_open = False
    return trade
