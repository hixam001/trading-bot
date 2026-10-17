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


from decimal import Decimal, ROUND_HALF_UP


def usd_to_raw_amount(usd: float, decimals: int = 6) -> int:
    """Consistently convert USD size to raw integer units (§81 Phase C-5)."""
    d_usd = Decimal(str(usd))
    d_scale = Decimal(10) ** decimals
    return int((d_usd * d_scale).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


