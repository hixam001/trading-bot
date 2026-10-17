#!/usr/bin/env python3
"""
scripts/dry_run_guard.py — Pre-arm and post-upgrade dry-run verification tool (§81 Phase G).

Operator-run, read-only, never signs transactions.
Uses only wallet.public_address() (no private key / keypair).
Executes:
  1. Jupiter quote fetching
  2. Transaction serialization (_build_swap_transaction)
  3. Static bytecode/instruction inspection (inspect_swap_transaction)
  4. Pre-sign simulation with multi-RPC quorum & intent assertions (simulate_and_assert)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Add backend directory to sys.path
_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import config
from data_providers.jupiter import JupiterProvider
from live_execution import config as live_config
from live_execution import solana, wallet
from live_execution.jupiter_client import quote_url
from live_execution.jupiter_executor import (
    _build_swap_transaction,
    _get_json,
    inspect_swap_transaction,
)
from live_execution.quote_params import build_quote_params
from live_execution.sim_guard import simulate_and_assert, SimulationRefused
from live_execution.trade_intent import TradeIntent, usd_to_raw_amount


async def async_main(args: argparse.Namespace) -> int:
    # Fail closed if live state dir is not explicitly isolated
    default_state = config.BASE_DIR / "live_execution" / "state"
    if not os.getenv("LIVE_EXECUTION_STATE_DIR") and default_state.exists() and not args.allow_live_state:
        sys.stderr.write(
            "Refusing to run: LIVE_EXECUTION_STATE_DIR is unset and default state dir exists.\n"
            "This dry run could mutate live safety counters. Pass --allow-live-state to proceed.\n"
        )
        return 2

    # Get wallet public address only (strictly no private key)
    payer_address = wallet.public_address()
    if not payer_address:
        sys.stderr.write(
            "Error: could not determine wallet public address. "
            "Ensure wallet_pubkey.txt or PUBLIC_ADDRESS is configured.\n"
        )
        return 2

    side = args.side.lower()
    mint = args.mint.strip()
    usd = float(args.usd)

    if usd <= 0:
        sys.stderr.write("Error: --usd must be greater than 0\n")
        return 1

    category = "preflight"
    try:
        if side == "buy":
            input_mint = config.USDC_MINT
            output_mint = mint
            output_decimals = await solana.get_mint_decimals(output_mint)
            if output_decimals is None:
                raise ValueError(f"Could not read mint decimals for {output_mint} via RPC")
            in_amount_raw = usd_to_raw_amount(usd, 6)
            ref_price = await JupiterProvider().get_current_price(output_mint, output_decimals)
            if not ref_price or ref_price <= 0:
                raise ValueError(f"Non-positive reference price {ref_price} for {output_mint}")
        elif side == "sell":
            input_mint = mint
            output_mint = config.USDC_MINT
            input_decimals = await solana.get_mint_decimals(input_mint)
            if input_decimals is None:
                raise ValueError(f"Could not read mint decimals for {input_mint} via RPC")
            ref_price = await JupiterProvider().get_current_price(input_mint, input_decimals)
            if not ref_price or ref_price <= 0:
                raise ValueError(f"Non-positive reference price {ref_price} for {input_mint}")
            tokens = usd / ref_price
            in_amount_raw = int(tokens * (10 ** input_decimals))
            if in_amount_raw <= 0:
                raise ValueError("Calculated sell token amount rounds to 0")
            output_decimals = 6
        else:
            sys.stderr.write(f"Invalid side: {side}\n")
            return 1

        # 1. Fetch quote
        category = "quote"
        params = build_quote_params(
            input_mint=input_mint,
            output_mint=output_mint,
            amount_raw=in_amount_raw,
            slippage_bps=live_config.SLIPPAGE_BPS,
            side=side,
        )
        q_url = quote_url()
        quote_resp = await _get_json(q_url, params)
        if not quote_resp or "outAmount" not in quote_resp:
            raise ValueError(f"Invalid quote response: {quote_resp}")

        # 2. Build swap transaction
        category = "build"
        swap_b64 = await _build_swap_transaction(quote_resp, payer_address)

        # 3. Inspect swap transaction
        category = "inspect"
        inspect_res = inspect_swap_transaction(swap_b64, payer_address) or {}

        # 4. Construct TradeIntent and simulate
        category = "simulate"
        other_threshold = int(quote_resp.get("otherAmountThreshold", 0))
        intent = TradeIntent(
            side=side,
            wallet=payer_address,
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount_raw=in_amount_raw,
            slippage_bps=live_config.SLIPPAGE_BPS,
            min_out_raw=other_threshold,
            reference_price_usd=ref_price,
        )

        sim_summary = await simulate_and_assert(
            swap_b64,
            quote_resp,
            payer_address,
            intent,
        )

        # 5. Output results
        input_decrease = sim_summary.get("input_decrease", 0)
        output_increase = sim_summary.get("output_increase", 0)
        sol_decrease = sim_summary.get("sol_decrease", 0)
        priority_fee = inspect_res.get("priority_fee_lamports", 0)
        compute_units = sim_summary.get("units", 0)
        agreeing_rpcs = sim_summary.get("agreeing_rpcs", 1)

        result_payload = {
            "verdict": "PASSED",
            "side": side,
            "mint": mint,
            "usd": usd,
            "payer": payer_address,
            "input_decrease": input_decrease,
            "output_increase": output_increase,
            "sol_decrease_lamports": sol_decrease,
            "priority_fee_lamports": priority_fee,
            "compute_units": compute_units,
            "agreeing_rpc_count": agreeing_rpcs,
            "reference_price_usd": ref_price,
        }

        if args.json:
            print(json.dumps(result_payload, indent=2))
        else:
            print("VERDICT: PASSED")
            print(f"  Side:                  {side.upper()}")
            print(f"  Mint:                  {mint}")
            print(f"  Amount (USD):          ${usd:.2f}")
            print(f"  Payer:                 {payer_address}")
            print(f"  Input decrease:        {input_decrease}")
            print(f"  Output increase:       {output_increase}")
            print(f"  SOL decrease:          {sol_decrease} lamports")
            print(f"  Priority fee:          {priority_fee} lamports")
            print(f"  Compute units:         {compute_units}")
            print(f"  Agreeing RPC count:    {agreeing_rpcs}")
            print(f"  Reference price:       ${ref_price:.6f}")
        return 0

    except Exception as exc:
        refusal_category = getattr(exc, "category", category)
        err_msg = str(exc)
        if args.json:
            print(json.dumps({
                "verdict": "REFUSED",
                "refusal_category": refusal_category,
                "reason": err_msg,
            }, indent=2))
        else:
            print(f"VERDICT: REFUSED (category: {refusal_category}, reason: {err_msg})")
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dry-run safety inspection and pre-sign simulation without keypair."
    )
    parser.add_argument("--side", choices=["buy", "sell"], required=True, help="Trade side (buy or sell)")
    parser.add_argument("--mint", required=True, help="Token mint address")
    parser.add_argument("--usd", type=float, required=True, help="Trade size in USD")
    parser.add_argument("--json", action="store_true", help="Output JSON results")
    parser.add_argument("--allow-live-state", action="store_true", help="Allow running against default live state directory")

    args = parser.parse_args()
    code = asyncio.run(async_main(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
