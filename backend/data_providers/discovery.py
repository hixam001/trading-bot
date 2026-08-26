"""
data_providers/discovery.py - omotrades-model rotating keyword scanner.

Two guarantees ported from omo market.server.ts:
1. ROTATION - ~45 DexScreener search queries, QUERIES_PER_TICK per tick, so
   different market slices are visible on different ticks.
2. SLOT COMPOSITION - the board is not just flow-ranked. omo reserves:
     - up to 3 newborn slots (young tape + real socials/site)
     - up to 2 mover slots (top 1h change with real volume)
     - 5 guaranteed rotating slots from the unranked remainder
   so flow ranking can never bury every fresh name.
Plus: boosted-token feeds (paid Dexscreener boosts) set Candidate.boosted,
and the fake-chart filter (wash-traded pairs whose 1h volume their liquidity
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
BOOSTS_TOP_URL = "https://api.dexscreener.com/token-boosts/top/v1"
LATEST_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
MAX_VOL_TO_LIQ_RATIO = 50.0

# Slot allocation (omo board composition).
NEWBORN_SLOTS = 3      # young tape worth looking at (needs socials or site)
MOVER_SLOTS = 2        # top 1h movers that flow ranking may have buried
RESERVED_ROTATION_SLOTS = 5   # guaranteed rotation from the remainder
BOARD_CAP = 16


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
    """Rotating DexScreener keyword-pool scanner with slot-guaranteed board."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._rotation = 0
        self._cache: dict[str, tuple[float, list]] = {}
        self._cache_ttl = 60.0
        self._boost_cache: tuple[float, set] = (0.0, set())

    def next_rotation(self) -> int:
        self._rotation += 1
        return self._rotation

    async def _fetch_boosted_mints(self) -> set:
        """Paid-boost feeds (top + latest), fail-soft. Cached one tick TTL."""
        now = time.monotonic()
        if now - self._boost_cache[0] < self._cache_ttl:
            return self._boost_cache[1]
        mints: set = set()
        for url in (BOOSTS_TOP_URL, LATEST_BOOSTS_URL):
            try:
                resp = await self._client.get(url, headers={"accept": "application/json"})
                if resp.status_code != 200:
                    continue
                rows = resp.json()
                if not isinstance(rows, list):
                    continue
                for row in rows[:100]:
                    if isinstance(row, dict) and row.get("chainId") == "solana":
                        mint = row.get("tokenAddress")
                        if mint:
                            mints.add(mint)
            except Exception as exc:
                log.debug("boost feed %s failed: %s", url, exc)
        self._boost_cache = (now, mints)
        return mints

    async def scan(self) -> list[Candidate]:
        """Rotated keyword slice + boost flags -> slot-composed board.
        Fail-soft -> []."""
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

        boosted = await self._fetch_boosted_mints()
        return self._build_board(all_pairs, boosted)

    def _rotated_queries(self) -> list[str]:
        start = (self._rotation * QUERIES_PER_TICK) % len(QUERY_POOL)
        return [QUERY_POOL[(start + i) % len(QUERY_POOL)]
                for i in range(QUERIES_PER_TICK)]

    def _build_board(self, all_pairs: list[dict], boosted_mints: set) -> list[Candidate]:
        """Dedupe + fake-chart filter + omo slot composition (capped)."""
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

        def enrich(info: dict) -> None:
            pair = info["pair"]
            vol = pair.get("volume") or {}
            chg = pair.get("priceChange") or {}
            txns = pair.get("txns") or {}
            h1 = txns.get("h1") or {}
            h6t = txns.get("h6") or {}
            info_obj = pair.get("info") or {}
            socials = [s.get("type") for s in (info_obj.get("socials") or [])
                       if isinstance(s, dict)]
            sites = info_obj.get("websites") or []
            info.update(
                vol_1h=_safe_float(vol.get("h1")) or 0.0,
                vol_24h=_safe_float(vol.get("h24")) or 0.0,
                vol_6h=_safe_float(vol.get("h6")) or 0.0,
                chg_5m=_safe_float(chg.get("m5")),
                chg_1h=_safe_float(chg.get("h1")),
                chg_6h=_safe_float(chg.get("h6")),
                chg_24h=_safe_float(chg.get("h24")),
                buys_1h=int(h1.get("buys") or 0) or None,
                sells_1h=int(h1.get("sells") or 0) or None,
                buys_6h=int(h6t.get("buys") or 0) or None,
                sells_6h=int(h6t.get("sells") or 0) or None,
                age_h=_age_hours(pair.get("pairCreatedAt")),
                socials=socials,
                has_site=bool(sites),
            )

        for info in best.values():
            enrich(info)

        by_flow = sorted(best.values(), key=lambda x: -x["liq"])

        # --- omo slot composition ---------------------------------------
        board: list[dict] = []
        seen: set = set()

        def push(info: dict) -> None:
            mint = (info["pair"].get("baseToken") or {}).get("address")
            if not mint or mint in seen:
                return
            seen.add(mint)
            board.append(info)

        for info in by_flow[:8]:            # flow-ranked core of the board
            push(info)

        newborns = [i for i in best.values()
                    if i["age_h"] is not None and 0 < i["age_h"] < 24
                    and i["vol_1h"] > 5_000
                    and ("twitter" in i["socials"] or i["has_site"])]
        for info in sorted(newborns, key=lambda x: -x["vol_1h"])[:NEWBORN_SLOTS]:
            push(info)

        movers = [i for i in best.values() if i["vol_1h"] > 10_000]
        for info in sorted(movers, key=lambda x: -(x["chg_1h"] or 0))[:MOVER_SLOTS]:
            push(info)

        rest = [i for i in by_flow if (i["pair"].get("baseToken") or {}).get("address") not in seen]
        start = (self._rotation * RESERVED_ROTATION_SLOTS) % len(rest) if rest else 0
        for k in range(min(RESERVED_ROTATION_SLOTS, len(rest))):
            push(rest[(start + k) % len(rest)])

        for info in by_flow:               # fill spare slots, rotation intact
            push(info)

        candidates: list[Candidate] = []
        for info in board[:BOARD_CAP]:
            pair = info["pair"]
            base = pair.get("baseToken") or {}
            mint = base.get("address") or ""
            candidates.append(Candidate(
                symbol=info["symbol"],
                name=info["name"] or info["symbol"],
                mint_address=mint,
                price_usd=_safe_float(pair.get("priceUsd")) or 0.0,
                liquidity_usd=info["liq"] or None,
                volume_24h_usd=info["vol_24h"],
                market_cap_usd=_safe_float(pair.get("marketCap")
                                           or pair.get("fdv")) or 0.0,
                fdv_usd=_safe_float(pair.get("fdv")),
                volume_1h_usd=info["vol_1h"] or None,
                volume_6h_usd=info["vol_6h"] or None,
                buys_1h=info["buys_1h"],
                sells_1h=info["sells_1h"],
                buys_6h=info["buys_6h"],
                sells_6h=info["sells_6h"],
                price_change_5m_pct=info["chg_5m"],
                price_change_1h_pct=info["chg_1h"],
                price_change_6h_pct=info["chg_6h"],
                price_change_24h_pct=info["chg_24h"],
                age_hours=info["age_h"],
                has_twitter="twitter" in info["socials"],
                has_telegram="telegram" in info["socials"],
                has_website=True if info["has_site"] else None,
                boosted=True if mint in boosted_mints else None,
                decimals=6,
                source="dexscreener:keyword",
                discovery_source="keyword",
            ))
        return candidates
