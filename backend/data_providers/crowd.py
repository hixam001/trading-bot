"""
data_providers/crowd.py — REAL crowd conviction for crowd_heat.

Source: the fomo.fun board — omotrades' exact source
(`prod-api.fomo.family/feed/token/thesis`). FAIL-SOFT: an unavailable feed
just degrades crowd_heat to the presence proxy; it can never block an exit
or stall the loop.

The API sits behind Cloudflare AND firewalls datacenter/residential IPs
(verified live 2026-08-23: a valid bearer still gets a 403 JS challenge on
direct calls). Reads therefore go DIRECT first and automatically fall back
to a FIRECRAWL stealth-proxy scrape when challenged — omotrades' own
architecture ("fomo family's API firewalls datacenter IPs, so direct
fetches from the worker 403"). Needs FOMO_PRIVY_REFRESH_TOKEN (Privy
session) + FIRECRAWL_API_KEY.

Heat formula is omo's own: heat = clamp(20 + 8 x thesis_count, 0, 100).

(pump.fun comments were evaluated as a secondary source and DEFERRED —
their legacy API host is dead and the current one 503s even via stealth
proxy; see docs/FOMO_INTEGRATION.md.)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
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


def heat_from_count(count: int) -> int:
    """omo's formula: heat = clamp(20 + 8 x conviction items, 0, 100)."""
    return max(0, min(100, config.CROWD_HEAT_BASE
                      + config.CROWD_HEAT_PER_SIGNAL * max(0, count)))


def _looks_like_challenge(raw: str) -> bool:
    head = raw.lstrip()[:200].lower()
    return raw.lstrip().startswith("<!doctype html") or "just a moment" in head


def _is_substantive(text: str) -> bool:
    """
    Junk-thesis filter (omo parity): raw invite links, single emojis and
    empty noise are not conviction and must not feed crowd_heat.
    """
    t = text.strip()
    if len(t) < 3:
        return False
    stripped = re.sub(r"https?://\S+", "", t)
    stripped = re.sub(r"[^a-z0-9]", "", stripped, flags=re.I)
    if len(stripped) < 3:
        return False
    if re.search(r"discord\.gg|t\.me/|join:", t, flags=re.I) and len(stripped) < 40:
        return False
    return True


# ---------------------------------------------------------------------------
# Privy session minting (fomo.family)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _PrivyApp:
    app_id: str
    refresh_token: str

    @property
    def origin(self) -> str:
        return "fomo.family"


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


# ---------------------------------------------------------------------------
# Transport parity with omotrades' fomo.server.ts:
#   * full browser-like header set on prod-api reads (origin / referer /
#     x-supported-chains are what Cloudflare keys on — omitting them is what
#     caused our earlier 403 challenges)
#   * ONE sequential queue with a 220ms gap between every prod-api call
#     ("four dependable reads beat six simultaneous 429s")
#   * identical reads inside a TTL window share a response (their
#     RESPONSE_TTL = 120s)
#   * 429 -> single backoff, then stealth-proxy fallback
# ---------------------------------------------------------------------------

_FOMO_QUEUE_LOCK = asyncio.Lock()
_LAST_FOMO_CALL = 0.0
_FOMO_CALL_GAP_SECONDS = 0.22


def _fomo_headers(token: str) -> dict:
    return {
        "accept": "*/*",
        "authorization": f"Bearer {token}",
        "origin": "https://fomo.family",
        "referer": "https://fomo.family/",
        "x-supported-chains": str(config.FOMO_NETWORK_ID),
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/151.0.0.0 Safari/537.36"),
    }


async def _direct_get(url: str, headers: dict) -> Optional[dict]:
    """Plain GET. Returns parsed JSON, or None on any failure/challenge."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers={
                "accept": "application/json",
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

async def fetch_fomo_theses(mint: str) -> Optional[dict]:
    """
    Theses attached to `mint` on the fomo.fun board, or None when the feed
    is unconfigured/unreachable. Returns {"theses": [...], "total": int}
    where total is the board's OWN count for that token
    (olderThesis + newerThesis + page items — omo's trick, since the page
    is capped at 40). Cached per mint.
    Each thesis: {who, text, size_usd, unrealized_usd, realized_usd,
    pnl_pct, closed}.
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
    # Sequential queue + 220ms gap: prod-api rate-limits bursts hard.
    global _LAST_FOMO_CALL
    async with _FOMO_QUEUE_LOCK:
        wait = _LAST_FOMO_CALL + _FOMO_CALL_GAP_SECONDS - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        payload = await _get_json_via(url, _fomo_headers(token))
        _LAST_FOMO_CALL = time.monotonic()
    if payload is None:
        return None

    items = (payload.get("responseObject") or {}).get("items") or []
    if not items:
        result = {"theses": [], "total": 0}
        _fomo_cache.put(mint, result)
        return result

    # Board's own total for this token (page is capped at 40):
    first_comment = items[0].get("comment") or {}
    total = (int(first_comment.get("olderThesis") or 0)
             + int(first_comment.get("newerThesis") or 0)
             + len(items))

    theses: list[dict] = []
    for it in items:
        trade = it.get("authorTrade") or {}
        comment = it.get("comment") or {}
        closed = bool(trade.get("closedAt"))
        # Percentage fields are optional in the API response — never
        # float(None) them into a silent skip.
        pct_raw = (trade.get("percentageRealizedPnl") if closed
                   else trade.get("percentageUnrealizedPnl"))
        try:
            thesis = {
                "who": str(it.get("userHandle") or it.get("displayName") or ""),
                "text": str(comment.get("comment") or ""),
                "size_usd": float(trade.get("usdValue") or 0.0),
                "unrealized_usd": float(trade.get("unrealizedPnlUsd") or 0.0),
                "realized_usd": float(trade.get("realizedPnlUsd") or 0.0),
                "pnl_pct": float(pct_raw) if pct_raw is not None else 0.0,
                "closed": closed,
            }
            if _is_substantive(thesis["text"]):
                theses.append(thesis)
        except (TypeError, ValueError):
            continue
    result = {"theses": theses, "total": total}
    _fomo_cache.put(mint, result)
    return result


# ---------------------------------------------------------------------------
# Enrichment entry point — called once per tick in the READ stage
# ---------------------------------------------------------------------------

async def _heat_for_mint(mint: str) -> tuple[Optional[int], str]:
    """(heat, source) from the fomo board; (None, '') if the feed is down."""
    data = await fetch_fomo_theses(mint)
    if data is not None:
        return heat_from_count(data["total"]), "fomo"
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