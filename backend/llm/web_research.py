"""
llm/web_research.py - live web search evidence for the think stage.

the reference web-research parity via Firecrawl search API: find what is driving a
name attention in the last 24h and condense it to evidence lines for the
thinker. EVIDENCE ONLY - no verdicts, fail-soft, disabled without key.
"""
from __future__ import annotations

import asyncio
import logging
import httpx

import config
from models import Candidate

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.firecrawl.dev/v1/search"


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
    except (httpx.HTTPError, ValueError) as exc:
        log.info("web search %s failed: %s", symbol, exc)
        return ""
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return ""
    return summarize_hits(rows)


async def enrich_web(candidates, client=None, limit=None) -> int:
    """Web-search the board head into web_summary. Fail-soft, keyless-off."""
    if not config.FIRECRAWL_API_KEY:
        return 0
    picks = [c for c in candidates if not getattr(c, "web_summary", None)]
    picks = picks[:limit or config.WEB_SEARCH_PER_TICK]
    if not picks:
        return 0
    own = client is None
    client = client if client is not None else httpx.AsyncClient()
    sem = asyncio.Semaphore(4)

    async def one(c):
        async with sem:
            try:
                return await search_web(client, c.symbol)
            except Exception:
                return ""
    results = await asyncio.gather(*(one(c) for c in picks), return_exceptions=True)
    if own:
        await client.aclose()
    applied = 0
    for c, res in zip(picks, results):
        if isinstance(res, Exception) or not res:
            continue
        c.web_summary = res
        applied += 1
    if applied:
        log.info("web research: %s/%s candidates got evidence", applied, len(picks))
    return applied
