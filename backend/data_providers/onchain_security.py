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
import time

import httpx

import config

log = logging.getLogger(__name__)

MISSING = "__missing__"

VERIFIED_DANGEROUS_EXTENSIONS: tuple[str, ...] = (
    "permanentDelegate",
    "transferHook",
    "nonTransferable",
    "pausableConfig",
)

_MINT_EXTENSION_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS: float = 3600.0  # 1 hour


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


def parse_mint_extensions(data: dict) -> dict:
    """Pure: extracts program and extensions from a jsonParsed mint account data object.

    Accepts data dict (either raw data dict with "program" and "parsed", or the parsed dict).
    Returns {"program": "spl-token" | "spl-token-2022", "extensions": list[str],
             "transfer_fee_bps": int | None, "dangerous_extensions": list[str]}.
    """
    out: dict = {
        "program": "spl-token",
        "extensions": [],
        "transfer_fee_bps": None,
        "dangerous_extensions": [],
    }
    if not isinstance(data, dict):
        return out

    # Determine program
    prog = data.get("program")
    if prog in ("spl-token-2022", "spl-token"):
        out["program"] = prog
    elif data.get("owner") == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb":
        out["program"] = "spl-token-2022"
    elif data.get("owner") == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA":
        out["program"] = "spl-token"

    parsed = data.get("parsed") if "parsed" in data else data
    info = parsed.get("info") if isinstance(parsed, dict) and "info" in parsed else parsed
    if not isinstance(info, dict):
        return out

    extensions_list = info.get("extensions") or []
    if not isinstance(extensions_list, list):
        return out

    ext_names = []
    dangerous = []
    transfer_fee_bps = None
    max_transfer_fee = getattr(config, "MAX_TRANSFER_FEE_BPS", 100)

    for item in extensions_list:
        if not isinstance(item, dict):
            continue
        ext_name = item.get("extension")
        if not ext_name:
            continue
        ext_names.append(ext_name)
        state = item.get("state") or {}

        # 1. Known dangerous extension names
        if ext_name in VERIFIED_DANGEROUS_EXTENSIONS:
            dangerous.append(ext_name)

        # 2. defaultAccountState with state == 'frozen'
        elif ext_name == "defaultAccountState":
            if state.get("accountState") == "frozen":
                dangerous.append("defaultAccountState (frozen)")

        # 3. transferFeeConfig over max_transfer_fee
        elif ext_name == "transferFeeConfig":
            newer = state.get("newerTransferFee") or {}
            older = state.get("olderTransferFee") or {}
            fee = newer.get("transferFeeBasisPoints")
            if fee is None:
                fee = older.get("transferFeeBasisPoints")
            if fee is not None:
                try:
                    fee_int = int(fee)
                    transfer_fee_bps = fee_int
                    if fee_int > max_transfer_fee:
                        dangerous.append(
                            f"transferFeeConfig ({fee_int} bps > {max_transfer_fee} bps)"
                        )
                except (ValueError, TypeError):
                    pass

    out["extensions"] = ext_names
    out["dangerous_extensions"] = dangerous
    out["transfer_fee_bps"] = transfer_fee_bps
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


async def get_mint_extension_flags(mint: str, timeout: float = 10.0) -> dict:
    """Fetch + parse Token-2022 mint extensions across ONCHAIN_RPC_URLS.

    Caches results for 1 hour. Fails closed on unreachable RPC (returns empty dict).
    """
    now = time.monotonic()
    if mint in _MINT_EXTENSION_CACHE:
        cached_ts, cached_data = _MINT_EXTENSION_CACHE[mint]
        if now - cached_ts < CACHE_TTL_SECONDS:
            return cached_data

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [mint, {"encoding": "jsonParsed"}],
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        for endpoint in config.ONCHAIN_RPC_URLS:
            try:
                resp = await client.post(endpoint, json=payload)
                if resp.status_code != 200 or not resp.text.strip():
                    continue
                result = (resp.json() or {}).get("result") or {}
                value = result.get("value") or {}
                data = value.get("data")
                if not isinstance(data, dict):
                    continue
                info = (data.get("parsed") or {}).get("info")
                if isinstance(info, dict) and info.get("type") not in (None, "mint"):
                    continue
                parsed = parse_mint_extensions(data)
                _MINT_EXTENSION_CACHE[mint] = (now, parsed)
                return parsed
            except Exception:
                continue
    log.warning("onchain extensions: every RPC failed for %s", mint[:8])
    return {}

