"""
api/rate_limiter.py — Server-enforced sliding-window rate limiting.

Implements Rule 11:
- Stricter limits on authentication endpoints (login, signup, password reset):
  prevents brute-force credential stuffing and password guessing.
- Stricter limits on paid service endpoints (AI models, RPC, LLM health):
  prevents bill inflation and API quota exhaustion.
- General limits on standard API routes: prevents denial of service.
- Server-enforced: cannot be bypassed by client-forged headers or spoofing.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

import config

log = logging.getLogger(__name__)


def get_client_ip(request: Request) -> str:
    """Extract real client IP with anti-spoofing enforcement (§55 / Rule 11).

    Security guarantee:
      - If socket connection is direct (non-loopback external IP), we NEVER trust
        client-provided X-Forwarded-For headers, preventing spoofing / bypass.
      - If socket is loopback (behind our local reverse proxy, e.g. Caddy),
        we take the leftmost IP injected by the trusted proxy.
    """
    client_host = getattr(getattr(request, "client", None), "host", "unknown") or "unknown"
    if client_host in ("127.0.0.1", "::1", "localhost", "testclient"):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first_ip = forwarded.split(",")[0].strip()
            if first_ip:
                return first_ip
    return client_host


def classify_tier(path: str, method: str) -> tuple[str, int]:
    """Classify an incoming request path into its rate limit tier.

    Tiers:
      1. 'auth' (strict): login, signup, password reset
      2. 'paid_service' (strict): calls AI models, LLM health, or external RPC
      3. 'general': all other /api/* endpoints
      4. 'exempt': static assets and UI shell
    """
    norm_path = path.rstrip("/")

    # Tier 1: Auth & Credential endpoints (login, signup, password reset)
    if norm_path in (
        "/api/auth/login",
        "/api/auth/signup",
        "/api/auth/reset-password",
        "/api/auth/forgot-password",
    ):
        return "auth", config.RATE_LIMIT_AUTH_PER_MINUTE

    # Tier 2: Paid Services & AI / LLM / External RPC endpoints
    # - /api/knowledge-base/ingest: triggers AI LLM summarization (_llm_digest)
    # - /api/system-status: probes active LLM provider (_llm_health)
    # - /api/safety: checks live risk state and LLM health probe
    # - /api/holdings: calls market price RPC
    # - /api/stats: calls Solana balance RPC
    # - /api/verify.json: calls Solana transaction RPC
    if norm_path in (
        "/api/knowledge-base/ingest",
        "/api/system-status",
        "/api/safety",
        "/api/holdings",
        "/api/stats",
        "/api/verify.json",
    ):
        return "paid_service", config.RATE_LIMIT_PAID_PER_MINUTE

    # Tier 3: General API endpoints
    if norm_path.startswith("/api"):
        return "general", config.RATE_LIMIT_GENERAL_PER_MINUTE

    # Non-API routes (SPA shell, static assets) are exempt
    return "exempt", 0


class SlidingWindowRateLimiter:
    """In-memory sliding window rate limiter."""

    def __init__(self, max_tracked: int = 20_000) -> None:
        self._requests: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._max_tracked = max_tracked

    def clear(self) -> None:
        """Clear tracking memory (used in testing and maintenance)."""
        self._requests.clear()

    def check(
        self,
        tier: str,
        client_ip: str,
        limit: int,
        window: float = 60.0,
    ) -> tuple[bool, int, int, int]:
        """Check whether request is allowed under the sliding window.

        Returns:
            (allowed: bool, limit: int, remaining: int, retry_after_or_reset: int)
        """
        now = time.time()
        cutoff = now - window
        key = (tier, client_ip)

        # Prune expired timestamps for this key
        timestamps = [t for t in self._requests[key] if t > cutoff]
        self._requests[key] = timestamps

        # Bound total tracked keys to protect server memory
        if len(self._requests) > self._max_tracked:
            for k in list(self._requests.keys()):
                fresh = [t for t in self._requests[k] if t > cutoff]
                if fresh:
                    self._requests[k] = fresh
                else:
                    del self._requests[k]

        count = len(timestamps)
        if count >= limit:
            oldest = timestamps[0] if timestamps else now
            retry_after = max(1, int(window - (now - oldest)))
            return False, limit, 0, retry_after

        # Record this request
        timestamps.append(now)
        remaining = max(0, limit - len(timestamps))
        reset_epoch = int(now + window)
        return True, limit, remaining, reset_epoch


limiter = SlidingWindowRateLimiter()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Enforces server-side tiered rate limits on incoming HTTP requests."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if not getattr(config, "RATE_LIMIT_ENABLED", True):
            return await call_next(request)

        tier, limit = classify_tier(request.url.path, request.method)
        if tier == "exempt" or limit <= 0:
            return await call_next(request)

        client_ip = get_client_ip(request)
        allowed, limit_val, remaining, retry_or_reset = limiter.check(
            tier, client_ip, limit
        )

        if not allowed:
            log.warning(
                "rate limit exceeded: tier=%s ip=%s path=%s retry_after=%ds",
                tier, client_ip, request.url.path, retry_or_reset,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "detail": (
                        f"Rate limit exceeded for {tier} — rate limited. "
                        f"Try again in {retry_or_reset} seconds."
                    )
                },
                headers={
                    "Retry-After": str(retry_or_reset),
                    "X-RateLimit-Limit": str(limit_val),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time() + retry_or_reset)),
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit_val)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(retry_or_reset)
        return response

