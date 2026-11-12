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
from bounded_http import bounded_request

from typing import Optional, NamedTuple

from live_execution import config
from public_registry import register_public_value

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
                resp = await bounded_request(client, "POST",
                    endpoint,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                if resp.status_code != 200:
                    continue
                body = resp.json()
            except (httpx.HTTPError, ValueError):
                continue
            if not isinstance(body, dict):
                log.info("[solana] %s returned a non-object body from %s",
                         method, endpoint)
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
    # §87 C: the transaction's recent_blockhash, so an ambiguous buy can be
    # proved expired later ("" means unparseable/unknown — never "expired").
    blockhash: str = ""

    def __post_init__(self) -> None:
        register_public_value(self.signature)


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


_CONSENSUS_CATEGORIES = ("structural", "control", "wallet_amount", "wallet_lamports", "vault_amount", "malformed")
_CONSENSUS_REFUSED = {side: {category: 0 for category in _CONSENSUS_CATEGORIES} for side in ("buy", "sell")}
_CONSENSUS_ATTEMPTS = {"buy": 0, "sell": 0}
_COUNTER_MAX = (1 << 63) - 1


def get_consensus_status() -> dict:
    """Fixed-size process-local counters; logs cover separate live processes."""
    return {"scope": "process", "refused": {s: dict(v) for s, v in _CONSENSUS_REFUSED.items()},
            "attempts": dict(_CONSENSUS_ATTEMPTS)}


def _consensus_refused(side: str, category: str, addr: str = "unknown") -> bool:
    side = "sell" if side == "sell" else "buy"
    counts = _CONSENSUS_REFUSED[side]
    counts[category] = min(_COUNTER_MAX, counts[category] + 1)
    log.warning("SIM_CONSENSUS_REFUSED side=%s category=%s addr=%s", side, category, addr[:8])
    return False


def _account_data(account: dict) -> bytes:
    data = account.get("data")
    if isinstance(data, list) and len(data) == 2 and data[1] == "base64":
        data = data[0]
    if not isinstance(data, str):
        raise ValueError("malformed base64 account data")
    return base64.b64decode(data, validate=True)


def _extension_control_bytes(raw: bytes, program: str) -> bytes:
    """Mask only Token-2022 TransferFeeAmount.withheld_amount (TLV 2, u64).

    Layout: spl-token-2022 interface extension/mod.rs (AccountType::Account=2,
    ExtensionType::TransferFeeAmount=2), extension/transfer_fee/mod.rs.
    Headers, account type, all other TLVs and unknown bytes stay exact.
    """
    tail = bytearray(raw[165:])
    if program != _TOKEN_2022_PROGRAM_ID or not tail:
        return bytes(tail)
    if tail[0] != 2:
        raise ValueError("invalid Token-2022 account type")
    offset = 1
    seen = set()
    while offset < len(tail):
        # Unused allocation is zero padding; it is still compared exactly.
        if not any(tail[offset:]):
            break
        if offset + 4 > len(tail):
            raise ValueError("truncated Token-2022 TLV header")
        kind = int.from_bytes(tail[offset:offset + 2], "little")
        length = int.from_bytes(tail[offset + 2:offset + 4], "little")
        start, end = offset + 4, offset + 4 + length
        if kind in seen or end > len(tail):
            raise ValueError("malformed Token-2022 TLV")
        seen.add(kind)
        if kind == 2:
            if length != 8:
                raise ValueError("malformed TransferFeeAmount")
            tail[start:end] = bytes(8)
        offset = end
    return bytes(tail)


def _redirection_verdict(response: dict, addresses: list[str], pre_state: dict,
                         intent) -> bool:
    """Exactly check F's predicate against the guard's chosen pre-state.

    Comparing magnitudes would reject active pools yet still permit a tiny
    cross-slot difference to flip F at zero. Predicate agreement protects the
    decision itself, including that boundary, without requiring vault equality.
    """
    from live_execution.sim_guard import decode_token_account_data
    from live_execution.jupiter_executor import _SPL_TOKEN_PROGRAM_ID, _TOKEN_2022_PROGRAM_ID
    programs = (_SPL_TOKEN_PROGRAM_ID, _TOKEN_2022_PROGRAM_ID)
    before = {a: decode_token_account_data(_account_data(v))
              for a, v in pre_state.items() if v and v.get("owner") in programs}
    after = {a: decode_token_account_data(_account_data(v))
             for a, v in zip(addresses, response["post_accounts"]) if v and v.get("owner") in programs}
    debit = sum(max(0, t["amount"] - (after[a]["amount"] if a in after and after[a]["owner"] == intent.wallet else 0))
                for a, t in before.items() if t["owner"] == intent.wallet and t["mint"] == intent.input_mint)
    return debit > intent.in_amount_raw and any(
        t["owner"] != intent.wallet and t["amount"] > before.get(a, {}).get("amount", 0)
        for a, t in after.items())


def responses_agree(a: dict, b: dict, wallet_owned_addrs: list[str],
                    all_addresses: Optional[list[str]] = None, *,
                    wallet_address: str | None = None, side: str = "buy",
                    pre_state: dict | None = None, intent=None) -> bool:
    """Structural facts everywhere, exact token controls, wallet tolerances.

    Explicit payer identity enables ownership discovery from BOTH responses.
    Callers without it retain the conservative all-token amount comparison.
    No guard decision consumes opaque market-state data or lamports.
    """
    refuse = lambda category, addr="unknown": _consensus_refused(side, category, addr)
    if not isinstance(a, dict) or not isinstance(b, dict) or "err" not in a or "err" not in b:
        return refuse("malformed")
    if a["err"] != b["err"]:
        return refuse("structural")
    accs_a, accs_b = a.get("post_accounts"), b.get("post_accounts")
    if not isinstance(accs_a, list) or not isinstance(accs_b, list) or len(accs_a) != len(accs_b):
        return refuse("malformed")
    if all_addresses is not None and (len(all_addresses) != len(accs_a) or len(set(all_addresses)) != len(all_addresses)):
        return refuse("malformed")
    addresses = all_addresses if all_addresses is not None else [str(i) for i in range(len(accs_a))]
    from live_execution.jupiter_executor import _SPL_TOKEN_PROGRAM_ID, _TOKEN_2022_PROGRAM_ID
    from live_execution.sim_guard import decode_token_account_data
    for addr, acc_a, acc_b in zip(addresses, accs_a, accs_b):
        if acc_a is None and acc_b is None:
            continue
        if (acc_a is None) != (acc_b is None):
            return refuse("structural", addr)
        if not isinstance(acc_a, dict) or not isinstance(acc_b, dict):
            return refuse("malformed", addr)
        owner_a, owner_b = acc_a.get("owner"), acc_b.get("owner")
        if not isinstance(owner_a, str) or not owner_a or not isinstance(owner_b, str) or not owner_b:
            return refuse("malformed", addr)
        if owner_a != owner_b:
            return refuse("structural", addr)
        if any(type(acc.get("executable", False)) is not bool for acc in (acc_a, acc_b)):
            return refuse("malformed", addr)
        if acc_a.get("executable", False) != acc_b.get("executable", False):
            return refuse("structural", addr)
        lamp_a, lamp_b = acc_a.get("lamports"), acc_b.get("lamports")
        if any(type(v) is not int or v < 0 for v in (lamp_a, lamp_b)):
            return refuse("malformed", addr)
        try:
            raw_a, raw_b = _account_data(acc_a), _account_data(acc_b)
        except (ValueError, TypeError):
            return refuse("malformed", addr)
        if len(raw_a) != len(raw_b):
            return refuse("structural", addr)
        wallet_owned = wallet_address is None or addr == wallet_address
        if owner_a in (_SPL_TOKEN_PROGRAM_ID, _TOKEN_2022_PROGRAM_ID):
            try:
                dec_a, dec_b = decode_token_account_data(raw_a), decode_token_account_data(raw_b)
                extensions_a = _extension_control_bytes(raw_a, owner_a)
                extensions_b = _extension_control_bytes(raw_b, owner_b)
            except Exception:
                return refuse("malformed", addr)
            if raw_a[:64] != raw_b[:64] or raw_a[72:121] != raw_b[72:121] or extensions_a != extensions_b:
                return refuse("control", addr)
            if raw_a[129:165] != raw_b[129:165]:
                return refuse("control", addr)
            if raw_a[121:129] != raw_b[121:129]:
                return refuse("control", addr)
            # close_authority and delegated_amount are enforced by the two
            # exact base-layout ranges immediately above. Decoded comparisons
            # would be redundant and create equivalent (surviving) mutants.
            # Raw owner equality above makes discovery independent of which
            # endpoint appears first, including accounts outside derived ATAs.
            wallet_owned = wallet_owned or dec_a["owner"] == wallet_address
            if wallet_owned:
                amt_a, amt_b = dec_a["amount"], dec_b["amount"]
                max_allowed = max(1, int(max(amt_a, amt_b) * config.SIM_AMOUNT_TOLERANCE_BPS / 10000))
                if abs(amt_a - amt_b) > max_allowed:
                    return refuse("wallet_amount", addr)
        if wallet_owned and abs(lamp_a - lamp_b) > config.SIM_LAMPORT_TOLERANCE:
            return refuse("wallet_lamports", addr)
        # Opaque program data and foreign-vault magnitudes are market state.
        # Their structural facts and token controls above remain mandatory.
    if pre_state is not None and intent is not None:
        try:
            if _redirection_verdict(a, addresses, pre_state, intent) != _redirection_verdict(b, addresses, pre_state, intent):
                return refuse("vault_amount")
        except Exception:
            return refuse("malformed")
    return True


async def get_multiple_accounts(
    addresses: list[str],
    endpoints: Optional[list[str]] = None,
    side: str = "buy",
    wallet_owned_addresses: Optional[list[str]] = None,
    wallet_address: str | None = None,
) -> list[dict | None]:
    """Fetch multiple accounts across distinct RPC endpoints with quorum agreement."""
    if not addresses:
        return []
    counter_side = "sell" if side == "sell" else "buy"
    _CONSENSUS_ATTEMPTS[counter_side] = min(_COUNTER_MAX, _CONSENSUS_ATTEMPTS[counter_side] + 1)
    log.info("SIM_CONSENSUS_ATTEMPT side=%s phase=pre", counter_side)

    target_endpoints = _dedup_endpoints_by_host(endpoints or config.RPC_URLS)[:3]
    if not target_endpoints:
        raise SimulationError("no RPC endpoints configured")

    params = [addresses, {"encoding": "base64", "commitment": "confirmed"}]

    async def _fetch(ep: str) -> dict:
        async with _get_async_client(timeout=8.0) as client:
            resp = await bounded_request(client, "POST",
                ep,
                json={"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts", "params": params},
            )
            if resp.status_code != 200:
                raise SimulationError(f"{ep}: HTTP {resp.status_code}")
            body = resp.json()
            if not isinstance(body, dict):
                raise SimulationError(f"{ep}: malformed getMultipleAccounts response")
            if body.get("error") or body.get("result") is None:
                raise SimulationError(f"{ep}: {body.get('error')}")
            result = body["result"]
            val = result.get("value") if isinstance(result, dict) else None
            if not isinstance(val, list):
                raise SimulationError(f"{ep}: malformed getMultipleAccounts response")
            response = {"endpoint": ep, "err": None, "post_accounts": val}
            if not responses_agree(response, response, addresses, all_addresses=addresses, wallet_address=wallet_address, side=side):
                raise SimulationError("malformed getMultipleAccounts account state")
            return response

    raw_results = await asyncio.gather(*[_fetch(ep) for ep in target_endpoints], return_exceptions=True)
    valid_results = [r for r in raw_results if isinstance(r, dict)]

    if not valid_results:
        raise SimulationError("getMultipleAccounts failed across all RPCs")

    agreeing = 0
    if valid_results:
        agreeing = 1
        for other in valid_results[1:]:
            if responses_agree(valid_results[0], other, wallet_owned_addresses or addresses, all_addresses=addresses, wallet_address=wallet_address, side=side):
                agreeing += 1

    is_sell = (side == "sell")
    if agreeing >= config.SIM_MIN_AGREEING_RPCS:
        return valid_results[0]["post_accounts"]
    elif is_sell and config.SIM_SELL_ALLOW_SINGLE_RPC and len(valid_results) == 1:
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
    wallet_address: str | None = None,
    pre_state: dict | None = None,
    intent=None,
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
            resp = await bounded_request(client, "POST",
                endpoint,
                json={"jsonrpc": "2.0", "id": 1, "method": "simulateTransaction", "params": params},
            )
            if resp.status_code != 200:
                raise SimulationError(f"{endpoint}: HTTP {resp.status_code}")
            body = resp.json()
            if not isinstance(body, dict):
                raise SimulationError(f"{endpoint}: malformed simulateTransaction response")
            if body.get("error"):
                raise SimulationError(f"{endpoint}: {body.get('error')}")
            result = body.get("result", {})
            value = result.get("value") if isinstance(result, dict) else None
            if not isinstance(value, dict) or "err" not in value:
                raise SimulationError(f"{endpoint}: malformed result value")
            response = {
                "endpoint": endpoint,
                "err": value.get("err"),
                "logs": value.get("logs", []),
                "units": value.get("unitsConsumed", 0),
                "post_accounts": value.get("accounts", []),
            }
            if not responses_agree(response, response, accounts, all_addresses=accounts, wallet_address=wallet_address, side=side):
                raise SimulationError("malformed simulateTransaction account state")
            return response

    raw_results = await asyncio.gather(*[_query_ep(ep) for ep in target_endpoints], return_exceptions=True)
    valid_results = [r for r in raw_results if isinstance(r, dict)]

    if not valid_results:
        errors = [str(r) for r in raw_results if isinstance(r, Exception)]
        raise SimulationError(f"simulateTransaction failed across all RPCs: {', '.join(errors)}")

    agreeing = 0
    if valid_results:
        agreeing = 1
        for other in valid_results[1:]:
            if responses_agree(valid_results[0], other, wallet_owned_addresses or accounts, all_addresses=accounts, wallet_address=wallet_address, side=side, pre_state=pre_state, intent=intent):
                agreeing += 1

    valid_results[0]["agreeing_count"] = agreeing
    is_sell = (side == "sell")
    if agreeing >= config.SIM_MIN_AGREEING_RPCS:
        return valid_results[0]
    elif is_sell and config.SIM_SELL_ALLOW_SINGLE_RPC and len(valid_results) == 1:
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
                resp = await bounded_request(client, "POST",
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
            if not isinstance(body, dict):
                attempts.append({
                    "endpoint": endpoint,
                    "kind": "transport",
                    "code": None,
                    "message": "non-object JSON-RPC response",
                    "err": None,
                })
                continue

            result = body.get("result")
            if isinstance(result, str) and result:
                register_public_value(result)
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
        from live_execution.exit_errors import is_already_processed
        # A transaction that is "already processed" has landed — treat as ambiguous
        # (return None) so last_sig is kept and get_signature_status decides.
        if any(is_already_processed(a) for a in outcome.attempts):
            return None
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
    register_public_value(signature)
    deadline = asyncio.get_running_loop().time() + (timeout_s or config.CONFIRM_TIMEOUT_SECONDS)
    while asyncio.get_running_loop().time() < deadline:
        res = await rpc(
            "getSignatureStatuses",
            [[signature], {"searchTransactionHistory": False}],
            endpoints=endpoints,
        )
        status = _first_signature_status(res)
        if status is not None:
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
    status = _first_signature_status(res)
    if (status is not None and not status.get("err")
            and status.get("confirmationStatus") in ("confirmed", "finalized")):
        return {"confirmed": True, "slot": status.get("slot"), "err": None}
    return {"confirmed": False, "slot": None, "err": "not confirmed before timeout"}


async def get_sol_balance(address: str) -> float | None:
    res = await rpc("getBalance", [address])
    if not isinstance(res, dict):
        return None
    value = res.get("value")
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value / LAMPORTS_PER_SOL


async def get_mint_decimals(mint: str) -> int | None:
    """
    Decimals straight from the chain via getTokenSupply. None means UNKNOWN
    and every caller must refuse - never a default fallback (the decimals
    lesson).
    """
    res = await rpc("getTokenSupply", [mint])
    if not isinstance(res, dict):
        return None
    value = res.get("value")
    dec = value.get("decimals") if isinstance(value, dict) else None
    if not isinstance(dec, int) or isinstance(dec, bool) or not 0 <= dec <= 255:
        return None
    return dec


async def latest_blockhash(endpoint: str) -> str | None:
    """Fresh blockhash for hand-built transactions (drill/devnet use)."""
    res = await rpc("getLatestBlockhash", [{"commitment": "confirmed"}], endpoints=[endpoint])
    if not isinstance(res, dict):
        return None
    value = res.get("value")
    return value.get("blockhash") if isinstance(value, dict) else None


async def get_transaction(sig: str) -> dict | None:
    """
    REF-R1: fetch a confirmed transaction by signature for binding verification.

    Returns the raw RPC result dict on success (callers must inspect
    `meta.err == null` for confirmation and extract `blockTime` /
    `transaction.message.accountKeys`), or None on any failure.

    Fail-closed: a None result means the check cannot be run and should
    be reported as `unknown`, not `pass`. Never raises.
    """
    register_public_value(sig)
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
                resp = await bounded_request(client, "POST",
                    endpoint,
                    json={"jsonrpc": "2.0", "id": 1,
                          "method": "getTokenAccountBalance", "params": [ata]},
                )
                if resp.status_code != 200:
                    continue
                body = resp.json()
            except (httpx.HTTPError, ValueError):
                continue
            if not isinstance(body, dict):
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
            err_msg = str(
                (body.get("error") or {}).get("message")
                if isinstance(body.get("error"), dict)
                else (body.get("error") or "")
            ).lower()
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
    (live_execution/reconcile.py). Returns None when EITHER program's read
    failed: a legacy-only answer must never masquerade as a complete book
    (fail closed — an empty read and an unreadable read must never be
    confused). {} means both programs answered and the wallet holds nothing.
    """
    balances: dict[str, float] = {}
    programs_ok = 0
    for program_id in (_TOKEN_PROGRAM_ID, _TOKEN_2022_PROGRAM_ID):
        res = await rpc(
            "getTokenAccountsByOwner",
            [address, {"programId": program_id}, {"encoding": "jsonParsed"}],
        )
        if res is None:
            continue
        if not isinstance(res, dict):
            continue
        entries = res.get("value")
        if not isinstance(entries, list) or any(
                not isinstance(e, dict) for e in entries):
            continue
        programs_ok += 1
        for entry in entries:
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
    # Both programs are required: a legacy-only answer would silently hide
    # token-2022 holdings, and reconcile would mistake them for vanished.
    return balances if programs_ok == 2 else None


async def is_blockhash_valid(blockhash: str, endpoints: Optional[list[str]] = None,
                            commitment: str = "confirmed") -> bool | None:
    """Check whether a blockhash is still valid on-chain (SEC-01 / §79 Phase B double-fill safety).
    Returns True if valid, False if expired, None on RPC failure.
    """
    res = await rpc("isBlockhashValid", [blockhash, {"commitment": commitment}], endpoints=endpoints)
    if isinstance(res, dict):
        value = res.get("value")
        if isinstance(value, bool):
            return value
    return None


def _first_signature_status(res) -> dict | None:
    """Shape-safe first entry from a getSignatureStatuses result."""
    values = res.get("value") if isinstance(res, dict) else None
    if isinstance(values, list) and values and isinstance(values[0], dict):
        return values[0]
    return None


async def get_signature_status(signature: str, endpoints: Optional[list[str]] = None) -> dict | None:
    """Fetch status for a specific signature with history search (§79 Phase B).
    Returns None if signature is unknown/not found or RPC failed, otherwise the status dict.
    """
    register_public_value(signature)
    res = await rpc(
        "getSignatureStatuses",
        [[signature], {"searchTransactionHistory": True}],
        endpoints=endpoints,
    )
    return _first_signature_status(res)


async def get_signature_status_all(
    signature: str, endpoints: Optional[list[str]] = None
) -> list[dict | None]:
    """Query EVERY distinct endpoint for one signature (§88 D).

    `get_signature_status`/`rpc` stop at the first endpoint that answers, so a
    lagging node can report "not found" while another node has the landed
    transaction. This returns one entry per deduped endpoint in order; None
    means that endpoint did not return a status (unknown/not found/RPC
    failure). All-None is NOT proof of a non-land by itself — callers must
    also satisfy their age guard before expiring a pending buy.
    """
    target = _dedup_endpoints_by_host(endpoints or config.RPC_URLS)

    async def _one(endpoint: str) -> dict | None:
        try:
            return await get_signature_status(signature, endpoints=[endpoint])
        except Exception as exc:
            log.warning("[solana] status query failed on %s: %s", endpoint, exc)
            return None

    return list(await asyncio.gather(*(_one(ep) for ep in target)))


class _Verdict:
    """Module sentinel; compared by identity, never by equality or truthiness."""
    def __init__(self, name): self._name = name
    def __repr__(self): return f"<{self._name}>"
    def __bool__(self): raise TypeError(f"{self._name} has no truth value; compare with `is`")


ABSENT = _Verdict("ABSENT")            # endpoint answered 200, value == [null]: "signature not found"
UNREACHABLE = _Verdict("UNREACHABLE")  # anything else: transport error, non-200, JSON-RPC error,
                                       # malformed/unexpected body, exception


def _classify_signature_status_result(result) -> dict | _Verdict:
    """Map one `rpc()` result to found-dict / ABSENT / UNREACHABLE (§91 A1)."""
    if result is None:
        # rpc() only returns None when every endpoint failed: transport error,
        # non-200, JSON-RPC error, or a malformed body. Never "not found".
        return UNREACHABLE
    if not isinstance(result, dict):
        return UNREACHABLE
    value = result.get("value")
    if not isinstance(value, list) or len(value) != 1:
        return UNREACHABLE
    entry = value[0]
    if entry is None:
        return ABSENT
    if isinstance(entry, dict):
        return entry
    return UNREACHABLE


async def get_signature_status_all_strict(
    signature: str, endpoints: Optional[list[str]] = None
) -> list[dict | _Verdict]:
    """Query EVERY distinct endpoint and keep absence separate from unknown.

    One entry per host-deduped endpoint, same order, queried concurrently.
    `ABSENT` means that endpoint answered 200 with `value == [null]`;
    `UNREACHABLE` covers every failure or unexpected shape. All-ABSENT is a
    necessary observation, not sufficient proof of historical non-execution;
    automatic sell release also requires finalized expiry, age and reobservation.
    """
    if endpoints is not None and not endpoints:
        return []
    target = _dedup_endpoints_by_host(endpoints or config.RPC_URLS)

    async def _one(endpoint: str) -> dict | _Verdict:
        try:
            result = await rpc(
                "getSignatureStatuses",
                [[signature], {"searchTransactionHistory": True}],
                endpoints=[endpoint],
            )
        except Exception as exc:
            log.warning("[solana] status query failed on %s: %s", endpoint, exc)
            return UNREACHABLE
        return _classify_signature_status_result(result)

    return list(await asyncio.gather(*(_one(ep) for ep in target)))


