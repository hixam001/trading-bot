"""
live_execution/solana.py - Solana RPC plumbing with rotating failover.

the reference solana.server.ts parity: try every configured endpoint in order until
one answers; a send is only journalled AFTER on-chain confirmation; nothing
here holds or logs secrets. Preflight stays ON (deliberately stricter than
the reference's skipPreflight=True - a rejected tx costs nothing, a surprise fill does).
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


async def rpc(
    method: str,
    params: list,
    timeout: float = 15.0,
    endpoints: Optional[list[str]] = None,
):
    """JSON-RPC across RPC_URLS; first non-error result wins.
    None = every endpoint failed."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        for endpoint in endpoints or config.RPC_URLS:
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


async def send_raw_transaction(
    raw_signed: bytes, endpoints: Optional[list[str]] = None
) -> Optional[str]:
    """Broadcast the signed tx across RPCs; return the first accepted
    signature, else None."""
    payload = base64.b64encode(raw_signed).decode()
    async with httpx.AsyncClient(timeout=15.0) as client:
        for endpoint in endpoints or config.RPC_URLS:
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


async def confirm_signature(
    signature: str,
    timeout_s: float | None = None,
    endpoints: Optional[list[str]] = None,
) -> dict:
    """
    Poll getSignatureStatuses until confirmed/finalized (2s cadence). A send
    that is never confirmed is NOT a fill and must never be journalled.
    Returns {confirmed: bool, slot: int|None, err: str|None}.
    """
    deadline = asyncio.get_event_loop().time() + (timeout_s or config.CONFIRM_TIMEOUT_SECONDS)
    while asyncio.get_event_loop().time() < deadline:
        res = await rpc(
            "getSignatureStatuses",
            [[signature], {"searchTransactionHistory": False}],
            endpoints=endpoints,
        )
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


async def get_transaction(sig: str) -> dict | None:
    """
    REF-R1: fetch a confirmed transaction by signature for binding verification.

    Returns the raw RPC result dict on success (callers must inspect
    `meta.err == null` for confirmation and extract `blockTime` /
    `transaction.message.accountKeys`), or None on any failure.

    Fail-closed: a None result means the check cannot be run and should
    be reported as `unknown`, not `pass`. Never raises.
    """
    res = await rpc(
        "getTransaction",
        [sig, {"encoding": "jsonParsed",
               "commitment": "confirmed",
               "maxSupportedTransactionVersion": 0}],
    )
    return res if isinstance(res, dict) else None


# ---------------------------------------------------------------------------
# REF-R11 micro-bootstrap: real on-chain USDC funding check.
# ---------------------------------------------------------------------------

# Token-program constants for associated-token-account derivation (mainnet
# program ids are network-wide constants, not configuration).
_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_ATA_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


def _usdc_mint() -> str:
    """Single source of truth: the backend config USDC mint (no drift)."""
    import sys
    from pathlib import Path
    # The package lives inside backend/: backend/ is the grandparent dir.
    backend = Path(__file__).resolve().parent.parent
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from config import USDC_MINT
    return USDC_MINT


def usdc_ata_for(owner: str) -> str:
    """Derive the owner's associated token account for USDC (PDA)."""
    from solders.pubkey import Pubkey  # type: ignore
    owner_pk = Pubkey.from_string(owner)
    mint_pk = Pubkey.from_string(_usdc_mint())
    token_pk = Pubkey.from_string(_TOKEN_PROGRAM_ID)
    ata, _ = Pubkey.find_program_address(
        [bytes(owner_pk), bytes(token_pk), bytes(mint_pk)],
        Pubkey.from_string(_ATA_PROGRAM_ID),
    )
    return str(ata)


async def get_usdc_balance(address: str) -> float | None:
    """
    Real USDC balance of the wallet's USDC associated token account.

    Returns a float (a MISSING token account is a 0.0 balance, not an
    error), or None when the balance cannot be determined at all (every RPC
    unreachable / unparseable). Callers MUST refuse the order on None —
    never assume funding that could not be verified (fail closed; with
    REF-R11 the memo fee is spent before the fill, so an unfunded order
    would burn a commitment for nothing).
    """
    try:
        ata = usdc_ata_for(address)
    except Exception as exc:
        log.info("[solana] usdc ata derivation failed for %s: %s", address[:8], exc)
        return None
    async with httpx.AsyncClient(timeout=15.0) as client:
        for endpoint in config.RPC_URLS:
            try:
                resp = await client.post(
                    endpoint,
                    json={"jsonrpc": "2.0", "id": 1,
                          "method": "getTokenAccountBalance", "params": [ata]},
                )
                if resp.status_code != 200:
                    continue
                body = resp.json()
            except (httpx.HTTPError, ValueError):
                continue
            result = body.get("result")
            if isinstance(result, dict) and isinstance(result.get("value"), dict):
                value = result["value"]
                try:
                    raw_amount = int(value.get("amount"))
                    decimals = int(value.get("decimals"))
                except (TypeError, ValueError):
                    continue
                return raw_amount / (10 ** decimals)
            err_msg = str(((body.get("error") or {}).get("message")) or "").lower()
            if "could not find account" in err_msg or "invalid param" in err_msg:
                return 0.0   # account does not exist -> zero balance
            log.info("[solana] usdc balance refused by %s: %s",
                     endpoint, body.get("error"))
    return None


# ---------------------------------------------------------------------------
# A2 (omo audit §28): chain-derived balance truth.
# ---------------------------------------------------------------------------

_TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


async def get_token_balances(address: str) -> Optional[dict[str, float]]:
    """
    Every SPL token balance > 0 the owner holds, across BOTH token programs
    (legacy + token-2022), as {mint: ui_amount}.

    The chain is the sole authority on HOW MANY tokens the wallet actually
    holds — the journal is cross-checked against this every live cycle
    (live_execution/reconcile.py). Returns None when no RPC answered at all:
    callers must treat that as "unknown", never as "empty" (fail closed —
    an empty read and an unreadable read must never be confused).
    """
    balances: dict[str, float] = {}
    any_ok = False
    for program_id in (_TOKEN_PROGRAM_ID, _TOKEN_2022_PROGRAM_ID):
        res = await rpc(
            "getTokenAccountsByOwner",
            [address, {"programId": program_id}, {"encoding": "jsonParsed"}],
        )
        if res is None:
            continue
        any_ok = True
        for entry in res.get("value") or []:
            try:
                info = entry["account"]["data"]["parsed"]["info"]
                mint = str(info["mint"])
                ta = info["tokenAmount"]
                ui = ta.get("uiAmount")
                if ui is None:
                    ui = int(ta.get("amount") or 0) / (10 ** int(ta.get("decimals") or 0))
                if ui and ui > 0:
                    balances[mint] = balances.get(mint, 0.0) + float(ui)
            except (KeyError, TypeError, ValueError):
                continue
    return balances if any_ok else None


