#!/usr/bin/env python3
"""
scripts/ack_wallet_change.py — Operator acknowledgment for deliberate wallet balance changes (§81 Phase B).

Resets or updates the wallet baseline snapshot (wallet_snapshot.json) after a deliberate
deposit, withdrawal, or rebalancing, preventing false WALLET_DROP alarms.

SECURITY INVARIANT:
  This script must NEVER import load_keypair or touch private keys (AST verified in tests).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

# Add backend directory to sys.path so live_execution is importable
repo_root = Path(__file__).resolve().parents[1]
backend_dir = repo_root / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from live_execution import config as live_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ack_wallet_change")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Acknowledge a deliberate wallet balance change and reset the baseline snapshot."
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="Operator rationale for the balance change (e.g. 'manual deposit of 500 USDC for testing').",
    )
    parser.add_argument(
        "--usdc",
        type=float,
        default=None,
        help="Current USDC balance. If omitted, requires --live.",
    )
    parser.add_argument(
        "--sol",
        type=float,
        default=None,
        help="Current SOL balance. If omitted, requires --live.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Read current balances from RPC without loading private key (uses EXPECTED_WALLET_ADDRESS).",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="State directory path (defaults to config.STATE_DIR).",
    )
    return parser.parse_args()


async def fetch_live_balances() -> tuple[float, float]:
    """Fetch live USDC and SOL balances via public RPC without touching private keys."""
    from live_execution import solana
    from live_execution.jupiter_executor import _BACKEND_USDC_MINT

    address = live_config.EXPECTED_WALLET_ADDRESS
    if not address:
        raise ValueError("EXPECTED_WALLET_ADDRESS is not set; cannot fetch live balances.")

    sol_bal = await solana.get_sol_balance(address)
    if sol_bal is None:
        raise RuntimeError(f"Failed to fetch SOL balance for {address} from RPC.")

    # Fetch USDC token balance
    # solana.get_token_balance returns float | None
    usdc_bal = await solana.get_token_balance(address, _BACKEND_USDC_MINT)
    if usdc_bal is None:
        raise RuntimeError(
            f"Failed to fetch USDC balance for {address} from RPC; refusing to "
            "acknowledge a baseline from incomplete data."
        )

    return usdc_bal, sol_bal


def main() -> int:
    args = parse_args()

    usdc = args.usdc
    sol = args.sol

    if args.live:
        try:
            usdc, sol = asyncio.run(fetch_live_balances())
            log.info("Fetched live balances from RPC: %.2f USDC, %.4f SOL", usdc, sol)
        except Exception as exc:
            log.error("Failed fetching live balances: %s", exc)
            return 1
    elif usdc is None or sol is None:
        log.error("Must specify either --live or both --usdc and --sol.")
        return 1

    target_dir = args.state_dir or live_config.STATE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    snap_file = target_dir / "wallet_snapshot.json"

    now = time.time()
    payload = {
        "ts": now,
        "usdc": usdc,
        "sol": sol,
        "reason": args.reason,
        "acknowledged_by": "operator",
    }

    # Atomic write via temporary file
    tmp_file = snap_file.with_suffix(".tmp")
    try:
        tmp_file.write_text(json.dumps(payload, indent=2))
        tmp_file.replace(snap_file)
        log.info("Wallet snapshot successfully acknowledged and updated at %s", snap_file)
        log.info("Baseline reset to: USDC=%.2f, SOL=%.4f, Reason='%s'", usdc, sol, args.reason)
        return 0
    except Exception as exc:
        log.error("Failed writing snapshot file: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

