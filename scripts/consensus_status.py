#!/usr/bin/env python3
"""Read only the live consensus snapshot. Exit 0 fresh, 1 invalid/stale, 2 unreadable."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import time

SIDES = ("buy", "sell")
CATEGORIES = ("structural", "control", "wallet_amount", "wallet_lamports", "vault_amount", "malformed")
COUNTER_MAX = (1 << 63) - 1
LOG_QUERY = "docker compose logs --since 1h live | rg 'SIM_CONSENSUS_REFUSED|SIM_CONSENSUS_ATTEMPT'"


def _valid(snapshot) -> bool:
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "scope", "written_at", "since_boot_at", "attempts", "refused"
    } or snapshot["scope"] != "live-process":
        return False
    for key in ("written_at", "since_boot_at"):
        value = snapshot[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return False
    if snapshot["since_boot_at"] > snapshot["written_at"]:
        return False
    for key in ("attempts", "refused"):
        if not isinstance(snapshot[key], dict) or set(snapshot[key]) != set(SIDES):
            return False
    for side in SIDES:
        refused = snapshot["refused"][side]
        if not isinstance(refused, dict) or set(refused) != set(CATEGORIES):
            return False
        if any(type(v) is not int or not 0 <= v <= COUNTER_MAX
               for v in (snapshot["attempts"][side], *refused.values())):
            return False
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(
        os.getenv("LIVE_EXECUTION_STATE_DIR") or os.getenv("LIVE_STATE_DIR")
        or Path(__file__).resolve().parents[1] / "backend/live_execution/state"))
    parser.add_argument("--cycle-interval-seconds", type=float,
                        default=os.getenv("TICK_INTERVAL_SECONDS", "60"))
    args = parser.parse_args(argv)
    if not math.isfinite(args.cycle_interval_seconds) or args.cycle_interval_seconds <= 0:
        parser.error("cycle interval must be positive and finite")
    print(f"Log cross-check (source of truth across restarts): {LOG_QUERY}")
    try:
        # Never follow a substituted snapshot into a secret sidecar.
        fd = os.open(args.state_dir / "consensus_counters.json", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            snapshot = json.load(stream)
    except FileNotFoundError:
        print("STALE: no data yet (snapshot missing); age_seconds=unknown")
        return 2
    except OSError:
        print("STALE: snapshot unreadable; age_seconds=unknown")
        return 2
    except (ValueError, UnicodeError):
        print("STALE: invalid snapshot; age_seconds=unknown")
        return 1
    if not _valid(snapshot):
        print("STALE: invalid snapshot; age_seconds=unknown")
        return 1
    age = time.time() - snapshot["written_at"]
    stale = age < 0 or age > 2 * args.cycle_interval_seconds
    print(f"{'STALE' if stale else 'OK'} scope=live-process age_seconds={age:.1f} "
          f"written_at={snapshot['written_at']} since_boot_at={snapshot['since_boot_at']}")
    print("Counts reset on live-process restart.")
    for side in SIDES:
        counts = " ".join(f"{c}={snapshot['refused'][side][c]}" for c in CATEGORIES)
        print(f"{side} attempts={snapshot['attempts'][side]} refused: {counts}")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
