"""
backend/tests/test_s6_rate_limit_buckets.py — §92 S6 limiter bucket proofs.

The limiter is the only thing between an unauthenticated caller and expensive
or sensitive routes. These tests pin the properties an attacker would try to
break:

  * buckets are keyed by (tier, client IP), so a general request cannot spend
    the auth budget and an IP change needs a real new peer;
  * every 429 carries a positive Retry-After and a coherent reset epoch;
  * the window really slides (a blocked caller recovers after it expires);
  * tracked key memory is hard-bounded even while every key is fresh.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import config
from api import rate_limiter
from api.rate_limiter import RateLimitMiddleware, SlidingWindowRateLimiter


def _clock(monkeypatch, value: float):
    monkeypatch.setattr(rate_limiter, "time",
                        SimpleNamespace(time=lambda: value))


def test_buckets_are_per_tier_and_per_client_ip(monkeypatch):
    _clock(monkeypatch, 1_000.0)
    limiter = SlidingWindowRateLimiter(max_tracked=100)

    assert limiter.check("auth", "203.0.113.1", 2)[0] is True
    assert limiter.check("auth", "203.0.113.1", 2)[0] is True
    assert limiter.check("auth", "203.0.113.1", 2)[0] is False

    # A different tier and a different client each have their own bucket.
    assert limiter.check("general", "203.0.113.1", 2)[0] is True
    assert limiter.check("paid_service", "203.0.113.1", 2)[0] is True
    assert limiter.check("auth", "203.0.113.2", 2)[0] is True


def test_sliding_window_expires_and_retry_after_is_positive(monkeypatch):
    _clock(monkeypatch, 1_000.0)
    limiter = SlidingWindowRateLimiter()

    allowed, limit, remaining, reset = limiter.check(
        "general", "203.0.113.9", 1, window=60.0)
    assert (allowed, limit, remaining) == (True, 1, 0)
    assert reset == 1_060

    allowed, limit, remaining, retry_after = limiter.check(
        "general", "203.0.113.9", 1, window=60.0)
    assert allowed is False
    assert remaining == 0
    assert retry_after == 60

    # One second before expiry still blocked; one second after it is allowed.
    _clock(monkeypatch, 1_059.5)
    assert limiter.check("general", "203.0.113.9", 1, window=60.0)[0] is False
    _clock(monkeypatch, 1_060.1)
    assert limiter.check("general", "203.0.113.9", 1, window=60.0)[0] is True


def test_tracked_key_memory_is_hard_bounded_while_fresh(monkeypatch):
    _clock(monkeypatch, 1_000.0)
    limiter = SlidingWindowRateLimiter(max_tracked=8)

    for i in range(200):
        limiter.check("general", f"198.51.100.{i}", 5, window=60.0)

    assert len(limiter._requests) <= 8
    assert all(limiter._requests[k] for k in limiter._requests)


def test_new_clients_are_counted_even_when_memory_is_full(monkeypatch):
    _clock(monkeypatch, 1_000.0)
    limiter = SlidingWindowRateLimiter(max_tracked=2)

    # Fill the LRU to capacity with other clients.
    for i in range(10):
        limiter.check("general", f"198.51.100.{i}", 5, window=60.0)
    assert len(limiter._requests) == 2

    # A brand-new client must get a real bucket, not a silently unrecorded
    # request: the first two calls pass, the third is blocked.
    assert limiter.check("general", "203.0.113.50", 2)[0] is True
    assert limiter.check("general", "203.0.113.50", 2)[0] is True
    assert limiter.check("general", "203.0.113.50", 2)[0] is False
    assert len(limiter._requests) <= 2


def test_expired_keys_are_dropped(monkeypatch):
    _clock(monkeypatch, 1_000.0)
    limiter = SlidingWindowRateLimiter(max_tracked=100)
    limiter.check("general", "198.51.100.7", 5, window=60.0)
    assert len(limiter._requests) == 1

    _clock(monkeypatch, 1_061.0)
    limiter.check("general", "198.51.100.8", 5, window=60.0)
    keys = {k[1] for k in limiter._requests}
    assert "198.51.100.7" not in keys
    assert "198.51.100.8" in keys


async def test_429_carries_retry_after_and_reset_headers(monkeypatch):
    _clock(monkeypatch, 2_000.0)
    rate_limiter.limiter.clear()
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_GENERAL_PER_MINUTE", 1)

    app = FastAPI()

    @app.get("/api/feed")
    async def feed():
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://127.0.0.1") as client:
        first = await client.get("/api/feed")
        second = await client.get("/api/feed")

    assert first.status_code == 200
    assert first.headers["x-ratelimit-limit"] == "1"
    assert first.headers["x-ratelimit-remaining"] == "0"

    assert second.status_code == 429
    retry = int(second.headers["retry-after"])
    reset = int(second.headers["x-ratelimit-reset"])
    assert retry > 0
    assert 2_000 < reset <= 2_060
    assert second.headers["x-ratelimit-remaining"] == "0"
    assert "general" in second.json()["detail"]
    rate_limiter.limiter.clear()


def test_retry_after_never_reports_zero_or_negative(monkeypatch):
    _clock(monkeypatch, time.time())
    limiter = SlidingWindowRateLimiter()
    limiter.check("auth", "198.51.100.77", 1)
    allowed, _, _, retry_after = limiter.check("auth", "198.51.100.77", 1)
    assert allowed is False
    assert retry_after >= 1


@pytest.mark.parametrize("path,tier", [
    ("/api/auth/login", "auth"),
    ("/api/auth/signup", "auth"),
    ("/api/knowledge-base/ingest", "paid_service"),
    ("/api/system-status", "paid_service"),
    ("/api/holdings", "paid_service"),
    ("/api/feed", "general"),
    ("/", "exempt"),
    ("/assets/index.js", "exempt"),
])
def test_route_tier_classification(path, tier):
    assert rate_limiter.classify_tier(path, "GET")[0] == tier
