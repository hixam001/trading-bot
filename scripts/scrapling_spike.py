"""
scripts/scrapling_spike.py — Phase-0 drill (2026-08-30, §47 prep).

Read-only validation of Scrapling as the LOCAL stealth transport for the
fomo.fun crowd feed, BEFORE wiring it into data_providers/crowd.py.

Tests the three hops the integration will use, in order:
  1. curl-cffi impersonated direct GET (AsyncFetcher) — the upgraded
     free "direct first" hop (Chrome TLS fingerprint instead of httpx's).
  2. Scrapling stealth browser (AsyncStealthySession, patchright Chromium,
     solve_cloudflare=True) — the paid-chain replacement hop.
  3. (reference) plain httpx direct — what the bot does today, for a fair
     success-rate comparison.

Auth reuses the bot's own Privy minting (data_providers.crowd.fomo_app +
_access_token + _fomo_headers) so the drill exercises the REAL auth chain.
NO trades, no DB writes, no state changes — pure reads.

Usage (repo root, venv active):
    .venv/bin/python scripts/scrapling_spike.py            # 3 mints x 3 hops
    .venv/bin/python scripts/scrapling_spike.py --n 20     # more samples
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx

import config
import data_providers.crowd as crowd


# Recent mints the live cycle actually scraped (logs/live_cycle.log).
DEFAULT_MINTS = [
    "8xH8ikqGXNTSYmmUVakCE2tVwU7aYJwz2JZkqAjW88sG",   # TREE (21 reads today)
    "5REUXJSTd84s8hxkfEKcrDdAFL7fsqLAm7c3BeEzpump",
    "CTPoyCwkjMvoJwU4xvZZqoD8tiYk6yDchySiN5gGpump",
    "SV151D5pjygAKA8aJJcKzm4wFnRX5G92Fye94jQJk7g",
]

THESIS_URL = (
    f"{config.FOMO_API_BASE}/feed/token/thesis"
    f"?tokenAddress={{mint}}&networkId={config.FOMO_NETWORK_ID}"
    f"&limit={{limit}}&threshold=0"
)


def looks_like_json_ok(text: str) -> tuple[bool, str]:
    """(parsed_ok, reason). Same discipline as crowd._json_from_body."""
    if not text:
        return False, "empty body"
    head = text.lstrip()[:200].lower()
    if text.lstrip().startswith("<!doctype html") or "just a moment" in head:
        return False, "CF challenge page"
    try:
        data = json.loads(text)
    except ValueError:
        return False, "not JSON"
    if isinstance(data, dict):
        sc = data.get("statusCode")
        if isinstance(sc, int) and sc >= 400:
            return False, f"origin statusCode {sc}"
    items = ((data.get("responseObject") or {}).get("items"))
    if not isinstance(items, list):
        return False, "JSON but no responseObject.items"
    return True, f"{len(items)} theses"


async def hop_httpx(url: str, headers: dict) -> tuple[bool, str, float]:
    """What the bot does today — plain httpx direct read."""
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers={"accept": "*/*", **headers})
        ok, reason = looks_like_json_ok(r.text)
        return ok, f"HTTP {r.status_code} — {reason}", time.monotonic() - t0
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:90], time.monotonic() - t0


async def hop_curl_cffi(url: str, headers: dict) -> tuple[bool, str, float]:
    """Hop 1 of the integration: curl-cffi impersonated GET (Chrome TLS)."""
    t0 = time.monotonic()
    try:
        from scrapling.fetchers import AsyncFetcher
        r = await AsyncFetcher.get(
            url, impersonate="chrome", headers=headers,
            timeout=15, follow_redirects=True,
        )
        body = r.body if isinstance(r.body, str) else (
            r.body or b"").decode("utf-8", "ignore")
        ok, reason = looks_like_json_ok(body)
        return ok, f"HTTP {r.status} — {reason}", time.monotonic() - t0
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:90], time.monotonic() - t0


async def hop_stealth(url: str, headers: dict, session) -> tuple[bool, str, float]:
    """Hop 2 of the integration: the warm stealth browser with CF solver."""
    t0 = time.monotonic()
    try:
        # NOT user-agent: the browser must keep a UA matching its own
        # fingerprint (patchright generates one); forwarding ours would
        # be a UA/fingerprint mismatch detection vector.
        extra = {k: v for k, v in headers.items()
                 if k.lower() not in ("user-agent", "accept")}
        page = await session.fetch(url, extra_headers=extra)
        raw = page.body
        if isinstance(raw, bytes):
            body = raw.decode("utf-8", "ignore")
        else:
            body = raw if isinstance(raw, str) else ""
        ok, reason = looks_like_json_ok(body)
        return ok, f"HTTP {page.status} — {reason}", time.monotonic() - t0
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:120], time.monotonic() - t0


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=3, help="rounds over the mint list")
    p.add_argument("--keep-open", action="store_true",
                   help="keep the browser session open between rounds (default: "
                        "close+reopen each round to measure cold start too)")
    args = p.parse_args()

    app = crowd.fomo_app()
    if app is None:
        print("FATAL: FOMO_PRIVY_REFRESH_TOKEN not set — cannot mint a session.")
        return 1
    print("minting Privy session…")
    token = await crowd._access_token(app)
    if not token:
        print("FATAL: Privy session mint failed.")
        return 1
    headers = crowd._fomo_headers(token)
    minutes = max(0, int((crowd._sessions[app.app_id][1] - time.time() * 1000) / 60000))
    print(f"session OK (valid ~{minutes} min)\n")

    from scrapling.fetchers import AsyncStealthySession
    session = None
    stats = {"httpx": [], "curl_cffi": [], "stealth": []}
    try:
        mints = (DEFAULT_MINTS * ((args.n + len(DEFAULT_MINTS) - 1)
                                  // len(DEFAULT_MINTS)))[:args.n]
        for rnd, mint in enumerate(mints, 1):
            url = THESIS_URL.format(mint=mint, limit=config.FOMO_THESIS_LIMIT)
            print(f"--- round {rnd}/{len(mints)}  mint {mint[:8]}…")
            for name, fn in (("httpx", hop_httpx),
                             ("curl_cffi", hop_curl_cffi)):
                ok, why, secs = await fn(url, headers)
                stats[name].append((ok, secs))
                print(f"  [{name:9s}] {'OK ' if ok else 'FAIL'} "
                      f"{secs:5.2f}s  {why}")
            if session is None:
                t0 = time.monotonic()
                session = AsyncStealthySession(
                    headless=True, solve_cloudflare=True,
                    disable_resources=True, google_search=False,
                    network_idle=False, timeout=30_000,
                )
                await session.__aenter__()
                print(f"  [browser   ] cold start {time.monotonic()-t0:.2f}s")
            ok, why, secs = await hop_stealth(url, headers, session)
            stats["stealth"].append((ok, secs))
            print(f"  [stealth   ] {'OK ' if ok else 'FAIL'} "
                  f"{secs:5.2f}s  {why}")
            if not args.keep_open:
                await session.__aexit__(None, None, None)
                session = None
            await asyncio.sleep(2.5)   # be a polite visitor, ~prod gap
    finally:
        if session is not None:
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                pass

    print("\n=== RESULTS ===")
    verdict = {}
    for name, rows in stats.items():
        if not rows:
            continue
        oks = [s for ok, s in rows if ok]
        lat = sorted(s for _, s in rows)
        p50 = lat[len(lat) // 2]
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
        rate = 100.0 * len(oks) / len(rows)
        verdict[name] = rate
        print(f"{name:10s} success {len(oks)}/{len(rows)} ({rate:5.1f}%)  "
              f"p50 {p50:5.2f}s  p95 {p95:5.2f}s")
    s = verdict.get("stealth", 0.0)
    print("\n=== GATE (stealth hop) ===")
    if s >= 80:
        print(f"{s:.0f}% ≥ 80% → PROCEED; after a 1-week shadow the paid "
              "scrape keys can go.")
    elif s >= 40:
        print(f"{s:.0f}% in 40–80% → PROCEED but keep paid keys as failover "
              "indefinitely.")
    else:
        print(f"{s:.0f}% < 40% → IP/endpoint hard-blocked from this machine; "
              "set SCRAPLING_PROXY or shelve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
