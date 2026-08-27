"""
blocklist.py — mint blocklist with manual + automatic entries (reference parity).

the reference bot enforces a blocklist in three places so a rugged name "cannot come
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
import re
import time
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# A6 — static SYMBOL blocklist (reference parity, src/lib/blocklist.ts).
#
# Tickers the bot is not allowed to track, grade, shortlist or talk about:
# manufactured or dead charts that slipped past the numeric filters once.
# This complements the mint-based blocklist below: a rugged name can re-launch
# under a NEW mint, so blocking by symbol catches the side door that a
# mint-only list leaves open. Enforced in filter_candidates() alongside the
# mint check, so blocked names never reach think/enrichment (no LLM or scrape
# credits burned).
# ---------------------------------------------------------------------------

BLOCKED_SYMBOLS = frozenset({
    "404",
    "404LIFE",
    "404LIFENOTFOUND",
    "WAWA",
    "POOPHORSI",
    "MACI",
    "SHEEP",
    # closed and done with: no more retrospection on this one
    "BIST",
    # dropped for good: no more calendar reasoning on this one
    "KIO",
    "KIONGAZI",
    # rugged: never track, grade or talk about this one again
    "CRASHIUS",
    # closed in profit and filed: no more grading this one
    "HANDSEM",
    # closed out and filed: no more grading these
    "BASECAT",
    "ZOE",
})


def _normalize_symbol(raw) -> str:
    """Strip a leading $, all whitespace, and upper-case (reference parity)."""
    return re.sub(r"\s+", "", str(raw or "").lstrip("$")).strip().upper()


def is_blocked_symbol(raw) -> bool:
    """True when this ticker must never be tracked or mentioned."""
    sym = _normalize_symbol(raw)
    if not sym:
        return False
    if sym in BLOCKED_SYMBOLS:
        return True
    # "404 life not found" and friends, in any spacing the source returns.
    if sym.startswith("404"):
        return True
    return False


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

    Two independent block layers (A6):
      * mint blocklist  — manual + auto-blocked mint addresses (state sidecar).
      * symbol blocklist — static BLOCKED_SYMBOLS + `^404` prefix, so a rugged
        name re-launched under a fresh mint is still caught.
    """
    mints = _load().get("mints", {})
    kept, blocked = [], []
    for c in candidates:
        info = mints.get(c.mint_address)
        if info is not None:
            blocked.append((c.symbol, info.get("reason", "")))
        elif is_blocked_symbol(getattr(c, "symbol", None)):
            blocked.append((c.symbol, "blocked symbol (static list)"))
        else:
            kept.append(c)
    return kept, blocked


def should_autoblock(recent_exit_reasons: list[str]) -> bool:
    """True when the newest N closes are ALL stop-outs (N = threshold)."""
    need = config.AUTO_BLOCK_CONSECUTIVE_STOPS
    if len(recent_exit_reasons) < need:
        return False
    return all(r == "exit_stop_loss" for r in recent_exit_reasons[:need])
