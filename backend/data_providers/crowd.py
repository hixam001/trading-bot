"""
data_providers/crowd.py — REAL crowd conviction for crowd_heat.

Two sources, tried in order per mint, both FAIL-SOFT (an unavailable feed
just degrades crowd_heat to the presence proxy; it can never block an exit
or stall the loop):

1. fomo.fun board  — omotrades' exact source. `prod-api.fomo.family` sits
   behind Cloudflare and needs a Privy bearer token: the operator extracts
   their refresh token ONCE from a logged-in fomo.family browser session
   (FOMO_PRIVY_REFRESH_TOKEN in .env), and we exchange it at
   auth.privy.io/api/v1/sessions for a ~1h access JWT (cached, re-minted a
   minute early). Endpoint: /feed/token/thesis?tokenAddress=<mint>...
   Response items carry the thesis text AND the author's live position
   (usdValue / unrealizedPnlUsd / realizedPnlUsd / closedAt) — conviction
   with money behind it.

2. pump.fun comments — secondary. The legacy frontend-api host is dead
   (HTTP 530, verified 2026-08-23); PUMPFUN_COMMENTS_URL_TEMPLATE is a
   config constant so pointing it at whatever route the current pump.fun
   web app uses requires zero code changes.

Heat formula is omo's own: heat = clamp(20 + 8 x conviction_items, 0, 100).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Optional

import httpx

import config

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0)


# ---------------------------------------------------------------------------
# Small TTL cache (per-mint) shared by both feeds
# ---------------------------------------------------------------------------

class _TtlCache:
    def __init__(self, ttl_seconds: float):
        self.ttl = ttl_seconds
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        hit = self._data.get(key)
        if hit is None:
            return None
        ts, value = hit
        if time.monotonic() - ts > self.ttl:
            del self._data[key]
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        self._data[key] = (time.monotonic(), value)


_fomo_cache = _TtlCache(config.FOMO_CACHE_TTL_SECONDS)
_pump_cache = _TtlCache(config.PUMPFUN_CACHE_TTL_SECONDS)


def heat_from_count(count: int) -> int:
    """omo's formula: heat = clamp(20 + 8 x conviction items, 0, 100)."""
    return max(0, min(100, config.CROWD_HEAT_BASE
                      + config.CROWD_HEAT_PER_SIGNAL * max(0, count)))


# ---------------------------------------------------------------------------
# Privy session (fomo.fun auth) — ported from omotrades' fomo-auth.server.ts
# ---------------------------------------------------------------------------

_token: Optional[str] = None
_token_expires_at: float = 0.0
_mint_lock = asyncio.Lock()


def _jwt_exp_ms(jwt: str) -> float:
    try:
        part = jwt.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        exp = payload.get("exp")
        return float(exp) * 1000.0 if exp else 0.0
    except Exception:
        return 0.0


async def _mint_session() -> Optional[str]:
    """Exchange the operator's long-lived refresh token for a short-lived
    access token. Returns None when unconfigured or refused."""
    refresh = config.FOMO_PRIVY_REFRESH_TOKEN
    if not refresh:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                config.PRIVY_SESSIONS_URL,
                headers={
                    "content-type": "application/json",
                    "privy-app-id": config.PRIVY_APP_ID,
                    "privy-client": "react-auth:2.28.1",
                    "origin": "https://fomo.family",
                    "referer": "https://fomo.family/",
                },
                json={"refresh_token": refresh},
            )
        if resp.status_code != 200:
            log.warning("fomo: session refresh failed HTTP %s", resp.status_code)
            return None
        token = resp.json().get("token")
        if not token:
            return None
        global _token, _token_expires_at
        _token = token
        _token_expires_at = _jwt_exp_ms(token) or (time.time() * 1000.0 + 45 * 60_000)
        minutes = max(0, int((_token_expires_at - time.time() * 1000.0) / 60_000))
        log.info("fomo: session minted, valid ~%d min", minutes)
        return token
    except Exception as exc:
        log.warning("fomo: session mint error: %s", exc)
        return None


async def _access_token() -> Optional[str]:
    """Valid bearer or None. Re-mints a minute before expiry; single-flight."""
    global _token, _token_expires_at
    if _token and time.time() * 1000.0 < _token_expires_at - 60_000:
        return _token
    async with _mint_lock:
        if _token and time.time() * 1000.0 < _token_expires_at - 60_000:
            return _token
        return await _mint_session()


# ---------------------------------------------------------------------------
# Feed readers
# ---------------------------------------------------------------------------

async def fetch_fomo_theses(mint: str) -> Optional[list[dict]]:
    """
    Theses attached to `mint` on the fomo.fun board, or None when the feed
    is unconfigured/unreachable. Cached per mint for FOMO_CACHE_TTL_SECONDS.
    Each item: {who, text, size_usd, unrealized_usd, realized_usd, pnl_pct,
    closed}.
    """
    cached = _fomo_cache.get(mint)
    if cached is not None:
        return cached

    token = await _access_token()
    if not token:
        return None
    url = (
        f"{config.FOMO_API_BASE}/feed/token/thesis"
        f"?tokenAddress={mint}&networkId={config.FOMO_NETWORK_ID}"
        f"&limit={config.FOMO_THESIS_LIMIT}&threshold=0"
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                url, headers={"authorization": f"Bearer {token}"}
            )
        if resp.status_code != 200:
            log.warning("fomo: thesis feed HTTP %s for %s",
                        resp.status_code, mint[:8])
            return None
        items = (resp.json().get("responseObject") or {}).get("items") or []
    except Exception as exc:
        log.warning("fomo: thesis feed failed for %s: %s", mint[:8], exc)
        return None

    theses: list[dict] = []
    for it in items:
        trade = it.get("authorTrade") or {}
        try:
            theses.append({
                "who": str(it.get("userHandle") or it.get("displayName") or ""),
                "text": str(it.get("text") or ""),
                "size_usd": float(trade.get("usdValue") or 0.0),
                "unrealized_usd": float(trade.get("unrealizedPnlUsd") or 0.0),
                "realized_usd": float(trade.get("realizedPnlUsd") or 0.0),
                "closed": bool(trade.get("closedAt")),
            })
        except (TypeError, ValueError):
            continue
    _fomo_cache.put(mint, theses)
    return theses


async def fetch_pump_comments(mint: str) -> Optional[list[dict]]:
    """
    Comments on `mint` from the configured pump.fun route, or None when
    unreachable/misconfigured (the legacy host is dead; the template points
    at whatever route the current web app uses — see config comment).
    """
    cached = _pump_cache.get(mint)
    if cached is not None:
        return cached
    url = config.PUMPFUN_COMMENTS_URL_TEMPLATE.format(mint=mint)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers={
                "user-agent": "trading-bot/1.0",
                "accept": "application/json",
            })
        if resp.status_code != 200:
            log.info("pumpfun: comments HTTP %s for %s",
                     resp.status_code, mint[:8])
            return None
        body = resp.json()
    except Exception as exc:
        log.info("pumpfun: comments failed for %s: %s", mint[:8], exc)
        return None
    items = body if isinstance(body, list) else (body.get("items") or [])
    comments = [{"who": str(c.get("user", "")), "text": str(c.get("text", ""))}
                for c in items if isinstance(c, dict)]
    _pump_cache.put(mint, comments)
    return comments


# ---------------------------------------------------------------------------
# Enrichment entry point — called once per tick in the READ stage
# ---------------------------------------------------------------------------

async def _heat_for_mint(mint: str) -> tuple[Optional[int], str]:
    """(heat, source) from the best available feed; (None, '') if none."""
    theses = await fetch_fomo_theses(mint)
    if theses is not None:
        return heat_from_count(len(theses)), "fomo"
    comments = await fetch_pump_comments(mint)
    if comments is not None:
        # Comments are weaker conviction than board theses with positions on
        # them; cap their contribution so pump chatter alone can't reach the
        # top of the band.
        capped = min(len(comments), 10)
        return min(100, config.CROWD_HEAT_BASE
                   + config.CROWD_HEAT_PER_SIGNAL * capped), "pumpfun"
    return None, ""


async def enrich_crowd_heat(candidates: list) -> None:
    """
    Fill candidate.fomo_heat / crowd_heat_source from the live feeds.
    Fail-soft everywhere: any exception leaves the proxy fallback intact.
    Called from main.run_tick's read stage (before think/gate).
    """
    if not candidates:
        return

    async def _one(candidate) -> None:
        try:
            heat, source = await _heat_for_mint(candidate.mint_address)
            if heat is not None:
                candidate.fomo_heat = heat
                candidate.crowd_heat_source = source
        except Exception as exc:
            log.info("crowd enrichment skipped for %s: %s",
                     candidate.symbol, exc)

    await asyncio.gather(*(_one(c) for c in candidates),
                         return_exceptions=True)