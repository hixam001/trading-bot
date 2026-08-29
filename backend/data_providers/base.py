"""
data_providers/base.py — MarketDataProvider protocol + HTTP discipline.

Every external call goes through fetch_json(): explicit timeout, bounded
retry with backoff, DISTINCT handling for HTTP 429 (capacity signal, not
just an error), and a per-provider daily counter row (A7/A8).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Protocol

import httpx

import config

log = logging.getLogger(__name__)


class MarketDataProvider(Protocol):
    async def get_candidates(self, limit: int) -> list[Any]: ...
    async def get_current_price(self, mint_address: str,
                                decimals: Optional[int] = None) -> float: ...
    async def get_security_info(self, mint_address: str) -> Any: ...


async def record_call(provider: str, ok: bool, rate_limited: bool = False) -> None:
    """Best-effort counter write; never let telemetry break the pipeline."""
    try:
        from api import db as _db
        async with _db.get_db() as conn:
            await _db.record_provider_call(conn, provider, ok=ok, rate_limited=rate_limited)
    except Exception:
        log.warning("record_call(%s): counter write failed", provider, exc_info=True)


class ProviderError(Exception):
    """Raised when an external provider cannot satisfy a request."""


class RateLimitedError(ProviderError):
    """Distinct exception so callers can treat 429 as a capacity signal."""


class ProviderAuthError(ProviderError):
    """
    401/403: the key/tier does not entitle this endpoint. NOT transient —
    retrying is pure waste, so fetch_json raises this immediately.
    """


class ProviderQuotaError(ProviderError):
    """
    The provider answered 400 with an explicit quota/body message (e.g.
    Birdeye's {"success": false, "message": "Compute units usage limit
    exceeded"}). The key's compute budget is spent for now — retrying is
    pure waste, so fetch_json raises this immediately. Callers self-disable
    the surface for the session (same treatment as 401/403).
    """


def _looks_like_quota_error(resp: httpx.Response) -> bool:
    """
    Best-effort quota-body sniff (2026-08-29). Birdeye's compute-units
    exhaustion is a 400 whose body explicitly says so; a generic 400 (a bad
    address, a malformed param) must keep the normal retry/fail path. Only
    known quota phrasings trip this — never a guess.
    """
    body = resp.text or ""
    if not body:
        return False
    needle = body[:400].lower()
    return ("limit exceeded" in needle
            or "compute units" in needle
            or "quota" in needle)


async def fetch_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    provider: str,
    params: dict | None = None,
    headers: dict | None = None,
) -> dict[str, Any]:
    """
    GET -> parsed JSON with bounded retries, backoff, and 429-specific
    handling. Raises RateLimitedError / ProviderError after the final
    attempt — never returns a guessed default (fail closed).
    """
    last_exc: Exception | None = None
    for attempt in range(1, config.EXTERNAL_API_MAX_RETRIES + 1):
        rate_limited = False
        failed = False
        try:
            resp = await client.get(
                url, params=params, headers=headers,
                timeout=config.EXTERNAL_API_TIMEOUT_SECONDS,
            )
            if resp.status_code == 429:
                rate_limited = True
                failed = True
                raise RateLimitedError(f"{provider}: HTTP 429 from {url}")
            if resp.status_code == 400 and _looks_like_quota_error(resp):
                # Key budget spent (Birdeye's compute-units exhaustion arrives
                # as 400 + {"success": false, "message": "...limit exceeded"}).
                # NOT transient — stop before the retry loop burns the tick.
                await record_call(provider, ok=False)
                raise ProviderQuotaError(
                    f"{provider}: quota/compute limit exceeded from {url} "
                    f"({resp.text[:120]})"
                )
            resp.raise_for_status()
            data = resp.json()
            await record_call(provider, ok=True)
            return data
        except ProviderQuotaError:
            raise   # already counted + messaged above; never retried
        except httpx.HTTPStatusError as exc:
            failed = True
            last_exc = exc
            if exc.response.status_code in (401, 403):
                # Auth/tier problem: not transient. Never retry — surface
                # immediately so callers can degrade gracefully instead of
                # burning the tick on backoffs against a forbidden endpoint.
                raise ProviderAuthError(
                    f"{provider}: HTTP {exc.response.status_code} from {url} "
                    f"(key/tier does not entitle this endpoint)"
                ) from exc
            log.warning("%s call failed (attempt %d/%d): %s",
                        provider, attempt, config.EXTERNAL_API_MAX_RETRIES, exc)
        except RateLimitedError as exc:
            last_exc = exc
            failed = True
            log.warning("rate_limited: %s attempt %d/%d", exc, attempt,
                        config.EXTERNAL_API_MAX_RETRIES)
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            failed = True
            log.warning("%s call failed (attempt %d/%d): %s",
                        provider, attempt, config.EXTERNAL_API_MAX_RETRIES, exc)
        if failed:
            # Backoff: longer and distinct on 429.
            delay = (
                config.RATE_LIMIT_EXTRA_BACKOFF_SECONDS * attempt if rate_limited
                else config.EXTERNAL_API_RETRY_BACKOFF_SECONDS * attempt
            )
            await asyncio.sleep(delay)
            await record_call(provider, ok=False, rate_limited=rate_limited)

    raise ProviderError(f"{provider}: giving up on {url} after "
                        f"{config.EXTERNAL_API_MAX_RETRIES} attempts") from last_exc


def require_type(value: Any, expected: type | tuple, field: str, source: str) -> Optional[Any]:
    """
    Explicit validation for external API fields (defense-first rule 1 /
    anti-pattern: .get(key, default)). Returns the value if it matches,
    else None (= unknown — never a fabricated default).
    """
    if isinstance(value, bool) and expected in (int, float):
        # bool is a subclass of int; a boolean is NOT a valid numeric field.
        return None
    if isinstance(value, expected) and not isinstance(value, bool):
        return value
    log.debug("%s: field %r has unexpected type %s", source, field, type(value).__name__)
    return None
