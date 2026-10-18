"""
live_execution/solana.py - Solana RPC plumbing with rotating failover.

the reference solana.server.ts parity: try every configured endpoint in order until
one answers; a send is only journalled AFTER on-chain confirmation; nothing
here holds or logs secrets. Preflight is enabled (skipPreflight=False, deliberately stricter
than the reference's skipPreflight=True - a rejected tx costs nothing, a surprise fill does).
"""
from __future__ import annotations

import asyncio
import base64
import logging

import httpx

from typing import Optional, NamedTuple

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
from dataclasses import dataclass
from urllib.parse import urlparse

# ExecutionError and SendRejected are defined in exit_errors.py so that
# SendRejected can subclass ExecutionError without a circular import.
# Re-export them here so existing callers that do
# `from live_execution.solana import ExecutionError` continue to work unchanged.
from live_execution.exit_errors import ExecutionError, SendRejected  # noqa: F401 — re-export



class SimulationError(Exception):
    """Raised when on-chain simulation or RPC verification fails."""


@dataclass(frozen=True)
class SignedSwap:
    signature: str
    raw: bytes
    _checked: bool = False


async def send_checked_swap(
    signed: SignedSwap,
    endpoints: Optional[list[str]] = None,
) -> Optional[str]:
    """Broadcast a swap transaction that has passed static inspection and pre-sign simulation."""
    if not isinstance(signed, SignedSwap) or not getattr(signed, "_checked", False):
        raise ExecutionError("send_checked_swap refused: transaction was not verified by sign_after_checks")
    return await send_raw_transaction(signed.raw, endpoints=endpoints)


def _get_async_client(timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def _dedup_endpoints_by_host(endpoints: list[str]) -> list[str]:
    seen_hosts = set()
    deduped = []
    for ep in endpoints:
        try:
            host = urlparse(ep).netloc
        except Exception:
            host = ep
        if host and host not in seen_hosts:
            seen_hosts.add(host)
            deduped.append(ep)
    return deduped


def check_rpc_diversity(endpoints: Optional[list[str]] = None) -> tuple[int, bool]:
    """Check number of distinct RPC hosts. Returns (distinct_host_count, is_diverse)."""
    eps = endpoints or config.RPC_URLS
    deduped = _dedup_endpoints_by_host(eps)
    distinct_count = len(deduped)
    is_diverse = distinct_count >= 2
    if not is_diverse:
        log.critical("RPC_NOT_DIVERSE: config.RPC_URLS has fewer than 2 distinct hosts (%d hosts: %s)",
                     distinct_count, [urlparse(e).netloc for e in eps])
        try:
            from alerts import alert_nowait, AlertEvent
            alert_nowait(
                AlertEvent.RPC_NOT_DIVERSE,
                f"RPC endpoints have fewer than 2 distinct hosts ({distinct_count} hosts: {[urlparse(e).netloc for e in eps]})",
                key="rpc_not_diverse",
            )
        except Exception:
            pass
    return distinct_count, is_diverse


def responses_agree(
    a: dict,
    b: dict,
    wallet_owned_addrs: list[str],
    all_addresses: Optional[list[str]] = None,
) -> bool:
    """
    Pure agreement rule comparing two simulation responses a and b (§81 Phase C-2):
    - Both err equal (both None or equal JSON/dict)
    - Same account count
    - For payer and every address in wallet_owned_addrs:
      - decoded token amount within SIM_AMOUNT_TOLERANCE_BPS (absolute floor 1 unit)
      - same owner
      - exact delegate pubkey match
      - exact close_authority pubkey match
      - exact delegated_amount match
      - exact state match
      - payer lamports within SIM_LAMPORT_TOLERANCE
    """
    if a.get("err") != b.get("err"):
        return False

    accs_a = a.get("post_accounts", [])
    accs_b = b.get("post_accounts", [])
    if len(accs_a) != len(accs_b):
        return False

    from live_execution.jupiter_executor import (
        _SPL_TOKEN_PROGRAM_ID,
        _TOKEN_2022_PROGRAM_ID,
    )
    from live_execution.sim_guard import decode_token_account_data

    # Map addresses to accounts if all_addresses is provided, else match by index
    addr_to_a: dict[str, dict | None] = {}
    addr_to_b: dict[str, dict | None] = {}
    if all_addresses and len(all_addresses) == len(accs_a):
        addr_to_a = dict(zip(all_addresses, accs_a))
        addr_to_b = dict(zip(all_addresses, accs_b))
    else:
        # Compare index by index for all available accounts
        for i in range(len(accs_a)):
            addr_to_a[str(i)] = accs_a[i]
            addr_to_b[str(i)] = accs_b[i]

    # Check payer lamports (index 0 / first account)
    if accs_a and accs_b:
        p_a = accs_a[0]
        p_b = accs_b[0]
        if isinstance(p_a, dict) and isinstance(p_b, dict):
            lamp_a = p_a.get("lamports", 0)
            lamp_b = p_b.get("lamports", 0)
            if abs(lamp_a - lamp_b) > config.SIM_LAMPORT_TOLERANCE:
                return False

    # Check wallet-owned accounts
    check_addrs = [addr for addr in wallet_owned_addrs if addr in addr_to_a] if all_addresses else list(addr_to_a.keys())
    for addr in check_addrs:
        acc_a = addr_to_a.get(addr)
        acc_b = addr_to_b.get(addr)
        if acc_a is None and acc_b is None:
            continue
        if (acc_a is None) != (acc_b is None):
            return False
        if not isinstance(acc_a, dict) or not isinstance(acc_b, dict):
            continue

        owner_a = acc_a.get("owner")
        owner_b = acc_b.get("owner")
        if owner_a != owner_b:
            return False

        lamp_a = acc_a.get("lamports", 0)
        lamp_b = acc_b.get("lamports", 0)
        if abs(lamp_a - lamp_b) > config.SIM_LAMPORT_TOLERANCE:
            return False

        # Token account comparisons
        if owner_a in (_SPL_TOKEN_PROGRAM_ID, _TOKEN_2022_PROGRAM_ID):
            data_a = acc_a.get("data")
            data_b = acc_b.get("data")
            raw_a = b""
            raw_b = b""
            if isinstance(data_a, list) and data_a:
                raw_a = base64.b64decode(data_a[0])
            elif isinstance(data_a, str):
                raw_a = base64.b64decode(data_a)
            if isinstance(data_b, list) and data_b:
                raw_b = base64.b64decode(data_b[0])
            elif isinstance(data_b, str):
                raw_b = base64.b64decode(data_b)

            try:
                dec_a = decode_token_account_data(raw_a)
                dec_b = decode_token_account_data(raw_b)
            except Exception:
                return False

            if dec_a["owner"] != dec_b["owner"]:
                return False
            if dec_a.get("delegate") != dec_b.get("delegate"):
                return False
            if dec_a.get("close_authority") != dec_b.get("close_authority"):
                return False
            if dec_a.get("delegated_amount") != dec_b.get("delegated_amount"):
                return False
            if dec_a.get("state") != dec_b.get("state"):
                return False

            amt_a = dec_a["amount"]
            amt_b = dec_b["amount"]
            diff = abs(amt_a - amt_b)
            max_allowed = max(1, int(max(amt_a, amt_b) * config.SIM_AMOUNT_TOLERANCE_BPS / 10000))
            if diff > max_allowed:
                return False

    return True


async def get_multiple_accounts(
    addresses: list[str],
    endpoints: Optional[list[str]] = None,
    side: str = "buy",
    wallet_owned_addresses: Optional[list[str]] = None,
) -> list[dict | None]:
    """Fetch multiple accounts across distinct RPC endpoints with quorum agreement."""
    if not addresses:
        return []

    target_endpoints = _dedup_endpoints_by_host(endpoints or config.RPC_URLS)[:3]
    if not target_endpoints:
        raise SimulationError("no RPC endpoints configured")

    params = [addresses, {"encoding": "base64", "commitment": "confirmed"}]

    async def _fetch(ep: str) -> dict:
        async with _get_async_client(timeout=8.0) as client:
            resp = await client.post(
                ep,
                json={"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts", "params": params},
            )
            if resp.status_code != 200:
                raise SimulationError(f"{ep}: HTTP {resp.status_code}")
            body = resp.json()
            if body.get("error") or body.get("result") is None:
                raise SimulationError(f"{ep}: {body.get('error')}")
            val = body["result"].get("value")
            if not isinstance(val, list):
                raise SimulationError(f"{ep}: malformed getMultipleAccounts response")
            return {"endpoint": ep, "err": None, "post_accounts": val}

    raw_results = await asyncio.gather(*[_fetch(ep) for ep in target_endpoints], return_exceptions=True)
    valid_results = [r for r in raw_results if isinstance(r, dict)]

    if not valid_results:
        raise SimulationError("getMultipleAccounts failed across all RPCs")

    agreeing = 0
    if valid_results:
        agreeing = 1
        for other in valid_results[1:]:
            if responses_agree(valid_results[0], other, wallet_owned_addresses or addresses, all_addresses=addresses):
                agreeing += 1

    is_sell = (side == "sell")
    if agreeing >= config.SIM_MIN_AGREEING_RPCS:
        return valid_results[0]["post_accounts"]
    elif is_sell and config.SIM_SELL_ALLOW_SINGLE_RPC and len(valid_results) >= 1:
        log.warning("SIM_SINGLE_RPC_SELL: pre-state fetch proceeding with 1 RPC response (%s)", valid_results[0]["endpoint"])
        try:
            from alerts import alert_nowait, AlertEvent
            alert_nowait(
                AlertEvent.SIM_SINGLE_RPC_SELL,
                f"Pre-state fetch proceeding with single RPC endpoint ({valid_results[0]['endpoint']})",
                key="sim_single_rpc_sell",
            )
        except Exception:
            pass
        return valid_results[0]["post_accounts"]
    else:
        raise SimulationError(
            f"getMultipleAccounts quorum not reached: {len(valid_results)} responses, {agreeing} agreeing (min {config.SIM_MIN_AGREEING_RPCS})"
        )


async def simulate_transaction(
    tx_b64: str,
    accounts: list[str],
    endpoints: Optional[list[str]] = None,
    side: str = "buy",
    wallet_owned_addresses: Optional[list[str]] = None,
    require_dual_rpc: bool = True,
) -> dict:
    """Simulate a transaction against Solana RPC with account state inspection and dual-RPC consensus."""
    params = [
        tx_b64,
        {
            "sigVerify": False,
            "replaceRecentBlockhash": True,
            "commitment": "confirmed",
            "encoding": "base64",
            "accounts": {
                "addresses": accounts,
                "encoding": "base64",
            },
        },
    ]

    target_endpoints = _dedup_endpoints_by_host(endpoints or config.RPC_URLS)[:3]
    if not target_endpoints:
        raise SimulationError("no RPC endpoints configured")

    async def _query_ep(endpoint: str) -> dict:
        async with _get_async_client(timeout=8.0) as client:
            resp = await client.post(
                endpoint,
                json={"jsonrpc": "2.0", "id": 1, "method": "simulateTransaction", "params": params},
            )
            if resp.status_code != 200:
                raise SimulationError(f"{endpoint}: HTTP {resp.status_code}")
            body = resp.json()
            if body.get("error"):
                raise SimulationError(f"{endpoint}: {body.get('error')}")
            result = body.get("result", {})
            value = result.get("value")
            if not isinstance(value, dict):
                raise SimulationError(f"{endpoint}: malformed result value")
            return {
                "endpoint": endpoint,
                "err": value.get("err"),
                "logs": value.get("logs", []),
                "units": value.get("unitsConsumed", 0),
                "post_accounts": value.get("accounts", []),
            }

    raw_results = await asyncio.gather(*[_query_ep(ep) for ep in target_endpoints], return_exceptions=True)
    valid_results = [r for r in raw_results if isinstance(r, dict)]

    if not valid_results:
        errors = [str(r) for r in raw_results if isinstance(r, Exception)]
        raise SimulationError(f"simulateTransaction failed across all RPCs: {', '.join(errors)}")

    agreeing = 0
    if valid_results:
        agreeing = 1
        for other in valid_results[1:]:
            if responses_agree(valid_results[0], other, wallet_owned_addresses or accounts, all_addresses=accounts):
                agreeing += 1

    valid_results[0]["agreeing_count"] = agreeing
    is_sell = (side == "sell")
    if agreeing >= config.SIM_MIN_AGREEING_RPCS:
        return valid_results[0]
    elif is_sell and config.SIM_SELL_ALLOW_SINGLE_RPC and len(valid_results) >= 1:
        log.warning("SIM_SINGLE_RPC_SELL: sell proceeding with 1 RPC response (%s)", valid_results[0]["endpoint"])
        try:
            from alerts import alert_nowait, AlertEvent
            alert_nowait(
                AlertEvent.SIM_SINGLE_RPC_SELL,
                f"Sell simulation proceeding with single RPC endpoint ({valid_results[0]['endpoint']})",
                key="sim_single_rpc_sell",
            )
        except Exception:
            pass
        return valid_results[0]
    else:
        raise SimulationError(
            f"simulation quorum not reached: {len(valid_results)} responses, {agreeing} agreeing (min {config.SIM_MIN_AGREEING_RPCS})"
        )



class SendOutcome(NamedTuple):
    signature: Optional[str]
    attempts: list[dict]


async def send_raw_transaction_detailed(
    raw_signed: bytes, endpoints: Optional[list[str]] = None
) -> SendOutcome:
    """Broadcast the signed tx across RPCs, capturing detailed per-endpoint outcomes."""
    payload = base64.b64encode(raw_signed).decode()
    attempts: list[dict] = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for endpoint in endpoints or config.RPC_URLS:
            try:
                resp = await client.post(
                    endpoint,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "sendTransaction",
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
                    attempts.append({
                        "endpoint": endpoint,
                        "kind": "http_status",
                        "status_code": resp.status_code,
                        "code": None,
                        "message": f"HTTP {resp.status_code}",
                        "err": None,
                    })
                    continue
                body = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                attempts.append({
                    "endpoint": endpoint,
                    "kind": "transport",
                    "code": None,
                    "message": str(exc),
                    "err": None,
                })
                continue

            result = body.get("result")
            if isinstance(result, str) and result:
                attempts.append({
                    "endpoint": endpoint,
                    "kind": "accepted",
                    "signature": result,
                    "code": None,
                    "message": None,
                    "err": None,
                })
                return SendOutcome(signature=result, attempts=attempts)

            err_obj = body.get("error")
            err_data = err_obj.get("data") if isinstance(err_obj, dict) else None
            onchain_err = err_data.get("err") if isinstance(err_data, dict) else None
            code = err_obj.get("code") if isinstance(err_obj, dict) else None
            msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)

            log.info("[solana] send refused by %s: %s", endpoint, err_obj)
            attempts.append({
                "endpoint": endpoint,
                "kind": "rpc_error",
                "code": code,
                "message": msg,
                "err": onchain_err,
                "data": err_data,
            })

    return SendOutcome(signature=None, attempts=attempts)


async def send_raw_transaction(
    raw_signed: bytes, endpoints: Optional[list[str]] = None
) -> Optional[str]:
    """Broadcast the signed tx across RPCs; return the first accepted signature.
    Raises SendRejected if every endpoint returned a definitive RPC error.
    Returns None if any endpoint failed with transport/HTTP error (ambiguous).
    """
    outcome = await send_raw_transaction_detailed(raw_signed, endpoints=endpoints)
    if outcome.signature:
        return outcome.signature

    # If EVERY attempt returned kind == "rpc_error", raise SendRejected
    if outcome.attempts and all(a.get("kind") == "rpc_error" for a in outcome.attempts):
        from live_execution.exit_errors import SendRejected
        first_err = outcome.attempts[0]
        raise SendRejected(
            f"every rpc refused the transaction: {first_err.get('message') or first_err.get('err')}",
            attempts=outcome.attempts,
        )

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
    # A7 (repo audit): some RPCs keep only a short recent-status cache;
    # ask ONCE with history search before declaring the send unconfirmed —
    # a fill that actually landed must not be reported as absent (the
    # caller would then leave it unjournaled while reconcile flags it).
    res = await rpc(
        "getSignatureStatuses",
        [[signature], {"searchTransactionHistory": True}],
        endpoints=endpoints,
    )
    values = (res or {}).get("value") or []
    if values and values[0]:
        status = values[0]
        if not status.get("err") and status.get("confirmationStatus") in ("confirmed", "finalized"):
            return {"confirmed": True, "slot": status.get("slot"), "err": None}
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
            # A7 (repo audit): ONLY "account not found" maps to a 0.0
            # balance. "invalid param" is a malformed-REQUEST error — the
            # old code reported it as "wallet holds $0", conflating an RPC
            # bug with a plausible-looking empty account instead of the
            # honest None ("unreadable") that callers refuse on.
            if "could not find account" in err_msg:
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


async def is_blockhash_valid(blockhash: str, endpoints: Optional[list[str]] = None) -> bool | None:
    """Check whether a blockhash is still valid on-chain (SEC-01 / §79 Phase B double-fill safety).
    Returns True if valid, False if expired, None on RPC failure.
    """
    res = await rpc("isBlockhashValid", [blockhash, {"commitment": "confirmed"}], endpoints=endpoints)
    if res is not None and "value" in res:
        return bool(res["value"])
    return None


async def get_signature_status(signature: str, endpoints: Optional[list[str]] = None) -> dict | None:
    """Fetch status for a specific signature with history search (§79 Phase B).
    Returns None if signature is unknown/not found or RPC failed, otherwise the status dict.
    """
    res = await rpc(
        "getSignatureStatuses",
        [[signature], {"searchTransactionHistory": True}],
        endpoints=endpoints,
    )
    values = (res or {}).get("value") or []
    if values and values[0]:
        return values[0]
    return None


