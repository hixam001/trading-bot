"""
data_providers/crowd.py — REAL crowd conviction for crowd_heat.

Source: the fomo.fun board — the reference bot' exact source
(`prod-api.fomo.family/feed/token/thesis`). FAIL-SOFT: an unavailable feed
just degrades crowd_heat to the presence proxy; it can never block an exit
or stall the loop.

The API sits behind Cloudflare AND firewalls datacenter/residential IPs
(verified live 2026-08-23: a valid bearer still gets a 403 JS challenge on
direct calls). Reads therefore go DIRECT first and automatically fall back
to a FIRECRAWL stealth-proxy scrape when challenged — the reference bot' own
architecture ("fomo family's API firewalls datacenter IPs, so direct
fetches from the worker 403"). Needs FOMO_PRIVY_REFRESH_TOKEN (Privy
session) + FIRECRAWL_API_KEY.

Heat formula is the reference's own: heat = clamp(20 + 8 x thesis_count, 0, 100).

(pump.fun comments were evaluated as a secondary source and DEFERRED —
their legacy API host is dead and the current one 503s even via stealth
proxy; see docs/FOMO_INTEGRATION.md.)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

import config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Log redaction: ZenRows/ScrapeOps/ScrapingBee carry their API key as a URL
# query parameter, and httpx logs full request URLs at INFO — the raw key
# would otherwise land in logs/backend.log. Attach a filter that masks it.
# Installed on both the httpx logger and root so it works under uvicorn
# (where main.setup_logging() never runs) and standalone alike. Idempotent.
# ---------------------------------------------------------------------------
_BS = chr(92)
_KEY_RE = re.compile("(?i)((?:api_?key)=)[^&" + _BS + "s]+")


class _ApiKeyRedactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = _KEY_RE.sub(_BS + "1<REDACTED>", msg)
            if redacted != msg:
                record.msg = redacted
                record.args = None
        except Exception:  # never break logging
            pass
        return True


def _install_key_redactor() -> None:
    for logger_name in ("httpx", ""):
        lg = logging.getLogger(logger_name)
        if not any(isinstance(f, _ApiKeyRedactor) for f in lg.filters):
            lg.addFilter(_ApiKeyRedactor())


_install_key_redactor()

_TIMEOUT = httpx.Timeout(15.0)
# Reference parity: their stealth-proxy call runs on a 25s budget. 45s per hop
# times candidates-per-tick is what stalled ticks ~15 minutes when a provider
# went dead (2026-08-27) — a stealth scrape that has not answered in 25s is
# not about to answer.
_STEALTH_TIMEOUT = httpx.Timeout(25.0)

# ---------------------------------------------------------------------------
# Refresh-token rotation persistence
#
# Privy ROTATES the refresh token on session mint: the response carries a
# fresh one and the previously stored value may stop working. To make the
# bot self-sustaining we persist every rotated token to a gitignored state
# file; on the next mint the PERSISTED token wins over the stale .env copy.
# Setup stays one-time: paste the initial token in .env, let the bot own the
# chain from there. Best practice: extract it from a DEDICATED browser
# profile whose fomo.family tab you never re-login with (the browser and the
# bot then hold independent chains that don't invalidate each other).
# ---------------------------------------------------------------------------


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
    """the reference's formula: heat = clamp(20 + 8 x conviction items, 0, 100)."""
    return max(0, min(100, config.CROWD_HEAT_BASE
                      + config.CROWD_HEAT_PER_SIGNAL * max(0, count)))


def _looks_like_challenge(raw: str) -> bool:
    head = raw.lstrip()[:200].lower()
    return raw.lstrip().startswith("<!doctype html") or "just a moment" in head


def _is_substantive(text: str) -> bool:
    """
    Junk-thesis filter (reference parity): raw invite links, single emojis and
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


def _state_file() -> str:
    """Gitignored sidecar holding the LATEST rotated refresh token."""
    return str(config.FOMO_PRIVY_STATE_FILE)


def _load_persisted_refresh() -> str:
    """
    Newest-known refresh token: the state file if present (it tracks
    rotations), else the bootstrap value from .env. Corrupt file degrades
    to the .env bootstrap rather than hard-failing.
    """
    try:
        with open(_state_file()) as fh:
            value = str(json.load(fh).get("refresh_token") or "")
            if value:
                return value
    except FileNotFoundError:
        pass
    except (ValueError, OSError):
        log.warning("fomo: state file unreadable — falling back to .env token")
    return config.FOMO_PRIVY_REFRESH_TOKEN


def _persist_refresh(value: str) -> None:
    try:
        tmp = _state_file() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"refresh_token": value}, fh)
        os.replace(tmp, _state_file())
        log.info("fomo: rotated refresh token persisted")
    except OSError as exc:
        # Non-fatal: the in-memory chain keeps working this process lifetime;
        # a restart will need a manual re-extract.
        log.error("fomo: could not persist rotated refresh token: %s", exc)


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
    """Exchange the newest refresh token for an access JWT. When Privy
    rotates the refresh token in the response, persist it so the chain is
    self-sustaining across restarts."""
    refresh = _load_persisted_refresh()
    if not app.refresh_token and not refresh:
        return None
    if not refresh:
        refresh = app.refresh_token
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
                json={"refresh_token": refresh},
            )
        if resp.status_code != 200:
            log.warning("privy[%s]: session refresh failed HTTP %s — "
                        "re-extract a fresh token from a re-login",
                        app.origin, resp.status_code)
            return None
        body = resp.json()
        token = body.get("token")
        if not token:
            return None
        # Capture rotation: Privy may hand back a NEW refresh token that
        # invalidates ours. Persist it or the next mint 401s.
        rotated = body.get("refresh_token")
        if rotated and rotated != refresh:
            _persist_refresh(str(rotated))
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
# Transport parity with the reference bot' fomo.server.ts:
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
    """Plain GET. Returns parsed JSON, or None on any failure/challenge.
    Two transport attempts (reference parity — their request() tries twice):
    Cloudflare only waves an IP through occasionally, so one retry is cheap.
    A real HTTP response (even 403) is NOT retried — only transport errors."""
    for attempt in range(2):
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
            log.info("direct get failed for %s (attempt %d): %s",
                     url[:80], attempt + 1, exc)
    return None


_BENCHED_UNTIL: dict[str, float] = {}             # scraper -> monotonic ts
_CONSECUTIVE_ERRORS: dict[str, int] = {}          # scraper -> transport errors in a row
_TRANSPORT_ERROR_BENCH_AFTER = 2   # N consecutive transport failures -> bench


def _bench(name: str, seconds: Optional[float] = None) -> None:
    """Stop using a provider for `seconds` (default STEALTH_BENCH_SECONDS).
    Credit exhaustion (402) gets the long bench; a transient rate-limit (429)
    gets the short STEALTH_THROTTLE_BACKOFF_SECONDS instead."""
    wait = config.STEALTH_BENCH_SECONDS if seconds is None else seconds
    _BENCHED_UNTIL[name] = time.monotonic() + wait
    log.warning("stealth scraper %s benched %.0f min", name, wait / 60.0)


def _is_benched(name: str) -> bool:
    return _BENCHED_UNTIL.get(name, 0.0) > time.monotonic()


def _handle_provider_status(name: str, status: int, body: str) -> None:
    """Bench a provider on quota/throttle signals and surface the provider's
    OWN reason in our logs. 402 = credit exhaustion (long bench); 429 = a
    transient rate-limit (short backoff). Logging the body is what makes e.g.
    ZenRows' AUTH004 "usage limit reached" self-diagnosable (2026-08-27)."""
    snippet = " ".join((body or "").split())[:160]
    if status == 402:
        log.warning("%s: 402 credit exhaustion — %s", name, snippet)
        _bench(name)
        _CONSECUTIVE_ERRORS[name] = 0
    elif status == 429:
        log.warning("%s: 429 rate-limited — %s", name, snippet)
        _bench(name, config.STEALTH_THROTTLE_BACKOFF_SECONDS)
        _CONSECUTIVE_ERRORS[name] = 0


def _transport_error(name: str) -> None:
    """Count a transport failure (timeout / connection error). A provider that
    fails _TRANSPORT_ERROR_BENCH_AFTER times IN A ROW is benched exactly like
    a 402 — a dead provider must cost a couple of timeouts ONCE, never one
    full timeout per candidate for the whole tick (the ~15-minute-tick bug,
    2026-08-27: ScrapingBee ReadTimeouts were never benched)."""
    n = _CONSECUTIVE_ERRORS.get(name, 0) + 1
    _CONSECUTIVE_ERRORS[name] = n
    if n >= _TRANSPORT_ERROR_BENCH_AFTER:
        log.warning("%s: %d consecutive transport errors — benching", name, n)
        _bench(name)
        _CONSECUTIVE_ERRORS[name] = 0


def _transport_success(name: str) -> None:
    """Any completed response (whatever its status) proves the transport
    works — the consecutive-error streak resets."""
    _CONSECUTIVE_ERRORS[name] = 0


def _json_from_body(text: str) -> Optional[dict]:
    """Parse a scrape body into JSON, rejecting HTML challenges and
    provider-level error envelopes. NOTE: prod-api includes statusCode:200
    in SUCCESS envelopes too, so only >=400 counts as an error here."""
    if not text or _looks_like_challenge(text):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict):
        sc = data.get("statusCode")
        if isinstance(sc, int) and sc >= 400:
            return None
    return data


async def _scrape_firecrawl(url: str, headers: dict) -> Optional[dict]:
    """
    the reference bot' own fallback: POST api.firecrawl.dev/v1/scrape with
    proxy:"stealth" — this one FORWARDS our auth headers, which matters
    because prod-api requires the Privy bearer even through a proxy.
    """
    body = {
        "url": url,
        "formats": ["rawHtml"],
        "onlyMainContent": False,
        "proxy": "stealth",
        "headers": {"accept": "application/json", **headers},
    }
    try:
        async with httpx.AsyncClient(timeout=_STEALTH_TIMEOUT) as client:
            resp = await client.post(
                config.FIRECRAWL_SCRAPE_URL,
                headers={"authorization": f"Bearer {config.FIRECRAWL_API_KEY}",
                         "content-type": "application/json"},
                json=body,
            )
    except Exception as exc:
        log.warning("firecrawl: scrape error: %s",
                    str(exc) or type(exc).__name__)
        _transport_error("firecrawl")
        return None
    _transport_success("firecrawl")
    if resp.status_code in (402, 429):
        _handle_provider_status("firecrawl", resp.status_code, resp.text)
        return None
    if resp.status_code != 200:
        log.warning("firecrawl: scrape HTTP %s", resp.status_code)
        return None
    data = resp.json().get("data") or {}
    raw = data.get("rawHtml") or data.get("markdown") or ""
    status = (data.get("metadata") or {}).get("statusCode")
    if status is not None and int(status) >= 400:
        log.warning("firecrawl: origin status %s for %s", status, url[:60])
        return None
    return _json_from_body(raw)


async def _scrape_get_template(name: str, template: str,
                               api_key: str, url: str,
                               fwd_headers: Optional[dict] = None) -> Optional[dict]:
    """
    Shared adapter for GET-return-body stealth services. When the provider
    supports header passthrough (keep_headers / custom_headers), pass
    `fwd_headers` — they are attached to the request to the provider and the
    provider forwards them to the origin (each verified live via httpbin
    echo). This is what lets ZenRows/ScrapeOps carry the Privy bearer.
    """
    from urllib.parse import quote
    target = quote(url, safe="")
    get_url = template.format(api_key=api_key, url=target)
    req_headers = {"accept": "*/*", **(fwd_headers or {})}
    try:
        async with httpx.AsyncClient(timeout=_STEALTH_TIMEOUT) as client:
            resp = await client.get(get_url, headers=req_headers)
    except Exception as exc:
        log.warning("%s: scrape error: %s", name,
                    str(exc) or type(exc).__name__)
        _transport_error(name)
        return None
    _transport_success(name)
    if resp.status_code in (402, 429):
        _handle_provider_status(name, resp.status_code, resp.text)
        return None
    if resp.status_code != 200:
        log.warning("%s: scrape HTTP %s", name, resp.status_code)
        return None
    return _json_from_body(resp.text)


async def _scrape_scrapingbee(url: str, headers: dict) -> Optional[dict]:
    """
    ScrapingBee CANNOT forward our Privy bearer: their platform consumes the
    Authorization header as their own API key (verified live — request dies
    with "Invalid api key" before reaching the origin). This provider stays
    useful for keyless routes and credit-failover breadth only.
    """
    return await _scrape_get_template(
        "scrapingbee",
        "https://app.scrapingbee.com/api/v1/?api_key={api_key}&url={url}"
        "&stealth_proxy=true",
        config.SCRAPINGBEE_API_KEY, url)


async def _scrape_scrapingdog(url: str, headers: dict) -> Optional[dict]:
    return await _scrape_get_template(
        "scrapingdog",
        "https://api.scrapingdog.com/scrape?api_key={api_key}&url={url}"
        "&dynamic=false",
        config.SCRAPINGDOG_API_KEY, url)


async def _scrape_zenrows(url: str, headers: dict) -> Optional[dict]:
    """
    custom_headers=true forwards our caller headers (incl. the Privy
    bearer) to the origin. Zenrows refuses to let us override browser-
    fingerprint headers (user-agent etc.) — it manages those itself — so
    drop them before sending.
    """
    fwd = {k: v for k, v in headers.items() if k.lower() != "user-agent"}
    # premium_proxy is REQUIRED for prod-api.fomo.family (RESP001 otherwise:
    # standard proxies can't pass its Cloudflare). Costs ~10-25 credits per
    # request instead of 1 — remove if you want the cheap tier back.
    return await _scrape_get_template(
        "zenrows",
        "https://api.zenrows.com/v1/?apikey={api_key}&url={url}"
        "&js_render=true&custom_headers=true&premium_proxy=true",
        config.ZENROWS_API_KEY, url, fwd_headers=fwd or None)


async def _scrape_scrapeops(url: str, headers: dict) -> Optional[dict]:
    """keep_headers=true forwards our caller headers (incl. Privy bearer)."""
    return await _scrape_get_template(
        "scrapeops",
        "https://proxy.scrapeops.io/v1/?api_key={api_key}&url={url}"
        "&keep_headers=true",
        config.SCRAPEOPS_API_KEY, url, fwd_headers=dict(headers))


def _configured_scrapers() -> list[tuple[str, Any]]:
    """Preference-ordered stealth scrapers with keys configured."""
    chain: list[tuple[str, Any]] = []
    if config.FIRECRAWL_API_KEY:
        chain.append(("firecrawl", _scrape_firecrawl))
    if config.SCRAPINGBEE_API_KEY:
        chain.append(("scrapingbee", _scrape_scrapingbee))
    if config.SCRAPINGDOG_API_KEY:
        chain.append(("scrapingdog", _scrape_scrapingdog))
    if config.ZENROWS_API_KEY:
        chain.append(("zenrows", _scrape_zenrows))
    if config.SCRAPEOPS_API_KEY:
        chain.append(("scrapeops", _scrape_scrapeops))
    return chain


async def _get_json_via(url: str, headers: dict) -> Optional[dict]:
    """
    DIRECT first (free; works whenever Cloudflare waves the IP through),
    then the stealth-scrape failover chain in preference order. Returns
    parsed JSON or None — callers degrade gracefully either way.
    """
    direct = await _direct_get(url, headers)
    if direct is not None:
        return direct
    scrapers = _configured_scrapers()
    if not scrapers:
        log.warning("no stealth scrapers configured — feed unavailable")
        return None
    for name, fn in scrapers:
        if _is_benched(name):
            continue
        body = await fn(url, headers)
        if body is not None:
            return body
    return None


# ---------------------------------------------------------------------------
# Feed readers
# ---------------------------------------------------------------------------

async def fetch_fomo_theses(mint: str) -> Optional[dict]:
    """
    Theses attached to `mint` on the fomo.fun board, or None when the feed
    is unconfigured/unreachable. Returns {"theses": [...], "total": int}
    where total is the board's OWN count for that token
    (olderThesis + newerThesis + page items — the reference's trick, since the page
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

async def enrich_crowd_heat(candidates: list) -> None:
    """
    Fill candidate.fomo_heat / crowd_heat_source / fomo_theses from the live feeds.
    Fail-soft everywhere: any exception leaves the proxy fallback intact.
    Called from main.run_tick's read stage (before think/gate).
    """
    if not candidates:
        return

    async def _one(candidate) -> None:
        try:
            data = await fetch_fomo_theses(candidate.mint_address)
            if data is not None:
                candidate.fomo_heat = heat_from_count(data["total"])
                candidate.crowd_heat_source = "fomo"
                candidate.fomo_theses = data["theses"]
        except Exception as exc:
            log.info("crowd enrichment skipped for %s: %s",
                     candidate.symbol, exc)

    await asyncio.gather(*(_one(c) for c in candidates),
                         return_exceptions=True)