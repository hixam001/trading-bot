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
from data_providers.new_listings import NewListingFeed
from models import Candidate, SecurityInfo

log = logging.getLogger(__name__)


class LiveProviderStack:
    """Implements MarketDataProvider over Birdeye + Dexscreener + Jupiter,
    with a dual-lens discovery merge: trending (hot now) + new listings
    (SUBSCRIBE_TOKEN_NEW_LISTING websocket, degrade-gracefully)."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient()
        self.birdeye = BirdeyeProvider(self._client)
        self.dexscreener = DexscreenerProvider(self._client)
        self.jupiter = JupiterProvider(self._client)
        self.new_listings = NewListingFeed()
        self.new_listings.start()

    async def get_candidates(self, limit: int) -> list[Candidate]:
        # Lens 1: trending. Lens 2: buffered new-listing events drained
        # concurrently (Task A: asyncio.gather, matching existing pattern).
        trending, fresh_events = await asyncio.gather(
            self.birdeye.get_candidates(limit),
            asyncio.to_thread(self.new_listings.drain, max(limit // 2, 1)),
        )

        fresh: list[Candidate] = []
        for ev in fresh_events:
            fresh.append(Candidate(
                symbol=ev.get("symbol", "?"),
                name=ev.get("name", ""),
                mint_address=ev["mint_address"],
                price_usd=0.0,                    # placeholder; Dexscreener fills it
                liquidity_usd=ev.get("liquidity_usd"),
                volume_24h_usd=0.0,
                market_cap_usd=0.0,
                decimals=ev.get("decimals"),
                discovery_source="new_listing",
                source="birdeye:new_listing",
            ))

        # Merge by mint; a mint in both lenses in the same tick is "both".
        by_mint: dict[str, Candidate] = {}
        for c in trending:
            by_mint[c.mint_address] = c
        for c in fresh:
            existing = by_mint.get(c.mint_address)
            if existing is None:
                by_mint[c.mint_address] = c
            else:
                existing.discovery_source = "both"
                # Keep the fresher Dexscreener-side numbers from whichever
                # entry has them; trending entry already carries v24h/mcap.
                if c.decimals is not None and existing.decimals is None:
                    existing.decimals = c.decimals

        merged = list(by_mint.values())[:limit]
        await self.dexscreener.enrich_candidates(merged)

        # Security enrichment concurrently; failure leaves None (= unknown).
        async def _secure(c: Candidate) -> None:
            try:
                info: SecurityInfo = await self.birdeye.get_security_info(c.mint_address)
                c.mint_authority_revoked = info.mint_authority_revoked
                c.freeze_authority_revoked = info.freeze_authority_revoked
                c.is_likely_honeypot = info.is_likely_honeypot
            except ProviderError:
                pass   # already logged upstream; fields stay unknown

        await asyncio.gather(*(_secure(c) for c in merged))
        log.info("Live stack: %d candidates enriched (trending=%d new_listing=%d)",
                 len(merged),
                 sum(1 for c in merged if "trending" in c.discovery_source or
                     c.discovery_source == "both"),
                 sum(1 for c in merged if "new_listing" in c.discovery_source))
        return merged

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
        await self.new_listings.stop()
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
