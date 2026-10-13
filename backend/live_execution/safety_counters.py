"""
live_execution/safety_counters.py — Persistent safety refusal counters (§79 Phase F).

Tracks refusal counts across reason classes:
- sim_refused: Pre-sign simulation balance/assertion refusals.
- guard_refused: Inspect transaction structural & invariant refusals.
- quorum_failed: Dual-RPC discrepancy or consensus failures.
- quote_mismatch: Jupiter quote validation discrepancies vs bot intent.

Writes STATE_DIR/safety_counters.json atomically so the read-only API process
and frontend can surface live counts and 24-hour rolling metrics.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

VALID_CATEGORIES = frozenset({"sim_refused", "guard_refused", "quorum_failed", "quote_mismatch"})


def _resolve_state_dir(state_dir: Optional[Path] = None) -> Path:
    if state_dir is not None:
        return state_dir
    try:
        from live_execution import config as live_config
        return Path(live_config.STATE_DIR)
    except Exception:
        return Path(os.getenv("STATE_DIR", "backend/live_execution/state"))


def record_safety_refusal(
    category: str,
    reason: str,
    state_dir: Optional[Path] = None,
    now_fn: Callable[[], float] = time.time,
) -> None:
    """Atomically record a refusal event in STATE_DIR/safety_counters.json."""
    cat = category if category in VALID_CATEGORIES else "sim_refused"
    target_dir = _resolve_state_dir(state_dir)
    target_file = target_dir / "safety_counters.json"

    now = now_fn()
    now_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

    data: dict[str, Any] = {
        "total": {c: 0 for c in VALID_CATEGORIES},
        "history": [],
        "last_reason": None,
        "last_at": None,
    }

    if target_file.is_file():
        try:
            raw = target_file.read_text(encoding="utf-8")
            existing = json.loads(raw)
            if isinstance(existing.get("total"), dict):
                for c in VALID_CATEGORIES:
                    data["total"][c] = int(existing["total"].get(c, 0))
            if isinstance(existing.get("history"), list):
                data["history"] = existing["history"]
            data["last_reason"] = existing.get("last_reason")
            data["last_at"] = existing.get("last_at")
        except Exception as e:
            log.warning("safety_counters: failed reading existing counters: %s", e)

    # Increment total
    data["total"][cat] = data["total"].get(cat, 0) + 1
    data["last_reason"] = reason
    data["last_at"] = now_iso

    # Append to history
    data["history"].append({"ts": now, "category": cat, "reason": reason})

    # Prune history older than 24 hours (86400 seconds)
    cutoff = now - 86400.0
    data["history"] = [
        item for item in data["history"]
        if isinstance(item, dict) and float(item.get("ts", 0)) >= cutoff
    ][-1000:]  # Keep at most 1000 recent items

    # Compute rolling 24h
    rolling = {c: 0 for c in VALID_CATEGORIES}
    for item in data["history"]:
        item_cat = item.get("category")
        if item_cat in rolling:
            rolling[item_cat] += 1

    payload = {
        "total": data["total"],
        "rolling_24h": rolling,
        "last_reason": data["last_reason"],
        "last_at": data["last_at"],
        "history": data["history"],
    }

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        tmp_file = target_file.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_file.replace(target_file)
    except Exception as e:
        log.warning("safety_counters: failed writing counters atomically: %s", e)


def load_safety_counters(
    state_dir: Optional[Path] = None,
    now_fn: Callable[[], float] = time.time,
) -> Optional[dict[str, Any]]:
    """
    Read STATE_DIR/safety_counters.json.
    Returns None if missing or unreadable (zero-vs-None rule).
    """
    target_dir = _resolve_state_dir(state_dir)
    target_file = target_dir / "safety_counters.json"

    if not target_file.is_file():
        return None

    try:
        raw = target_file.read_text(encoding="utf-8")
        data = json.loads(raw)
        total = data.get("total", {})
        history = data.get("history", [])

        # Recalculate 24h rolling based on current time
        now = now_fn()
        cutoff = now - 86400.0
        rolling = {c: 0 for c in VALID_CATEGORIES}
        for item in history:
            if isinstance(item, dict) and float(item.get("ts", 0)) >= cutoff:
                c = item.get("category")
                if c in rolling:
                    rolling[c] += 1

        return {
            "sim_refused": int(total.get("sim_refused", 0)),
            "guard_refused": int(total.get("guard_refused", 0)),
            "quorum_failed": int(total.get("quorum_failed", 0)),
            "quote_mismatch": int(total.get("quote_mismatch", 0)),
            "rolling_24h": {
                "sim_refused": rolling["sim_refused"],
                "guard_refused": rolling["guard_refused"],
                "quorum_failed": rolling["quorum_failed"],
                "quote_mismatch": rolling["quote_mismatch"],
            },
            "last_reason": data.get("last_reason"),
            "last_at": data.get("last_at"),
        }
    except Exception as e:
        log.warning("safety_counters: failed loading counters: %s", e)
        return None

