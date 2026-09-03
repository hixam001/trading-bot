"""
llm/web_research.py - live web search evidence for the think stage.

the reference web-research parity: find what is driving a name attention in
the last 24h and condense it to evidence lines for the thinker. EVIDENCE
ONLY - no verdicts, fail-soft, disabled without any transport.

§51 (2026-09-02): the transport is now a FREE-FIRST chain —
  1. Brave Search API (primary): $5 of free credits auto-applied every
     month (~1,000 searches/month at $5/1k). freshness=pd keeps the 24h
     window parity with the old Firecrawl tbs=qdr:d.
  2. Self-hosted SearXNG (keyless secondary): our own sidecar container
     (deploy/searxng/ + docker-compose.yml) answering format=json —
     public instances disable that format (403), so only OUR instance works.
  3. Firecrawl /v1/search (last-resort paid failover): the §48 incumbent,
     benched on 402 credit exhaustion exactly as before.
Every hop normalizes to the SAME {title, description} row list, so
summarize_hits() and the thinker prompt are byte-identical to §48 — only
the transport changed. Bench semantics mirror crowd.py §34: 402/422 → long
bench, 429 → short backoff, 2 consecutive transport errors → bench.

§48 (2026-08-30): the search left the unconditional read stage — it runs
INSIDE the staged gate (§44 pattern) for the one candidate whose cheap rules
+ crowd rule all passed (i.e. the only candidates the per-candidate thinker
will actually evaluate), behind a TWO-TIER cross-tick TTL cache:
  - a hit (real evidence lines) is cached WEB_SEARCH_CACHE_TTL (default 2h)
  - a miss (no results) is cached only WEB_SEARCH_CACHE_MISS_TTL (default
    30m) so "nothing found" stays reasonably current — a fresh memecoin's
    attention often starts BETWEEN searches, and a long-lived empty cache
    entry would delay first detection.
Cache key is mint_address (symbol only as fallback): two different pump.fun
mints can share a ticker and must never inherit each other's evidence.
"""
from __future__ import annotations

import logging
import time
import httpx

import config
from models import Candidate

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.firecrawl.dev/v1/search"


# ---------------------------------------------------------------------------
# §48 — two-tier cross-tick TTL cache (per mint)
# ---------------------------------------------------------------------------
class _EvidenceCache:
    """mint-keyed TTL cache with separate hit/miss expiry windows."""

    def __init__(self, hit_ttl: float, miss_ttl: float):
        self.hit_ttl = hit_ttl
        self.miss_ttl = miss_ttl
        self._data: dict[str, tuple[float, str]] = {}

    def get(self, key: str) -> tuple[str, bool]:
        """(value, is_fresh). Returns ("", False) on expiry/no-entry."""
        hit = self._data.get(key)
        if hit is None:
            return "", False
        ts, value = hit
        ttl = self.hit_ttl if value else self.miss_ttl
        if time.monotonic() - ts > ttl:
            del self._data[key]
            return "", False
        return value, True

    def put(self, key: str, value: str) -> None:
        self._data[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._data.clear()


_evidence_cache = _EvidenceCache(config.WEB_SEARCH_CACHE_TTL,
                                 config.WEB_SEARCH_CACHE_MISS_TTL)


def cache_key_for(candidate) -> str:
    """Mint-addressed; symbol fallback only when the mint is missing."""
    mint = str(getattr(candidate, "mint_address", "") or "").strip()
    return f"mint:{mint}" if mint else \
        f"sym:{getattr(candidate, 'symbol', '')}"


def summarize_hits(rows) -> str:
    """Condense search result rows into a short evidence string."""
    out = []
    if not isinstance(rows, list):
        return ""
    for r in rows[:3]:
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or "").strip()[:110]
        desc = str(r.get("description") or r.get("markdown") or "").strip()[:160]
        line = (title + " - " + desc).strip(" -")
        if line:
            out.append(line)
    return " | ".join(out)[:600]


# ---------------------------------------------------------------------------
# §51 — chain benching (crowd.py §34 semantics, per search hop)
# ---------------------------------------------------------------------------
_BENCHED_UNTIL: dict[str, float] = {}             # hop -> monotonic ts
_CONSECUTIVE_ERRORS: dict[str, int] = {}          # hop -> transport errors in a row
_TRANSPORT_ERROR_BENCH_AFTER = 2   # N consecutive transport failures -> bench


def reset_search_chain_state() -> None:
    """Test hook: clear the bench + streak dicts between cases."""
    _BENCHED_UNTIL.clear()
    _CONSECUTIVE_ERRORS.clear()


def _bench(name: str, seconds: float = None) -> None:
    wait = config.SEARCH_BENCH_SECONDS if seconds is None else seconds
    _BENCHED_UNTIL[name] = time.monotonic() + wait
    log.warning("search hop %s benched %.0f min", name, wait / 60.0)


def _is_benched(name: str) -> bool:
    return _BENCHED_UNTIL.get(name, 0.0) > time.monotonic()


def _transport_error(name: str) -> None:
    """Count a transport failure; N in a row benches the hop (§34 lesson:
    a dead provider must cost a couple of timeouts ONCE, never one per
    candidate per tick)."""
    n = _CONSECUTIVE_ERRORS.get(name, 0) + 1
    _CONSECUTIVE_ERRORS[name] = n
    if n >= _TRANSPORT_ERROR_BENCH_AFTER:
        log.warning("%s: %d consecutive transport errors — benching", name, n)
        _bench(name)
        _CONSECUTIVE_ERRORS[name] = 0


def _transport_success(name: str) -> None:
    _CONSECUTIVE_ERRORS[name] = 0


def _handle_hop_status(name: str, status: int, body: str) -> None:
    """Bench a hop on quota/throttle signals (crowd.py §34 semantics):
    402 (and Brave's 422) = free/paid credits exhausted -> long bench;
    429 = transient rate-limit -> short backoff. Surfacing the body keeps it
    self-diagnosable."""
    snippet = " ".join((body or "").split())[:160]
    if status in (402, 422):
        log.warning("%s: %s credit/quota exhaustion — %s", name, status,
                    snippet)
        _bench(name)
        _CONSECUTIVE_ERRORS[name] = 0
    elif status == 429:
        log.warning("%s: 429 rate-limited — %s", name, snippet)
        _bench(name, config.SEARCH_THROTTLE_BACKOFF_SECONDS)
        _CONSECUTIVE_ERRORS[name] = 0


# ---------------------------------------------------------------------------
# §51 — hop 1: Brave Search API (free tier, freshness=pd = last 24h)
# ---------------------------------------------------------------------------
async def brave_search(client, symbol: str):
    """GET /res/v1/web/search with the X-Subscription-Token header. Returns
    (rows, status, body) — NEVER raises; a transport failure reports
    status None so the streak machinery can bench after repeats."""
    params = {
        "q": f"{symbol} solana memecoin",
        "count": 5,
        "freshness": "pd",           # past-day: the 24h window parity
        "text_decorations": "false",
    }
    headers = {
        "X-Subscription-Token": config.BRAVE_SEARCH_API_KEY,
        "Accept": "application/json",
    }
    try:
        resp = await client.get(config.BRAVE_SEARCH_URL, params=params,
                                headers=headers)
        body = resp.text
        if resp.status_code != 200:
            return None, resp.status_code, body
        data = resp.json()
    except Exception as exc:
        log.info("brave search %s failed: %s", symbol, exc)
        return None, None, ""
    _transport_success("brave")
    web = data.get("web") if isinstance(data, dict) else None
    rows = web.get("results") if isinstance(web, dict) else None
    if not isinstance(rows, list):
        return [], 200, ""            # answered, nothing found
    return [
        {"title": r.get("title"), "description": r.get("description")}
        for r in rows if isinstance(r, dict)
    ], 200, ""


# ---------------------------------------------------------------------------
# §51 — hop 2: self-hosted SearXNG (keyless, format=json, time_range=day)
# ---------------------------------------------------------------------------
async def searxng_search(client, symbol: str):
    """GET /search?q=…&format=json&time_range=day on OUR instance (public
    instances disable format=json — 403). Same (rows, status, body)
    contract as brave_search; never raises."""
    params = {
        "q": f"{symbol} solana memecoin",
        "format": "json",
        "time_range": "day",          # the 24h window parity
        "safesearch": "0",
    }
    try:
        resp = await client.get(
            config.SEARXNG_URL.rstrip("/") + "/search", params=params,
            headers={"Accept": "application/json"},
            timeout=config.WEB_RESEARCH_TIMEOUT_SECONDS)
        body = resp.text
        if resp.status_code != 200:
            return None, resp.status_code, body
        data = resp.json()
    except Exception as exc:
        log.info("searxng search %s failed: %s", symbol, exc)
        return None, None, ""
    _transport_success("searxng")
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return [], 200, ""
    return [
        {"title": r.get("title"),
         "description": (r.get("content") or r.get("snippet") or "")}
        for r in rows if isinstance(r, dict)
    ], 200, ""



# ---------------------------------------------------------------------------
# §51 — hop 3: Firecrawl /v1/search (last-resort paid failover, §48 shape)
# ---------------------------------------------------------------------------
def _headers() -> dict:
    h = dict()
    h["authorization"] = "Bearer " + config.FIRECRAWL_API_KEY
    h["content-type"] = "application/json"
    return h


def _payload(symbol: str) -> dict:
    return dict(query=symbol + " solana memecoin", limit=5, tbs="qdr:d")


async def firecrawl_search(client, symbol: str):
    """POST /v1/search (the §48 call, verbatim). Same (rows, status, body)
    contract as the free hops; never raises."""
    try:
        req = client.build_request("POST", SEARCH_URL,
                                   json=_payload(symbol),
                                   headers=_headers())
        resp = await client.send(req)
        body = resp.text
        if resp.status_code != 200:
            return None, resp.status_code, body
        data = resp.json()
    except Exception as exc:
        log.info("firecrawl search %s failed: %s", symbol, exc)
        return None, None, ""
    _transport_success("firecrawl")
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return [], 200, ""
    return rows, 200, ""


# ---------------------------------------------------------------------------
# §51 — the chain driver
# ---------------------------------------------------------------------------
def _configured_searchers() -> list[tuple[str, object]]:
    """Preference-ordered search hops with a transport configured. FREE
    first: Brave (free credits) → our SearXNG (keyless) → Firecrawl (paid
    failover while its credits last)."""
    chain: list[tuple[str, object]] = []
    if config.BRAVE_SEARCH_API_KEY:
        chain.append(("brave", brave_search))
    if config.SEARXNG_URL:
        chain.append(("searxng", searxng_search))
    if config.FIRECRAWL_API_KEY:
        chain.append(("firecrawl", firecrawl_search))
    return chain


async def search_web(client, symbol: str) -> str:
    """One search across the free-first chain (last 24h). Condensed
    evidence or empty. Never raises (§48 fail-soft contract).

    Transport failures (status is None) are counted per hop; two in a row
    benches that hop — a dead free transport costs a couple of timeouts
    ONCE, never one per candidate per tick (§34 lesson). Non-200 statuses
    route through _handle_hop_status (402/422/429 bench semantics)."""
    for name, fn in _configured_searchers():
        if _is_benched(name):
            continue
        rows, status, body = await fn(client, symbol)
        if status is None:
            _transport_error(name)               # timeout / conn refused
            continue
        if status != 200:
            _handle_hop_status(name, status, body)
            continue
        if rows:
            return summarize_hits(rows)
        # Answered, nothing found — fall through to the next hop: a
        # multi-engine metasearch may find what the primary didn't (fresh
        # memecoins are often absent from any single index), and an
        # all-empty chain still ends as a cached miss.
    return ""


async def search_for_candidate(c) -> str:
    """
    §48 STAGED entry: search evidence for ONE candidate (cache first, live
    only on miss). Called from decision_pipeline.gate_candidate_staged ONLY
    for candidates whose rules all passed — a candidate that failed a rule
    never reaches here, so it never costs a search (the §44 discipline
    applied to the web-search quota). Fail-soft: errors return "" and the
    thinker simply runs without the web line, exactly as before. §51:
    "any transport configured" replaced "firecrawl key set" — the stage is
    live when Brave OR our SearXNG OR the Firecrawl failover is
    configured, preferring the free hops.
    """
    if not _configured_searchers():
        return ""
    key = cache_key_for(c)
    cached, fresh = _evidence_cache.get(key)
    if fresh:
        return cached
    async with httpx.AsyncClient(
            timeout=config.WEB_RESEARCH_TIMEOUT_SECONDS) as client:
        evidence = await search_web(client, c.symbol)
    _evidence_cache.put(key, evidence)
    if evidence:
        log.info("web research (%s): fresh evidence", c.symbol)
    else:
        log.info("web research (%s): no results (cached %ss)",
                 c.symbol, int(config.WEB_SEARCH_CACHE_MISS_TTL))
    return evidence


async def enrich_web(candidates, client=None, limit=None) -> int:
    """
    §48: LEGACY read-stage entry — RETAINED but no longer called by the
    pipeline (the staged gate calls search_for_candidate instead). Kept as
    a cache-aware convenience for scripts/tests and any external caller:
    fills web_summary for the head of the list WITHOUT spending searches on
    candidates that already have fresh cache entries.
    """
    if not _configured_searchers():
        return 0
    applied = 0
    for c in candidates[: (limit or config.WEB_SEARCH_PER_TICK)]:
        if getattr(c, "web_summary", None):
            continue
        evidence = await search_for_candidate(c)
        if evidence:
            c.web_summary = evidence
            applied += 1
    return applied

