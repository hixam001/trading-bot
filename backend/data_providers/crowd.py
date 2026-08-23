"""
data_providers/crowd.py — REAL crowd conviction for crowd_heat.

Two sources, tried in order per mint, both FAIL-SOFT (an unavailable feed
just degrades crowd_heat to the presence proxy; it can never block an exit
or stall the loop):

1. fomo.fun board  — omotrades' exact source. `prod-api.fomo.family` sits
   behind Cloudflare AND firewalls datacenter/residential IPs (verified live
   2026-08-23: a valid bearer still gets a 403 JS challenge on direct calls).
   Reads therefore go DIRECT first and automatically fall back to a FIRECRAWL
   stealth-proxy scrape when challenged — omotrades' own architecture ("fomo
   family's API firewalls datacenter IPs, so direct fetches from the worker
   403"). Needs FOMO_PRIVY_REFRESH_TOKEN (Privy session) + FIRECRAWL_API_KEY.

2. pump.fun comments — secondary, same transport. The legacy frontend-api
   host is dead (HTTP 530, verified 2026-08-23). PUMPFUN_COMMENTS_URL_TEMPLATE
   points at whatever route the current web app uses; pump.fun also uses
   Privy auth, so PUMPFUN_PRIVY_REFRESH_TOKEN / PUMPFUN_PRIVY_APP_ID are
   supported the same way (bearer attached when present).

Heat formula is omo's own: heat = clamp(20 + 8 x conviction_items, 0, 100).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

import config

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0)
_FIRECRAWL_TIMEOUT = httpx.Timeout(45.0)


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


def _looks_like_challenge(raw: str) -> bool:
    head = raw.lstrip()[:200].lower()
    return raw.lstrip().startswith("<!doctype html") or "just a moment" in head


# ---------------------------------------------------------------------------
# Generic Privy session minting (fomo.family AND pump.fun both use Privy)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _PrivyApp:
    app_id: str
    refresh_token: str

    @property
    def origin(self) -> str:
        return ("pump.fun"
                if self.app_id == _PUMPFUN_APP_ID_DEFAULT
                else "fomo.family")


# pump.fun's own Privy app id, extracted from their bundle
# (privy.pump.fun/recovery?recovery_app_id=...). Public identifier.
_PUMPFUN_APP_ID_DEFAULT = "cm1p2gzot03fzqty5xzgjgthq"


_sessions: dict[str, tuple[str, float]] = {}      # app_id -> (jwt, exp_ms)
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(app_id: str) -> asyncio.Lock:
    if app_id not in _locks:
        _locks[app_id] = asyncio.Lock()
    return _locks[app_id]


def _jwt_exp_ms(jwt: str) -> float:
    try:
        part = jwt.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        exp = payload.get("exp")
        return float(exp) * 1000.0 if exp else 0.0
    except Exception:
        return 0.0


async def _mint_privy_session(app: _PrivyApp) -> Optional[str]:
    """Exchange the operator's long-lived refresh token for an access JWT."""
    if not app.refresh_token:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                config.PRIVY_SESSIONS_URL,
                headers={
                    "content-type": "application/json",
                    "privy-app-id": app.app_id,
                    "privy-client": "react-auth:2.28.1",
                    "origin": f"https://{app.origin}",
                    "referer": f"https://{app.origin}/",
                },
                json={"refresh_token": app.refresh_token},
            )
        if resp.status_code != 200:
            log.warning("privy[%s]: session refresh failed HTTP %s",
                        app.origin, resp.status_code)
            return None
        token = resp.json().get("token")
        if not token:
            return None
        exp_ms = _jwt_exp_ms(token) or (time.time() * 1000.0 + 45 * 60_000)
        _sessions[app.app_id] = (token, exp_ms)
        minutes = max(0, int((exp_ms - time.time() * 1000.0) / 60_000))
        log.info("privy[%s]: session minted, valid ~%d min", app.origin, minutes)
        return token
    except Exception as exc:
        log.warning("privy[%s]: session mint error: %s", app.origin, exc)
        return None


async def _access_token(app: _PrivyApp) -> Optional[str]:
    """Valid bearer or None. Re-mints a minute before expiry; single-flight."""
    cached = _sessions.get(app.app_id)
    if cached and time.time() * 1000.0 < cached[1] - 60_000:
        return cached[0]
    lock = _lock_for(app.app_id)
    async with lock:
        cached = _sessions.get(app.app_id)
        if cached and time.time() * 1000.0 < cached[1] - 60_000:
            return cached[0]
        return await _mint_privy_session(app)


def fomo_app() -> Optional[_PrivyApp]:
    if not config.FOMO_PRIVY_REFRESH_TOKEN:
        return None
    return _PrivyApp(config.PRIVY_APP_ID, config.FOMO_PRIVY_REFRESH_TOKEN)


def pump_app() -> Optional[_PrivyApp]:
    """Pump.fun Privy session — only when the operator configured one."""
    if not config.PUMPFUN_PRIVY_REFRESH_TOKEN:
        return None
    # An EMPTY env line must fall back to pump's own app id, never fomo's.
    app_id = (config.PUMPFUN_PRIVY_APP_ID or "").strip() or _PUMPFUN_APP_ID_DEFAULT
    return _PrivyApp(app_id, config.PUMPFUN_PRIVY_REFRESH_TOKEN)


# ---------------------------------------------------------------------------
# Transport: DIRECT first, FIRECRAWL stealth-scrape on Cloudflare challenge
# ---------------------------------------------------------------------------

async def _direct_get(url: str, headers: dict) -> Optional[dict]:
    """Plain GET. Returns parsed JSON, or None on any failure/challenge."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers={
                "accept": "application/json",
                "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/126.0.0.0 Safari/537.36"),
                **headers,
            })
        if resp.status_code != 200 or _looks_like_challenge(resp.text):
            return None
        return resp.json()
    except Exception as exc:
        log.info("direct get failed for %s: %s", url[:80], exc)
        return None


async def _firecrawl_get_json(url: str, headers: dict) -> Optional[dict]:
    """
    Same GET through Firecrawl's stealth proxy (omo's own fallback path).
    Returns parsed JSON or None. Requires FIRECRAWL_API_KEY.
    """
    if not config.FIRECRAWL_API_KEY:
        return None
    body = {
        "url": url,
        "formats": ["rawHtml"],
        "onlyMainContent": False,
        "proxy": "stealth",
        "headers": {"accept": "application/json", **headers},
    }
    try:
        async with httpx.AsyncClient(timeout=_FIRECRAWL_TIMEOUT) as client:
            resp = await client.post(
                config.FIRECRAWL_SCRAPE_URL,
                headers={"authorization": f"Bearer {config.FIRECRAWL_API_KEY}",
                         "content-type": "application/json"},
                json=body,
            )
        if resp.status_code != 200:
            log.warning("firecrawl: scrape HTTP %s", resp.status_code)
            return None
        data = resp.json().get("data") or {}
        raw = data.get("rawHtml") or data.get("markdown") or ""
        status = (data.get("metadata") or {}).get("statusCode")
        if status is not None and int(status) >= 400:
            # The proxy reached the origin but the origin rejected the read.
            log.warning("firecrawl: origin status %s for %s", status, url[:60])
            return None
        if not raw or _looks_like_challenge(raw):
            log.warning("firecrawl: blocked at origin (status=%s)", status)
            return None
        return json.loads(raw)
    except Exception as exc:
        log.warning("firecrawl: scrape error: %s", exc)
        return None


async def _get_json_via(url: str, headers: dict) -> Optional[dict]:
    """DIRECT first; Firecrawl stealth proxy when challenged/blocked."""
    direct = await _direct_get(url, headers)
    if direct is not None:
        return direct
    return await _firecrawl_get_json(url, headers)


# ---------------------------------------------------------------------------
# Feed readers
# ---------------------------------------------------------------------------

async def fetch_fomo_theses(mint: str) -> Optional[list[dict]]:
    """
    Theses attached to `mint` on the fomo.fun board, or None when the feed
    is unconfigured/unreachable. Cached per mint for FOMO_CACHE_TTL_SECONDS.
    Each item: {who, text, size_usd, unrealized_usd, realized_usd, closed}.
    """
    cached = _fomo_cache.get(mint)
    if cached is not None:
        return cached

    app = fomo_app()
    if app is None:
        return None
    token = await _access_token(app)
    if not token:
        return None

    url = (
        f"{config.FOMO_API_BASE}/feed/token/thesis"
        f"?tokenAddress={mint}&networkId={config.FOMO_NETWORK_ID}"
        f"&limit={config.FOMO_THESIS_LIMIT}&threshold=0"
    )
    payload = await _get_json_via(url, {"authorization": f"Bearer {token}"})
    if payload is None:
        return None

    items = (payload.get("responseObject") or {}).get("items") or []
    theses: list[dict] = []
    for it in items:
        trade = it.get("authorTrade") or {}
        # The thesis text lives NESTED: item["comment"]["comment"]
        comment = it.get("comment") or {}
        try:
            theses.append({
                "who": str(it.get("userHandle") or it.get("displayName") or ""),
                "text": str(comment.get("comment") or ""),
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
    unreachable/misconfigured. Bearer attached when a pump Privy session is
    configured. Cached per mint for PUMPFUN_CACHE_TTL_SECONDS.
    """
    cached = _pump_cache.get(mint)
    if cached is not None:
        return cached

    headers: dict = {}
    app = pump_app()
    if app is not None:
        token = await _access_token(app)
        if token:
            headers["authorization"] = f"Bearer {token}"

    url = config.PUMPFUN_COMMENTS_URL_TEMPLATE.format(mint=mint)
    body = await _get_json_via(url, headers)
    if body is None:
        return None
    # Error bodies ({"statusCode": 404, ...}) must read as failure, not as
    # an empty comment list.
    if isinstance(body, dict) and "statusCode" in body:
        log.info("pumpfun: origin error body for %s", mint[:8])
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