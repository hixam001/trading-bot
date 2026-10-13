#!/usr/bin/env python3
"""
scripts/gate_funnel_report.py — Read-only diagnostic of the gate & thinker refusal funnel (§79 Phase G).

Answers why the bot has had zero gate-passers / closed trades since §57.
Reads verbatim feed_events (rule_breakdown and failed_rule_ids) and trades.

Usage:
    python scripts/gate_funnel_report.py --since YYYY-MM-DD [--db path] [--json]

Output:
    - Daily candidate funnel: candidates seen, gate-passed, think-passed, entered
    - Per-rule fail rate & sole-blocker count
    - Counterfactual analysis: "if rule X were removed, N candidates would have passed"
    - Top 3 blocking rules: distribution (min, median, p90) of the failing metric vs threshold
    - Thinker / LLM refusal rate among gate-passers and verdict mix
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _parse_json(val: Any) -> Any:
    if val is None:
        return []
    if isinstance(val, (list, dict)):
        return val
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return []
        try:
            return json.loads(val)
        except Exception:
            return []
    return []


def _extract_date(ts_val: Any) -> str | None:
    if not ts_val:
        return None
    if isinstance(ts_val, (int, float)):
        try:
            return datetime.fromtimestamp(ts_val, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return None
    s = str(ts_val).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n == 1:
        return s[0]
    k = (n - 1) * p
    f = int(k)
    c = min(f + 1, n - 1)
    d = k - f
    return s[f] + (s[c] - s[f]) * d


def _calc_distribution(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    s = sorted(values)
    return {
        "min": round(s[0], 4),
        "median": round(statistics.median(s), 4),
        "p90": round(_percentile(s, 0.90), 4),
        "count": len(s),
    }


def _extract_threshold(rule_id: str, sample_detail: str | None) -> str:
    if sample_detail:
        m = re.search(r"(?:vs floor|vs min|act band|vs intended size)\s+([^—,]+)", sample_detail)
        if m:
            return m.group(0).strip()
    defaults = {
        "liquidity_floor": "floor $15,000",
        "volume_alive": "min $8,000",
        "buy_pressure": "buys > sells (delta > 0)",
        "crowd_heat": "act band [36, 100]",
        "not_newborn_fade": "age >= 24h or change > -15%",
        "cash_available": "cash >= intended size",
        "security_clear": "no known-bad signals",
        "public_presence": ">= 1 confirmed channel",
        "market_regime_ok": "regime_ok == True",
        "already_held": "no open position in mint",
        "not_on_break": "no operator break",
    }
    return defaults.get(rule_id, "unknown threshold")


def fetch_data_sqlite(db_path: str | Path) -> tuple[list[dict], list[dict]]:
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='feed_events'")
        if not cur.fetchone():
            return [], []
        feed_rows = [dict(r) for r in conn.execute("SELECT * FROM feed_events ORDER BY id ASC").fetchall()]
        
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades'")
        trade_rows = []
        if cur.fetchone():
            trade_rows = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY opened_at ASC").fetchall()]
        return feed_rows, trade_rows
    finally:
        conn.close()


async def fetch_data_pg() -> tuple[list[dict], list[dict]]:
    from api import db_pg
    async with db_pg.get_db() as conn:
        feed_records = await conn.fetch("SELECT * FROM feed_events ORDER BY id ASC")
        feed_rows = [dict(r) for r in feed_records]
        try:
            trade_records = await conn.fetch("SELECT * FROM trades ORDER BY opened_at ASC")
            trade_rows = [dict(r) for r in trade_records]
        except Exception:
            trade_rows = []
        return feed_rows, trade_rows


def load_rows(db_path: str | Path | None = None) -> tuple[list[dict], list[dict], str]:
    """Load feed_events and trades rows. Returns (feed_rows, trade_rows, source_desc)."""
    if db_path is not None:
        p = Path(db_path)
        if not p.is_file():
            return [], [], f"sqlite:{p} (file not found)"
        return *fetch_data_sqlite(p), f"sqlite:{p}"

    # Try Postgres if configured
    use_pg = os.environ.get("USE_SUPABASE_DB") == "1"
    if use_pg:
        try:
            feed, trades = asyncio.run(fetch_data_pg())
            return feed, trades, "postgres:supabase"
        except Exception as exc:
            # Fall back to SQLite if offline
            pass

    # Default local SQLite
    default_db = BACKEND / "trading_bot.db"
    if not default_db.is_file():
        # Check config
        try:
            import config
            default_db = getattr(config, "DB_PATH", default_db)
        except Exception:
            pass
    if Path(default_db).is_file():
        return *fetch_data_sqlite(default_db), f"sqlite:{default_db}"

    return [], [], "none"


def generate_funnel_report(
    db_path: str | Path | None = None,
    since: str | None = None,
) -> dict:
    """Generate diagnostic metrics from feed_events and trades."""
    feed_rows, trade_rows, source = load_rows(db_path)

    # Filter by date if requested
    if since:
        since_date = since.strip()[:10]
        feed_rows = [r for r in feed_rows if (_extract_date(r.get("ts")) or "") >= since_date]
        trade_rows = [t for t in trade_rows if (_extract_date(t.get("opened_at")) or "") >= since_date]

    total_candidates = len(feed_rows)

    # 1. Daily Funnel Counts
    daily_groups: dict[str, list[dict]] = {}
    for r in feed_rows:
        d = _extract_date(r.get("ts")) or "unknown"
        daily_groups.setdefault(d, []).append(r)

    # Index trades by opened date for cross-check
    trades_by_date: dict[str, int] = {}
    for t in trade_rows:
        td = _extract_date(t.get("opened_at")) or "unknown"
        trades_by_date[td] = trades_by_date.get(td, 0) + 1

    daily_funnel: list[dict] = []
    for day in sorted(daily_groups.keys()):
        rows = daily_groups[day]
        seen = len(rows)
        # Gate passed = failed_rule_ids is empty
        gate_passed = sum(1 for r in rows if len(_parse_json(r.get("failed_rule_ids"))) == 0)
        # Think passed = verdict is 'pass'
        think_passed = sum(1 for r in rows if str(r.get("verdict")).lower() == "pass")
        # Entered = led_to_trade_id present or trades recorded on that day
        entered_from_feed = sum(1 for r in rows if r.get("led_to_trade_id"))
        entered_trades = trades_by_date.get(day, 0)
        entered = max(entered_from_feed, entered_trades)

        daily_funnel.append({
            "date": day,
            "candidates_seen": seen,
            "gate_passed": gate_passed,
            "think_passed": think_passed,
            "entered": entered,
            "gate_pass_rate": round(gate_passed / seen, 4) if seen else 0.0,
        })

    # Overall baseline counts
    baseline_gate_passers = sum(1 for r in feed_rows if len(_parse_json(r.get("failed_rule_ids"))) == 0)
    baseline_think_passers = sum(1 for r in feed_rows if str(r.get("verdict")).lower() == "pass")
    baseline_entered = max(
        sum(1 for r in feed_rows if r.get("led_to_trade_id")),
        len(trade_rows),
    )

    # 2. Per-rule failure rates & sole blockers
    rule_fail_counts: dict[str, int] = {}
    rule_sole_blockers: dict[str, int] = {}
    rule_sample_details: dict[str, str] = {}
    rule_failing_values: dict[str, list[float]] = {}
    all_known_rules: set[str] = set()

    for r in feed_rows:
        failed = set(_parse_json(r.get("failed_rule_ids")))
        breakdown = _parse_json(r.get("rule_breakdown"))

        # Also harvest all rule_ids evaluated from breakdown
        for b in breakdown:
            if isinstance(b, dict) and "rule_id" in b:
                all_known_rules.add(b["rule_id"])

        all_known_rules.update(failed)

        for rule in failed:
            rule_fail_counts[rule] = rule_fail_counts.get(rule, 0) + 1

        # Check for sole blocker: failed EXACTLY one rule
        if len(failed) == 1:
            only_rule = next(iter(failed))
            rule_sole_blockers[only_rule] = rule_sole_blockers.get(only_rule, 0) + 1

        # Collect failing values and details for distribution analysis
        for b in breakdown:
            if isinstance(b, dict):
                rid = b.get("rule_id")
                if rid in failed:
                    val = b.get("value")
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        rule_failing_values.setdefault(rid, []).append(float(val))
                    if rid not in rule_sample_details and b.get("detail"):
                        rule_sample_details[rid] = str(b.get("detail"))

    per_rule_stats: list[dict] = []
    counterfactuals: dict[str, dict] = {}

    for rule in sorted(all_known_rules):
        fails = rule_fail_counts.get(rule, 0)
        sole = rule_sole_blockers.get(rule, 0)
        fail_rate = round(fails / total_candidates, 4) if total_candidates else 0.0
        sole_rate = round(sole / total_candidates, 4) if total_candidates else 0.0

        # Counterfactual: if rule removed, how many total candidates pass?
        # A candidate passes without rule if its failed set is either empty or {rule}
        would_pass_total = baseline_gate_passers + sole
        would_pass_rate = round(would_pass_total / total_candidates, 4) if total_candidates else 0.0

        per_rule_stats.append({
            "rule_id": rule,
            "fail_count": fails,
            "fail_rate": fail_rate,
            "sole_blocker_count": sole,
            "sole_blocker_rate": sole_rate,
        })

        counterfactuals[rule] = {
            "total_passed_without_rule": would_pass_total,
            "incremental_gain": sole,
            "pass_rate_without_rule": would_pass_rate,
            "description": f"if {rule} were removed, {would_pass_total} candidates would have passed the gate (+{sole} new)",
        }

    # Sort per-rule stats by fail_count descending, then sole_blocker_count descending
    per_rule_stats.sort(key=lambda x: (x["fail_count"], x["sole_blocker_count"]), reverse=True)

    # 3. Distribution of the failing metric for the top 3 blocking rules
    top_3_rules = [r["rule_id"] for r in per_rule_stats[:3]]
    top_3_distributions: list[dict] = []

    for rule in top_3_rules:
        vals = rule_failing_values.get(rule, [])
        threshold_desc = _extract_threshold(rule, rule_sample_details.get(rule))
        dist = _calc_distribution(vals)
        top_3_distributions.append({
            "rule_id": rule,
            "threshold": threshold_desc,
            "distribution": dist,
            "sample_detail": rule_sample_details.get(rule),
        })

    # 4. LLM refusal rate among gate-passers & verdict mix
    model_refused = 0
    model_approved = 0
    verdict_counts: dict[str, int] = {}

    for r in feed_rows:
        v = str(r.get("verdict") or "unknown").lower()
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
        failed = _parse_json(r.get("failed_rule_ids"))
        if len(failed) == 0:
            if v == "pass":
                model_approved += 1
            elif v == "fail":
                model_refused += 1

    llm_refusal_rate = (
        round(model_refused / baseline_gate_passers, 4)
        if baseline_gate_passers > 0
        else None
    )

    verdict_mix = {
        k: {
            "count": cnt,
            "percentage": round(cnt / total_candidates, 4) if total_candidates else 0.0,
        }
        for k, cnt in verdict_counts.items()
    }

    return {
        "source": source,
        "since": since,
        "total_candidates": total_candidates,
        "baseline_summary": {
            "candidates_seen": total_candidates,
            "gate_passed": baseline_gate_passers,
            "think_passed": baseline_think_passers,
            "entered": baseline_entered,
            "gate_pass_rate": round(baseline_gate_passers / total_candidates, 4) if total_candidates else 0.0,
        },
        "daily_funnel": daily_funnel,
        "per_rule_stats": per_rule_stats,
        "counterfactuals": counterfactuals,
        "top_blocking_rules_distribution": top_3_distributions,
        "llm_funnel": {
            "gate_passers": baseline_gate_passers,
            "model_approved": model_approved,
            "model_refused": model_refused,
            "llm_refusal_rate_of_gate_passers": llm_refusal_rate,
        },
        "verdict_mix": verdict_mix,
    }


def format_text_report(report: dict) -> str:
    lines = [
        "=" * 80,
        "GATE FUNNEL DIAGNOSTIC REPORT (§79 Phase G)",
        f"Data source: {report['source']}",
        f"Window: since {report['since'] or 'all-time'}",
        f"Total candidates evaluated: {report['total_candidates']}",
        "=" * 80,
        "",
        "1. DAILY FUNNEL COUNTS",
        "-" * 80,
        f"{'Date':<12} {'Seen':>8} {'Gate-Passed':>12} {'Think-Passed':>13} {'Entered':>9} {'Gate Pass %':>12}",
    ]
    if not report["daily_funnel"]:
        lines.append("  (no candidate events in window)")
    else:
        for d in report["daily_funnel"]:
            pct_str = f"{d['gate_pass_rate'] * 100:.1f}%"
            lines.append(
                f"{d['date']:<12} {d['candidates_seen']:>8} {d['gate_passed']:>12} "
                f"{d['think_passed']:>13} {d['entered']:>9} {pct_str:>12}"
            )

    lines.extend([
        "",
        "2. PER-RULE FAILURE RATES & SOLE BLOCKERS",
        "-" * 80,
        f"{'Rule ID':<25} {'Failed':>8} {'Fail Rate':>11} {'Sole Blocker':>14} {'Sole Rate':>11}",
    ])
    if not report["per_rule_stats"]:
        lines.append("  (no rules recorded)")
    else:
        for r in report["per_rule_stats"]:
            fr_str = f"{r['fail_rate'] * 100:.1f}%"
            sr_str = f"{r['sole_blocker_rate'] * 100:.1f}%"
            lines.append(
                f"{r['rule_id']:<25} {r['fail_count']:>8} {fr_str:>11} {r['sole_blocker_count']:>14} {sr_str:>11}"
            )

    lines.extend([
        "",
        "3. COUNTERFACTUAL ANALYSIS ('What if rule X were removed?')",
        "-" * 80,
        f"Current baseline gate-passers: {report['baseline_summary']['gate_passed']} "
        f"({report['baseline_summary']['gate_pass_rate'] * 100:.1f}%)",
        f"{'Rule ID':<25} {'Passers Without Rule':>21} {'Incremental Gain':>18} {'New Pass Rate':>14}",
    ])
    if not report["per_rule_stats"]:
        lines.append("  (no counterfactual data)")
    else:
        for r in report["per_rule_stats"]:
            rid = r["rule_id"]
            cf = report["counterfactuals"][rid]
            n_pass = cf["total_passed_without_rule"]
            gain = cf["incremental_gain"]
            pr_str = f"{cf['pass_rate_without_rule'] * 100:.1f}%"
            lines.append(f"{rid:<25} {n_pass:>21} {f'+{gain}':>18} {pr_str:>14}")

    lines.extend([
        "",
        "4. TOP BLOCKING RULES: FAILING METRIC DISTRIBUTION",
        "-" * 80,
    ])
    if not report["top_blocking_rules_distribution"]:
        lines.append("  (no failure distributions to report)")
    else:
        for idx, item in enumerate(report["top_blocking_rules_distribution"], 1):
            rid = item["rule_id"]
            thresh = item["threshold"]
            dist = item["distribution"]
            lines.append(f"[{idx}] {rid} (Threshold: {thresh})")
            if not dist:
                lines.append("    Metric values: non-numeric or unavailable")
            else:
                lines.append(
                    f"    Metric values ({dist['count']} samples): "
                    f"min={dist['min']:,.2f}, median={dist['median']:,.2f}, p90={dist['p90']:,.2f}"
                )
            if item.get("sample_detail"):
                lines.append(f"    Sample breakdown: {item['sample_detail']}")

    refusal_rate_val = report["llm_funnel"]["llm_refusal_rate_of_gate_passers"]
    refusal_rate_str = f"{refusal_rate_val * 100:.1f}%" if refusal_rate_val is not None else "N/A"
    lines.extend([
        "",
        "5. THINKER / LLM REFUSAL FUNNEL & VERDICT MIX",
        "-" * 80,
        f"Gate-passers: {report['llm_funnel']['gate_passers']}",
        f"  - LLM Approved (think-passed): {report['llm_funnel']['model_approved']}",
        f"  - LLM Refused (model declined): {report['llm_funnel']['model_refused']}",
        f"  - LLM Refusal Rate: {refusal_rate_str}",
        "",
        "Feed Verdict Mix:",
    ])
    for v, info in report["verdict_mix"].items():
        lines.append(f"  - {v}: {info['count']} ({info['percentage'] * 100:.1f}%)")

    lines.append("=" * 80)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Gate Funnel Diagnostic Report (§79 Phase G) — read-only analysis of candidate attrition."
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Filter candidates on or after YYYY-MM-DD (e.g. 2026-09-01).",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database file (defaults to config.DB_PATH or Supabase).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of human-readable text.",
    )
    args = parser.parse_args()

    report = generate_funnel_report(db_path=args.db, since=args.since)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_text_report(report))


if __name__ == "__main__":
    main()
