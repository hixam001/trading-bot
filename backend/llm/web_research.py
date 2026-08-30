"""
llm/web_research.py - live web search evidence for the think stage.

the reference web-research parity via Firecrawl search API: find what is driving a
name attention in the last 24h and condense it to evidence lines for the
thinker. EVIDENCE ONLY - no verdicts, fail-soft, disabled without key.

§48 (2026-08-30): the search left the unconditional read stage — it now runs
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

import asyncio
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



def _headers() -> dict:
    h = dict()
    h["authorization"] = "Bearer " + config.FIRECRAWL_API_KEY
    h["content-type"] = "application/json"
    return h


def _payload(symbol: str) -> dict:
    return dict(query=symbol + " solana memecoin", limit=5, tbs="qdr:d")


async def search_web(client, symbol: str) -> str:
    """One Firecrawl search (last 24h). Condensed evidence or empty."""
    try:
        pl = _payload(symbol)
        req = client.build_request("POST", SEARCH_URL, json=pl,
                                   headers=_headers())
        resp = await client.send(req)
        if resp.status_code != 200:
            log.info("web search %s: HTTP %s", symbol, resp.status_code)
            return ""
        data = resp.json()
    except Exception as exc:
        # Transport/parsing failures are all the same to the caller: no
        # evidence. Broad catch is deliberate — this helper must NEVER
        # raise into the staged gate (fail-soft contract, §48).
        log.info("web search %s failed: %s", symbol, exc)
        return ""
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return ""
    return summarize_hits(rows)


async def search_for_candidate(c) -> str:
    """
    §48 STAGED entry: search evidence for ONE candidate (cache first, live
    only on miss). Called from decision_pipeline.gate_candidate_staged ONLY
    for candidates whose rules all passed — a candidate that failed a rule
    never reaches here, so it never costs a search (the §44 discipline
    applied to the web-search quota). Fail-soft: errors return "" and the
    thinker simply runs without the web line, exactly as before.
    """
    if not config.FIRECRAWL_API_KEY:
        return ""
    key = cache_key_for(c)
    cached, fresh = _evidence_cache.get(key)
    if fresh:
        return cached
    async with httpx.AsyncClient(timeout=15.0) as client:
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
    if not config.FIRECRAWL_API_KEY:
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

