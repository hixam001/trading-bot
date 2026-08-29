"""
data_providers/onchain_security.py - keyless on-chain security checks.

Replaces the Birdeye-only token_security dependency for the two rug vectors
that live directly on-chain: mint authority and freeze authority. Reads the
SPL mint account via getAccountInfo(jsonParsed) across rotating public RPCs.

None semantics preserved: unreachable chain or unparsable account => flags
stay None (= unknown), never fabricated. Honeypot / mutable-metadata remain
Birdeye-only fields and stay None when Birdeye is not configured.
"""
from __future__ import annotations

import logging

import httpx

import config

log = logging.getLogger(__name__)

MISSING = "__missing__"


def parse_mint_authorities(info: dict) -> dict:
    """Pure: revoked-flags from a jsonParsed SPL mint account info object."""
    out = {"mint_authority_revoked": None, "freeze_authority_revoked": None}
    if not isinstance(info, dict):
        return out
    ma = info.get("mintAuthority", MISSING)
    fa = info.get("freezeAuthority", MISSING)
    if ma != MISSING:
        out["mint_authority_revoked"] = ma is None
    if fa != MISSING:
        out["freeze_authority_revoked"] = fa is None
    return out


async def get_authority_flags(mint: str, timeout: float = 10.0) -> dict:
    """Fetch + parse across ONCHAIN_RPC_URLS. Missing keys stay None.

    Two hardening details (2026-08-29, proven against live endpoints):
    1. The FULL JSON-RPC envelope is required: {"jsonrpc": "2.0", "id": 1,
       method, params}. Without jsonrpc/id, api.mainnet-beta returns HTTP 200
       with an EMPTY body (rate-limit masquerading as success —
       x-ratelimit-endpoint-remaining goes negative) and publicnode returns
       400 "Parse error". This bug shipped silently because a 200-with-
       empty-body fails only at resp.json() — the old code never sent the
       envelope, so the fallback NEVER worked.
    2. A 200 with an empty/unparseable body is a FAILURE (rotate to the next
       endpoint), never "no data" — and the failure to parse any endpoint is
       logged once per mint, not once per endpoint.
    """
    payload = {"jsonrpc": "2.0", "id": 1,
               "method": "getAccountInfo",
               "params": [mint, {"encoding": "jsonParsed"}]}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for endpoint in config.ONCHAIN_RPC_URLS:
            try:
                resp = await client.post(endpoint, json=payload)
                if resp.status_code != 200:
                    continue
                if not resp.text.strip():
                    # mainnet-beta's rate-limited response: 200 + empty body.
                    # Not a parseable success — try the next endpoint.
                    continue
                result = (resp.json() or {}).get("result") or {}
                value = result.get("value") or {}
                info = ((value.get("data") or {}).get("parsed") or {}).get("info")
                if not isinstance(info, dict):
                    continue
                if info.get("type") is not None and info.get("type") != "mint":
                    continue   # parsed account is not an SPL mint — unusable
                return parse_mint_authorities(info)
            except (httpx.HTTPError, ValueError):
                continue
    log.warning("onchain security: every RPC failed for %s", mint[:8])
    return {}
