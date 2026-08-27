"""
calibration.py - REF-R9 closed-loop learning: conviction factor from the
closed book (the reference computeCalibration() parity, ported verbatim).

The feedback term that closes the loop: realized outcomes produce a single
bounded conviction factor that MULTIPLIES the REF-R8 risk budget, so a run
of losses shrinks the next ticket and a run of wins restores it. This is
arithmetic, NOT model output and NOT automatic threshold changes (those
stay manual per the standing safety conditions).

Fail-closed rules:
  * no usable closed trades -> factor 1.0 (FLAT, no adjustment);
  * small samples are pulled toward 1.0 by the confidence term, so three
    trades cannot rewrite the book;
  * factor hard-bounded to [0.6, 1.2];
  * any exception -> FLAT. A skipped adjustment costs nothing; a guessed
    one corrupts the track record.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Calibration:
    samples: int
    wins: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    expectancy_pct: float          # expected percent per trade over the sample
    conviction_factor: float       # 0.6 .. 1.2, multiplies the derived order size
    formula: str

    def to_dict(self) -> dict:
        return {
            "samples": self.samples,
            "wins": self.wins,
            "win_rate": self.win_rate,
            "avg_win_pct": self.avg_win_pct,
            "avg_loss_pct": self.avg_loss_pct,
            "expectancy_pct": self.expectancy_pct,
            "conviction_factor": self.conviction_factor,
            "formula": self.formula,
        }


FLAT_CALIBRATION = Calibration(
    samples=0,
    wins=0,
    win_rate=0.0,
    avg_win_pct=0.0,
    avg_loss_pct=0.0,
    expectancy_pct=0.0,
    conviction_factor=1.0,
    formula="no closed trades yet, so conviction factor = 1 (no adjustment)",
)


def _round(value: float, places: int = 3) -> float:
    """JS Math.round parity: round(value * 10**places) / 10**places, half up."""
    factor = 10 ** places
    return math.floor(value * factor + 0.5) / factor


def compute_calibration(closed_trades) -> Calibration:
    """
    Verbatim port of the reference computeCalibration(outcomes), over our
    closed Trade rows. The outcome per trade is realized_pnl_pct; rows
    without a finite pct are unusable and skipped:

        win_rate      = winners / usable          (winners: pnl_pct > 0,
                                                   losers:  pnl_pct <= 0)
        avg_win_pct   = mean pnl over winners     (0 when no winners)
        avg_loss_pct  = mean pnl over losers      (0 when no losers)
        expectancy    = win_rate * avg_win + (1 - win_rate) * avg_loss
        raw           = 1 + min(expectancy / 50, 0.2)     if expectancy >= 0
                        1 + max(expectancy / 25, -0.4)    otherwise
        confidence    = min(usable / 12, 1.0)
        factor        = clamp(1 + (raw - 1) * confidence, 0.6, 1.2)

    +10% expectancy earns a 20% larger ticket; -10% expectancy takes 40%
    off. No usable closed trades -> FLAT (factor 1.0). Never raises: any
    exception fails closed to FLAT.
    """
    try:
        usable = [
            t for t in (closed_trades or [])
            if getattr(t, "realized_pnl_pct", None) is not None
            and math.isfinite(t.realized_pnl_pct)
        ]
        if not usable:
            return FLAT_CALIBRATION

        winners = [t for t in usable if t.realized_pnl_pct > 0]
        losers = [t for t in usable if t.realized_pnl_pct <= 0]
        win_rate = len(winners) / len(usable)
        avg_win = (
            sum(t.realized_pnl_pct for t in winners) / len(winners)
            if winners else 0.0
        )
        avg_loss = (
            sum(t.realized_pnl_pct for t in losers) / len(losers)
            if losers else 0.0
        )
        expectancy = win_rate * avg_win + (1.0 - win_rate) * avg_loss

        if expectancy >= 0:
            raw = 1.0 + min(expectancy / 50.0, 0.2)
        else:
            raw = 1.0 + max(expectancy / 25.0, -0.4)
        confidence = min(len(usable) / 12.0, 1.0)
        factor = max(0.6, min(1.2, 1.0 + (raw - 1.0) * confidence))

        formula = (
            "expectancy = hit rate %s * avg win %s%% + miss rate %s * "
            "avg loss %s%% = %s%%; conviction factor = clamp(1 + expectancy "
            "scaled by sample confidence %s, 0.6, 1.2)" % (
                _round(win_rate), _round(avg_win, 2),
                _round(1.0 - win_rate), _round(avg_loss, 2),
                _round(expectancy, 2), _round(confidence))
        )
        return Calibration(
            samples=len(usable),
            wins=len(winners),
            win_rate=_round(win_rate),
            avg_win_pct=_round(avg_win, 2),
            avg_loss_pct=_round(avg_loss, 2),
            expectancy_pct=_round(expectancy, 2),
            conviction_factor=_round(factor),
            formula=formula,
        )
    except Exception:
        log.warning("compute_calibration failed closed (FLAT 1.0)",
                    exc_info=True)
        return FLAT_CALIBRATION
