"""
live_execution/solana.py - Solana RPC plumbing with rotating failover.

omo solana.server.ts parity: try every configured endpoint in order until
one answers; a send is only journalled AFTER on-chain confirmation; nothing
here holds or logs secrets. Preflight stays ON (deliberately stricter than
omos skipPreflight=True - a rejected tx costs nothing, a surprise fill does).
"""
from __future__ import annotations

import asyncio
import base64
import logging

import httpx

from typing import Optional

from live_execution import config

log = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000


async def rpc(method: str, params: list, timeout: float = 15.0):
    """JSON-RPC across RPC_URLS; first non-error result wins.
    None = every endpoint failed."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        for endpoint in config.RPC_URLS:
            try:
                resp = await client.post(
                    endpoint,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                if resp.status_code != 200:
                    continue
                body = resp.json()
            except (httpx.HTTPError, ValueError):
                continue
            if body.get("error") or body.get("result") is None:
                log.info("[solana] %s refused by %s: %s", method, endpoint, body.get("error"))
                continue
            return body["result"]
    return None


async def send_raw_transaction(raw_signed: bytes) -> Optional[str]:
    """Broadcast the signed tx across RPCs; return the first accepted
    signature, else None."""
    payload = base64.b64encode(raw_signed).decode()
    async with httpx.AsyncClient(timeout=15.0) as client:
        for endpoint in config.RPC_URLS:
            try:
                resp = await client.post(
                    endpoint,
                    json={
                        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                        "params": [
                            payload,
                            {
                                "encoding": "base64",
                                "skipPreflight": False,
                                "maxRetries": 3,
                                "preflightCommitment": "confirmed",
                            },
                        ],
                    },
                )
                if resp.status_code != 200:
                    continue
                body = resp.json()
            except (httpx.HTTPError, ValueError):
                continue
            result = body.get("result")
            if isinstance(result, str) and result:
                return result
            log.info("[solana] send refused by %s: %s", endpoint, body.get("error"))
    return None


async def confirm_signature(signature: str, timeout_s: float | None = None) -> dict:
    """
    Poll getSignatureStatuses until confirmed/finalized (2s cadence). A send
    that is never confirmed is NOT a fill and must never be journalled.
    Returns {confirmed: bool, slot: int|None, err: str|None}.
    """
    deadline = asyncio.get_event_loop().time() + (timeout_s or config.CONFIRM_TIMEOUT_SECONDS)
    while asyncio.get_event_loop().time() < deadline:
        res = await rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": False}])
        values = (res or {}).get("value") or []
        if values and values[0]:
            status = values[0]
            if status.get("err"):
                return {"confirmed": False, "slot": status.get("slot"), "err": str(status["err"])}
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                return {"confirmed": True, "slot": status.get("slot"), "err": None}
        await asyncio.sleep(2.0)
    return {"confirmed": False, "slot": None, "err": "not confirmed before timeout"}


async def get_sol_balance(address: str) -> float | None:
    res = await rpc("getBalance", [address])
    if not res:
        return None
    return int(res.get("value", 0)) / LAMPORTS_PER_SOL


async def get_mint_decimals(mint: str) -> int | None:
    """
    Decimals straight from the chain via getTokenSupply. None means UNKNOWN
    and every caller must refuse - never a default fallback (the decimals
    lesson).
    """
    res = await rpc("getTokenSupply", [mint])
    if not res:
        return None
    dec = (res.get("value") or {}).get("decimals")
    return int(dec) if isinstance(dec, int) else None


async def latest_blockhash(endpoint: str) -> str | None:
    """Fresh blockhash for hand-built transactions (drill/devnet use)."""
    res = await rpc("getLatestBlockhash", [{"commitment": "confirmed"}], endpoints=[endpoint])
    if not res:
        return None
    return (res.get("value") or {}).get("blockhash")
