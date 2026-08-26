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
    """Fetch + parse across ONCHAIN_RPC_URLS. Missing keys stay None."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        for endpoint in config.ONCHAIN_RPC_URLS:
            try:
                payload = dict(method="getAccountInfo", params=[mint, dict(encoding="jsonParsed")])
                resp = await client.post(endpoint, json=payload)
                if resp.status_code != 200:
                    continue
                result = (resp.json() or {}).get("result") or {}
                value = result.get("value") or {}
                info = ((value.get("data") or {}).get("parsed") or {}).get("info")
                if not isinstance(info, dict):
                    continue
                return parse_mint_authorities(info)
            except (httpx.HTTPError, ValueError):
                continue
    log.warning("onchain security: every RPC failed for %s", mint[:8])
    return {}
