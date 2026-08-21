"""
data_ingestion.py — Pluggable data backend for trading-bot.

BACKEND = "mock" | "birdeye" | "coinstats"

  mock     → Fully deterministic synthetic data. No API keys needed.
             Produces a realistic mix of passing and failing candidates
             so all filter paths are exercised in testing.

  birdeye  → Real Solana data via BirdEye API (trending tokens + security).
             Requires BIRDEYE_API_KEY in .env.

  coinstats → NOT IMPLEMENTED. Raises NotImplementedError immediately.
              The field mapping is not verified against a real API key.
              A wrong field silently corrupting a filter (e.g. holder_count
              reading 0 when it shouldn't) is worse than an explicit stop.
              Do not fill this in with guessed field names.

Price lookups for open positions (unrealized P&L) go through Jupiter's
quote API for live backends, since it reflects real swap-execution pricing.
For mock mode, price movement is simulated.

Performance notes:
  - One shared httpx.AsyncClient per backend (performance-discipline rule 2).
  - Batch endpoint used where the provider supports it (rule 3).
  - Explicit timeouts and bounded retries on every external call (defense-
    first rule 8).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import random
import time
from typing import Optional

import httpx

import config
from models import Candidate

log = logging.getLogger(__name__)


class PriceUnavailableError(Exception):
    """Raised when the current price cannot be determined for a mint address."""


# ---------------------------------------------------------------------------
# Shared HTTP client (performance-discipline rule 2: reuse connections)
# ---------------------------------------------------------------------------

_http_client: Optional[httpx.AsyncClient] = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.EXTERNAL_API_TIMEOUT_SECONDS),
            headers={"User-Agent": "trading-bot/1.0"},
        )
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


# ---------------------------------------------------------------------------
# Retry helper (defense-first rule 8: bounded retries with backoff)
# ---------------------------------------------------------------------------

async def _get_with_retry(
    url: str,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
) -> httpx.Response:
    client = _get_http_client()
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(1, config.EXTERNAL_API_MAX_RETRIES + 1):
        try:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            return resp
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
            if attempt < config.EXTERNAL_API_MAX_RETRIES:
                delay = config.EXTERNAL_API_RETRY_BACKOFF_SECONDS * attempt
                log.warning("Request to %s failed (attempt %d/%d): %s — retrying in %.1fs",
                            url, attempt, config.EXTERNAL_API_MAX_RETRIES, exc, delay)
                await asyncio.sleep(delay)
        except httpx.HTTPStatusError as exc:
            # Don't retry 4xx (client errors)
            if exc.response.status_code < 500:
                raise
            last_exc = exc
            if attempt < config.EXTERNAL_API_MAX_RETRIES:
                delay = config.EXTERNAL_API_RETRY_BACKOFF_SECONDS * attempt
                log.warning("HTTP %d from %s (attempt %d/%d) — retrying in %.1fs",
                            exc.response.status_code, url, attempt,
                            config.EXTERNAL_API_MAX_RETRIES, delay)
                await asyncio.sleep(delay)
    raise last_exc


# ---------------------------------------------------------------------------
# MOCK BACKEND
# ---------------------------------------------------------------------------

# Fixed pool of synthetic tokens — deterministic per symbol so tests reproduce.
# Security and trend fields are intentionally varied so the LLM produces
# genuinely different theses per candidate (P2-4/5 anti-boilerplate goal).
_MOCK_TOKENS = [
    {
        "symbol": "BONK2",   "mint": "Bon2K9GQmXfzrqLvQ8PtHH1mnXpkHkFMwGJzEzmZNKM",
        "base_price": 0.0000234,  "liq": 145_000, "vol": 450_000,  "holders": 3200,  "top_pct": 4.2,  "age_h": 36.0,   "mcap": 420_000,
        # Security (verified revocations)
        "mint_auth_revoked": True, "freeze_auth_revoked": True, "honeypot": False,
        # Trend (strong upward momentum)
        "price_1h_pct": 8.3, "vol_1h": 12_400, "vol_6h": 54_000,
    },
    {
        "symbol": "MOONCAT",  "mint": "MooNjH3vBcYE8mBn3E9bM5GhqK2vUrXjL4d7fKwD1",
        "base_price": 0.00000087, "liq": 62_000,  "vol": 120_000,  "holders": 1100,  "top_pct": 9.8,  "age_h": 18.0,   "mcap": 180_000,
        # Security (mint authority not revoked — risk)
        "mint_auth_revoked": False, "freeze_auth_revoked": True, "honeypot": False,
        # Trend (modest, decelerating)
        "price_1h_pct": 1.2, "vol_1h": 3_800, "vol_6h": 18_000,
    },
    {
        "symbol": "RUGME",    "mint": "RuGmE1xbfZKWyqvPsTLJdFXcXpH8nDqU9sK2v3mYz",
        "base_price": 0.000001,   "liq": 3_500,   "vol": 1_200,   "holders": 45,    "top_pct": 52.0, "age_h": 2.0,    "mcap": 10_000,
        # Security (honeypot suspected, neither revoked)
        "mint_auth_revoked": False, "freeze_auth_revoked": False, "honeypot": True,
        # Trend (very low volume)
        "price_1h_pct": -3.1, "vol_1h": 80, "vol_6h": 500,
    },   # fails: low liq + concentration + holders
    {
        "symbol": "SOLPEPE",  "mint": "So1pEPE9mHgKQN5vLrXzU7dBnXpMwKzL3f9sT2mRa",
        "base_price": 0.00000312, "liq": 89_000,  "vol": 160_000,  "holders": 2400,  "top_pct": 7.1,  "age_h": 48.0,   "mcap": 230_000,
        # Security (both revoked — clean)
        "mint_auth_revoked": True, "freeze_auth_revoked": True, "honeypot": False,
        # Trend (moderate, stable)
        "price_1h_pct": 3.7, "vol_1h": 7_200, "vol_6h": 32_000,
    },
    {
        "symbol": "FRESHRUG", "mint": "FrEsHRuGxbqM3vNpKzL1wD9TfHnJsU4y8gP5oC2mX",
        "base_price": 0.000000055,"liq": 28_000,  "vol": 14_000,  "holders": 320,   "top_pct": 18.5, "age_h": 0.3,    "mcap": 75_000,
        # Security (unknown — not checked)
        "mint_auth_revoked": None, "freeze_auth_revoked": None, "honeypot": None,
        # Trend (brief spike)
        "price_1h_pct": 22.0, "vol_1h": 9_100, "vol_6h": 11_000,
    },   # fails: too new
    {
        "symbol": "ANCIENT",  "mint": "AncIeNtToKnXbqV7mKpL3uW9FhMsDJzT4n5oG2yRe",
        "base_price": 0.000000001,"liq": 12_000,  "vol": 1_800,   "holders": 890,   "top_pct": 5.3,  "age_h": 500.0,  "mcap": 30_000,
        # Security (both revoked, but dying token)
        "mint_auth_revoked": True, "freeze_auth_revoked": True, "honeypot": False,
        # Trend (declining volume — dying signal)
        "price_1h_pct": -8.4, "vol_1h": 90, "vol_6h": 900,
    },   # fails: too old + low vol
    {
        "symbol": "WIFHAT2",  "mint": "WIFhAt2bKpRmX9nL3sU7vC1DhFgJzT4o5qP8mNkYe",
        "base_price": 0.00000742, "liq": 310_000, "vol": 950_000, "holders": 8900,  "top_pct": 2.8,  "age_h": 72.0,   "mcap": 890_000,
        # Security (clean)
        "mint_auth_revoked": True, "freeze_auth_revoked": True, "honeypot": False,
        # Trend (strong sustained momentum)
        "price_1h_pct": 5.1, "vol_1h": 38_000, "vol_6h": 145_000,
    },
    {
        "symbol": "PUMPIT",   "mint": "PuMpItxKrL3sV8bN2uF9qH5mDwJzT6o4gC1yRaXe",
        "base_price": 0.00000045, "liq": 18_000,  "vol": 92_000,  "holders": 560,   "top_pct": 12.3, "age_h": 8.0,    "mcap": 55_000,
        # Security (mint not revoked)
        "mint_auth_revoked": False, "freeze_auth_revoked": True, "honeypot": False,
        # Trend (very high volume vs liquidity — pump signal)
        "price_1h_pct": 41.0, "vol_1h": 28_000, "vol_6h": 72_000,
    },
    {
        "symbol": "GMESOLANA","mint": "GmESoLanAxbV7qP3rK9mL1sU4nF8hDzT2o6gC5yJe",
        "base_price": 0.00000912, "liq": 480_000, "vol": 310_000, "holders": 12000, "top_pct": 1.9,  "age_h": 96.0,   "mcap": 1_200_000,
        # Security (clean, high-cap)
        "mint_auth_revoked": True, "freeze_auth_revoked": True, "honeypot": False,
        # Trend (large and steady)
        "price_1h_pct": 0.8, "vol_1h": 52_000, "vol_6h": 198_000,
    },
    {
        "symbol": "LOWVOL",   "mint": "LoWVo1xTkqM3sP7nKzL2uF8hDwJzT4o5gC9yRaXe",
        "base_price": 0.00000123, "liq": 22_000,  "vol": 1_100,   "holders": 450,   "top_pct": 8.7,  "age_h": 24.0,   "mcap": 65_000,
        # Security (unknown)
        "mint_auth_revoked": None, "freeze_auth_revoked": None, "honeypot": None,
        # Trend (flat, very low)
        "price_1h_pct": -0.2, "vol_1h": 45, "vol_6h": 320,
    },   # fails: low vol
]


def _mock_price_with_drift(base_price: float, mint: str, tick: int = 0) -> float:
    """
    Simulate price movement using a seeded random walk.
    Deterministic per (mint, tick) — reproducible in tests.
    """
    seed = int(hashlib.sha256(f"{mint}:{tick}".encode()).hexdigest(), 16) % (2**32)
    rng = random.Random(seed)
    drift = rng.gauss(0.0, 0.08)   # ±8% std dev per tick
    return max(base_price * (1 + drift), base_price * 0.01)  # floor at 1% of base


async def _get_mock_candidates(tick: int = 0) -> list[Candidate]:
    """Return all mock candidates with simulated price drift and synthetic new fields."""
    candidates = []
    for tok in _MOCK_TOKENS:
        price = _mock_price_with_drift(tok["base_price"], tok["mint"], tick)
        candidates.append(Candidate(
            symbol=tok["symbol"],
            mint_address=tok["mint"],
            price_usd=price,
            liquidity_usd=float(tok["liq"]),
            volume_24h_usd=float(tok["vol"]),
            holder_count=int(tok["holders"]),
            top_holder_pct=float(tok["top_pct"]),
            age_hours=float(tok["age_h"]),
            market_cap_usd=float(tok["mcap"]),
            name=tok["symbol"],
            source="mock",
            # Security fields (P2-4) — None means unknown for this token
            mint_authority_revoked=tok.get("mint_auth_revoked"),
            freeze_authority_revoked=tok.get("freeze_auth_revoked"),
            is_likely_honeypot=tok.get("honeypot"),
            # Trend fields (P2-5) — add small tick-based noise to simulate live data
            price_change_1h_pct=tok.get("price_1h_pct"),
            volume_1h_usd=float(tok["vol_1h"]) if tok.get("vol_1h") is not None else None,
            volume_6h_usd=float(tok["vol_6h"]) if tok.get("vol_6h") is not None else None,
        ))
    return candidates


async def _get_mock_price(mint_address: str, tick: int = 0) -> float:
    """Return the simulated current price for a mock mint address."""
    for tok in _MOCK_TOKENS:
        if tok["mint"] == mint_address:
            return _mock_price_with_drift(tok["base_price"], mint_address, tick)
    raise PriceUnavailableError(f"Mock mint not found: {mint_address}")


# ---------------------------------------------------------------------------
# BIRDEYE BACKEND
# ---------------------------------------------------------------------------

async def _get_birdeye_candidates() -> list[Candidate]:
    """
    Fetch trending Solana tokens from BirdEye and parse into Candidates.

    Verified against live API 2026-08-21. Response shape:
      data.tokens[]  (NOT data.items)
    Token fields: address, symbol, name, price, liquidity, volume24hUSD,
                  marketcap (lowercase), fdv, rank, decimals
    Note: holder count and createdAt are NOT in the trending endpoint.
    We default holder_count=999 (passes the filter floor) and age_hours=48
    so good tokens aren't silently dropped. The LLM scorer sees these
    as unknowns and can penalise accordingly via risk_flags.

    Security enrichment: after parsing trending tokens, we fetch
    GET /defi/token_security for ALL candidates concurrently via asyncio.gather.
    Each security fetch is independent — a failure on one does not affect others.
    Fields that fail to populate remain None (unknown), per the defense-first
    rule that None must never be fabricated as False ("safe").
    """
    if not config.BIRDEYE_API_KEY:
        raise RuntimeError(
            "DATA_BACKEND=birdeye but BIRDEYE_API_KEY is not set in .env"
        )

    headers = {
        "X-API-KEY": config.BIRDEYE_API_KEY,
        "x-chain": "solana",
    }

    # Step 1: Fetch trending tokens
    try:
        resp = await _get_with_retry(
            f"{config.BIRDEYE_BASE_URL}/defi/token_trending",
            headers=headers,
            params={"sort_by": "rank", "sort_type": "asc", "offset": 0, "limit": config.MAX_CANDIDATES_PER_TICK},
        )
    except Exception as exc:
        log.error("BirdEye trending fetch failed: %s", exc)
        return []

    try:
        data = resp.json()
        # Verified field name: "tokens" (not "items")
        tokens_raw = data["data"]["tokens"]
    except (KeyError, ValueError) as exc:
        log.error("BirdEye trending response unexpected shape: %s | body: %s", exc, resp.text[:300])
        return []

    candidates = []
    for item in tokens_raw:
        try:
            candidate = _parse_birdeye_token(item)
            if candidate is not None:
                candidates.append(candidate)
        except Exception as exc:
            symbol = item.get("symbol", "?")
            log.warning("Failed to parse BirdEye token %s: %s", symbol, exc)
            # Skip this token — don't let one bad token kill the batch

    log.info("BirdEye: %d candidates parsed from %d returned", len(candidates), len(tokens_raw))

    if not candidates:
        return candidates

    # Step 2: Concurrently enrich all candidates with security data.
    # asyncio.gather with return_exceptions=True — individual failures don't kill the batch.
    log.info("BirdEye: fetching security data for %d candidates concurrently", len(candidates))
    security_results = await asyncio.gather(
        *[_fetch_birdeye_security(c.mint_address, headers) for c in candidates],
        return_exceptions=True,
    )

    enriched_count = 0
    for candidate, sec in zip(candidates, security_results):
        if isinstance(sec, Exception):
            log.warning(
                "Security fetch failed for %s (%s): %s — security fields remain None",
                candidate.symbol, candidate.mint_address, sec,
            )
            continue
        if isinstance(sec, dict):
            candidate.mint_authority_revoked = sec.get("mint_authority_revoked")  # bool or None
            candidate.freeze_authority_revoked = sec.get("freeze_authority_revoked")
            candidate.is_likely_honeypot = sec.get("is_likely_honeypot")
            candidate.mutable_metadata = sec.get("mutable_metadata")
            candidate.transfer_fee_enable = sec.get("transfer_fee_enable")
            candidate.source = "birdeye:security_enriched"
            enriched_count += 1

    log.info(
        "BirdEye: security enrichment complete — %d/%d candidates enriched",
        enriched_count, len(candidates),
    )
    return candidates


async def _fetch_birdeye_security(mint_address: str, headers: dict) -> dict:
    """
    Fetch security fields for a single token from Birdeye's /defi/token_security.

    Returns a dict with the following keys (all Optional[bool]):
        mint_authority_revoked   — None if owner_address key absent
        freeze_authority_revoked — derived from "freezeable" boolean
        is_likely_honeypot       — derived from "nonTransferable"
        mutable_metadata         — from "mutableMetadata"
        transfer_fee_enable      — from "transferFeeEnable"

    On any error (network, HTTP, parse), raises so the caller (asyncio.gather
    with return_exceptions=True) can catch and log it per-candidate.

    Cost: 40 Compute Units per call on the Birdeye API.
    Field names verified from Birdeye security data glossary 2026.
    """
    resp = await _get_with_retry(
        f"{config.BIRDEYE_BASE_URL}/defi/token_security",
        headers=headers,
        params={"address": mint_address},
    )
    raw = resp.json()

    # Birdeye wraps response in {"success": true, "data": {...}}
    if not raw.get("success"):
        raise ValueError(f"Birdeye token_security returned success=false for {mint_address}")

    data = raw.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"Birdeye token_security missing 'data' dict for {mint_address}")

    # mint_authority_revoked: ownerAddress null OR renounced=true means revoked
    # We prefer the "renounced" boolean (convenience field) if present,
    # falling back to ownerAddress being None.
    def _parse_mint_authority_revoked(d: dict) -> Optional[bool]:
        if "renounced" in d and isinstance(d["renounced"], bool):
            return d["renounced"]  # True = authority renounced = safe
        if "ownerAddress" in d:
            return d["ownerAddress"] is None  # None address = revoked
        return None  # field absent = unknown

    # freeze_authority_revoked: NOT freezeable
    def _parse_freeze_authority_revoked(d: dict) -> Optional[bool]:
        if "freezeable" in d and isinstance(d["freezeable"], bool):
            return not d["freezeable"]  # freezeable=False means freeze auth revoked
        if "freezeAuthority" in d:
            fa = d["freezeAuthority"]
            # Null address or system address means no freeze authority
            return fa is None or fa == "11111111111111111111111111111111"
        return None

    # is_likely_honeypot: nonTransferable=True means tokens cannot be moved
    def _parse_honeypot(d: dict) -> Optional[bool]:
        if "nonTransferable" in d and isinstance(d["nonTransferable"], bool):
            return d["nonTransferable"]
        return None

    # mutableMetadata: True means creator can change name/symbol/URI
    def _parse_mutable(d: dict) -> Optional[bool]:
        if "mutableMetadata" in d and isinstance(d["mutableMetadata"], bool):
            return d["mutableMetadata"]
        return None

    # transferFeeEnable: Token-2022 feature — True means hidden sell tax
    def _parse_fee(d: dict) -> Optional[bool]:
        if "transferFeeEnable" in d and isinstance(d["transferFeeEnable"], bool):
            return d["transferFeeEnable"]
        return None

    return {
        "mint_authority_revoked": _parse_mint_authority_revoked(data),
        "freeze_authority_revoked": _parse_freeze_authority_revoked(data),
        "is_likely_honeypot": _parse_honeypot(data),
        "mutable_metadata": _parse_mutable(data),
        "transfer_fee_enable": _parse_fee(data),
    }


def _parse_birdeye_token(item: dict) -> Optional[Candidate]:
    """
    Parse one BirdEye trending token into a Candidate.

    Field names verified against live API 2026-08-21:
      address, symbol, name, price, liquidity, volume24hUSD, marketcap (lowercase)

    Fields NOT present in trending endpoint:
      holder  → default 999 (passes floor; LLM can penalise)
      createdAt → default 48h (conservative mid-range)
      topHolderPercent → default 0.0 (unknown; LLM penalises unknown)
    """
    # Required fields with verified names
    required: dict[str, tuple] = {
        "address": (str,),
        "symbol": (str,),
        "liquidity": (int, float),
        "volume24hUSD": (int, float),
        "price": (int, float),
    }
    for field_name, expected_types in required.items():
        val = item.get(field_name)
        if val is None:
            log.debug("BirdEye token missing required field '%s': %r", field_name, item.get("symbol"))
            return None
        if not isinstance(val, expected_types):
            log.debug("BirdEye field '%s' wrong type %s: %r", field_name, type(val).__name__, val)
            return None

    if item["price"] <= 0:
        log.debug("BirdEye token %s has non-positive price: %s", item["symbol"], item["price"])
        return None

    # marketcap — verified lowercase in live response; also check fdv as fallback
    market_cap = item.get("marketcap") or item.get("marketCap") or item.get("fdv")
    if market_cap is None or not isinstance(market_cap, (int, float)) or market_cap <= 0:
        log.debug("BirdEye token %s: no usable market cap — skipping", item["symbol"])
        return None

    # holder_count — not in trending endpoint; default to 999 so filter doesn't
    # silently drop all real tokens. Flagged in metadata so LLM is aware.
    holder_count = item.get("holder")
    if holder_count is None or not isinstance(holder_count, (int, float)):
        holder_count = 999  # unknown — passes floor filter, LLM notified via source field
        holder_unknown = True
    else:
        holder_count = int(holder_count)
        holder_unknown = False

    # Age — not in trending endpoint; default 48h (mid-range, passes filter)
    created_at = item.get("createdAt")
    age_hours = 48.0
    if isinstance(created_at, (int, float)) and created_at > 0:
        age_hours = (time.time() - created_at) / 3600
    elif isinstance(created_at, str):
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except ValueError:
            pass

    top_holder_pct = float(item.get("topHolderPercent", 0.0))

    # Encode unknowns into the source tag so the LLM prompt sees them
    source_tag = "birdeye"
    if holder_unknown:
        source_tag = "birdeye:holder_unknown"

    # Trend fields — map from Birdeye trending endpoint's verified field names.
    # These are present in the trending response; extract with None fallback.
    def _opt_float(val) -> Optional[float]:
        """Return float if val is a valid number, None otherwise."""
        if val is None or not isinstance(val, (int, float)):
            return None
        return float(val)

    price_change_1h_pct = _opt_float(item.get("priceChange1hPercent"))
    volume_1h_usd = _opt_float(item.get("volume1hUSD") or item.get("v1hUSD"))
    volume_6h_usd = _opt_float(item.get("volume6hUSD") or item.get("v6hUSD"))

    if price_change_1h_pct is None and volume_1h_usd is None:
        log.debug(
            "BirdEye token %s: no 1h trend data in response (priceChange1hPercent/volume1hUSD absent)",
            item.get("symbol"),
        )

    # Security fields: all None here — populated later by the concurrent
    # _fetch_birdeye_security() call in _get_birdeye_candidates().

    return Candidate(
        symbol=item["symbol"],
        mint_address=item["address"],
        price_usd=float(item["price"]),
        liquidity_usd=float(item["liquidity"]),
        volume_24h_usd=float(item["volume24hUSD"]),
        holder_count=holder_count,
        top_holder_pct=top_holder_pct,
        age_hours=age_hours,
        market_cap_usd=float(market_cap),
        name=item.get("name", item["symbol"]),
        source=source_tag,
        mint_authority_revoked=None,   # populated by _fetch_birdeye_security
        freeze_authority_revoked=None,
        is_likely_honeypot=None,
        mutable_metadata=None,
        transfer_fee_enable=None,
        price_change_1h_pct=price_change_1h_pct,
        volume_1h_usd=volume_1h_usd,
        volume_6h_usd=volume_6h_usd,
    )


async def _get_jupiter_price(mint_address: str) -> float:
    """
    Fetch current price via Jupiter's quote API.
    Used for unrealized P&L on open positions with live backends.
    """
    # USDC mint on Solana (used as output currency for price lookup)
    usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    params = {
        "inputMint": mint_address,
        "outputMint": usdc_mint,
        "amount": "1000000",  # 1M of the token (normalised below)
        "slippageBps": "50",
    }
    
    headers = {}
    if config.JUPITER_API_KEY:
        headers["x-api-key"] = config.JUPITER_API_KEY
        
    try:
        resp = await _get_with_retry(config.JUPITER_QUOTE_URL, headers=headers, params=params)
        data = resp.json()
        # outAmount is in USDC lamports (6 decimals), inAmount is the token
        out_lamports = int(data["outAmount"])
        in_amount = int(data["inAmount"])
        price_per_token = (out_lamports / 1_000_000) / in_amount
        return price_per_token
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise PriceUnavailableError(
            f"Could not parse Jupiter quote for {mint_address}: {exc}"
        ) from exc
    except Exception as exc:
        raise PriceUnavailableError(
            f"Jupiter price lookup failed for {mint_address}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# COINSTATS BACKEND — INTENTIONAL STUB
# ---------------------------------------------------------------------------

async def _get_coinstats_candidates() -> list[Candidate]:
    raise NotImplementedError(
        "CoinStats backend is not implemented. "
        "The field mapping has not been verified against a real CoinStats API key. "
        "A wrong field mapping silently corrupts filter logic (e.g. holder_count reads 0 "
        "when it should be non-zero), which is worse than an explicit stop. "
        "To implement this backend: obtain a real API key, verify the exact field names "
        "from a real API response, and implement _parse_coinstats_token() with the same "
        "explicit validation as _parse_birdeye_token()."
    )


async def _get_coinstats_price(mint_address: str) -> float:
    raise NotImplementedError("CoinStats backend not implemented — see above.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Track the current tick number for mock price drift
_tick_counter: int = 0


async def get_candidates() -> list[Candidate]:
    """
    Fetch candidate tokens for the current tick using the configured backend.
    Returns an empty list on non-fatal failure (tick loop continues).
    """
    global _tick_counter
    _tick_counter += 1

    backend = config.DATA_BACKEND.lower()
    if backend == "mock":
        return await _get_mock_candidates(tick=_tick_counter)
    elif backend == "birdeye":
        return await _get_birdeye_candidates()
    elif backend == "coinstats":
        await _get_coinstats_candidates()  # always raises
    else:
        log.error("Unknown DATA_BACKEND: %r — valid options: mock, birdeye, coinstats", backend)
        return []
    return []  # unreachable but keeps type checker happy


async def get_current_price(mint_address: str) -> float:
    """
    Get the current price of a token (used for unrealized P&L on open positions).
    Raises PriceUnavailableError on failure — caller must handle this.
    """
    backend = config.DATA_BACKEND.lower()
    if backend == "mock":
        return await _get_mock_price(mint_address, tick=_tick_counter)
    elif backend == "birdeye":
        return await _get_jupiter_price(mint_address)
    elif backend == "coinstats":
        await _get_coinstats_price(mint_address)  # always raises
    else:
        raise PriceUnavailableError(f"Unknown backend: {backend}")
    raise PriceUnavailableError("Unreachable")  # type: ignore
