"""
thesis_restate.py — A11 thesis re-authoring (omo audit §30).

Reference: omotrades/omo src/lib/thesis-author.server.ts (restateTheses).

A write-up typed once at entry and never touched again is a static string
with extra steps, and a reader is right to treat it as one. This job walks
the open book on the tick cadence and has the main reasoning model rewrite
any write-up that is STALE (not advanced in THESIS_RESTATE_STALE_HOURS) or
not model-authored, against the position's current numbers. At most
THESIS_RESTATE_PER_PASS rows per pass, oldest text first, so a tick never
turns into a batch job.

Hard boundaries (defense-first):
  * NARRATIVE ONLY — a restatement can only ever change theses.thesis /
    author / updated_at. It never touches trades, cash, sizing, exits, or
    any verdict. Size, P&L, and retirement come from the journal.
  * Never invents a position: only rows that already exist and are still
    open are eligible, and the DB write itself is guarded by
    closed_at IS NULL, so a row retired mid-pass is never rewritten.
  * Fail-closed + fail-soft: empty / too-short / oversized model output is
    REJECTED (old text kept, refusal logged); any error skips the row or
    the pass — this module never raises into the tick.
  * No extra network I/O (performance-discipline: don't over-fetch): the
    current mark is REUSED from the tick's own price_map. Documented
    deviation from the reference, which fetches a per-row tape snapshot —
    our tick has already priced every open position this cycle.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import config
from api import db
from llm.client import build_main_client, main_max_tokens, _is_peak_window

log = logging.getLogger(__name__)

# Validation bounds for the model's rewrite. The contract asks for under 60
# words; anything shorter than 20 chars is not a write-up, anything longer
# than 1000 chars ignored the contract. Both are rejected fail-closed — the
# old text stays, the refusal is logged (reference parity for the low bound).
MIN_RESTATEMENT_CHARS: int = 20
MAX_RESTATEMENT_CHARS: int = 1000

RESTATE_SYSTEM_PROMPT = (
    "You are the trading bot holding this position. Rewrite its write-up in "
    "under 60 words so it reflects where the position is now: why it is "
    "still on, what has changed since it was opened, and the single "
    "condition that takes you out. Advance the argument, do not restate the "
    "old text. Use ONLY the data given below; do not invent information. "
    "No hype, no price targets. Reply with plain text only."
)


def parse_ts(value: Any) -> Optional[datetime]:
    """ISO string -> aware UTC datetime, or None when unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        ts = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def is_due(row: dict, now: datetime, stale_hours: float) -> bool:
    """Whether one open thesis row is due for a restatement pass.

    Due when the write-up is older than the stale horizon, OR was never
    model-authored, OR its updated_at does not parse (fail toward
    refreshing — a row with a broken timestamp must not sit unrevised
    forever). Reference parity: their isStale() treats an unparseable
    timestamp as stale.
    """
    updated = parse_ts(row.get("updated_at"))
    if updated is None:
        return True
    age = now - updated
    if age > timedelta(hours=stale_hours):
        return True
    author = str(row.get("author") or "")
    if not author.startswith("model"):
        return True
    return False


def select_due(
    rows: list[dict], now: datetime, stale_hours: float, per_pass: int,
) -> list[dict]:
    """The due subset, oldest text first, capped at per_pass.

    Rows with an unparseable updated_at sort FIRST (most in need of a
    rewrite); the rest sort by age descending. Deterministic and pure so
    the selection is hand-testable.
    """
    due = [r for r in rows if is_due(r, now, stale_hours)]

    def sort_key(r: dict):
        ts = parse_ts(r.get("updated_at"))
        # None -> earliest possible, so broken timestamps lead the pass.
        return (0, datetime.min.replace(tzinfo=timezone.utc)) if ts is None \
            else (1, ts)

    due.sort(key=sort_key)
    return due[: max(per_pass, 0)]


def validate_restatement(text: Any) -> Optional[str]:
    """Cleaned rewrite, or None when the model output is rejected."""
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if len(cleaned) < MIN_RESTATEMENT_CHARS:
        return None
    if len(cleaned) > MAX_RESTATEMENT_CHARS:
        return None
    return cleaned


def position_numbers(trade: Any, price: Optional[float]) -> dict:
    """Position facts for the brief, derived from the journal row + the
    tick's own mark. Unrealized P&L reuses the house money-math
    (sizing.compute_unrealized_pnl) — never re-derived here.
    Every field is optional; missing facts are omitted from the brief."""
    pos: dict = {
        "size_usd": None, "entry_price_usd": None, "opened_at": None,
        "current_price_usd": None, "unrealized_usd": None,
        "unrealized_pct": None,
    }
    if trade is None:
        return pos
    size = getattr(trade, "position_size_usd", None)
    pos["size_usd"] = float(size) if size else None
    entry = getattr(trade, "entry_price_usd", None)
    pos["entry_price_usd"] = float(entry) if entry else None
    pos["opened_at"] = getattr(trade, "opened_at", None)
    if price is not None and price > 0:
        pos["current_price_usd"] = float(price)
        try:
            from sizing import compute_unrealized_pnl
            pnl_usd, pnl_pct = compute_unrealized_pnl(trade, float(price))
            pos["unrealized_usd"] = round(pnl_usd, 4)
            pos["unrealized_pct"] = round(pnl_pct, 2)
        except (ValueError, TypeError):
            pass   # unpriceable position -> mark shown without pnl
    return pos


def build_brief(row: dict, pos: dict) -> str:
    """The prompt body: the position's current numbers + the current text.
    Concise by construction (performance-discipline: prompt length costs
    real tokens on every call)."""
    lines = [f"name: {row.get('symbol')} ({row.get('mint_address')})"]
    bits = []
    if pos.get("size_usd") is not None:
        bits.append(f"${pos['size_usd']:.2f} open")
    if pos.get("entry_price_usd") is not None:
        bits.append(f"entry ${pos['entry_price_usd']:.8f}")
    if pos.get("current_price_usd") is not None:
        bits.append(f"current mark ${pos['current_price_usd']:.8f}")
    if pos.get("unrealized_usd") is not None:
        bits.append(
            f"unrealized ${pos['unrealized_usd']:+.2f} "
            f"({pos['unrealized_pct']:+.1f}%)"
        )
    lines.append(
        "position: " + (", ".join(bits) if bits else "(numbers unavailable this pass)")
    )
    if pos.get("opened_at"):
        lines.append(f"opened: {pos['opened_at']}")
    lines.append(f"current write-up: {row.get('thesis') or 'none on file'}")
    return "\n".join(lines)



async def _record_usage(conn, now_iso: str, mint: str, result) -> None:
    """REF-R2: every restatement call is accounted for, successes and
    degradations alike. Fail-soft: usage accounting never blocks the job."""
    try:
        await db.insert_llm_call_usage(
            conn,
            ts=now_iso,
            task="thesis_restate",
            provider=result.provider,
            model=result.model,
            status="success" if not result.degradation_reason else "error",
            tick_ts=now_iso,
            mint_address=mint,
            latency_ms=int(result.latency_ms),
            input_tokens=result.input_tokens,
            cache_hit_tokens=result.cache_hit_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
            estimated_cost_usd=result.estimated_cost_usd,
            is_peak_window=result.is_peak_window,
            degradation_reason=result.degradation_reason,
        )
    except Exception:
        log.warning("thesis_restate usage accounting failed (non-fatal)",
                    exc_info=True)


async def _restate_one(conn, llm, row: dict, trade: Any,
                       price_map: dict, now: datetime) -> Optional[dict]:
    """One row: prompt -> model -> validate -> guarded write -> journal.
    Returns a restatement record, or None when the rewrite was refused or
    skipped (always logged)."""
    symbol = row.get("symbol", "")
    mint = row.get("mint_address", "")
    now_iso = now.isoformat()

    brief = build_brief(row, position_numbers(trade, price_map.get(mint)))
    result = await llm.complete_json(
        task="thesis_restate",
        system_prompt=RESTATE_SYSTEM_PROMPT,
        user_prompt=brief,
        budget=main_max_tokens(),
        json_mode=False,
    )
    if result is not None:
        await _record_usage(conn, now_iso, mint, result)

    if result is None or result.degradation_reason:
        reason = (result.degradation_reason if result is not None
                  else "no response")
        log.info("thesis restatement REFUSED for %s: provider degraded (%s) "
                 "- old text kept", symbol, reason)
        return None

    text = validate_restatement(result.text)
    if text is None:
        log.info("thesis restatement REFUSED for %s: model output failed "
                 "validation (empty/short/oversized) - old text kept", symbol)
        return None

    author = f"model:{result.provider}:{result.model}"
    written = await db.update_thesis_text(conn, row["trade_id"], text, author)
    if written == 0:
        log.info("thesis restatement skipped for %s: row retired mid-pass "
                 "(write guard held)", symbol)
        return None

    try:
        await db.insert_event(
            conn, "did", now_iso, symbol=symbol, mint_address=mint,
            payload={"action": "thesis_restate",
                     "trade_id": row["trade_id"],
                     "model": result.model},
        )
    except Exception:
        log.warning("thesis restatement event journal failed (non-fatal)",
                    exc_info=True)
    log.info("thesis restatement: %s advanced by %s", symbol, author)
    return {"trade_id": row["trade_id"], "symbol": symbol, "mint": mint,
            "model": result.model, "before": row.get("thesis"), "after": text}


async def restate_theses(conn, positions: list,
                         price_map: Optional[dict] = None) -> list[dict]:
    """One restatement pass over the open book. Returns the restatement
    records (empty when nothing was due or the pass was skipped).

    Never raises: a failed pass costs nothing — the write-ups simply stay
    as they are until the next tick (reference: "Never throws").

    positions — the tick's own open-position rows (Trade-like), used only
    for the brief's numbers; price_map — the tick's own marks
    (mint -> price). Neither is fetched here.
    """
    try:
        return await _restate(conn, positions, price_map or {})
    except Exception:
        log.warning("thesis restatement pass aborted (non-fatal)",
                    exc_info=True)
        return []


async def _restate(conn, positions: list, price_map: dict) -> list[dict]:
    if config.DATA_BACKEND != "live":
        return []   # mock mode is deterministic — no LLM, no restatements
    rows = await db.get_open_theses(conn)
    now = datetime.now(timezone.utc)
    due = select_due(rows, now, config.THESIS_RESTATE_STALE_HOURS,
                     config.THESIS_RESTATE_PER_PASS)
    if not due:
        return []
    # Non-urgent LLM work (docs/08 §5): during DeepSeek peak windows the
    # pass is skipped entirely rather than paying 2x rates. Logged, never
    # silent; the rows stay due and the next off-peak tick picks them up.
    if config.MAIN_LLM_PROVIDER == "deepseek" and _is_peak_window():
        log.info("thesis restatement skipped: deepseek peak window "
                 "(%d row(s) due)", len(due))
        return []

    by_mint = {getattr(t, "mint_address", ""): t for t in positions}
    llm = build_main_client()
    out: list[dict] = []
    try:
        for row in due:
            try:
                rec = await _restate_one(
                    conn, llm, row, by_mint.get(row.get("mint_address")),
                    price_map, now)
            except Exception:
                log.warning("thesis restatement failed for %s (row skipped)",
                            row.get("symbol"), exc_info=True)
                rec = None
            if rec:
                out.append(rec)
    finally:
        await llm.aclose()
    return out

