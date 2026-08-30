"""
data_providers/stealth_browser.py — LOCAL stealth transport for the fomo.fun
crowd feed (§47, Scrapling integration).

Two free hops replace the paid stealth-proxy chain's job, both validated by
the 2026-08-30 Phase-0 drill (scripts/scrapling_spike.py, 6/6 success each):

  1. curl-cffi impersonated GET  — fomo's Cloudflare blocks plain httpx on
     TLS fingerprint (JA3), not on auth. `scrapling.fetchers.AsyncFetcher`
     speaks Chrome's exact TLS handshake, which passes 100% from a
     residential IP WITHOUT any browser (p50 0.42s). This is now the
     direct-first hop inside crowd._direct_get.
  2. the stealth browser          — AsyncStealthySession (patchright
     undetected Chromium, solve_cloudflare=True) for the harder days. Warm
     single session, tabs reused (Scrapling 0.4.15), idle auto-close so a
     quiet bot doesn't hold ~300–500 MB of Chromium forever.

DESIGN CONTRACT (mirrors every fail-soft provider in this repo):
  - NOTHING here may raise into the tick: every public entry returns
    Optional[dict]; any failure -> None -> caller falls to the next hop.
  - Browser is created LAZILY and only in live mode; the module is imported
    function-locally so mock runs / tests never touch scrapling at all
    (hermeticity contract, same discipline as §43/§44).
  - One session per process, guarded by a lock; idle > SCRAPLING_IDLE_CLOSE
    seconds -> close and drop it; N consecutive failures -> hard recreate.
  - NEVER forward our hand-set user-agent to the browser: patchright
    generates a UA matching its own fingerprint; a mismatched one is a
    detection vector (Phase-0 drill note).

Scrapling is BSD-3-Clause (credit: README "Open-source credits").
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import config

log = logging.getLogger(__name__)

# Consecutive transport/parse failures after which the browser session is
# torn down and rebuilt on the next call (a zombie Chromium must not linger).
_MAX_CONSECUTIVE_FAILURES = 3

_session: Optional[object] = None
_session_lock = asyncio.Lock()
_last_used: float = 0.0
_consecutive_failures: int = 0
_import_ok: Optional[bool] = None


def _body_text(response) -> str:
    """Scrapling returns .body as bytes (browser) or str/bytes (curl-cffi)."""
    raw = getattr(response, "body", None)
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "ignore")
    return raw if isinstance(raw, str) else ""


def looks_like_json_ok(text: str) -> bool:
    """Same discipline as crowd._looks_like_challenge + _json_from_body:
    reject CF challenge pages, provider/origin error envelopes, non-JSON."""
    if not text:
        return False
    head = text.lstrip()[:200].lower()
    if text.lstrip().startswith("<!doctype html") or "just a moment" in head:
        return False
    try:
        data = json.loads(text)
    except ValueError:
        return False
    if isinstance(data, dict):
        sc = data.get("statusCode")
        if isinstance(sc, int) and sc >= 400:
            return False
    return True


async def curl_get(url: str, headers: dict,
                   timeout_seconds: float = 15.0) -> Optional[dict]:
    """
    The free impersonated GET (hop 1). Chrome TLS fingerprint via curl-cffi.
    Returns parsed JSON or None — NEVER raises. Import is function-local so
    a host without scrapling installed degrades to None (caller's chain
    continues) instead of breaking the process.
    """
    try:
        from scrapling.fetchers import AsyncFetcher
    except ImportError:
        return None
    try:
        r = await AsyncFetcher.get(
            url, impersonate="chrome", headers=headers,
            timeout=int(timeout_seconds), follow_redirects=True,
        )
        if int(getattr(r, "status", 0) or 0) != 200:
            return None
        text = _body_text(r)
        if not looks_like_json_ok(text):
            return None
        return json.loads(text)
    except Exception as exc:
        log.info("scrapling curl hop failed for %s: %s",
                 url[:70], type(exc).__name__)
        return None


async def _get_browser_session():
    """Lazily create (or reuse) the ONE warm AsyncStealthySession."""
    global _session, _last_used, _import_ok
    if _session is not None:
        _last_used = time.monotonic()
        return _session
    try:
        from scrapling.fetchers import AsyncStealthySession
    except ImportError:
        _import_ok = False
        raise
    _session = AsyncStealthySession(
        headless=True,
        solve_cloudflare=True,      # Turnstile/interstitial solver
        disable_resources=True,     # drop font/image/media/… requests
        google_search=False,        # no synthetic Google referer
        network_idle=False,         # JSON API — no idle wait needed
        timeout=config.SCRAPLING_TIMEOUT_MS,
    )
    await _session.__aenter__()
    _last_used = time.monotonic()
    return _session


async def _close_session() -> None:
    global _session
    if _session is None:
        return
    try:
        await _session.__aexit__(None, None, None)
    except Exception:
        pass
    _session = None


async def browser_fetch_json(url: str, headers: dict) -> Optional[dict]:
    """
    The stealth-browser hop (hop 2). Warm session, tab reuse, CF solver.
    Returns parsed JSON or None — NEVER raises into the tick.

    headers: the caller's fomo headers (incl. the Privy bearer). user-agent
    and accept are deliberately NOT forwarded to the browser (see module
    docstring — patchright's own UA must match its fingerprint).
    """
    global _consecutive_failures
    async with _session_lock:
        if not getattr(config, "SCRAPLING_ENABLED", True):
            return None
        if _import_ok is False:
            return None
        try:
            session = await _get_browser_session()
        except ImportError:
            return None
        except Exception as exc:
            log.warning("stealth browser failed to start: %s",
                        type(exc).__name__)
            _consecutive_failures += 1
            if _consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                await _close_session()
                _consecutive_failures = 0
            return None
        try:
            extra = {k: v for k, v in headers.items()
                     if k.lower() not in ("user-agent", "accept")}
            page = await session.fetch(url, extra_headers=extra)
            if int(getattr(page, "status", 0) or 0) != 200:
                _consecutive_failures += 1
                if _consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    await _close_session()
                    _consecutive_failures = 0
                return None
            text = _body_text(page)
            if not looks_like_json_ok(text):
                _consecutive_failures += 1
                if _consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    await _close_session()
                    _consecutive_failures = 0
                return None
            _consecutive_failures = 0
            return json.loads(text)
        except Exception as exc:
            log.info("stealth browser fetch failed for %s: %s",
                     url[:70], type(exc).__name__)
            _consecutive_failures += 1
            if _consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                await _close_session()
                _consecutive_failures = 0
            return None


async def idle_housekeeping() -> None:
    """Close the browser after SCRAPLING_IDLE_CLOSE_SECONDS of no fetches.
    Called opportunistically (not on a timer): crowd.py invokes it after
    every feed read, so a quiet bot holds zero Chromium memory."""
    if _session is None:
        return
    idle_for = time.monotonic() - _last_used
    if idle_for > config.SCRAPLING_IDLE_CLOSE_SECONDS:
        async with _session_lock:
            if _session is not None and \
                    time.monotonic() - _last_used > config.SCRAPLING_IDLE_CLOSE_SECONDS:
                log.info("stealth browser idle %.0fs — closing", idle_for)
                await _close_session()


async def shutdown() -> None:
    """Process-exit hook: close the browser cleanly (best-effort)."""
    async with _session_lock:
        await _close_session()

