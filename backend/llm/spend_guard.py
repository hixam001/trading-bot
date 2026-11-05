"""
llm/spend_guard.py — hard daily call/token/cost ceilings (§92 S5).

The guard is deliberately boring and fail-closed:

  * counters persist in `LIVE_STATE_DIR/spend_guard.json` (0600, atomic
    replace) and roll over at UTC midnight;
  * a call is RESERVED before the network request, so a provider cannot be
    hammered while a retry loop decides what to do;
  * token/cost totals are recorded after the response;
  * a zero or negative cap means ZERO calls, never unlimited (zero != none);
  * an unreadable/corrupt counter file blocks paid calls until an operator
    inspects it — deterministic exits do not call the LLM, so they keep
    running;
  * one deduped `SPEND_CAP` alert per provider/day reaches the operator.

Scope: LLM calls and entry-side web search. RPC/Birdeye calls are not capped
here because they are also used by exit paths; capping them could disable a
stop-loss. Their provider-side quotas are an operator control (docs/18).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import threading
from typing import Any

import config

log = logging.getLogger(__name__)

STATE_FILE_NAME = "spend_guard.json"
_ALERTED: set[tuple[str, str, str]] = set()
_LOCK = threading.RLock()


class SpendCapExceeded(RuntimeError):
    """Raised before a paid call when the daily ceiling is reached."""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _state_path() -> Path:
    return Path(config.LIVE_STATE_DIR) / STATE_FILE_NAME


def _fresh_state() -> dict[str, Any]:
    return {"version": 1, "day": _today(), "providers": {}, "search": {"calls": 0}}


def _load_locked() -> dict[str, Any]:
    path = _state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _fresh_state()
    except OSError as exc:
        raise SpendCapExceeded(
            f"spend guard unreadable at {path}: {exc}") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise SpendCapExceeded(
            f"spend guard corrupt at {path}; refusing paid calls") from None
    if not isinstance(data, dict) or data.get("version") != 1:
        raise SpendCapExceeded(
            f"spend guard format unsupported at {path}; refusing paid calls")
    if data.get("day") != _today():
        return _fresh_state()
    providers = data.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    search = data.get("search")
    if not isinstance(search, dict):
        search = {"calls": 0}
    return {"version": 1, "day": _today(), "providers": providers,
            "search": search}


def _atomic_write_locked(state: dict[str, Any]) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError as exc:
        # A guard that cannot persist its counter must not silently allow
        # unlimited spending.
        raise SpendCapExceeded(
            f"spend guard cannot persist at {path}: {exc}") from None


def _counter(state: dict[str, Any], provider: str) -> dict[str, float]:
    providers = state["providers"]
    row = providers.get(provider)
    if not isinstance(row, dict):
        row = {"calls": 0, "tokens": 0, "cost_usd": 0.0}
        providers[provider] = row
    for key in ("calls", "tokens"):
        if not isinstance(row.get(key), (int, float)):
            row[key] = 0
    if not isinstance(row.get("cost_usd"), (int, float)):
        row["cost_usd"] = 0.0
    return row


def _alert(kind: str, provider: str, detail: str) -> None:
    key = (kind, provider, _today())
    if key in _ALERTED:
        return
    _ALERTED.add(key)
    try:
        from alerts import AlertEvent, alert_nowait
        alert_nowait(
            AlertEvent.SPEND_CAP,
            f"Daily {kind} cap reached for {provider}: {detail}. "
            "New entries are paused; deterministic exits keep running.",
            key=f"spend_cap:{kind}:{provider}:{_today()}",
            kind=kind,
            provider=provider,
        )
    except Exception:
        log.warning("could not enqueue SPEND_CAP alert", exc_info=True)


def _check_cap(kind: str, provider: str, row: dict[str, float],
               calls_cap: int, token_cap: int | None = None,
               cost_cap: float | None = None) -> None:
    calls = float(row.get("calls", 0))
    tokens = float(row.get("tokens", 0))
    cost = float(row.get("cost_usd", 0.0))
    reason = ""
    if calls_cap <= 0 or calls >= calls_cap:
        reason = f"call cap {calls_cap} (used {int(calls)})"
    elif token_cap is not None and (token_cap <= 0 or tokens >= token_cap):
        reason = f"token cap {token_cap} (used {int(tokens)})"
    elif cost_cap is not None and (cost_cap <= 0 or cost >= cost_cap):
        reason = f"cost cap ${cost_cap:.2f} (used ${cost:.4f})"
    if reason:
        _alert(kind, provider, reason)
        raise SpendCapExceeded(f"{kind} {reason} for {provider}")


def reserve_llm(provider: str) -> None:
    """Reserve one paid LLM call or raise SpendCapExceeded (fail closed)."""
    with _LOCK:
        state = _load_locked()
        row = _counter(state, provider)
        _check_cap(
            "llm", provider, row,
            int(getattr(config, "LLM_DAILY_CALL_CAP", 2000)),
            int(getattr(config, "LLM_DAILY_TOKEN_CAP", 2_000_000)),
            float(getattr(config, "LLM_DAILY_COST_CAP_USD", 5.0)),
        )
        row["calls"] = int(row.get("calls", 0)) + 1
        _atomic_write_locked(state)


def record_llm(provider: str, *, tokens: int = 0,
               cost_usd: float = 0.0) -> None:
    """Record post-call usage. Never raises into the caller."""
    try:
        with _LOCK:
            state = _load_locked()
            row = _counter(state, provider)
            row["tokens"] = int(row.get("tokens", 0)) + max(int(tokens), 0)
            row["cost_usd"] = float(row.get("cost_usd", 0.0)) + max(
                float(cost_usd), 0.0)
            _atomic_write_locked(state)
    except Exception:
        log.warning("spend guard could not record usage; next call will "
                    "fail closed if counters stay unreadable", exc_info=True)


def reserve_search() -> None:
    """Reserve one entry-side web-search call."""
    with _LOCK:
        state = _load_locked()
        search = state["search"]
        calls = int(search.get("calls", 0))
        cap = int(getattr(config, "SEARCH_DAILY_CALL_CAP", 500))
        if cap <= 0 or calls >= cap:
            _alert("search", "web", f"call cap {cap} (used {calls})")
            raise SpendCapExceeded(
                f"search call cap {cap} reached (used {calls})")
        search["calls"] = calls + 1
        _atomic_write_locked(state)


def reset_for_tests() -> None:
    """Clear alert dedupe (tests point LIVE_STATE_DIR at a throwaway dir)."""
    _ALERTED.clear()


def status() -> dict[str, Any]:
    """Read-only snapshot for tests / diagnostics."""
    with _LOCK:
        return _load_locked()
