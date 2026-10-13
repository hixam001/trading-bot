"""
live_execution/trade_intent.py — Independent local trade intent (§79 Phase A).

Binds pre-sign simulation balance assertions to the bot's own locally computed intent,
rather than trusting numbers provided by the external Jupiter quote.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TradeIntent:
    side: Literal["buy", "sell"]
    wallet: str            # payer pubkey (base58)
    input_mint: str        # buy: USDC mint; sell: the token mint
    output_mint: str       # buy: the token mint; sell: USDC mint
    in_amount_raw: int     # computed LOCALLY (buy: usd*10**6; sell: sell_amount*10**decimals)
    slippage_bps: int      # the value WE sent in the quote request
    min_out_raw: int | None  # independent floor; None only for sells w/o a mark
    reference_price_usd: float | None  # our own mark (DexScreener/Birdeye path, NOT Jupiter swap API)

