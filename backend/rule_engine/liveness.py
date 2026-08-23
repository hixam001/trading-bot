"""
rule_engine/liveness.py — operator break state for the not_on_break rule.

omotrades' gate includes `not_on_break` ("loop awake, not on a break"): an
operational liveness flag, deliberately separate from the kill switch. Here
the default is AWAKE; the operator can pause ENTRIES (exits always keep
running — pausing risk-reduction is never allowed) via set_break(True).

This is process-local state by design: restarting the loop clears it, so a
forgotten break can never silently persist across a reboot.
"""
from __future__ import annotations

_ON_BREAK = False


def set_break(on: bool) -> None:
    global _ON_BREAK
    _ON_BREAK = bool(on)


def is_on_break() -> bool:
    return _ON_BREAK