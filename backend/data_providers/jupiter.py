"""
data_providers/jupiter.py — Jupiter quote API (A5): live execution-quality
price for open positions (reflects real swap pricing, not a display price).

Price derivation: quote EXACTLY ONE token's worth of raw units into USDC
(6 decimals); price_usd = usdc_out / 1.0. The raw-unit amount is computed
per-mint via raw_units_for_one_token(decimals) — decimals come from the
candidate snapshot and are NEVER assumed. Unknown decimals fail closed
(refuse to quote) rather than misprice the position: assuming 9 decimals for
every mint once fabricated 1000× prices on 6-decimal tokens.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

import config
from data_providers.base import ProviderError, fetch_json, require_type

log = logging.getLogger(__name__)


def raw_units_for_one_token(decimals: int) -> int:
    """Raw mint units representing exactly 1 token (pure; unit-tested)."""
    if decimals < 0:
        raise ValueError(f"decimals must be >= 0, got {decimals!r}")
    return 10 ** decimals


def price_from_quote(out_amount_raw: str | int, decimals: int) -> float:
    """
    Price of ONE token in USDC from a Jupiter quote that sold exactly one
    token's worth of raw units. Pure; unit-tested. This is where the
    BARRON 96k%-P&L bug lived: assuming 9 decimals for every mint scaled
    6-decimal tokens' prices by 1000x.
    """
    units = raw_units_for_one_token(decimals)
    try:
        out = int(out_amount_raw)
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"jupiter: unparseable outAmount {out_amount_raw!r}") from exc
    # The quote sold exactly `units` raw units == exactly ONE token, so the
    # USDC received (6 decimals on Solana) is the USD price of one token.
    return float(out / 1e6)


class JupiterProvider:
    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self._client = client

    async def get_current_price(self, mint_address: str,
                                decimals: Optional[int] = None) -> float:
        """
        Execution-quality price for ONE token in USD.

        FAILS CLOSED when `decimals` is unknown: quoting a wrong raw-unit
        amount scales the resulting price by 10**(9 - true_decimals) — the
        exact bug that produced a fabricated +96,000% P&L. Callers pass the
        decimals stored on the candidate snapshot; without them we refuse to
        guess.
        """
        if decimals is None or decimals <= 0:
            raise ProviderError(
                f"jupiter: token decimals unknown for {mint_address} — "
                f"refusing to quote (a wrong decimals value would fabricate "
                f"the price)"
            )
        own_client = self._client is None
        client = self._client or httpx.AsyncClient()
        # Quote API v6 is keyless; a Jupiter Pro key rides as x-api-key.
        headers = {"x-api-key": config.JUPITER_API_KEY} if config.JUPITER_API_KEY else {}
        try:
            data = await fetch_json(
                client,
                config.JUPITER_QUOTE_URL,
                provider="jupiter",
                params={
                    "inputMint": mint_address,
                    "outputMint": config.USDC_MINT,
                    "amount": str(raw_units_for_one_token(decimals)),
                    "slippageBps": "50",
                },
                headers=headers,
            )
        finally:
            if own_client:
                await client.aclose()

        out_amount = require_type(
            data.get("outAmount") if isinstance(data, dict) else None,
            str, "outAmount", "jupiter",
        )
        if out_amount is None:
            raise ProviderError(f"jupiter: no quote for {mint_address}")
        price = price_from_quote(out_amount, decimals)
        if price <= 0:
            raise ProviderError(f"jupiter: non-positive quote for {mint_address}")
        return price
