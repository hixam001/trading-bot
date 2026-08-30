"""
blocklist.py — mint blocklist with manual + automatic entries (reference parity).

the reference bot enforces a blocklist in three places so a rugged name "cannot come
back through a side door". Ours mirrors that — and exceeds it (§49, 2026-08-30):

  * MANUAL blocks — operator-added, cleared only explicitly.
  * AUTO blocks — the DONT pattern killer, now fed by BOTH books: when a mint
    accumulates AUTO_BLOCK_CONSECUTIVE_LOSSES consecutive LOSS closes
    (realized PnL < 0, any exit rule — §49 changed the semantic from
    "stop-outs only" to "any close at a loss", the operator's actual words),
    it is blocked until a human clears it. (15 straight stop-outs once cost
    -$709 because nothing remembered the last fourteen.)
  * RE-ENTRY COOLDOWN — a mint whose LAST recorded close is a loss younger
    than REENTRY_COOLDOWN_HOURS is filtered at read time (both books, zero
    quota spent). Unlike the block this self-expires: the 24h window IS the
    punishment for one loss; the block is the punishment for a pattern.

State lives in a JSON sidecar (gitignored): {"mints": {mint: entry}}.
Enforced in read_candidates (BOTH books — decision_pipeline) BEFORE
think/enrichment, so blocked/cooling mints never burn LLM or scrape
credits either.

§49 also records every full-close outcome (win or loss, either book) into
the mint's `closes` history here, making this sidecar the SINGLE source of
truth for "what happened last time we touched this mint" — no drift
between the trades table and the live ledger.
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

# §49: per-mint close-history cap (newest-first; oldest trimmed).
_MAX_CLOSE_HISTORY = 10


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
    except OSError:
        # Unreadable path (permissions, not-a-directory, ...): same
        # fail-open philosophy as corrupt state — a tick must never die
        # because the sidecar can't be read; log loudly and continue
        # with no blocks enforced.
        log.error("blocklist state unreadable (%s) — continuing with no "
                  "blocks enforced (fail-open, non-fatal)", path)
        return {}


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def _is_block_entry(entry: Optional[dict]) -> bool:
    """True when the sidecar entry is an actual BLOCK (manual/auto), not a
    §49 closes-history record. Block entries carry kind/reason/blocked_at;
    close-history-only entries carry just symbol + closes."""
    return bool(entry and (entry.get("kind") or entry.get("reason")))


def is_blocked_mint(mint: str) -> bool:
    return _is_block_entry(_load().get("mints", {}).get(mint))


def mint_entry(mint: str) -> Optional[dict]:
    return _load().get("mints", {}).get(mint)


def block_mint(mint: str, symbol: str = "", reason: str = "",
               kind: str = "manual") -> None:
    data = _load()
    mints = data.setdefault("mints", {})
    # §49: preserve the closes history when writing a block — the block is
    # the verdict, the history is the evidence; overwriting would erase the
    # memory that justified it.
    closes = (mints.get(mint) or {}).get("closes") or []
    mints[mint] = {
        "symbol": symbol,
        "reason": reason,
        "kind": kind,                      # "manual" | "auto"
        "blocked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if closes:
        mints[mint]["closes"] = closes
    _save(data)


def unblock_mint(mint: str) -> bool:
    """Remove the BLOCK but keep the §49 closes history — the memory of why
    it was blocked must survive the unblock (it feeds future cooldowns)."""
    data = _load()
    mints = data.get("mints", {})
    entry = mints.get(mint)
    if entry is None:
        return False
    closes = entry.get("closes") or []
    if closes:
        # History-only entry: keep it, drop the block fields.
        mints[mint] = {"symbol": entry.get("symbol", ""), "closes": closes}
        _save(data)
        return True
    removed = mints.pop(mint, None)
    if removed is not None:
        _save(data)
    return removed is not None


def filter_candidates(candidates: list) -> tuple[list, list]:
    """
    Returns (kept, blocked) where blocked is [(symbol, reason), ...].
    Called in read_candidates (BOTH books) BEFORE think/enrichment.

    Three independent block layers (A6 + §49):
      * mint blocklist  — manual + auto-blocked mint addresses (state sidecar).
      * symbol blocklist — static BLOCKED_SYMBOLS + `^404` prefix, so a rugged
        name re-launched under a fresh mint is still caught.
      * re-entry cooldown (§49) — the mint's LAST recorded close is a loss
        younger than REENTRY_COOLDOWN_HOURS. Self-expiring; belt-and-
        suspenders derivation from the closes history, not a separate flag.
    """
    mints = _load().get("mints", {})
    now = time.time()
    kept, blocked = [], []
    for c in candidates:
        info = mints.get(c.mint_address)
        if _is_block_entry(info):
            blocked.append((c.symbol, (info or {}).get("reason", "")))
            continue
        if is_blocked_symbol(getattr(c, "symbol", None)):
            blocked.append((c.symbol, "blocked symbol (static list)"))
            continue
        cooldown = _cooldown_reason(c.mint_address, info, now)
        if cooldown is not None:
            blocked.append((c.symbol, cooldown))
            continue
        kept.append(c)
    return kept, blocked


def _last_close(entry: Optional[dict]) -> Optional[dict]:
    """Newest recorded close for a mint entry (closes are newest-first)."""
    closes = (entry or {}).get("closes") or []
    return closes[0] if isinstance(closes, list) and closes else None


def _cooldown_reason(mint: str, entry: Optional[dict],
                     now: float) -> Optional[str]:
    """§49 re-entry cooldown: None when the mint is free to be considered;
    a reason string when its last close is a loss inside the window."""
    hours = getattr(config, "REENTRY_COOLDOWN_HOURS", 0.0)
    if hours <= 0:
        return None
    last = _last_close(entry)
    if not last:
        return None
    if not last.get("loss"):
        return None
    ts = last.get("ts") or 0.0
    age_h = (now - ts) / 3600.0
    if age_h >= hours:
        return None
    return (f"re-entry cooldown: loss exit ({last.get('rule', '?')}) "
            f"{age_h:.1f}h ago < {hours:.0f}h window")


def record_close_outcome(mint: str, symbol: str, rule_id: str,
                         pnl_usd: float, book: str = "paper") -> None:
    """
    §49: ONE place where both books record a full-close outcome. Appends
    {rule, ts, pnl, loss, book} to the mint's newest-first `closes` history
    (capped at _MAX_CLOSE_HISTORY) in the same sidecar the blocklist reads.
    Never raises: state-file trouble is logged and swallowed — losing the
    memory is strictly better than losing the tick.
    """
    try:
        data = _load()
        mints = data.setdefault("mints", {})
        entry = mints.setdefault(mint, {"symbol": symbol})
        closes = entry.setdefault("closes", [])
        closes.insert(0, {
            "rule": str(rule_id or "?"),
            "ts": time.time(),
            "pnl": round(float(pnl_usd or 0.0), 4),
            "loss": bool(float(pnl_usd or 0.0) < 0.0),
            "book": str(book or "paper"),
        })
        del closes[_MAX_CLOSE_HISTORY:]
        _save(data)
    except Exception:
        log.warning("close-outcome recording failed for %s (non-fatal)",
                    symbol or mint[:8], exc_info=True)


def maybe_autoblock(mint: str, symbol: str) -> bool:
    """
    §49 DONT-pattern killer: block the mint when its newest
    AUTO_BLOCK_CONSECUTIVE_LOSSES recorded closes are ALL losses (realized
    PnL < 0, any rule — "sells for loss", the operator's words). A
    profitable close between losses resets the streak by construction.
    Returns True when a block was written. Never raises.
    """
    try:
        need = config.AUTO_BLOCK_CONSECUTIVE_LOSSES
        entry = mint_entry(mint) or {}
        closes = entry.get("closes") or []
        if len(closes) < need:
            return False
        if not all(c.get("loss") for c in closes[:need]):
            return False
        rules = ", ".join(str(c.get("rule")) for c in closes[:need])
        block_mint(mint, symbol,
                   f"{need} consecutive loss closes ({rules})",
                   kind="auto")
        log.warning(
            "AUTO-BLOCK %s (%s): %s — mint will no longer be considered "
            "for entry on either book", symbol, mint[:12], rules)
        return True
    except Exception:
        log.warning("auto-block check failed for %s (non-fatal)",
                    symbol or mint[:8], exc_info=True)
        return False


# Legacy alias — the pre-§49 paper-side trigger name (kept so old imports
# and docs referencing it still resolve; the semantic moved to any-loss).
def should_autoblock(recent_exit_reasons: list[str]) -> bool:
    """Deprecated by §49: the PnL-based maybe_autoblock() replaced this
    rule-ID-based check. Retained for backward compatibility; new code
    must call maybe_autoblock() after record_close_outcome()."""
    need = config.AUTO_BLOCK_CONSECUTIVE_LOSSES
    if len(recent_exit_reasons) < need:
        return False
    return all(r == "exit_stop_loss" for r in recent_exit_reasons[:need])
