"""
blocklist.py — mint blocklist with manual + automatic entries (omo parity).

omotrades enforces a blocklist in three places so a rugged name "cannot come
back through a side door". Ours mirrors that:

  * MANUAL blocks — operator-added, cleared only explicitly.
  * AUTO blocks — added when a mint accumulates AUTO_BLOCK_CONSECUTIVE_STOPS
    consecutive stop-outs (the DONT pattern: 15 straight stop-outs cost
    -$709 because nothing remembered the last fourteen).

State lives in a JSON sidecar (gitignored): {"mints": {mint: entry}}.
Enforced in main.run_tick BEFORE think/enrichment, so blocked mints never
burn Ollama or scrape credits either.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger(__name__)


def _path() -> Path:
    """Resolved per call so tests/operator can retarget via config."""
    return Path(str(getattr(config, "BLOCKLIST_STATE_FILE",
                            str(config.BASE_DIR / "blocklist_state.json"))))


def _load() -> dict:
    path = _path()
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except ValueError:
        # Corrupt state: quarantine and start clean, but shout loudly.
        quarantined = path.with_suffix(".corrupt")
        try:
            path.rename(quarantined)
            log.warning("blocklist state corrupt — quarantined to %s",
                        quarantined)
        except OSError:
            log.error("blocklist state corrupt and could not be quarantined")
        return {}


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def is_blocked_mint(mint: str) -> bool:
    return mint in _load().get("mints", {})


def mint_entry(mint: str) -> Optional[dict]:
    return _load().get("mints", {}).get(mint)


def block_mint(mint: str, symbol: str = "", reason: str = "",
               kind: str = "manual") -> None:
    data = _load()
    data.setdefault("mints", {})[mint] = {
        "symbol": symbol,
        "reason": reason,
        "kind": kind,                      # "manual" | "auto"
        "blocked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save(data)


def unblock_mint(mint: str) -> bool:
    data = _load()
    removed = data.get("mints", {}).pop(mint, None)
    if removed is not None:
        _save(data)
    return removed is not None


def filter_candidates(candidates: list) -> tuple[list, list]:
    """
    Returns (kept, blocked) where blocked is [(symbol, reason), ...].
    Called in run_tick BEFORE think/enrichment.
    """
    mints = _load().get("mints", {})
    kept, blocked = [], []
    for c in candidates:
        info = mints.get(c.mint_address)
        if info is not None:
            blocked.append((c.symbol, info.get("reason", "")))
        else:
            kept.append(c)
    return kept, blocked


def should_autoblock(recent_exit_reasons: list[str]) -> bool:
    """True when the newest N closes are ALL stop-outs (N = threshold)."""
    need = config.AUTO_BLOCK_CONSECUTIVE_STOPS
    if len(recent_exit_reasons) < need:
        return False
    return all(r == "exit_stop_loss" for r in recent_exit_reasons[:need])
