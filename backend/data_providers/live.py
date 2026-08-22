"""
data_providers/live.py — combined live stack (A9 selection target).

Candidate discovery + security from Birdeye; buys/sell counts from
Dexscreener; prices via Jupiter. All enrichment is concurrent and every
failure leaves fields None (= unknown) rather than fabricated.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

import config
from data_providers.base import ProviderError
from data_providers.birdeye import BirdeyeProvider
from data_providers.dexscreener import DexscreenerProvider
from data_providers.jupiter import JupiterProvider
from models import Candidate, SecurityInfo

log = logging.getLogger(__name__)


class LiveProviderStack:
    """Implements MarketDataProvider over Birdeye + Dexscreener + Jupiter."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient()
        self.birdeye = BirdeyeProvider(self._client)
        self.dexscreener = DexscreenerProvider(self._client)
        self.jupiter = JupiterProvider(self._client)

    async def get_candidates(self, limit: int) -> list[Candidate]:
        candidates = await self.birdeye.get_candidates(limit)
        await self.dexscreener.enrich_candidates(candidates)

        # Security enrichment concurrently; failure leaves None (= unknown).
        async def _secure(c: Candidate) -> None:
            try:
                info: SecurityInfo = await self.birdeye.get_security_info(c.mint_address)
                c.mint_authority_revoked = info.mint_authority_revoked
                c.freeze_authority_revoked = info.freeze_authority_revoked
                c.is_likely_honeypot = info.is_likely_honeypot
            except ProviderError:
                pass   # already logged upstream; fields stay unknown

        await asyncio.gather(*(_secure(c) for c in candidates))
        log.info("Live stack: %d candidates enriched", len(candidates))
        return candidates

    async def get_current_price(self, mint_address: str,
                                decimals: Optional[int] = None) -> float:
        try:
            return await self.jupiter.get_current_price(mint_address, decimals)
        except ProviderError:
            log.warning("jupiter price unavailable for %s — falling back to birdeye",
                        mint_address)
            return await self.birdeye.get_current_price(mint_address)

    async def get_security_info(self, mint_address: str) -> SecurityInfo:
        return await self.birdeye.get_security_info(mint_address)

    async def aclose(self) -> None:
        await self._client.aclose()


def build_provider():
    """Single selection point (A9): DATA_BACKEND chooses the stack."""
    if config.DATA_BACKEND == "mock":
        from data_providers.mock import MockProvider
        return MockProvider()
    if config.DATA_BACKEND == "live":
        if not config.BIRDEYE_API_KEY:
            # Fail fast and loud: live mode without a Birdeye key means every
            # candidate fetch would 401 — never start half-configured.
            raise RuntimeError(
                "DATA_BACKEND=live requires BIRDEYE_API_KEY in /.env "
                "(Dexscreener and Jupiter keys are optional; their basic "
                "endpoints are currently keyless)."
            )
        return LiveProviderStack()
    raise RuntimeError(
        f"Unknown DATA_BACKEND {config.DATA_BACKEND!r} — expected 'mock' or 'live'"
    )
