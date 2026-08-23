"""
data_providers/discovery.py — omotrades-model rotating keyword scanner.

Fixes "keeps revolving around the same tokens": instead of one sticky
trending feed, rotates through ~45 DexScreener search queries each tick,
guaranteeing different market slices are visible on different ticks.

Also: fake-chart filter (wash-traded pairs whose 1h volume their liquidity
could never absorb never reach the reasoning layer).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

import config
from models import Candidate

log = logging.getLogger(__name__)

QUERY_POOL = [
    "SOL pump", "SOL bonk", "SOL cat", "SOL dog", "SOL raydium",
    "SOL meteora", "SOL frog", "SOL moon", "SOL ai", "SOL coin",
    "SOL pepe", "SOL wif", "SOL baby", "SOL trump", "SOL bird",
    "SOL fomo", "SOL inu", "SOL fish", "SOL bear", "SOL bull",
    "SOL chill", "SOL wojak", "SOL grok", "SOL agent", "SOL mascot",
    "SOL king", "SOL goat", "SOL monkey", "SOL duck", "SOL sigma",
    "SOL gme", "SOL usa", "SOL elon", "SOL pengu", "SOL launch",
    "SOL community", "SOL dao", "SOL team", "SOL founder",
    "SOL protocol", "SOL app", "SOL creator", "SOL streamer",
    "SOL artist", "SOL brand",
]
QUERIES_PER_TICK = 5
SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
MAX_VOL_TO_LIQ_RATIO = 50.0


def _is_fake_chart(liq_usd: float, vol_1h: float) -> bool:
    if liq_usd <= 0:
        return True
    return (vol_1h / max(liq_usd, 1.0)) > MAX_VOL_TO_LIQ_RATIO


def _safe_float(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _age_hours(created_ms) -> Optional[float]:
    if not created_ms:
        return None
    try:
        return max(0.0, (time.time() * 1000.0 - float(created_ms)) / 3_600_000)
    except (TypeError, ValueError):
        return None


class KeywordScanner:
    """Rotating DexScreener keyword-pool scanner (third discovery lens)."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._rotation = 0
        self._cache: dict[str, tuple[float, list]] = {}
        self._cache_ttl = 60.0

    def next_rotation(self) -> int:
        self._rotation += 1
        return self._rotation

    async def scan(self) -> list[Candidate]:
        """Rotated keyword slice → fresh candidates. Fail-soft → []."""
        queries = self._rotated_queries()
        all_pairs: list[dict] = []
        now = time.monotonic()

        for q in queries:
            key = f"q:{q}"
            cached = self._cache.get(key)
            if cached and now - cached[0] < self._cache_ttl:
                all_pairs.extend(cached[1])
                continue
            try:
                resp = await self._client.get(
                    SEARCH_URL, params={"q": q},
                    headers={"accept": "application/json"})
                if resp.status_code != 200:
                    continue
                pairs = resp.json().get("pairs") or []
                solana = [p for p in pairs if isinstance(p, dict)
                          and p.get("chainId") == "solana"]
                self._cache[key] = (now, solana)
                all_pairs.extend(solana)
            except Exception as exc:
                log.debug("keyword scan %r failed: %s", q, exc)

        return self._build_board(all_pairs)

    def _rotated_queries(self) -> list[str]:
        start = (self._rotation * QUERIES_PER_TICK) % len(QUERY_POOL)
        return [QUERY_POOL[(start + i) % len(QUERY_POOL)]
                for i in range(QUERIES_PER_TICK)]

    def _build_board(self, all_pairs: list[dict]) -> list[Candidate]:
        best: dict[str, dict] = {}
        for pair in all_pairs:
            base = pair.get("baseToken") or {}
            mint = base.get("address")
            if not mint or mint in best:
                continue
            liq = (pair.get("liquidity") or {}).get("usd") or 0
            vol_1h = (pair.get("volume") or {}).get("h1") or 0
            if _is_fake_chart(liq_usd=liq, vol_1h=vol_1h):
                continue
            best[mint] = {"pair": pair, "liq": liq or 0,
                          "symbol": (base.get("symbol") or "?").replace("$", ""),
                          "name": base.get("name") or ""}

        candidates = []
        for info in sorted(best.values(), key=lambda x: -x["liq"]):
            pair = info["pair"]
            vol = pair.get("volume") or {}
            chg = pair.get("priceChange") or {}
            txns = pair.get("txns") or {}
            h1 = txns.get("h1") or {}
            info_obj = pair.get("info") or {}
            socials = [s.get("type") for s in (info_obj.get("socials") or [])
                       if isinstance(s, dict)]
            sites = info_obj.get("websites") or []
            mint = (pair.get("baseToken") or {}).get("address") or ""

            candidates.append(Candidate(
                symbol=info["symbol"],
                name=info["name"] or info["symbol"],
                mint_address=mint,
                price_usd=_safe_float(pair.get("priceUsd")) or 0.0,
                liquidity_usd=info["liq"] or None,
                volume_24h_usd=_safe_float(vol.get("h24")) or 0.0,
                market_cap_usd=_safe_float(pair.get("marketCap")
                                           or pair.get("fdv")) or 0.0,
                volume_1h_usd=_safe_float(vol.get("h1")) or None,
                buys_1h=int(h1.get("buys") or 0) or None,
                sells_1h=int(h1.get("sells") or 0) or None,
                price_change_1h_pct=_safe_float(chg.get("h1")),
                age_hours=_age_hours(pair.get("pairCreatedAt")),
                has_twitter="twitter" in socials,
                has_telegram="telegram" in socials,
                has_website=bool(sites),
                decimals=6,
                source="dexscreener:keyword",
                discovery_source="keyword",
            ))
        return candidates