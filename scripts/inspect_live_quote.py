#!/usr/bin/env python3
"""
scripts/inspect_live_quote.py — No-key Jupiter swap transaction inspector and simulation runner.

Fetches real $1 quotes (USDC -> token and token -> USDC) from Jupiter without loading any
keypair secret, decodes instructions, runs `inspect_swap_transaction`, optionally runs
pre-sign simulation if available, and prints a formatted diagnostic table.

Usage:
    python scripts/inspect_live_quote.py [--wallet <SOLANA_PUBKEY>] [--token <MINT_PUBKEY>]
"""
import argparse
import asyncio
import base64
import struct
import sys
from pathlib import Path

# Add backend to sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import httpx
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from config import JUPITER_QUOTE_URL
from live_execution import config as live_config, wallet
from live_execution.jupiter_executor import (
    _ATA_PROGRAM_ID,
    _COMPUTE_BUDGET_PROGRAM_ID,
    _SPL_TOKEN_PROGRAM_ID,
    _SYSTEM_PROGRAM_ID,
    _TOKEN_2022_PROGRAM_ID,
    ExecutionError,
    inspect_swap_transaction,
)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DEFAULT_TOKEN_MINT = "So11111111111111111111111111111111111111112"  # WSOL


PROGRAM_NAMES = {
    _SYSTEM_PROGRAM_ID: "System Program",
    _COMPUTE_BUDGET_PROGRAM_ID: "Compute Budget",
    _SPL_TOKEN_PROGRAM_ID: "SPL Token",
    _TOKEN_2022_PROGRAM_ID: "SPL Token-2022",
    _ATA_PROGRAM_ID: "Associated Token Account",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter v6",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": "Jupiter v4",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
}


def decode_instruction_tag(prog_id: str, data: bytes) -> str:
    """Return human-readable tag/discriminant description for instruction."""
    if prog_id == _SYSTEM_PROGRAM_ID:
        if len(data) >= 4:
            disc = struct.unpack("<I", data[:4])[0]
            names = {0: "CreateAccount", 1: "Assign", 2: "Transfer", 4: "Allocate", 11: "TransferWithSeed"}
            return names.get(disc, f"disc={disc}")
        return "raw"
    elif prog_id in (_SPL_TOKEN_PROGRAM_ID, _TOKEN_2022_PROGRAM_ID):
        if len(data) >= 1:
            tag = data[0]
            names = {
                1: "InitializeAccount",
                3: "Transfer",
                4: "Approve",
                6: "SetAuthority",
                7: "MintTo",
                8: "Burn",
                9: "CloseAccount",
                12: "TransferChecked",
                13: "ApproveChecked",
                15: "BurnChecked",
                16: "InitializeAccount2",
                17: "SyncNative",
                18: "InitializeAccount3",
            }
            return names.get(tag, f"tag={tag}")
        return "empty"
    elif prog_id == _ATA_PROGRAM_ID:
        tag = data[0] if data else 0
        names = {0: "Create", 1: "CreateIdempotent"}
        return names.get(tag, f"tag={tag}")
    elif prog_id == _COMPUTE_BUDGET_PROGRAM_ID:
        tag = data[0] if data else 0
        names = {0: "RequestUnits", 1: "RequestHeapFrame", 2: "SetComputeUnitLimit", 3: "SetComputeUnitPrice"}
        return names.get(tag, f"tag={tag}")
    return "execute"


async def fetch_quote_and_swap(client: httpx.AsyncClient, input_mint: str, output_mint: str, amount_raw: int, wallet_pubkey: str) -> tuple[dict, str]:
    quote_url = f"{JUPITER_QUOTE_URL}?inputMint={input_mint}&outputMint={output_mint}&amount={amount_raw}&slippageBps=50&asLegacyTransaction=true"
    r = await client.get(quote_url, timeout=10.0)
    r.raise_for_status()
    quote = r.json()

    swap_payload = {
        "quoteResponse": quote,
        "userPublicKey": wallet_pubkey,
        "wrapAndUnwrapSol": True,
        "asLegacyTransaction": True,
    }
    r_swap = await client.post(live_config.JUPITER_SWAP_URL, json=swap_payload, timeout=15.0)
    r_swap.raise_for_status()
    swap_tx_b64 = r_swap.json().get("swapTransaction", "")
    return quote, swap_tx_b64


async def inspect_direction(name: str, input_mint: str, output_mint: str, amount_raw: int, wallet_pubkey: str):
    print(f"\n================================================================================")
    print(f"DIRECTION: {name}")
    print(f"Payer / Wallet: {wallet_pubkey}")
    print(f"Input Mint:     {input_mint}")
    print(f"Output Mint:    {output_mint}")
    print(f"================================================================================")

    async with httpx.AsyncClient() as client:
        try:
            quote, swap_b64 = await fetch_quote_and_swap(client, input_mint, output_mint, amount_raw, wallet_pubkey)
        except Exception as e:
            print(f"Failed to fetch quote/swap from Jupiter: {e}")
            return False

    raw_bytes = base64.b64decode(swap_b64)
    tx = VersionedTransaction.from_bytes(raw_bytes)
    account_keys = [str(k) for k in getattr(tx.message, "account_keys", [])]

    # Run signing guard inspection
    guard_verdict = "PASS"
    guard_error = None
    try:
        inspect_swap_transaction(tx, wallet_pubkey)
    except ExecutionError as e:
        guard_verdict = "REFUSED"
        guard_error = str(e)
    except Exception as e:
        guard_verdict = "ERROR"
        guard_error = str(e)

    # Decode instruction table
    print(f"\n{'#':<3} | {'Program':<22} | {'Instruction / Tag':<24} | {'Accounts':<8}")
    print("-" * 70)
    for i, ix in enumerate(tx.message.instructions):
        pid = account_keys[ix.program_id_index] if ix.program_id_index < len(account_keys) else "UNKNOWN"
        prog_label = PROGRAM_NAMES.get(pid, pid[:8] + "...")
        tag_label = decode_instruction_tag(pid, bytes(getattr(ix, "data", b"")))
        acc_count = len(ix.accounts)
        print(f"{i:<3} | {prog_label:<22} | {tag_label:<24} | {acc_count:<8}")

    print("\n--------------------------------------------------------------------------------")
    print(f"SIGNING GUARD VERDICT: {guard_verdict}")
    if guard_error:
        print(f"REASON: {guard_error}")

    # Run simulation if sim_guard is available
    sim_verdict = "SKIPPED (Phase B pending)"
    try:
        from live_execution import sim_guard
        sim_res = await sim_guard.simulate_and_assert(swap_b64, quote, wallet_pubkey)
        sim_verdict = "PASS"
    except ImportError:
        pass
    except Exception as e:
        sim_verdict = f"REFUSED ({e})"

    print(f"SIMULATION VERDICT:    {sim_verdict}")
    print(f"OVERALL VERDICT:       {'PASS' if guard_verdict == 'PASS' and 'REFUSED' not in sim_verdict else 'REFUSED'}")
    return guard_verdict == "PASS"


async def main():
    parser = argparse.ArgumentParser(description="Inspect live Jupiter swap quote without signing key.")
    parser.add_argument("--wallet", help="Solana public address (default: expected/state wallet)")
    parser.add_argument("--token", default=DEFAULT_TOKEN_MINT, help="Target token mint")
    args = parser.parse_args()

    pubkey = args.wallet or wallet.public_address() or "11111111111111111111111111111111"
    if pubkey == "11111111111111111111111111111111" and not args.wallet:
        print("Note: No wallet configured in environment or state. Using fallback public address.")

    print(f"Running inspection with public address: {pubkey}")
    # 1. $1 USDC -> Token (1_000_000 raw USDC)
    ok1 = await inspect_direction("BUY (1 USDC -> Token)", USDC_MINT, args.token, 1_000_000, pubkey)
    # 2. Token -> USDC (e.g. 0.005 SOL = 5_000_000 lamports)
    ok2 = await inspect_direction("SELL (Token -> USDC)", args.token, USDC_MINT, 5_000_000, pubkey)

    if ok1 and ok2:
        print("\nALL DIRECTIONS PASSED SIGNING GUARD.")
        sys.exit(0)
    else:
        print("\nONE OR MORE DIRECTIONS REFUSED BY SIGNING GUARD.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
