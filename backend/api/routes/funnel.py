"""
api/routes/funnel.py — GET /api/funnel: the §57 refusal funnel (§63).

The selectivity numbers behind model_refusal_rate_of_gate_passers,
computed server-side with the EXACT classification scripts/perf_report.py
uses, so the UI funnel and the operator report reconcile line-by-line
(§63 verification requirement). Read-only and ungated like /api/feed:
no wallet or position data crosses it.

Funnel stages (the most recent `limit` feed events are the window):
  candidates_seen  every feed event in the window (candidate evaluated)
  gate_refused     verdict=fail with non-empty failed_rule_ids
  model_refused    verdict=fail with EMPTY failed_rule_ids — gate passed
                   and the MODEL declined (the reference decline layer)
  gate_passed      model_refused + pass rows
  model_approved   pass rows (think+gate intersection said buy)
  filled           sealed commits in the window that bound to a fill
                   (commit signature present = fill tx recorded)
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Query

from api import db

router = APIRouter()


def _failed_ids(row: dict) -> list:
    failed = row.get("failed_rule_ids") or []
    if isinstance(failed, str):
        try:
            failed = json.loads(failed)
        except ValueError:
            failed = []
    return failed


@router.get("/api/funnel")
async def get_funnel(limit: int = Query(1000, ge=1, le=10000)):
    async with db.get_db() as conn:
        total = await db.count_feed_events(conn)
        feed = await db.get_feed_events(conn, limit=limit)
        commits = await db.get_recent_decision_commits(conn, limit=limit)

    gate_refused = 0
    model_refused = 0
    for row in feed:
        if str(row.get("verdict")) != "fail":
            continue
        if _failed_ids(row):
            gate_refused += 1
        else:
            model_refused += 1

    model_approved = sum(1 for row in feed if str(row.get("verdict")) == "pass")
    gate_passed = model_refused + model_approved
    filled = sum(1 for c in commits if c.get("signature"))

    return {
        "window": {"limit": limit, "feed_events": len(feed)},
        "candidates_seen_total": total,
        "candidates_seen": len(feed),
        "gate_refused": gate_refused,
        "model_refused": model_refused,
        "gate_passed": gate_passed,
        "model_approved": model_approved,
        "filled": filled,
        "model_refusal_rate_of_gate_passers": (
            round(model_refused / gate_passed, 3) if gate_passed else None
        ),
        "note": ("classification identical to scripts/perf_report.py §57: "
                 "verdict=fail with empty failed_rule_ids = a MODEL refusal "
                 "among gate-passers; filled counts windowed commits whose "
                 "fill signature is present"),
    }
