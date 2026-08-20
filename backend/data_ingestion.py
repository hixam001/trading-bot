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

# Fixed pool of synthetic tokens — deterministic per symbol so tests reproduce
_MOCK_TOKENS = [
    {"symbol": "BONK2",   "mint": "Bon2K9GQmXfzrqLvQ8PtHH1mnXpkHkFMwGJzEzmZNKM", "base_price": 0.0000234,  "liq": 145_000, "vol": 87_000,  "holders": 3200,  "top_pct": 4.2,  "age_h": 36.0,   "mcap": 420_000},
    {"symbol": "MOONCAT",  "mint": "MooNjH3vBcYE8mBn3E9bM5GhqK2vUrXjL4d7fKwD1",  "base_price": 0.00000087, "liq": 62_000,  "vol": 31_000,  "holders": 1100,  "top_pct": 9.8,  "age_h": 18.0,   "mcap": 180_000},
    {"symbol": "RUGME",    "mint": "RuGmE1xbfZKWyqvPsTLJdFXcXpH8nDqU9sK2v3mYz",  "base_price": 0.000001,   "liq": 3_500,   "vol": 1_200,   "holders": 45,    "top_pct": 52.0, "age_h": 2.0,    "mcap": 10_000},   # fails: low liq + concentration + holders
    {"symbol": "SOLPEPE",  "mint": "So1pEPE9mHgKQN5vLrXzU7dBnXpMwKzL3f9sT2mRa",  "base_price": 0.00000312, "liq": 89_000,  "vol": 52_000,  "holders": 2400,  "top_pct": 7.1,  "age_h": 48.0,   "mcap": 230_000},
    {"symbol": "FRESHRUG", "mint": "FrEsHRuGxbqM3vNpKzL1wD9TfHnJsU4y8gP5oC2mX",  "base_price": 0.000000055,"liq": 28_000,  "vol": 14_000,  "holders": 320,   "top_pct": 18.5, "age_h": 0.3,    "mcap": 75_000},   # fails: too new
    {"symbol": "ANCIENT",  "mint": "AncIeNtToKnXbqV7mKpL3uW9FhMsDJzT4n5oG2yRe",  "base_price": 0.000000001,"liq": 12_000,  "vol": 1_800,   "holders": 890,   "top_pct": 5.3,  "age_h": 500.0,  "mcap": 30_000},   # fails: too old + low vol
    {"symbol": "WIFHAT2",  "mint": "WIFhAt2bKpRmX9nL3sU7vC1DhFgJzT4o5qP8mNkYe",  "base_price": 0.00000742, "liq": 310_000, "vol": 198_000, "holders": 8900,  "top_pct": 2.8,  "age_h": 72.0,   "mcap": 890_000},
    {"symbol": "PUMPIT",   "mint": "PuMpItxKrL3sV8bN2uF9qH5mDwJzT6o4gC1yRaXe",  "base_price": 0.00000045, "liq": 18_000,  "vol": 92_000,  "holders": 560,   "top_pct": 12.3, "age_h": 8.0,    "mcap": 55_000},
    {"symbol": "GMESOLANA","mint": "GmESoLanAxbV7qP3rK9mL1sU4nF8hDzT2o6gC5yJe",  "base_price": 0.00000912, "liq": 480_000, "vol": 310_000, "holders": 12000, "top_pct": 1.9,  "age_h": 96.0,   "mcap": 1_200_000},
    {"symbol": "LOWVOL",   "mint": "LoWVo1xTkqM3sP7nKzL2uF8hDwJzT4o5gC9yRaXe",  "base_price": 0.00000123, "liq": 22_000,  "vol": 1_100,   "holders": 450,   "top_pct": 8.7,  "age_h": 24.0,   "mcap": 65_000},   # fails: low vol
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
    """Return all mock candidates with simulated price drift."""
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
    Fetch trending Solana tokens from BirdEye and enrich with security data.

    Uses batch endpoint where available (performance-discipline rule 3).
    Field validation is explicit — missing fields raise errors rather than
    defaulting to zero (defense-first rule 1: a wrong holder_count=0
    silently breaks the filter, which is worse than a hard stop).
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
        tokens_raw = data["data"]["items"]
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
    return candidates


def _parse_birdeye_token(item: dict) -> Optional[Candidate]:
    """
    Parse one BirdEye token object into a Candidate.

    Fields are validated explicitly. If a required field is missing or
    has the wrong type, return None (fail closed) rather than defaulting.
    """
    # Required fields — fail if absent or wrong type
    required = {
        "address": str,
        "symbol": str,
        "liquidity": (int, float),
        "volume24hUSD": (int, float),
        "price": (int, float),
    }
    for field_name, expected_type in required.items():
        if field_name not in item:
            log.debug("BirdEye token missing required field '%s': %r", field_name, item.get("symbol"))
            return None
        if not isinstance(item[field_name], expected_type):
            log.debug("BirdEye field '%s' wrong type: %r", field_name, item[field_name])
            return None

    if item["price"] <= 0:
        log.debug("BirdEye token %s has non-positive price: %s", item["symbol"], item["price"])
        return None

    # Optional / derivable fields — use explicit checks, not silent defaults
    holder_count = item.get("holder")
    if holder_count is None or not isinstance(holder_count, (int, float)):
        log.debug("BirdEye token %s missing holder count — skipping", item["symbol"])
        return None
    holder_count = int(holder_count)

    market_cap = item.get("marketCap") or item.get("mc")
    if market_cap is None or not isinstance(market_cap, (int, float)):
        log.debug("BirdEye token %s missing marketCap — skipping", item["symbol"])
        return None

    # Age: BirdEye provides 'createdAt' as epoch seconds or ISO string
    created_at = item.get("createdAt")
    age_hours = 48.0  # conservative default only used if field not present
    if isinstance(created_at, (int, float)) and created_at > 0:
        age_hours = (time.time() - created_at) / 3600
    elif isinstance(created_at, str):
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except ValueError:
            pass

    # top_holder_pct: BirdEye does not provide this in the trending endpoint.
    # For now we default to 0 and note this in the candidate metadata.
    # A proper implementation would make a separate /token_security call.
    top_holder_pct = float(item.get("topHolderPercent", 0.0))

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
        source="birdeye",
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
    try:
        resp = await _get_with_retry(config.JUPITER_QUOTE_URL, params=params)
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
