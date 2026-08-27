"""
data_providers/dexscreener.py — Dexscreener provider (A4): the richest
per-mint source for rule-relevant fields — buys/sell 1h tx counts, 1h volume,
1h price change, liquidity, market cap, pair age, and public presence
channels. No API key needed for basic use.

Enriches existing Candidate objects in place; fields Dexscreener doesn't
return stay None.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

import config
from data_providers.base import ProviderError, fetch_json, require_type
from models import Candidate

log = logging.getLogger(__name__)


def _extract_pair_fields(pair: dict) -> dict:
    """
    Extract every rule-relevant field from a Dexscreener pair. Field names
    confirmed against the real /latest/dex/search payload: priceUsd (string),
    liquidity.usd, marketCap/fdv, volume.{h24,h1}, priceChange.h1,
    txns.h1.{buys,sells}, pairCreatedAt (ms epoch), info.{socials,websites}.
    Anything absent/wrong-typed stays None — never a fabricated default
    (defense-first rule 1).
    """
    price_raw = pair.get("priceUsd")
    price = None
    if isinstance(price_raw, str):
        try:
            price = float(price_raw)
        except ValueError:
            price = None
    price = require_type(price, (int, float), "priceUsd", "dexscreener")

    liq_obj = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
    vol_obj = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
    chg_obj = pair.get("priceChange") if isinstance(pair.get("priceChange"), dict) else {}
    txns = pair.get("txns") if isinstance(pair.get("txns"), dict) else {}
    h1 = txns.get("h1") if isinstance(txns.get("h1"), dict) else {}
    h6t = txns.get("h6") if isinstance(txns.get("h6"), dict) else {}
    info = pair.get("info") if isinstance(pair.get("info"), dict) else {}

    # Token age from the pair's creation timestamp (ms epoch). None stays None.
    created_ms = require_type(pair.get("pairCreatedAt"), int, "pairCreatedAt", "dexscreener")
    age_hours = None
    if created_ms is not None and created_ms > 0:
        age_hours = max((time.time() * 1000.0 - created_ms) / 3_600_000.0, 0.0)

    # Public presence channels: confirmed present only when listed.
    socials = info.get("socials") if isinstance(info.get("socials"), list) else []
    websites = info.get("websites") if isinstance(info.get("websites"), list) else []

    def _has_channel(channel_type: str, urls: list) -> Optional[bool]:
        for s in urls:
            if isinstance(s, dict):
                t = s.get("type") or s.get("url") or ""
                if isinstance(t, str) and channel_type in t.lower():
                    return True
        return False

    has_twitter = _has_channel("twitter", socials)
    has_telegram = _has_channel("telegram", socials)
    has_website = True if websites else None   # any website URL counts; absent list = unknown

    market_cap = require_type(pair.get("marketCap"), (int, float), "marketCap", "dexscreener")
    if market_cap is None:
        market_cap = require_type(pair.get("fdv"), (int, float), "fdv", "dexscreener")
    # FDV recorded separately from marketCap (the reference breadth field).
    fdv_usd = require_type(pair.get("fdv"), (int, float), "fdv", "dexscreener")

    return {
        "price_usd": price,
        "liquidity_usd": require_type(liq_obj.get("usd"), (int, float), "liquidity.usd", "dexscreener"),
        "market_cap_usd": market_cap,
        "volume_24h_usd": require_type(vol_obj.get("h24"), (int, float), "volume.h24", "dexscreener"),
        "volume_1h_usd": require_type(vol_obj.get("h1"), (int, float), "volume.h1", "dexscreener"),
        "price_change_1h_pct": require_type(chg_obj.get("h1"), (int, float), "priceChange.h1", "dexscreener"),
        "buys_1h": require_type(h1.get("buys"), int, "txns.h1.buys", "dexscreener"),
        "sells_1h": require_type(h1.get("sells"), int, "txns.h1.sells", "dexscreener"),
        "age_hours": age_hours,
        "has_twitter": has_twitter,
        "has_telegram": has_telegram,
        "has_website": has_website,
        "price_change_5m_pct": require_type(chg_obj.get("m5"), (int, float), "priceChange.m5", "dexscreener"),
        "price_change_6h_pct": require_type(chg_obj.get("h6"), (int, float), "priceChange.h6", "dexscreener"),
        "price_change_24h_pct": require_type(chg_obj.get("h24"), (int, float), "priceChange.h24", "dexscreener"),
        "fdv_usd": fdv_usd,
        "buys_6h": int(h6t.get("buys") or 0) or None,
        "sells_6h": int(h6t.get("sells") or 0) or None,
        "volume_6h_usd": require_type(vol_obj.get("h6"), (int, float), "volume.h6", "dexscreener"),
    }


def _headers() -> dict[str, str]:
    """Optional auth headers — Dexscreener's basic endpoints are keyless;
    a future paid-tier key is sent as a bearer token when configured."""
    if config.DEXSCREENER_API_KEY:
        return {"Authorization": f"Bearer {config.DEXSCREENER_API_KEY}"}
    return {}


class DexscreenerProvider:
    def __init__(self, client: Optional[httpx.AsyncClient] = None,
                 max_concurrency: int = 5) -> None:
        self._client = client
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def _fetch_pair_fields(self, client: httpx.AsyncClient, mint: str) -> Optional[dict]:
        async with self._semaphore:
            try:
                data = await fetch_json(
                    client,
                    f"{config.DEXSCREENER_BASE_URL}/latest/dex/search",
                    provider="dexscreener",
                    params={"q": mint},
                    headers=_headers(),
                )
            except ProviderError as exc:
                log.warning("dexscreener: no pair data for %s: %s", mint, exc)
                return None
        pairs = data.get("pairs", []) if isinstance(data, dict) else []
        # Prefer the deepest Solana pair for this mint (highest liquidity).
        best_pair = None
        best_liq = -1.0
        for p in pairs or []:
            if not isinstance(p, dict):
                continue
            token = p.get("baseToken", {})
            if isinstance(token, dict) and token.get("address") == mint:
                liq_obj = p.get("liquidity") if isinstance(p.get("liquidity"), dict) else {}
                liq = require_type(liq_obj.get("usd"), (int, float),
                                   "liquidity.usd", "dexscreener") or 0.0
                if liq > best_liq:
                    best_liq = liq
                    best_pair = p
        if best_pair is None:
            log.info("dexscreener: no solana pair found for %s", mint)
            return None
        return _extract_pair_fields(best_pair)

    async def enrich_candidates(self, candidates: list[Candidate]) -> None:
        """
        Fill every rule-relevant field concurrently from the deepest pair per
        mint (performance-discipline rules 1/2: one shared client, bounded
        concurrency). Missing values remain None — never fabricated.
        """
        own_client = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            results = await asyncio.gather(
                *(self._fetch_pair_fields(client, c.mint_address) for c in candidates)
            )
        finally:
            if own_client:
                await client.aclose()
        for cand, fields in zip(candidates, results):
            if not fields:
                continue
            # Dexscreener's numbers are fresher than the trending snapshot;
            # only apply fields it actually returned (None = keep existing).
            for attr in ("price_usd", "liquidity_usd", "market_cap_usd",
                         "volume_24h_usd", "volume_1h_usd",
                         "price_change_1h_pct", "buys_1h", "sells_1h",
                         "age_hours", "has_twitter", "has_telegram",
                         "has_website", "price_change_5m_pct",
                         "price_change_6h_pct", "price_change_24h_pct",
                         "fdv_usd", "buys_6h", "sells_6h", "volume_6h_usd"):
                value = fields.get(attr)
                if value is not None:
                    setattr(cand, attr, value)
            cand.source = f"{cand.source}+dexscreener"
