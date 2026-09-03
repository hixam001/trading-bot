"""
sizing.py — the book-independent money math (§52).

Extracted VERBATIM from paper_trading_engine.py before the paper book was
retired: these functions are the shared sizing spine BOTH books always used
(the live cycle imported them from the paper module since REF-R8/REF-R11).
They are pure: no DB, no I/O, no state; deterministic and recomputable.
The model decides WHETHER to enter, never the size.

Lives here now so the single live book (run_live_cycle) — and any future
book — imports sizing from a neutral module, not from a paper artifact.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

import config
from models import PortfolioState, Trade

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
    §26) can size a small book without touching the frozen $25 default.

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
    Risk-budget inputs from a book:

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
    Ticket sizing, modes keyed on SIZING_MODE:

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
    optional floor exists so the LIVE path can apply its own floor without
    touching the frozen default.
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