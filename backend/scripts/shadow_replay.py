"""
Shadow Replay script for model regression testing (handoff §18 gate).

Re-runs the CURRENT main thinker (MAIN_LLM_PROVIDER) against sealed
historical candidate snapshots and compares its verdict with the original
thinker verdict recorded in the decision commit.

Data sources (both via the db repository layer — SQLite AND Supabase):
  * feed_events.candidate_snapshot — the exact tape the original thinker saw
  * decision_commits.think_verdict — the original model verdict

Reports verdict agreement, degradation count, and latency percentiles;
token/cost accounting lands in llm_call_usage automatically.
"""
import asyncio
import logging
from datetime import datetime

import click

from api.db import get_db, get_feed_events, get_recent_decision_commits
from models import Candidate
from llm.thinker import Thinker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def _parse_ts(ts):
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


async def run_shadow_replay(limit: int):
    thinker = Thinker()
    print(f"Replay thinker provider: {thinker._main_llm.provider}:{thinker._main_llm.model}")
    try:
        async with get_db() as conn:
            commits = await get_recent_decision_commits(conn, limit=limit)
            events = await get_feed_events(conn, limit=max(limit * 4, 100))

        if not commits:
            print("No decision commits found.")
            return

        # mint -> feed events (newest first); each carries the sealed
        # candidate snapshot the original thinker actually saw.
        snaps_by_mint: dict[str, list] = {}
        for e in events:
            snaps_by_mint.setdefault(e["mint_address"], []).append(e)

        matches = mismatches = degraded = skipped = 0
        latencies: list[float] = []

        for row in commits:
            commit_id, symbol, mint = row["id"], row["symbol"], row["mint"]
            original = row["think_verdict"]
            cands = snaps_by_mint.get(mint) or []
            if not cands:
                skipped += 1
                log.warning("Commit %s (%s): no feed snapshot for mint — skipped.",
                            commit_id, symbol)
                continue
            # Pick the snapshot closest to the commit timestamp (same tick).
            ev = cands[0]
            commit_ts = _parse_ts(row["tick_ts"])
            if commit_ts is not None:
                best = None
                for e in cands:
                    ets = _parse_ts(e["ts"])
                    d = abs((ets - commit_ts).total_seconds()) if ets else float("inf")
                    if best is None or d < best[0]:
                        best = (d, e)
                ev = best[1]

            try:
                c = Candidate(**ev["candidate_snapshot"])
            except TypeError as exc:
                skipped += 1
                log.warning("Commit %s (%s): snapshot schema drift: %s",
                            commit_id, symbol, exc)
                continue

            print(f"Replaying {symbol} (commit {commit_id})...")
            new_think = await thinker.think(c)
            if new_think.llm_usage is not None:
                latencies.append(new_think.llm_usage.latency_ms)
            if new_think.source.startswith("degraded:"):
                degraded += 1
                print(f"  [DEGRADED] {new_think.source}")
                continue
            if new_think.verdict == original:
                matches += 1
                print(f"  [MATCH] original={original} new={new_think.verdict} ({new_think.source})")
            else:
                mismatches += 1
                print(f"  [MISMATCH] original={original} new={new_think.verdict} ({new_think.source})")
                print(f"  New thesis: {new_think.thesis}")

        total = matches + mismatches
        print(f"\nReplay complete: {matches}/{total} verdict agreement, "
              f"{degraded} degraded, {skipped} skipped.")
        if latencies:
            lat = sorted(latencies)
            p95 = lat[max(0, int(len(lat) * 0.95) - 1)]
            print(f"Latency: min {lat[0]:.0f}ms  p95 {p95:.0f}ms  max {lat[-1]:.0f}ms")
    finally:
        await thinker.aclose()


@click.command()
@click.option("--limit", default=10, help="Number of recent decisions to replay.")
def main(limit):
    asyncio.run(run_shadow_replay(limit))


if __name__ == "__main__":
    main()
