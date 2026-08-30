"""
live_execution/kill_switch.py — persistent kill switch + daily-loss breaker.

Two mechanisms, one file-backed state:

1. MANUAL KILL SWITCH — the operator (or scripts/confirm_trade.py) trips it
   with a reason; every trade path asserts it is clear BEFORE anything else.
   Clearing is equally explicit. Missing state file == clear (the package is
   separately disarmed by the hardcoded LIVE_TRADING_ENABLED=False).

2. AUTOMATIC DAILY-LOSS CIRCUIT BREAKER — consults the execution ledger's
   realized P&L for today; at/below the configured loss it trips ITSELF.
   Only a human clear() re-enables trading afterwards.

Fail-safe direction: any error reading state is surfaced, never swallowed
into "assume fine".
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Optional

from live_execution import config


class KillSwitchTripped(Exception):
    """Trading refused because the kill switch is engaged."""


def _state_path(state_dir: Optional[Path] = None) -> Path:
    return Path(state_dir or config.STATE_DIR) / "kill_switch.json"


def _read(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        # Corrupt switch state must never silently mean "clear".
        raise RuntimeError(
            f"kill-switch state at {path} is corrupt ({exc}) — refusing to "
            f"trade until a human inspects it"
        )


def _write(tripped: bool, reason: str, state_dir: Optional[Path]) -> None:
    path = _state_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(
        {"tripped": tripped, "reason": reason, "ts": time.time()}, indent=2))
    os.replace(tmp, path)


def is_tripped(state_dir: Optional[Path] = None) -> bool:
    state = _read(_state_path(state_dir))
    return bool(state and state.get("tripped"))


def trip_reason(state_dir: Optional[Path] = None) -> str:
    state = _read(_state_path(state_dir)) or {}
    return str(state.get("reason", ""))


def trip(reason: str, state_dir: Optional[Path] = None) -> None:
    _write(True, reason, state_dir)


def clear(state_dir: Optional[Path] = None) -> None:
    _write(False, "", state_dir)


def assert_not_tripped(state_dir: Optional[Path] = None) -> None:
    if is_tripped(state_dir):
        raise KillSwitchTripped(
            f"KILL SWITCH ENGAGED — reason: {trip_reason(state_dir)!r}. "
            f"A human must clear it explicitly before any trade."
        )


def check_daily_loss_breaker(
    ledger,
    state_dir: Optional[Path] = None,
    now_fn: Callable[[], float] = time.time,
) -> bool:
    """
    Trip the switch automatically when today's realized PnL is at/below
    -DAILY_LOSS_BREAKER_USD. Idempotent: a tripped switch stays tripped.
    Returns whether the switch is tripped after the check.
    """
    if is_tripped(state_dir):
        return True
    pnl = ledger.realized_pnl_today()
    if pnl <= -abs(config.DAILY_LOSS_BREAKER_USD):
        trip(
            f"AUTO: realized daily loss {pnl:.2f} USD breached breaker "
            f"(-{abs(config.DAILY_LOSS_BREAKER_USD):.2f})",
            state_dir,
        )
        return True
    return False
