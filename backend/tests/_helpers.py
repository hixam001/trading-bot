"""
backend/tests/_helpers.py — Test utilities for hermetic testing (§81 Phase C).

NOTE: intent_from_quote_for_tests is ONLY for test fixtures and test setup.
Production code must never synthesize TradeIntent from an untrusted quote.
An AST test asserts that production code under live_execution/ never imports _helpers.
"""
from __future__ import annotations

import struct
from solders.pubkey import Pubkey

from live_execution.trade_intent import TradeIntent
from live_execution.jupiter_executor import _BACKEND_USDC_MINT


def intent_from_quote_for_tests(quote: dict, wallet: str) -> TradeIntent:
    """Helper for test fixtures to construct an explicit TradeIntent matching a quote."""
    if not isinstance(quote, dict):
        return TradeIntent("buy", wallet, _BACKEND_USDC_MINT, "", 1_000_000, 50, None, None)

    if "swapMode" not in quote:
        quote["swapMode"] = "ExactIn"

    if "slippageBps" not in quote:
        quote["slippageBps"] = 50
    slippage = int(quote["slippageBps"])

    if "inAmount" not in quote:
        quote["inAmount"] = "1000000"
    in_amount = int(quote["inAmount"])

    if "otherAmountThreshold" not in quote:
        quote["otherAmountThreshold"] = "50000"
    other_threshold = int(quote["otherAmountThreshold"])

    if "outAmount" not in quote:
        if other_threshold > 0:
            quote["outAmount"] = str(max(1, (other_threshold * 10000 // max(1, 10000 - slippage)) + 1))
        else:
            quote["outAmount"] = "1000000"

    input_mint = quote.get("inputMint", _BACKEND_USDC_MINT)
    output_mint = quote.get("outputMint", "")
    return TradeIntent(
        side="buy" if input_mint == _BACKEND_USDC_MINT else "sell",
        wallet=wallet,
        input_mint=input_mint,
        output_mint=output_mint,
        in_amount_raw=in_amount,
        slippage_bps=slippage,
        min_out_raw=other_threshold,
        reference_price_usd=None,
    )


# Alias for backward compatibility with helpers.py
make_intent_from_quote = intent_from_quote_for_tests


def encode_token_account_data(
    mint: str,
    owner: str,
    amount: int = 0,
    delegate: str | None = None,
    state: int = 1,
    is_native: int | None = None,
    delegated_amount: int = 0,
    close_authority: str | None = None,
) -> bytes:
    """Encode a 165-byte SPL token account data layout for testing."""
    mint_bytes = bytes(Pubkey.from_string(mint))
    owner_bytes = bytes(Pubkey.from_string(owner))
    amt_bytes = struct.pack("<Q", amount)
    if delegate is not None:
        del_bytes = struct.pack("<I", 1) + bytes(Pubkey.from_string(delegate))
    else:
        del_bytes = struct.pack("<I", 0) + (b"\x00" * 32)
    state_byte = bytes([state])
    pad12 = b"\x00" * 12
    del_amt_bytes = struct.pack("<Q", delegated_amount)
    if close_authority is not None:
        close_bytes = struct.pack("<I", 1) + bytes(Pubkey.from_string(close_authority))
    else:
        close_bytes = struct.pack("<I", 0) + (b"\x00" * 32)

    res = mint_bytes + owner_bytes + amt_bytes + del_bytes + state_byte + pad12 + del_amt_bytes + close_bytes
    assert len(res) == 165
    return res

