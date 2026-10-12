"""
live_execution/sim_guard.py — Pre-sign transaction simulation with balance-delta assertions (§78 Phase B).

Second layer of defense: static inspection (inspect_swap_transaction) cannot see CPI or dynamic
state; RPC skipPreflight=false only checks execution without validating wallet deltas. This module
simulates the exact transaction against RPC before signing and asserts strict bounds on SOL,
input token, output token, and untouched third accounts.

Fail closed throughout: any RPC failure, timeout, missing quote field, or assertion failure
raises ExecutionError, ensuring nothing is signed and nothing is sent.
"""
from __future__ import annotations

import base64
import logging
import struct
from typing import Optional

from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from live_execution import config, solana
from live_execution.jupiter_executor import (
    _ATA_PROGRAM_ID,
    _SPL_TOKEN_PROGRAM_ID,
    _TOKEN_2022_PROGRAM_ID,
    _WSOL_MINT,
    ExecutionError,
)

log = logging.getLogger(__name__)

# Track cumulative simulation refusals for visibility
_SIMULATION_REFUSAL_COUNT: int = 0


def get_simulation_refusal_count() -> int:
    return _SIMULATION_REFUSAL_COUNT


def reset_simulation_refusal_count() -> None:
    global _SIMULATION_REFUSAL_COUNT
    _SIMULATION_REFUSAL_COUNT = 0


def _find_ata(wallet_pk: Pubkey, mint_pk: Pubkey, token_program_id: Pubkey) -> Pubkey:
    ata_pid = Pubkey.from_string(_ATA_PROGRAM_ID)
    pda, _ = Pubkey.find_program_address(
        [bytes(wallet_pk), bytes(token_program_id), bytes(mint_pk)],
        ata_pid,
    )
    return pda


def decode_token_account_data(raw_data: bytes) -> dict:
    """
    Defensively decode standard 165-byte SPL Token and base Token-2022 layout.
    Throws ExecutionError on malformed or truncated data.
    """
    if len(raw_data) < 165:
        raise ExecutionError(f"malformed token account layout: {len(raw_data)} bytes (< 165)")
    try:
        mint = str(Pubkey.from_bytes(raw_data[0:32]))
        owner = str(Pubkey.from_bytes(raw_data[32:64]))
        amount = struct.unpack("<Q", raw_data[64:72])[0]
        return {"mint": mint, "owner": owner, "amount": amount}
    except Exception as exc:
        raise ExecutionError(f"unparseable token account data: {exc}") from exc


def get_writable_accounts(tx: VersionedTransaction) -> list[str]:
    """Extract all writable account addresses from a VersionedTransaction message."""
    account_keys = [str(k) for k in getattr(tx.message, "account_keys", [])]
    header = getattr(tx.message, "header", None)
    if not header:
        return account_keys

    num_req_sigs = getattr(header, "num_required_signatures", 1)
    num_ro_signed = getattr(header, "num_readonly_signed_accounts", 0)
    num_ro_unsigned = getattr(header, "num_readonly_unsigned_accounts", 0)

    writable_indices = set()
    # Signed writable: [0 .. num_req_sigs - num_ro_signed)
    for i in range(0, max(0, num_req_sigs - num_ro_signed)):
        if i < len(account_keys):
            writable_indices.add(i)

    # Unsigned writable: [num_req_sigs .. len(account_keys) - num_ro_unsigned)
    for i in range(num_req_sigs, max(num_req_sigs, len(account_keys) - num_ro_unsigned)):
        if i < len(account_keys):
            writable_indices.add(i)

    return [account_keys[i] for i in sorted(writable_indices)]


async def simulate_and_assert(
    swap_b64: str,
    quote: dict,
    payer_pubkey: str,
    endpoints: Optional[list[str]] = None,
) -> dict:
    """
    Execute pre-sign simulation and verify balance-delta assertions.
    Raises ExecutionError on any failure. Returns simulation summary on success.
    """
    global _SIMULATION_REFUSAL_COUNT

    def _refuse(reason: str) -> None:
        global _SIMULATION_REFUSAL_COUNT
        _SIMULATION_REFUSAL_COUNT += 1
        log.error("PRE-SIGN SIMULATION REFUSED (#%d): %s", _SIMULATION_REFUSAL_COUNT, reason)
        raise ExecutionError(f"simulation refused: {reason}")

    if not quote or not isinstance(quote, dict):
        _refuse("quote is missing or invalid")

    input_mint = quote.get("inputMint")
    output_mint = quote.get("outputMint")
    in_amount_raw = quote.get("inAmount")
    other_amount_threshold_raw = quote.get("otherAmountThreshold")

    if not input_mint or not output_mint:
        _refuse("quote missing inputMint or outputMint")

    if in_amount_raw is None or other_amount_threshold_raw is None:
        _refuse("quote missing inAmount or otherAmountThreshold (fail closed)")

    try:
        in_amount = int(in_amount_raw)
        other_amount_threshold = int(other_amount_threshold_raw)
    except (TypeError, ValueError) as exc:
        _refuse(f"unparseable quote amounts inAmount={in_amount_raw}, threshold={other_amount_threshold_raw}: {exc}")

    try:
        raw_tx_bytes = base64.b64decode(swap_b64)
        tx = VersionedTransaction.from_bytes(raw_tx_bytes)
    except Exception as exc:
        _refuse(f"failed to deserialize unsigned swap transaction: {exc}")

    payer_pk = Pubkey.from_string(payer_pubkey)
    spl_token_pk = Pubkey.from_string(_SPL_TOKEN_PROGRAM_ID)
    token_2022_pk = Pubkey.from_string(_TOKEN_2022_PROGRAM_ID)

    # Derive ATAs to monitor
    try:
        in_mint_pk = Pubkey.from_string(input_mint)
        out_mint_pk = Pubkey.from_string(output_mint)
        payer_in_ata_spl = str(_find_ata(payer_pk, in_mint_pk, spl_token_pk))
        payer_in_ata_2022 = str(_find_ata(payer_pk, in_mint_pk, token_2022_pk))
        payer_out_ata_spl = str(_find_ata(payer_pk, out_mint_pk, spl_token_pk))
        payer_out_ata_2022 = str(_find_ata(payer_pk, out_mint_pk, token_2022_pk))
    except Exception as exc:
        _refuse(f"failed deriving mint ATAs: {exc}")

    writable_accounts = get_writable_accounts(tx)
    all_addresses = list(dict.fromkeys(
        [payer_pubkey, payer_in_ata_spl, payer_in_ata_2022, payer_out_ata_spl, payer_out_ata_2022] + writable_accounts
    ))

    # 1. Fetch pre-state with getMultipleAccounts
    try:
        pre_accounts = await solana.get_multiple_accounts(all_addresses, endpoints=endpoints)
    except Exception as exc:
        _refuse(f"pre-state fetch failed: {exc}")

    if not pre_accounts or len(pre_accounts) != len(all_addresses):
        _refuse("pre-state returned invalid account count")

    pre_state: dict[str, dict | None] = dict(zip(all_addresses, pre_accounts))

    payer_pre = pre_state.get(payer_pubkey)
    if not payer_pre:
        _refuse(f"payer account {payer_pubkey} does not exist on-chain")
    pre_payer_sol = payer_pre.get("lamports", 0)

    # Decode pre-state token accounts
    pre_token_accounts: dict[str, dict] = {}
    wallet_owned_pre_accounts: dict[str, dict] = {}

    for addr, acc_info in pre_state.items():
        if acc_info and acc_info.get("owner") in (_SPL_TOKEN_PROGRAM_ID, _TOKEN_2022_PROGRAM_ID):
            data_field = acc_info.get("data")
            raw_bytes = b""
            if isinstance(data_field, list) and data_field:
                raw_bytes = base64.b64decode(data_field[0])
            elif isinstance(data_field, str):
                raw_bytes = base64.b64decode(data_field)
            decoded = decode_token_account_data(raw_bytes)
            decoded["lamports"] = acc_info.get("lamports", 0)
            pre_token_accounts[addr] = decoded
            if decoded["owner"] == payer_pubkey:
                wallet_owned_pre_accounts[addr] = decoded

    # 2. Execute simulation
    try:
        sim_res = await solana.simulate_transaction(swap_b64, all_addresses, endpoints=endpoints)
    except Exception as exc:
        _refuse(f"RPC simulation failed: {exc}")

    if sim_res.get("err") is not None:
        _refuse(f"simulation failed on-chain with err: {sim_res['err']}")

    post_accounts_list = sim_res.get("post_accounts", [])
    if len(post_accounts_list) != len(all_addresses):
        _refuse(f"simulation post_accounts length mismatch: {len(post_accounts_list)} vs {len(all_addresses)}")

    post_state: dict[str, dict | None] = dict(zip(all_addresses, post_accounts_list))

    # 3. Assertions
    # A. Wallet SOL decrease cap
    payer_post = post_state.get(payer_pubkey)
    if not payer_post:
        _refuse("payer account vanished in simulation post-state")
    post_payer_sol = payer_post.get("lamports", 0)
    sol_decrease = pre_payer_sol - post_payer_sol
    if sol_decrease > config.SIM_MAX_SOL_DECREASE_LAMPORTS:
        _refuse(f"wallet SOL decrease {sol_decrease} exceeds SIM_MAX_SOL_DECREASE_LAMPORTS ({config.SIM_MAX_SOL_DECREASE_LAMPORTS})")

    # Decode post-state token accounts
    post_token_accounts: dict[str, dict] = {}
    for addr, acc_info in post_state.items():
        if acc_info and acc_info.get("owner") in (_SPL_TOKEN_PROGRAM_ID, _TOKEN_2022_PROGRAM_ID):
            data_field = acc_info.get("data")
            raw_bytes = b""
            if isinstance(data_field, list) and data_field:
                raw_bytes = base64.b64decode(data_field[0])
            elif isinstance(data_field, str):
                raw_bytes = base64.b64decode(data_field)
            decoded = decode_token_account_data(raw_bytes)
            decoded["lamports"] = acc_info.get("lamports", 0)
            post_token_accounts[addr] = decoded

    # B. Input-token decrease check
    max_input_decrease = int(in_amount * (1.0 + config.SIM_INPUT_TOLERANCE))
    total_input_decrease = 0
    # Sum decrease across all wallet-owned token accounts matching input_mint
    for addr, pre_tok in wallet_owned_pre_accounts.items():
        if pre_tok["mint"] == input_mint:
            post_tok = post_token_accounts.get(addr)
            post_amt = post_tok["amount"] if (post_tok and post_tok["owner"] == payer_pubkey) else 0
            if pre_tok["amount"] > post_amt:
                total_input_decrease += (pre_tok["amount"] - post_amt)

    if total_input_decrease > max_input_decrease:
        _refuse(f"input token decrease {total_input_decrease} exceeds expected {in_amount} + tolerance ({max_input_decrease})")

    # C. Output-token increase check
    total_output_increase = 0
    # Sum increase across all post token accounts owned by wallet matching output_mint
    for addr, post_tok in post_token_accounts.items():
        if post_tok["owner"] == payer_pubkey and post_tok["mint"] == output_mint:
            pre_tok = wallet_owned_pre_accounts.get(addr)
            pre_amt = pre_tok["amount"] if pre_tok else 0
            if post_tok["amount"] > pre_amt:
                total_output_increase += (post_tok["amount"] - pre_amt)

    # If output mint is WSOL and transaction unwrap is enabled, output lands in native SOL of payer
    if output_mint == _WSOL_MINT:
        sol_gain = post_payer_sol - pre_payer_sol
        # Add positive SOL gain to output increase
        if sol_gain > 0:
            total_output_increase += sol_gain

    if total_output_increase < other_amount_threshold:
        _refuse(
            f"output token increase {total_output_increase} below otherAmountThreshold {other_amount_threshold} "
            f"(token output must land in wallet-owned ATA)"
        )

    # D. Every other wallet-owned token account unchanged
    for addr, pre_tok in wallet_owned_pre_accounts.items():
        if pre_tok["mint"] not in (input_mint, output_mint):
            post_tok = post_token_accounts.get(addr)
            if not post_tok:
                # Account was closed! Only allowed if WSOL unwrap
                if pre_tok["mint"] != _WSOL_MINT:
                    _refuse(f"wallet token account {addr} (mint {pre_tok['mint']}) closed without authorization")
            else:
                if post_tok["amount"] != pre_tok["amount"]:
                    _refuse(
                        f"unrelated wallet token account {addr} (mint {pre_tok['mint']}) balance changed: "
                        f"{pre_tok['amount']} -> {post_tok['amount']}"
                    )

    # E. No wallet-owned account owner changed; no account closed except allowed unwrap
    for addr, pre_tok in wallet_owned_pre_accounts.items():
        post_tok = post_token_accounts.get(addr)
        if post_tok:
            if post_tok["owner"] != payer_pubkey:
                _refuse(f"wallet token account {addr} owner changed to {post_tok['owner']}")
        else:
            # Closed account: must be WSOL unwrap
            if pre_tok["mint"] != _WSOL_MINT:
                _refuse(f"wallet-owned token account {addr} (mint {pre_tok['mint']}) was closed (not WSOL)")

    # F. Value redirection detection: foreign token accounts gaining balance unexpectedly
    if total_input_decrease > in_amount:
        excess_drain = total_input_decrease - in_amount
        for addr, post_tok in post_token_accounts.items():
            if post_tok["owner"] != payer_pubkey:
                pre_tok = pre_token_accounts.get(addr)
                pre_amt = pre_tok["amount"] if pre_tok else 0
                if post_tok["amount"] > pre_amt:
                    _refuse(f"foreign token account {addr} increased while wallet input decreased by excess ({excess_drain})")

    log.info(
        "PRE-SIGN SIMULATION PASSED: SOL -%d lamports, input -%d, output +%d (min %d)",
        sol_decrease,
        total_input_decrease,
        total_output_increase,
        other_amount_threshold,
    )
    return {
        "status": "passed",
        "sol_decrease": sol_decrease,
        "input_decrease": total_input_decrease,
        "output_increase": total_output_increase,
        "units": sim_res.get("units", 0),
    }
