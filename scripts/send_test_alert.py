#!/usr/bin/env python3
"""
scripts/send_test_alert.py — Send test alert to configured channels (§79 Phase D).

Verifies connectivity and credentials for outbound alerting channels (ntfy, Telegram).
Prints HTTP status codes and delivery outcomes; never outputs secrets or tokens.

Usage:
    python scripts/send_test_alert.py
    python scripts/send_test_alert.py --channel ntfy
    python scripts/send_test_alert.py --channel telegram
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

# Ensure backend is on sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from alerts import get_configured_channels, send_test_alert


async def main() -> int:
    parser = argparse.ArgumentParser(description="Send test alert to configured channels")
    parser.add_argument("--channel", choices=["ntfy", "telegram"], help="Specific channel to test")
    args = parser.parse_args()

    active_channels = get_configured_channels()
    print("=" * 60)
    print("OUTBOUND ALERT CHANNEL VERIFICATION (§79 Phase D)")
    print("=" * 60)
    print(f"Configured channels detected: {active_channels or 'None'}")

    if not active_channels:
        print("\nNo channels configured. To configure:")
        print("  ntfy:")
        print("    export ALERT_NTFY_URL='https://ntfy.sh'  # or self-hosted")
        print("    export ALERT_NTFY_TOPIC='your-private-topic'")
        print("    export ALERT_NTFY_TOKEN='...'           # optional auth token")
        print("  Telegram:")
        print("    export ALERT_TELEGRAM_BOT_TOKEN='...'   # from @BotFather")
        print("    export ALERT_TELEGRAM_CHAT_ID='...'     # chat/channel ID")
        return 0

    target = args.channel
    if target and target not in active_channels:
        print(f"\nError: Requested channel '{target}' is not configured in environment.")
        return 1

    print(f"\nDispatching test alert to: {target or active_channels} ...")
    results = await send_test_alert(channel=target)

    all_ok = True
    for ch, res in results.items():
        ok = res.get("ok", False)
        status_code = res.get("status_code", "N/A")
        err = res.get("error", "")
        status_str = f"HTTP {status_code}" if status_code != "N/A" else "FAILED"
        print(f"  [{ch.upper()}] Status: {status_str} — {'SUCCESS' if ok else 'FAILED'}")
        if err:
            print(f"       Detail: {err}")
        if not ok:
            all_ok = False

    print("=" * 60)
    if all_ok:
        print("All test alerts delivered successfully.")
        return 0
    else:
        print("One or more alert deliveries failed.")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
