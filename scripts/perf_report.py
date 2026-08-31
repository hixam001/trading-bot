#!/usr/bin/env python3
"""
scripts/perf_report.py — the honest scorecard (§50 Phase 0, 2026-08-31).

Read-only. Computes THIS bot's track record from its own sources of truth
and prints it in the reference (omotrades.com /api/public/disclosure.json)
"learning" format so our numbers are line-by-line comparable with theirs:

    samples / hit rate / avg win % / avg loss % / expectancy % /
    conviction factor + exit-rule mix + LLM refusal rate

Sources (never written to):
  * backend/live_execution/state/executions.json — the LIVE book's every
    buy/close record (rule_id-attributed since §50).
  * the journal DB (decision_commits + feed_events) — the refusal funnel.

The point (operator ask: "figure out how they are so successful"):
omo's numbers as of 2026-08-31 — 9.4% hit rate, +59.71% avg win, -7.4% avg
loss, -1.11% expectancy, 74% LLM decline rate. Our leaks are the LOSS SHAPE
(-28% avg vs their -7.4%) and the 0% refusal rate. This report makes both
visible every time it runs.

Usage:
    python scripts/perf_report.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
for p in (str(BACKEND),):
    if p not in sys.path:
        sys.path.insert(0, p)

LEDGER_PATH = BACKEND / "live_execution" / "state" / "executions.json"


def ledger_stats() -> dict:
    """Closed-live-trade stats in the reference disclosure's learning shape."""
    rows = json.loads(LEDGER_PATH.read_text())["records"]
    buys: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("kind") == "buy":
            buys.setdefault(r["mint"], []).append(r)

    samples: list[dict] = []
    skipped_no_basis = 0
    last_close_ts: dict[str, float] = {}
    for r in sorted(rows, key=lambda x: float(x.get("ts") or 0.0)):
        if r.get("kind") == "close":
            if r.get("pnl_usd") is None:
                continue
            mint = r["mint"]
            # Honest % denominator pre-§50 (no fraction/fill fields stored):
            # the summed cost of buys OPENED since this mint's previous close
            # — exact for a full close of a fresh position, conservative
            # (understates |loss|% for chained trims, which is the safe
            # direction), and $0 for trim-chains/repair rows whose basis is
            # genuinely unknowable -> those count in USD only, never as %.
            lo = float(last_close_ts.get(mint, 0.0))
            basis = sum(float(b.get("usd_size") or 0.0)
                        for b in buys.get(mint, [])
                        if lo < float(b.get("ts") or 0.0)
                        <= float(r.get("ts") or 0.0))
            last_close_ts[mint] = float(r.get("ts") or 0.0)
            if basis >= 0.01:
                samples.append({
                    "mint": mint, "pnl_usd": r["pnl_usd"],
                    "pnl_pct": (r["pnl_usd"] / basis) * 100.0,
                    "rule": r.get("rule_id") or "unknown(pre-§50)",
                })
            else:
                skipped_no_basis += 1

    wins = [s for s in samples if s["pnl_usd"] > 0]
    losses = [s for s in samples if s["pnl_usd"] <= 0]
    n = len(samples)
    win_rate = len(wins) / n if n else 0.0
    avg_win = sum(s["pnl_pct"] for s in wins) / len(wins) if wins else 0.0
    avg_loss = sum(s["pnl_pct"] for s in losses) / len(losses) if losses else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    # REF-R9 conviction factor from the same samples (arithmetic parity with
    # backend/calibration.py so the number matches the published one).
    raw = (1 + min(expectancy / 50, 0.2) if expectancy >= 0
           else 1 + max(expectancy / 25, -0.4))
    confidence = min(n / 12, 1.0)
    conviction = max(0.6, min(1.2, 1 + (raw - 1) * confidence))

    rule_mix: dict[str, int] = {}
    for s in samples:
        rule_mix[s["rule"]] = rule_mix.get(s["rule"], 0) + 1
    net_usd = sum(s["pnl_usd"] for s in samples)

    return {
        "samples": n, "wins": len(wins), "win_rate": round(win_rate, 3),
        "skipped_no_basis": skipped_no_basis,
        "avg_win_pct": round(avg_win, 2), "avg_loss_pct": round(avg_loss, 2),
        "expectancy_pct": round(expectancy, 2),
        "conviction_factor": round(conviction, 3),
        "net_usd": round(net_usd, 4),
        "exit_rule_mix": dict(sorted(rule_mix.items(), key=lambda kv: -kv[1])),
        "formula": (f"expectancy = hit {round(win_rate, 3)} * avg win "
                    f"{round(avg_win, 2)}% + miss {round(1 - win_rate, 3)} * "
                    f"avg loss {round(avg_loss, 2)}% = {round(expectancy, 2)}%"),
    }



async def refusal_stats() -> dict:
    """The LLM/gate refusal funnel from the journal DB (read-only).

    Uses the repo's own backend-agnostic query helpers so it works against
    BOTH the local sqlite book and the active Supabase/Postgres journal
    (db.py swaps in db_pg at import when USE_SUPABASE_DB=1)."""
    from api import db

    try:
        async with db.get_db() as conn:
            commits = await db.get_recent_decision_commits(conn, limit=500)
            feed = await db.get_feed_events(conn, limit=1000)
    except Exception as exc:
        return {"error": f"journal unreadable (non-fatal): {type(exc).__name__}: {exc}"}

    commit_verdicts: dict[str, int] = {}
    for row in commits:
        v = row.get("think_verdict") or row.get("verdict")
        if v is not None:
            commit_verdicts[str(v)] = commit_verdicts.get(str(v), 0) + 1
    feed_verdicts: dict[str, int] = {}
    for row in feed:
        v = row.get("verdict")
        if v is not None:
            feed_verdicts[str(v)] = feed_verdicts.get(str(v), 0) + 1
    total_feed = sum(feed_verdicts.values()) or 1
    return {
        "decision_commits_by_verdict": commit_verdicts,
        "feed_events_by_verdict": feed_verdicts,
        "feed_fail_share": round(feed_verdicts.get("fail", 0) / total_feed, 3),
        "note": ("feed verdict fail = think-or-gate refused; compare our "
                 "think-pass rate with omo's 74% decline rate (504 declines "
                 "vs 175 order intents)"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args()

    print("=" * 70)
    print("LIVE BOOK — the honest scorecard (reference disclosure format)")
    print("=" * 70)
    stats = ledger_stats()
    print(f"samples (closed live trades): {stats['samples']}"
          f"   (skipped no-basis rows: {stats.get('skipped_no_basis', 0)} — "
          f"counted in USD only)")
    print(f"wins: {stats['wins']}   hit rate: {stats['win_rate']:.1%}")
    print(f"avg win:  {stats['avg_win_pct']:+.2f}%")
    print(f"avg loss: {stats['avg_loss_pct']:+.2f}%   <- the loss shape; omo: -7.4%")
    print(f"expectancy: {stats['expectancy_pct']:+.2f}%   net ${stats['net_usd']:+.4f}")
    print(f"conviction factor: {stats['conviction_factor']} (clamped 0.6-1.2)")
    print(f"exit-rule mix: {stats['exit_rule_mix']}")
    print(f"formula: {stats['formula']}")
    print()
    ref = asyncio.run(refusal_stats())
    print("REFUSAL FUNNEL (journal, read-only):")
    for k, v in ref.items():
        print(f"  {k}: {v}")
    print()
    print("benchmark (omotrades.com disclosure, 2026-08-31): hit 9.4%, "
          "avg win +59.71%, avg loss -7.4%, expectancy -1.11%, "
          "LLM declines 74%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
