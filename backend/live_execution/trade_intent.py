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



def valid_decimals(value) -> bool:
    """SPL mint decimals is an unsigned byte, never a boolean/coerced float."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255


def canonical_operation(*, side: str, wallet: str, input_mint: str,
                        output_mint: str, in_amount_raw: int, decimals: int,
                        slippage_bps: int, min_out_raw: int | None = None) -> dict:
    """Stable manual-approval identity and raw-unit bounds, independent of quotes."""
    if side not in ("buy", "sell") or not all(
        isinstance(v, str) and v for v in (wallet, input_mint, output_mint)
    ):
        raise ValueError("invalid operation identity")
    if not valid_decimals(decimals):
        raise ValueError("invalid operation decimals")
    for value, name, minimum, maximum in (
        (in_amount_raw, "raw amount", 1, None),
        (slippage_bps, "slippage", 0, 10000),
        (min_out_raw, "minimum output", 0, None),
    ):
        if value is None and name == "minimum output":
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum or (maximum is not None and value > maximum):
            raise ValueError(f"invalid operation {name}")
    return dict(side=side, wallet=wallet, input_mint=input_mint,
                output_mint=output_mint, in_amount_raw=in_amount_raw,
                decimals=decimals, slippage_bps=slippage_bps,
                min_out_raw=min_out_raw)
