"""
live_execution/exit_errors.py — Structured exit-failure classification (§81 Phase A).

Classifies failures on the sell exit slippage ladder so that real on-chain
and preflight slippage failures escalate, while security guard refusals
and ambiguous transport failures fail closed.
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional


def custom_error_code(err) -> int | None:
    """
    Extract a custom program error code from an on-chain or RPC error.
    Accepts:
      (a) JSON form: {"InstructionError": [idx, {"Custom": N}]}
      (b) Direct list form: [idx, {"Custom": N}]
      (c) Text form: "... custom program error: 0x1771" (parses hex)
      (d) Text form: "Custom: 6001" or "Custom program error: 6001"
    Returns None if no custom code found.
    """
    if err is None:
        return None

    # Handle dict form: {"InstructionError": [idx, {"Custom": N}]}
    if isinstance(err, dict):
        ie = err.get("InstructionError")
        if isinstance(ie, list) and len(ie) >= 2:
            detail = ie[1]
            if isinstance(detail, dict):
                code = detail.get("Custom")
                if isinstance(code, int):
                    return code
        # In case err is directly {"Custom": N}
        if "Custom" in err and isinstance(err["Custom"], int):
            return err["Custom"]

    # Handle direct list form: [idx, {"Custom": N}]
    if isinstance(err, list) and len(err) >= 2:
        detail = err[1]
        if isinstance(detail, dict):
            code = detail.get("Custom")
            if isinstance(code, int):
                return code

    # Handle string form (logs / RPC error text)
    if isinstance(err, str):
        # Hex form: "custom program error: 0x1771"
        m = re.search(r"custom program error:\s*0x([0-9a-fA-F]+)", err)
        if m:
            return int(m.group(1), 16)
        # Decimal form: "Custom: 6001" or "Custom program error: 6001"
        m = re.search(r"\bCustom(?:[^:\d]*):\s*(\d+)", err)
        if m:
            return int(m.group(1))

    return None


class ExitFailure(NamedTuple):
    retry: bool
    reason: str


class SendRejected(Exception):
    """Raised when every RPC endpoint definitively rejected the transaction at preflight."""
    def __init__(self, msg: str, attempts: list[dict]):
        super().__init__(msg)
        self.attempts = attempts

    def __str__(self) -> str:
        details = []
        for a in self.attempts:
            if a.get("err"):
                details.append(str(a["err"]))
            elif a.get("message"):
                details.append(str(a["message"]))
        if details:
            return f"{super().__str__()} ({'; '.join(details)})"
        return super().__str__()


def classify_exit_failure(exc_or_msg, *, onchain_err=None) -> ExitFailure:
    """
    Classify whether an exit failure is retryable.
    Rules (in order):
    1. SimulationRefused with onchain_err is not None and code in EXIT_RETRYABLE_CUSTOM_ERRORS -> retry
    2. Any other SimulationRefused -> no retry (guard refusals must never be retried)
    3. SendRejected with definitive retryable RPC error or expired blockhash -> retry
    4. On-chain error dict/list with retryable code -> retry
    5. Fallback to existing string markers (non-retryable markers first)
    """
    from live_execution import config as live_config

    try:
        from live_execution.sim_guard import SimulationRefused
    except ImportError:
        SimulationRefused = None

    # Rule 1 & 2: SimulationRefused
    if SimulationRefused is not None and isinstance(exc_or_msg, SimulationRefused):
        if exc_or_msg.onchain_err is not None:
            code = custom_error_code(exc_or_msg.onchain_err)
            if code is not None and code in live_config.EXIT_RETRYABLE_CUSTOM_ERRORS:
                return ExitFailure(retry=True, reason=f"sim slippage error {code}")
        return ExitFailure(retry=False, reason="guard refusal (non-retryable)")

    # Rule 3: SendRejected
    if isinstance(exc_or_msg, SendRejected):
        attempts = exc_or_msg.attempts
        # Only retry if ALL attempts were definitive rpc_error
        if attempts and all(a.get("kind") == "rpc_error" for a in attempts):
            for a in attempts:
                code = custom_error_code(a.get("err"))
                if code is not None and code in live_config.EXIT_RETRYABLE_CUSTOM_ERRORS:
                    return ExitFailure(retry=True, reason=f"preflight slippage {code}")
            for a in attempts:
                msg = str(a.get("message", "")).lower()
                if "blockhash" in msg and any(w in msg for w in ("not found", "expired", "invalid")):
                    return ExitFailure(retry=True, reason="blockhash expired at preflight")
        return ExitFailure(retry=False, reason="send rejected (ambiguous or non-retryable)")

    # Rule 4: On-chain error passed directly or via onchain_err keyword
    err_candidate = onchain_err if onchain_err is not None else exc_or_msg
    if isinstance(err_candidate, (dict, list)):
        code = custom_error_code(err_candidate)
        if code is not None:
            if code in live_config.EXIT_RETRYABLE_CUSTOM_ERRORS:
                return ExitFailure(retry=True, reason=f"landed slippage error {code}")
            return ExitFailure(retry=False, reason=f"landed non-retryable error {code}")

    # Rule 5: Fallback to existing string markers
    msg = str(exc_or_msg).lower()
    non_retryable_markers = (
        "unapproved",
        "simulation refused",
        "simulation assertion failure",
        "guard refused",
        "owner changed",
        "price impact",
        "kill switch",
        "reserve",
        "balance unreadable",
        "insufficient",
        "hard cap",
        "consensus required",
        "pre-state fetch failed",
    )
    if any(m in msg for m in non_retryable_markers):
        return ExitFailure(retry=False, reason="non-retryable marker")

    retryable_markers = (
        "6001",
        "0x1771",
        "slippage",
        "blockhash",
        "timeout",
        "not confirmed before timeout",
        "has expired",
        "expired",
    )
    if any(m in msg for m in retryable_markers):
        return ExitFailure(retry=True, reason="retryable marker")

    return ExitFailure(retry=False, reason="unknown (fail closed)")

