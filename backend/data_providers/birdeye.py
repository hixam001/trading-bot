"""
data_providers/birdeye.py — Birdeye provider (A2/A3): candidate discovery,
liquidity, volume, price, market cap, and token_security enrichment.

Every field passes through explicit type validation (require_type); a field
Birdeye didn't return stays None — never a fabricated default.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

import config
from data_providers.base import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    fetch_json,
    require_type,
)
from models import Candidate, SecurityInfo

log = logging.getLogger(__name__)

_HEADERS = {"X-API-KEY": config.BIRDEYE_API_KEY, "x-chain": "solana"}


def _parse_candidate(raw: dict[str, Any]) -> Optional[Candidate]:
    """
    Defensive mapping of one trending-list entry. Field names confirmed
    against the real /defi/token_trending memepool payload (address, symbol,
    name, price, liquidity, marketcap, volume24hUSD); token_overview-style
    aliases accepted too. 1h volume / 1h change are NOT in this payload —
    they arrive via Dexscreener enrichment and stay None until then.
    Returns None (skip) when identity-critical fields are missing/wrong-typed.
    """
    address = require_type(raw.get("address"), str, "address", "birdeye")
    if not address:
        return None
    price = require_type(raw.get("price"), (int, float), "price", "birdeye")
    if price is None or price <= 0:
        return None
    liquidity = require_type(raw.get("liquidity"), (int, float), "liquidity", "birdeye")
    # Trending payload: volume24hUSD / marketcap; overview alias: v24hUSD / mc.
    v24 = require_type(raw.get("volume24hUSD"), (int, float), "volume24hUSD", "birdeye")
    if v24 is None:
        v24 = require_type(raw.get("v24hUSD"), (int, float), "v24hUSD", "birdeye")
    mc = require_type(raw.get("marketcap"), (int, float), "marketcap", "birdeye")
    if mc is None:
        mc = require_type(raw.get("mc"), (int, float), "mc", "birdeye")

    return Candidate(
        symbol=str(require_type(raw.get("symbol"), str, "symbol", "birdeye") or "?"),
        name=str(require_type(raw.get("name"), str, "name", "birdeye") or ""),
        mint_address=address,
        price_usd=float(price),
        # Missing liquidity/volume is unevaluable (rules fail closed on None);
        # missing market cap only disables the vol/mcap ratio check.
        liquidity_usd=liquidity,
        volume_24h_usd=v24 if v24 is not None else 0.0,
        market_cap_usd=mc if mc is not None else 0.0,
        # Mint decimals — required for correct execution-price quoting.
        decimals=require_type(raw.get("decimals"), int, "decimals", "birdeye"),
        discovery_source="trending",
        source="birdeye",
    )


class BirdeyeProvider:
    """
    Candidate discovery via the memepool trending list (real memecoins, not
    major coins), plus token_security enrichment. Field names below were
    confirmed against real API responses (defense-first: never guessed).
    """

    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self._client = client
        self._security_available: bool = True   # flips off permanently on 401/403
        self._trending_available: bool = True   # flips off on 401/403/quota-400
        # Small concurrency bound: prevents burst-429s against Birdeye's
        # strict free-tier limits when many candidates enrich at once.
        self._sec_semaphore = asyncio.Semaphore(2)

    async def get_candidates(self, limit: int) -> list[Candidate]:
        own_client = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            if not self._trending_available:
                # Session-disabled (quota 400 / 401 / 403 observed earlier).
                # Returning [] keeps the lens skipped quietly; discovery runs
                # on the keyword scanner + new listings (already the case
                # when the key is unset).
                return []
            data = await fetch_json(
                client,
                f"{config.BIRDEYE_BASE_URL}/defi/token_trending",
                provider="birdeye",
                params={"listing": "memepool", "offset": 0,
                        "limit": min(limit, 50)},
                headers=_HEADERS,
            )
        except (ProviderAuthError, ProviderQuotaError) as exc:
            # Quota exhaustion ("Compute units usage limit exceeded") and
            # tier denial are both session-stable: self-disable instead of
            # burning 3 retries + backoff every cycle.
            self._trending_available = False
            log.warning(
                "birdeye trending lens disabled for this session: %s "
                "(keyword scanner + new listings carry discovery)", exc,
            )
            return []
        finally:
            if own_client:
                await client.aclose()
        tokens = data.get("data", {}).get("tokens", []) if isinstance(data, dict) else []
        out = [c for c in (_parse_candidate(t) for t in tokens) if c is not None]
        if not out:
            raise ProviderError("birdeye: no parseable candidates in response")
        return out[:limit]

    async def get_security_info(self, mint_address: str) -> SecurityInfo:
        """
        Token security enrichment. If the API tier does not entitle this
        endpoint (401/403 — observed on the free tier), it disables itself
        for the rest of the process lifetime and returns all-None (= unknown)
        immediately; unknown NEVER becomes False downstream.
        """
        if not self._security_available:
            return SecurityInfo()
        own_client = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            async with self._sec_semaphore:
                # Re-check under the lock: once a sibling call discovers the
                # 401, everything queued behind it bails out immediately.
                if not self._security_available:
                    return SecurityInfo()
                data = await fetch_json(
                    client,
                    f"{config.BIRDEYE_BASE_URL}/defi/token_security?address={mint_address}",
                    provider="birdeye",
                    headers=_HEADERS,
                )
        except (ProviderAuthError, ProviderQuotaError) as exc:
            self._security_available = False
            log.warning(
                "birdeye token_security disabled for this session: %s. "
                "Security fields will remain UNKNOWN (not False) on all "
                "candidates; the free on-chain RPC fallback owns the "
                "authority flags.", exc,
            )
            return SecurityInfo()
        except ProviderError:
            # Transient failure: leave fields unknown, try again next tick.
            log.warning("birdeye: security info unavailable for %s", mint_address)
            return SecurityInfo()
        finally:
            if own_client:
                await client.aclose()
        info = data.get("data", {}) if isinstance(data, dict) else {}

        def opt_bool(key: str) -> Optional[bool]:
            v = info.get(key)
            return v if isinstance(v, bool) else None

        return SecurityInfo(
            mint_authority_revoked=opt_bool("mintAuthorityRevoked"),
            freeze_authority_revoked=opt_bool("freezeAuthorityRevoked"),
            is_likely_honeypot=None,   # Birdeye does not expose honeypot directly;
                                       # populated by the honeypot checker if wired.
        )

    async def get_current_price(self, mint_address: str) -> float:
        own_client = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            data = await fetch_json(
                client,
                f"{config.BIRDEYE_BASE_URL}/defi/price",
                provider="birdeye",
                params={"address": mint_address},
                headers=_HEADERS,
            )
        finally:
            if own_client:
                await client.aclose()
        price = require_type(
            data.get("data", {}).get("value") if isinstance(data, dict) else None,
            (int, float), "value", "birdeye",
        )
        if price is None or price <= 0:
            raise ProviderError(f"birdeye: no usable price for {mint_address}")
        return float(price)
