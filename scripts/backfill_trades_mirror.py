#!/usr/bin/env python3
"""
scripts/backfill_trades_mirror.py — §57: one-shot ledger -> trades mirror
backfill (the §56-deferred operator-gated item).

WHY: the shared `trades` table is what stats, calibration, the learning
loop, reflections and /api/promotion-gate read — but the live book's 26
closes all predate the §52 mirror (Sept 3), so those consumers are blind
to the real closed-trade history (the promotion gate currently reports
"0 / 40 closed trades" against a book that has 25 closed basis-paired
trades). The ExecutionLedger stays the money authority — this is the same
one-way, idempotent mirror `_mirror_live_close` writes for NEW closes.

WHAT: pairs each ledger close with the buy(s) it closed (same mint,
opened after that mint's previous close — the exact honest-basis logic of
perf_report.ledger_stats), builds a CLOSED Trade row per pair, and inserts
them idempotently (INSERT OR IGNORE / ON CONFLICT DO NOTHING keyed by
trade_id). Re-running is always safe: already-present rows are skipped and
counted. OPEN positions are NOT touched — `_mirror_live_trades` owns those
for the running cycle.

SAFETY: DRY-RUN BY DEFAULT — prints the rows it would write and exits
without touching the DB. `--apply` performs the inserts. Never mutates the
ledger; never deletes or updates an existing trades row.

Usage (from backend/, or anywhere — self-bootstrapping path):
    python ../scripts/backfill_trades_mirror.py            # dry run
    python ../scripts/backfill_trades_mirror.py --apply    # write
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

LEDGER_PATH = BACKEND / "live_execution" / "state" / "executions.json"


def closed_trade_rows(records: list[dict]) -> tuple[list[dict], int]:
    """Pair closes with their basis buys; emit insertable row dicts.

    Honest denominators, exactly as perf_report.ledger_stats(): a close's
    basis is the summed cost of buys of the same mint OPENED after the
    mint's previous close (and at/before this one). Closes with no basis
    (trim chains / repair rows whose basis is genuinely unknowable) are
    counted separately and skipped — never a fabricated % row.
    Returns (rows, skipped_no_basis).
    """
    def ts(r: dict) -> float:
        return float(r.get("ts") or 0.0)

    rows = sorted(records, key=ts)
    buys_by_mint: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("kind") == "buy":
            buys_by_mint.setdefault(r.get("mint"), []).append(r)

    out: list[dict] = []
    skipped_no_basis = 0
    last_close_ts: dict[str, float] = {}
    for r in rows:
        if r.get("kind") != "close" or r.get("pnl_usd") is None:
            continue
        mint = r.get("mint")
        lo = last_close_ts.get(mint, 0.0)
        basis_buys = [b for b in buys_by_mint.get(mint, [])
                      if lo < ts(b) <= ts(r)]
        last_close_ts[mint] = ts(r)
        basis = sum(float(b.get("usd_size") or 0.0) for b in basis_buys)
        if basis < 0.01 or not basis_buys:
            skipped_no_basis += 1
            continue
        first_buy = min(basis_buys, key=ts)
        pnl = float(r["pnl_usd"])
        entry_price = float(first_buy.get("price_usd") or 0.0)
        qty = sum(float(b.get("tokens_out") or 0.0) for b in basis_buys)
        exit_price = float(r.get("price_usd") or 0.0)
        closed_at = dt.datetime.fromtimestamp(ts(r), tz=dt.timezone.utc)
        opened_at = dt.datetime.fromtimestamp(ts(first_buy),
                                              tz=dt.timezone.utc)
        out.append({
            "trade_id": f"ledger-{mint[:8]}-{int(ts(r))}",
            "symbol": str(r.get("symbol") or first_buy.get("symbol")
                          or mint[:6]),
            "mint_address": mint,
            "opened_at": opened_at.isoformat(),
            "entry_price_usd": entry_price,
            "position_size_usd": basis,
            "quantity": qty,
            "candidate_snapshot": {"backfilled": True,
                                   "source": "execution-ledger"},
            "thesis": "ledger backfill (§57 — pre-§52 close, no thesis row)",
            "closed_at": closed_at.isoformat(),
            "exit_price_usd": exit_price,
            "exit_reason": str(r.get("rule_id") or "unknown(pre-§50)"),
            "realized_pnl_usd": pnl,
            "realized_pnl_pct": (pnl / basis) * 100.0,
            "high_water_usd": None,
        })
    return out, skipped_no_basis


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="ledger -> trades mirror backfill (§57; dry-run by "
                    "default)")
    parser.add_argument("--apply", action="store_true",
                        help="actually write the rows (default: dry run)")
    args = parser.parse_args()

    if not LEDGER_PATH.is_file():
        print(f"no ledger at {LEDGER_PATH} — nothing to backfill")
        return 0
    records = json.loads(LEDGER_PATH.read_text()).get("records", [])
    rows, skipped = closed_trade_rows(records)
    print(f"ledger records: {len(records)}")
    print(f"closed basis-paired trades to mirror: {len(rows)} "
          f"(skipped no-basis/trim rows: {skipped})")

    if not rows:
        print("nothing to backfill")
        return 0

    from models import Trade
    from api import db

    trades = [Trade(**r, is_open=False) for r in rows]
    wins = sum(1 for t in trades if (t.realized_pnl_usd or 0) > 0)
    print(f"  wins {wins} / {len(trades)} "
          f"({wins / len(trades):.1%} hit rate) — this is what the "
          f"promotion gate will now see")

    if not args.apply:
        print()
        print("DRY RUN — no rows written. Re-run with --apply to write them.")
        for t in trades:
            print(f"  would insert {t.trade_id}  {t.symbol:<10} "
                  f"pnl ${t.realized_pnl_usd:+.4f} "
                  f"({t.realized_pnl_pct:+.1f}%)  {t.exit_reason}")
        return 0

    written = 0
    await db.init_db()
    async with db.get_db() as conn:
        for t in trades:
            n = await db.insert_closed_trade_row(conn, t)
            written += n
            if n:
                print(f"  inserted {t.trade_id}  {t.symbol:<10} "
                      f"pnl ${t.realized_pnl_usd:+.4f}")
    print(f"\nAPPLIED: {written} row(s) inserted "
          f"({len(trades) - written} already present — idempotent no-ops)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

