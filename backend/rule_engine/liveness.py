"""
rule_engine/liveness.py — operator break state for the not_on_break rule (REF-R4).

the reference bot' gate includes `not_on_break` ("loop awake, not on a break"): an
operational liveness flag. The loop may pause itself for a stated reason, persisted
until a timestamp; while broken, the existing `not_on_break` gate rule fails CLOSED
and the refusal records "on break" loudly.

Uses a file-backed state beside kill_switch.json. Fail-safe semantics: if the state
file is corrupt, it fails closed (refuses to trade) until human intervention.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import config


def _state_path() -> Path:
    # As requested by REF-R4, state file is beside kill_switch.json.
    # live_execution is a sibling package INSIDE backend/ since 2026-08-30
    # (§42 deployable restructure), so BASE_DIR is the correct anchor —
    # BASE_DIR.parent would silently recreate a SECOND state dir at the old
    # repo-root path, splitting break state from the kill switch.
    return config.BASE_DIR / "live_execution" / "state" / "break_state.json"


def _read() -> Optional[dict]:
    path = _state_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        # Corrupt state must never silently mean "awake".
        raise RuntimeError(
            f"break state at {path} is corrupt ({exc}) — refusing to "
            f"trade until a human inspects it"
        )


def _write(taking: bool, minutes: int, reason: str) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    
    break_until = 0.0
    if taking:
        if minutes > 0:
            break_until = time.time() + (minutes * 60)
        else:
            break_until = float("inf")
        
    tmp.write_text(json.dumps({
        "taking": taking,
        "minutes": minutes,
        "reason": reason,
        "break_until": break_until,
        "ts": time.time(),
    }, indent=2))
    os.replace(tmp, path)


def set_break(taking: bool, minutes: int = 0, reason: str = "") -> None:
    _write(taking, minutes, reason)


def is_on_break() -> bool:
    state = _read()
    if not state:
        return False
    taking = bool(state.get("taking"))
    break_until = float(state.get("break_until", 0.0))
    if taking and time.time() < break_until:
        return True
    return False


def break_reason() -> str:
    state = _read()
    if not state:
        return ""
    return str(state.get("reason", ""))


def break_until() -> float:
    state = _read()
    if not state:
        return 0.0
    return float(state.get("break_until", 0.0))