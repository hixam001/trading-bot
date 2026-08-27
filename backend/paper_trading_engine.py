"""
paper_trading_engine.py — Simulated portfolio management.

PAPER TRADING ONLY. This module manages simulated positions in SQLite. It
never constructs, signs, or broadcasts any real transaction; there is no
wallet interaction of any kind in this file, under any framing.

Every state-changing function (open_position, close_position,
trim_position):
  1. asserts config.PAPER_TRADING_ONLY at runtime (E7, belt-and-suspenders),
  2. performs the conditional state write FIRST (§5.1) and reads the
     affected row count,
  3. treats rowcount == 0 as "already happened" — logs and touches NOTHING,
  4. only after rowcount == 1 is confirmed, adjusts cash (guarded).

The old scale-in path was REMOVED in the the reference bot-model rebuild: the
`already_held` gate rule forbids any size on a held name ("one position per
name"), so pyramiding into a ticket is structurally impossible.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


def compute_position_size(price_usd: float,
                          size_usd: Optional[float] = None) -> tuple[float, float]:
    """
    Entry sizing: fixed intended USD size unless overridden by a
    conviction-sized ticket, adjusted for simulated entry costs.
    Returns (position_size_usd, quantity) where
      cost_basis = size * (1 + FEE) * (1 + SLIPPAGE)  [what the buyer pays]
      quantity   = size / price
    Raises ValueError on non-positive price.
    """
    if price_usd <= 0:
        raise ValueError(f"price_usd must be > 0, got {price_usd!r}")
    size = float(size_usd) if size_usd is not None \
        else config.INTENDED_POSITION_SIZE_USD
    if size <= 0:
        raise ValueError(f"size must be > 0, got {size!r}")
    quantity = size / price_usd
    return size, quantity


# ---------------------------------------------------------------------------
# REF-R8 - drawdown-adaptive risk budget (reference computeBudget() parity).
# Pure + deterministic: the published numbers are recomputable from the same
# public inputs. The model decides WHETHER to enter, never the size.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskBudget:
    equity_usd: float
    drawdown_factor: float      # 0.5 .. 1.0; shrinks while open risk is under water
    max_order_usd: float
    max_daily_usd: float
    formula: str                # the arithmetic, published so numbers are reproducible
    derived: bool               # False when the budget failed closed to the floor

    def to_dict(self) -> dict:
        return {
            "equity_usd": self.equity_usd,
            "drawdown_factor": self.drawdown_factor,
            "max_order_usd": self.max_order_usd,
            "max_daily_usd": self.max_daily_usd,
            "formula": self.formula,
            "derived": self.derived,
        }


def _round_half_up(x: float) -> int:
    """JS Math.round parity (half up) - the reference rounds ticket sizes with
    Math.round; the Python builtin round() uses banker rounding and would
    diverge on .5 boundaries. Money math must match the published formula."""
    return int(math.floor(float(x) + 0.5))


def _fail_closed_budget(min_ticket_usd: Optional[float] = None) -> RiskBudget:
    """Minimum-ticket budget: an unreadable book may never authorise size."""
    min_t = (config.MIN_TICKET_USD if min_ticket_usd is None
             else float(min_ticket_usd))
    max_daily = float(_round_half_up(min(
        config.HARD_DAILY_CEILING_USD,
        max(min_t, min_t * config.DAY_MULTIPLE))))
    return RiskBudget(
        equity_usd=0.0, drawdown_factor=1.0, max_order_usd=min_t,
        max_daily_usd=max_daily,
        formula="fail closed: unreadable book -> minimum ticket",
        derived=False,
    )


def compute_risk_budget(equity_usd: float, unrealized_usd: float,
                        min_ticket_usd: Optional[float] = None) -> RiskBudget:
    """
    Verbatim port of the reference computeBudget(equityUsd, unrealizedUsd):

        open_drawdown_pct = min(0, unrealized) / equity      (0 if equity <= 0)
        drawdown_factor   = clamp(1 + open_drawdown_pct * 2.5, 0.5, 1.0)
        max_order_usd     = round(clamp(equity * PER_ORDER_FRACTION *
                                        drawdown_factor, floor,
                                        HARD_ORDER_CEILING_USD))
                            (equity <= 0 -> floor: fail closed, never open)
        max_daily_usd     = round(clamp(max_order_usd * DAY_MULTIPLE,
                                        floor, HARD_DAILY_CEILING_USD))

    floor = min_ticket_usd when given, else config.MIN_TICKET_USD. The
    optional floor exists so the LIVE path (REF-R11 micro-bootstrap, handoff
    §26) can size a $3-5 book without touching the paper $25 floor; default
    None keeps every paper expectation bit-identical (frozen for calibration).

    -20% of equity in open losses halves the ticket; flat or green = full size.
    Never raises: malformed inputs fail CLOSED to the minimum-ticket budget
    (a skipped opportunity costs nothing; a guessed size corrupts the book).
    """
    floor = (config.MIN_TICKET_USD if min_ticket_usd is None
             else float(min_ticket_usd))
    try:
        equity = (float(equity_usd)
                  if math.isfinite(equity_usd) and equity_usd > 0 else 0.0)
        # A non-finite unrealized mark is unreadable -> refuse to guess (the
        # JS original lets NaN propagate into the factor; we fail closed).
        if equity > 0 and not math.isfinite(unrealized_usd):
            equity = 0.0
        open_dd_pct = (min(0.0, float(unrealized_usd)) / equity
                       if equity > 0 else 0.0)
        df_raw = min(1.0, max(0.5, 1.0 + open_dd_pct * 2.5))
        df_display = round(df_raw, 3)
        if equity > 0:
            max_order = float(_round_half_up(min(
                config.HARD_ORDER_CEILING_USD,
                max(floor,
                    equity * config.PER_ORDER_FRACTION * df_raw))))
        else:
            max_order = float(floor)
        max_daily = float(_round_half_up(min(
            config.HARD_DAILY_CEILING_USD,
            max(floor,
                max_order * config.DAY_MULTIPLE))))
        equity_display = _round_half_up(equity)
        formula = (
            "per order = equity %d * %s * drawdown factor %.3f, clamped to "
            "[%g, %g]; per day = per order * %d, clamped to %g" % (
                equity_display, config.PER_ORDER_FRACTION, df_display,
                floor, config.HARD_ORDER_CEILING_USD,
                config.DAY_MULTIPLE, config.HARD_DAILY_CEILING_USD)
        )
        return RiskBudget(
            equity_usd=float(equity_display), drawdown_factor=df_display,
            max_order_usd=max_order, max_daily_usd=max_daily,
            formula=formula, derived=equity > 0,
        )
    except Exception:
        log.warning("compute_risk_budget failed closed (equity=%r unrealized=%r)",
                    equity_usd, unrealized_usd, exc_info=True)
        return _fail_closed_budget(min_ticket_usd)


def portfolio_equity_and_unrealized(portfolio: PortfolioState,
                                    price_map: dict) -> tuple[float, float]:
    """
    Risk-budget inputs from the live book:

        equity     = cash + sum(position value over open positions)
        unrealized = sum(unrealized pnl over open positions)

    price_map: {mint_address: current_price}. A position missing from the map
    (or with degenerate inputs) is marked AT COST - value = position_size_usd,
    unrealized 0. Conservative and never fabricated: an unreadable mark can
    neither inflate nor deflate the budget. Raises nothing on bad marks.
    """
    equity = float(portfolio.cash_usd)
    unrealized = 0.0
    for t in portfolio.open_positions:
        pnl = 0.0
        price = price_map.get(t.mint_address)
        if price is not None and price > 0:
            try:
                pnl, _ = compute_unrealized_pnl(t, float(price))
                if not math.isfinite(pnl):
                    pnl = 0.0
            except (ValueError, TypeError):
                pnl = 0.0     # degenerate position -> mark at cost
        equity += float(t.position_size_usd) + pnl
        unrealized += pnl
    if not math.isfinite(equity):
        return 0.0, 0.0       # unreadable book -> caller fails closed
    return equity, unrealized


def compute_ticket(cash_usd: float, heat: Optional[int],
                   equity_usd: Optional[float] = None,
                   unrealized_usd: Optional[float] = None,
                   conviction_factor: Optional[float] = None,
                   min_ticket_usd: Optional[float] = None) -> float:
    """
    Ticket sizing, three modes keyed on SIZING_MODE:

    "fixed"      - INTENDED_POSITION_SIZE_USD unchanged (calibration baseline).
    "conviction" - cash x crowd-heat conviction, capped hard:
        base       = min(cash * TICKET_CASH_FRACTION, TICKET_MAX_USD)
        conviction = min(1, heat/100 + 0.3)      (heat None -> 50 neutral)
        ticket     = base * conviction
    "risk_budget" - REF-R8 drawdown-adaptive sizing (reference computeBudget
        parity) times the REF-R9 conviction factor:
        ticket = risk_budget.max_order_usd * conviction_factor,
        clamped to [floor, HARD_ORDER_CEILING_USD].
        Missing equity/unrealized default to (0, 0) -> the budget fails closed
        to the floor; a malformed conviction factor is treated as 1.0.

    floor = min_ticket_usd when given, else config.MIN_TICKET_USD. The
    optional floor exists so the LIVE path (REF-R11 micro-bootstrap, handoff
    §26) can apply its own MIN_LIVE_TICKET_USD without touching the paper
    floor; default None keeps paper output bit-identical (frozen for
    calibration comparability).
    """
    floor = config.MIN_TICKET_USD if min_ticket_usd is None else float(min_ticket_usd)
    if config.SIZING_MODE == "fixed":
        return float(config.INTENDED_POSITION_SIZE_USD)
    if config.SIZING_MODE == "risk_budget":
        budget = compute_risk_budget(
            0.0 if equity_usd is None else equity_usd,
            0.0 if unrealized_usd is None else unrealized_usd,
            min_ticket_usd=min_ticket_usd,
        )
        cf = conviction_factor
        if cf is None or not math.isfinite(cf) or cf <= 0:
            cf = 1.0      # fail closed: never scale on a malformed factor
        ticket = budget.max_order_usd * cf
        return float(min(config.HARD_ORDER_CEILING_USD,
                         max(floor, _round_half_up(ticket))))
    heat_val = config.CROWD_HEAT_MIN + 14 if heat is None else heat  # ~50 neutral
    conviction = min(1.0, max(0.0, heat_val / 100.0 + 0.3))
    base = min(cash_usd * config.TICKET_CASH_FRACTION, config.TICKET_MAX_USD)
    return max(floor, round(base * conviction))


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
    reason: str   # "closed" | "already_closed" | "cash_refused" |
                  # "implausible_proceeds" (§32 backstop)


# ---------------------------------------------------------------------------
# State-changing functions — atomicity pattern per §5.1
# ---------------------------------------------------------------------------

async def open_position(
    conn: aiosqlite.Connection,
    candidate: Candidate,
    gate: GateDecision,
    ticket_usd: Optional[float] = None,
) -> OpenResult:
    """Open a new simulated position. Idempotent per mint (§5.1).
    ticket_usd: conviction-sized ticket from run_tick; None = fixed size."""
    config.assert_paper_trading_only()

    price = candidate.price_usd
    if price <= 0:
        return OpenResult(False, None, "invalid_price")
    size, quantity = compute_position_size(price, ticket_usd)
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
        await db.delete_trade_row(conn, trade.trade_id)
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
    proceeds = trade.position_size_usd + realized_usd

    # §32 backstop: a single close crediting more than MAX_EXIT_PROCEEDS_MULT
    # times cost basis is a bad exit price (decimals/quote bug), not a gain.
    # Refuse BEFORE any state write so cash can never be corrupted.
    if proceeds > trade.position_size_usd * config.MAX_EXIT_PROCEEDS_MULT:
        log.critical(
            "close_position: %s implausible proceeds $%.2f (%.0fx cost basis "
            "$%.2f) — refusing to credit (bad exit price); position stays open",
            trade.symbol, proceeds,
            proceeds / trade.position_size_usd, trade.position_size_usd,
        )
        return CloseResult(False, trade, "implausible_proceeds")

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
    await db.retire_thesis(
        conn,
        trade_id=trade.trade_id,
        closed_at=closed_at,
        realized_pnl_usd=realized_usd,
    )
    trade.closed_at = closed_at
    trade.exit_price_usd = exit_price
    trade.exit_reason = exit_reason
    trade.realized_pnl_usd = realized_usd
    trade.realized_pnl_pct = realized_pct
    trade.is_open = False
    return CloseResult(True, trade, "closed")


# ---------------------------------------------------------------------------
# Exit conditions (§5.2) and unified entry point (§5 / E4)
# ---------------------------------------------------------------------------

def check_exit_conditions(trade: Trade, current_price: float) -> Optional[str]:
    """
    Backward-compatible single-price exit probe. Delegates to the
    the reference bot-model engine (rule_engine.exits) with price-only inputs —
    rules needing richer market data (liquidity break, invalidation, stale
    volume) evaluate only in scan_and_execute_exits where that data exists.
    Returns the fired rule_id or None. These are the SOLE decision-makers
    for exits — no LLM involved.
    """
    from rule_engine.exits import ExitInput, evaluate_exits
    decision = evaluate_exits(ExitInput(trade=trade, price_usd=current_price))
    return decision.rule_id or None


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
    Unified entry point (§5, the reference bot model): evaluate the gate; on pass
    with NO existing position, open. The `already_held` rule already fails
    any held name, so this branch is belt-and-suspenders — a pass against a
    held mint is logged loudly instead of silently pyramiding.
    The gate decision is ALWAYS returned so the caller logs it either way.
    """
    gate = evaluate_gate(candidate, portfolio, regime, ACTIVE_RULES)

    if gate.all_passed:
        existing = portfolio.get_open_trade_for_mint(candidate.mint_address)
        if existing is None:
            await open_position(conn, candidate, gate)
        else:
            log.warning(
                "decide_and_act: gate passed but %s already has size on "
                "(should be blocked by already_held) — refusing to pyramid",
                candidate.symbol,
            )

    return gate


# ---------------------------------------------------------------------------
# the reference bot-model exit machinery (E8/E9 + §5.2 rebuild)
# ---------------------------------------------------------------------------

@dataclass
class TrimResult:
    applied: bool
    trade: Optional[Trade]
    reason: str   # "trimmed" | "position_closed" | "degenerate" |
                  # "cash_refused" | "implausible_proceeds" (§32 backstop)


async def trim_position(
    conn: aiosqlite.Connection,
    trade: Trade,
    fraction: float,
    exit_price: float,
) -> TrimResult:
    """
    ATOMIC PARTIAL CLOSE — a take-profit ladder tranche (the reference bot model).
    Reduces quantity/cost basis by `fraction`, credits net proceeds, and
    bumps the tranche counter. Same discipline as every state change here:
    conditional row write FIRST, rowcount is the authority, cash only after.
    """
    config.assert_paper_trading_only()

    if not 0.0 < fraction < 1.0:
        return TrimResult(False, trade, "invalid_fraction")
    if exit_price <= 0:
        return TrimResult(False, trade, "invalid_price")

    qty_out = trade.quantity * fraction
    size_out = trade.position_size_usd * fraction

    # §32 backstop (same as close_position): a trim crediting more than
    # MAX_EXIT_PROCEEDS_MULT times cost basis is a bad exit price. Refuse
    # BEFORE the quantity reduction so the position is never trimmed without
    # a matching, plausible cash credit.
    gross = qty_out * exit_price
    proceeds = gross * (1.0 - config.SLIPPAGE_PCT) * (1.0 - config.FEE_PCT)
    if proceeds > trade.position_size_usd * config.MAX_EXIT_PROCEEDS_MULT:
        log.critical(
            "trim_position: %s implausible proceeds $%.2f (%.0fx cost basis "
            "$%.2f) — refusing to credit (bad exit price); position untouched",
            trade.symbol, proceeds,
            proceeds / trade.position_size_usd, trade.position_size_usd,
        )
        return TrimResult(False, trade, "implausible_proceeds")

    # 1. Conditional partial write FIRST.
    rows = await db.trim_position_row(conn, trade.trade_id, qty_out, size_out)
    if rows == 0:
        persisted = await db.get_trade_by_id(conn, trade.trade_id)
        if persisted is not None and not persisted.is_open:
            return TrimResult(False, persisted, "position_closed")
        log.warning(
            "trim_position: %s degenerate trim (fraction %.2f) — no-op",
            trade.symbol, fraction,
        )
        return TrimResult(False, persisted or trade, "degenerate")

    # 2. Confirmed. Credit net proceeds for the trimmed slice.
    moved = await db.adjust_cash(conn, proceeds)
    if moved == 0:
        log.critical(
            "trim_position: cash credit REFUSED for %s after confirmed trim "
            "— portfolio state inconsistent, investigate immediately",
            trade.trade_id,
        )
        return TrimResult(False, await db.get_trade_by_id(conn, trade.trade_id),
                          "cash_refused")

    updated = await db.get_trade_by_id(conn, trade.trade_id)
    realized_here = proceeds - size_out
    log.info(
        "TRIMMED %s [take_profit tranche %d]: %.0f%% of remaining | "
        "slice pnl $%+.4f | proceeds $%.2f",
        trade.symbol, updated.tranches_taken if updated else -1,
        fraction * 100.0, realized_here, proceeds,
    )
    return TrimResult(True, updated, "trimmed")


async def scan_and_execute_exits(
    provider,
    conn: aiosqlite.Connection,
    now: Optional[datetime] = None,
    on_close=None,
) -> int:
    """
    The the reference bot 'manage' step: re-price EVERY open position and run it
    through the exit rule set, then the sell risk gate, then execute.

    Runs on its own fast loop (config.EXIT_SCAN_INTERVAL_SECONDS) in main()
    AND once per tick, because memecoins gap through stops between 60s
    cycles — the DB forensics show stops realizing −40% on average when
    checked only once a minute.

    Market data: uses provider.get_exit_context(mint, decimals) when the
    provider offers it (price + liquidity + 6h windows in one call);
    otherwise falls back to price-only via get_current_price, in which case
    the data-dependent rules report not-evaluable this cycle.

    Returns the number of executed actions (closes + trims). `on_close`
    (optional async callback receiving the closed Trade) lets callers hook
    reflections without coupling this scanner to the LLM.
    """
    from rule_engine.exits import ExitInput, evaluate_exits, sell_risk_gate

    now = now or datetime.now(timezone.utc)
    actions = 0

    for trade in await db.get_open_trades(conn):
        try:
            snapshot = trade.candidate_snapshot or {}
            decimals = snapshot.get("decimals")
            price = None
            liquidity = chg6h = None
            buys6h = sells6h = vol6h = None

            ctx_fn = getattr(provider, "get_exit_context", None)
            if ctx_fn is not None:
                ctx = await ctx_fn(trade.mint_address, decimals)
                if ctx:
                    price = ctx.get("price_usd")
                    liquidity = ctx.get("liquidity_usd")
                    chg6h = ctx.get("chg6h_pct")
                    buys6h = ctx.get("buys6h")
                    sells6h = ctx.get("sells6h")
                    vol6h = ctx.get("vol6h_usd")
            if price is None:
                price = await provider.get_current_price(
                    trade.mint_address, decimals
                )
        except Exception as exc:
            log.warning("exit scan: price unavailable for %s — skipping: %s",
                        trade.symbol, exc)
            continue
        if price is None or price <= 0:
            continue

        # Price-plausibility guard (§32 cash-corruption incident): a single-scan
        # price this far ABOVE the position's established peak is a bad quote,
        # not a market move. Skip this position this scan and do NOT ratchet
        # high-water on suspect data (a poisoned peak later forces a premature
        # trail exit). Upward-only on purpose: a genuine collapse must still
        # exit, and a poisoned peak must not trap a position.
        peak = trade.high_water_usd or trade.entry_price_usd
        if peak and peak > 0 and price > peak * config.EXIT_PRICE_JUMP_MAX:
            log.warning(
                "exit scan: %s price $%.8f is %.0fx the established peak "
                "$%.8f — treating as a bad quote, skipping this scan "
                "(high-water NOT updated)",
                trade.symbol, price, price / peak, peak,
            )
            continue

        # Trail memory: the peak includes right now.
        hwm = max(price, trade.high_water_usd or trade.entry_price_usd)
        await db.update_high_water(conn, trade.trade_id, hwm)

        decision = evaluate_exits(ExitInput(
            trade=trade,
            price_usd=price,
            high_water_usd=hwm,
            tranches_taken=trade.tranches_taken,
            liquidity_usd=liquidity,
            chg6h_pct=chg6h,
            buys6h=buys6h,
            sells6h=sells6h,
            vol6h_usd=vol6h,
            now=now,
        ))
        if decision.action == "hold":
            continue

        # Sell risk gate inputs (rolling 24h window).
        last_exit_iso = await db.get_last_closed_at_for_mint(
            conn, trade.mint_address
        )
        last_exit_dt = (
            datetime.fromisoformat(last_exit_iso) if last_exit_iso else None
        )
        since = (now - timedelta(hours=24)).isoformat()
        closes_24h = await db.count_closes_since(conn, since)

        est_value = trade.quantity * price * decision.fraction
        gated, gate_note = sell_risk_gate(
            decision, est_value, last_exit_dt, closes_24h, now
        )
        if gated.action == "hold":
            log.info(
                "EXIT GATE held %s [%s]: %s (%s)",
                trade.symbol, decision.rule_id, gate_note, decision.detail,
            )
            continue

        nonce = uuid.uuid4().hex
        payload = {
            "v": 1, "kind": "exit",
            "trade_id": trade.trade_id, "symbol": trade.symbol,
            "mint_address": trade.mint_address,
            "rule": gated.rule_id, "action": gated.action,
            "fraction": gated.fraction, "detail": gated.detail,
            "price_usd": price, "decided_at": now.isoformat(),
        }
        payload_json = json.dumps(payload, sort_keys=True)
        payload_hash = hashlib.sha256((nonce + "|" + payload_json).encode()).hexdigest()
        await db.insert_decision_commit(
            conn, now.isoformat(), now.isoformat(), trade.symbol,
            trade.mint_address, "sell", True, nonce,
            payload_json, payload_hash,
        )
        if gated.action == "close_full":
            result = await close_position(conn, trade, price, gated.rule_id)
            if result.applied:
                actions += 1
                log.info("EXIT %s [%s] %s",
                         trade.symbol, gated.rule_id, gated.detail)
                # Auto-block on consecutive stop-outs (the DONT pattern killer).
                if gated.rule_id == "exit_stop_loss":
                    from blocklist import block_mint as _block, \
                        should_autoblock as _should
                    reasons = await db.get_recent_closed_reasons(
                        conn, trade.mint_address,
                        limit=config.AUTO_BLOCK_CONSECUTIVE_STOPS)
                    if _should(reasons):
                        _block(trade.mint_address,
                               f"{len(reasons)} consecutive stop-outs",
                               kind="auto")
                        log.warning(
                            "AUTO-BLOCK %s (%s): %s — mint will no longer "
                            "be considered for entry", trade.symbol,
                            trade.mint_address[:12],
                            "; ".join(reasons))
                if on_close is not None:
                    try:
                        await on_close(result.trade)
                    except Exception:
                        log.warning("on_close callback failed (non-fatal)",
                                    exc_info=True)
        elif gated.action == "trim":
            result = await trim_position(conn, trade, gated.fraction, price)
            if result.applied:
                actions += 1
                log.info("EXIT %s [%s] %s",
                         trade.symbol, gated.rule_id, gated.detail)

    return actions


