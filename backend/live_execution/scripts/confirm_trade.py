"""
live_execution/scripts/confirm_trade.py — operator CLI for the human side of
the safety model. Run from backend/ (the package is inside the deployable
module now):

    cd backend
    python -m live_execution.scripts.confirm_trade list
    python -m live_execution.scripts.confirm_trade approve <id>
    python -m live_execution.scripts.confirm_trade deny <id>
    python -m live_execution.scripts.confirm_trade kill "reason"
    python -m live_execution.scripts.confirm_trade resume

This tool NEVER executes, quotes, or signs anything. It only moves
confirmations through pending→approved/denied and engages/clears the kill
switch. Execution happens solely via jupiter_executor with an approved id.
"""
from __future__ import annotations

import argparse
import sys
import time

from live_execution import config, kill_switch
from live_execution.confirmation_queue import (
    ConfirmationError,
    ConfirmationQueue,
)


def _queue() -> ConfirmationQueue:
    return ConfirmationQueue(config.STATE_DIR / "confirmations.json")


def cmd_list(_args) -> int:
    items = _queue().list_active()
    if not items:
        print("no active confirmations")
        return 0
    now = time.time()
    print(f"{'id':14s} {'status':9s} {'usd':>8s} {'dec':>3s} "
          f"{'expires_in':>10s}  mint / snapshot")
    for pc in items:
        mint = f"{pc.mint[:12]}…"
        snap = ""
        if pc.quote_snapshot:
            snap = (f"~{pc.quote_snapshot.get('tokens_out', 0):.6f} tok "
                    f"@ ${pc.quote_snapshot.get('price_usd', 0):.8g}")
        print(f"{pc.id:14s} {pc.status:9s} {pc.usd_size:8.2f} "
              f"{pc.decimals:3d} {max(0.0, pc.expires_at - now):9.0f}s  "
              f"{mint}  {snap}")
    return 0


def cmd_approve(args) -> int:
    try:
        pc = _queue().approve(args.id)
    except ConfirmationError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"approved {pc.id} ({pc.mint[:8]}… ${pc.usd_size:.2f}) — pass "
          f"--confirmation-id {pc.id} to the executor within its window")
    return 0


def cmd_deny(args) -> int:
    try:
        pc = _queue().deny(args.id)
    except ConfirmationError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"denied {pc.id}")
    return 0


def cmd_kill(args) -> int:
    kill_switch.trip(args.reason)
    print("KILL SWITCH ENGAGED — every trade path now refuses until `resume`")
    return 0


def cmd_resume(_args) -> int:
    kill_switch.clear()
    print("kill switch cleared (LIVE_TRADING_ENABLED still gates everything)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    pa = sub.add_parser("approve"); pa.add_argument("id"); pa.set_defaults(fn=cmd_approve)
    pd = sub.add_parser("deny"); pd.add_argument("id"); pd.set_defaults(fn=cmd_deny)
    pk = sub.add_parser("kill"); pk.add_argument("reason"); pk.set_defaults(fn=cmd_kill)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
