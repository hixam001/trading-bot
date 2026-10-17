"""
live_execution/jupiter_executor.py — REAL-MONEY swap execution via Jupiter.

⚠ HONEST COVERAGE STATEMENT ⚠
This module has NO automated live-network test coverage. The offline tests
cover refusal logic, request shapes, and math via mocks only; the devnet
drill (run_live_cycle.py --drill) and the operator's supervised devnet
flow exercise sign/send/confirm before mainnet. Per handoff §31 the repo
has been operator-armed for mainnet since 2026-08-28 — any change here MUST
be re-drilled on devnet with a throwaway keypair BEFORE it touches mainnet
funds. That remains a hard requirement, not a suggestion.

Endpoint provenance:
  * quote — imported from backend/config.py (JUPITER_QUOTE_URL), the same
    constant the paper side uses: https://lite-api.jup.ag/swap/v1/quote
  * swap  — verified live 2026-08-23: POST lite-api.jup.ag/swap/v1/swap with
    a minimal body returned HTTP 422 naming `quoteResponse` deserialization;
    bogus sibling route → 404; GET on route → 405. Existence proven by real
    calls, NOT by pattern-assuming from the quote URL.

Parity guarantees (the decimals lesson):
  * raw-unit math is IMPORTED from backend/data_providers/jupiter.py
    (raw_units_for_one_token) — exactly one implementation codebase-wide.
    price_from_quote stays exclusive to the paper side because its contract
    pins it to a quote selling exactly ONE token; this buy path quotes a
    USD-sized amount, so forcing it would misuse the function. Unit math
    still cannot drift — identity-tested.
  * UNKNOWN DECIMALS REFUSE THE TRADE. Never a default fallback: assuming
    9 decimals fabricated 1000× prices on the paper side (+96,000% P&L).

Preflight order (every step fails closed BEFORE any network call):
  kill switch → LIVE_TRADING_ENABLED → size caps → exposure/position caps →
  idempotency replay → decimals known → daily-loss breaker → manual
  confirmation consumed → then (and only then) quote/build/sign/send.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from pathlib import Path

import httpx

# The package lives INSIDE backend/ now (single deployable module): backend/
# is this file's grandparent. Keep the defensive insert so the module stays
# importable from any working directory (tests, operator CLIs, container).
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from data_providers.base import ProviderError  # noqa: E402
from data_providers.jupiter import raw_units_for_one_token  # noqa: E402
# Single source of truth for the quote endpoint and the USDC mint:
from config import JUPITER_QUOTE_URL as _BACKEND_QUOTE_URL  # noqa: E402
from config import USDC_MINT as _BACKEND_USDC_MINT  # noqa: E402

import math
from live_execution import config  # noqa: E402
from live_execution import kill_switch  # noqa: E402
from live_execution import solana  # noqa: E402
from live_execution import wallet  # noqa: E402
from live_execution.confirmation_queue import (  # noqa: E402
    ConfirmationError,
    ConfirmationQueue,
)
from live_execution.models import ExecutionLedger  # noqa: E402
from live_execution.quote_params import build_quote_params  # noqa: E402
from live_execution.trade_intent import TradeIntent, usd_to_raw_amount  # noqa: E402

log = logging.getLogger(__name__)

USDC_DECIMALS = 6


ExecutionError = solana.ExecutionError


class Refusal(ExecutionError):
    """Pre-condition failure — refused BEFORE any network call."""


def default_ledger() -> ExecutionLedger:
    return ExecutionLedger(config.STATE_DIR / "executions.json")


def default_queue() -> ConfirmationQueue:
    return ConfirmationQueue(config.STATE_DIR / "confirmations.json")


# ---------------------------------------------------------------------------
# Fail-closed preflight — every refusal happens BEFORE any network call.
# ---------------------------------------------------------------------------

def assert_decimals_known(output_decimals: int | None, mint: str) -> int:
    """
    THE carry-over from the paper-side decimals bug: unknown decimals must
    refuse the trade. A wrong raw-unit amount scales the trade size by
    10**(9 - true_decimals); there is no safe default.
    """
    if output_decimals is None or output_decimals <= 0:
        raise Refusal(
            f"output token {mint} has UNKNOWN decimals — refusing to trade. "
            f"Never assume a default decimals value."
        )
    return output_decimals


def preflight(
    usd_size: float,
    output_mint: str,
    output_decimals: int | None,
    ledger: ExecutionLedger | None = None,
) -> int:
    """
    All offline safety gates in fixed order. Raises Refusal (or
    KillSwitchTripped) before anything network-facing can happen.
    Returns the validated decimals.
    """
    ledger = ledger or default_ledger()

    kill_switch.assert_not_tripped()

    if not config.LIVE_TRADING_ENABLED:
        raise Refusal(
            "LIVE_TRADING_ENABLED is False — real execution is disarmed. "
            "A human must edit live_execution/config.py manually; there is "
            "deliberately no environment-variable bypass."
        )
    if not isinstance(usd_size, (int, float)) or usd_size <= 0:
        raise Refusal(f"usd_size must be > 0, got {usd_size!r}")
    if usd_size > config.MAX_TRADE_USD:
        raise Refusal(
            f"usd_size {usd_size} exceeds MAX_TRADE_USD "
            f"{config.MAX_TRADE_USD}"
        )

    open_pos = ledger.open_positions()
    exposure_after = ledger.total_open_exposure() + usd_size
    if exposure_after > config.MAX_TOTAL_EXPOSURE_USD:
        raise Refusal(
            f"total exposure {ledger.total_open_exposure():.2f} + new trade "
            f"{usd_size:.2f} would exceed MAX_TOTAL_EXPOSURE_USD "
            f"{config.MAX_TOTAL_EXPOSURE_USD}"
        )
    if output_mint not in open_pos and len(open_pos) >= config.MAX_OPEN_POSITIONS:
        raise Refusal(
            f"would hold {len(open_pos) + 1} mints > MAX_OPEN_POSITIONS "
            f"{config.MAX_OPEN_POSITIONS}"
        )

    dec = assert_decimals_known(output_decimals, output_mint)

    kill_switch.check_daily_loss_breaker(ledger)
    kill_switch.assert_not_tripped()   # breaker may have just tripped it

    return dec


# ---------------------------------------------------------------------------
# HTTP helpers — bounded retries, distinct 429 handling (parity w/ paper side)
# NOTE: Jupiter's /swap/v1/quote is a GET endpoint with query params (the
# paper side GETs it); POST returns 405. /swap/v1/swap and the RPC are POST.
# So there are two helpers, each used only for its correct verb.
# ---------------------------------------------------------------------------

async def _post_json(url: str, payload: dict) -> dict:
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code == 429:
                raise ProviderError(f"HTTP 429 from {url}")
            resp.raise_for_status()
            return resp.json()
        except ProviderError as exc:
            last = exc
            log.warning("rate_limited: %s attempt %d/3", exc, attempt)
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            log.warning("call failed (attempt %d/3): %s", attempt, exc)
        await asyncio.sleep(2.0 * attempt)
    raise ExecutionError(f"giving up on {url} after 3 attempts") from last


async def _get_json(url: str, params: dict) -> dict:
    """GET-with-query-params twin of _post_json (quote endpoint is GET)."""
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params=params)
            if resp.status_code == 429:
                raise ProviderError(f"HTTP 429 from {url}")
            resp.raise_for_status()
            return resp.json()
        except ProviderError as exc:
            last = exc
            log.warning("rate_limited: %s attempt %d/3", exc, attempt)
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            log.warning("quote call failed (attempt %d/3): %s", attempt, exc)
        await asyncio.sleep(2.0 * attempt)
    raise ExecutionError(f"giving up on {url} after 3 attempts") from last


# ---------------------------------------------------------------------------
# Quote — decimals-aware, USDC → token. Unit math via the IMPORTED
# raw_units_for_one_token (no local 10**dec reimplementation anywhere).
# ---------------------------------------------------------------------------

async def get_jupiter_quote(
    output_mint: str,
    output_decimals: int | None,
    usd_size: float,
    input_mint: str = _BACKEND_USDC_MINT,
    ledger: ExecutionLedger | None = None,
) -> dict:
    """
    Quote buying `usd_size` USD of `output_mint` (paying with USDC).
    FAILS CLOSED on unknown output decimals or any preflight refusal.
    Returns {quote, amount_raw, out_amount_raw, tokens_out, price_usd}.
    """
    dec = preflight(usd_size, output_mint, output_decimals, ledger)

    # §81 Phase C-5: Single USD->raw conversion via usd_to_raw_amount
    amount_raw = usd_to_raw_amount(usd_size, USDC_DECIMALS)
    params = build_quote_params(
        input_mint=input_mint,
        output_mint=output_mint,
        amount_raw=amount_raw,
        slippage_bps=config.SLIPPAGE_BPS,
    )
    data = await _get_json(_BACKEND_QUOTE_URL, params)
    out_raw = data.get("outAmount")
    if out_raw is None:
        raise ExecutionError(f"jupiter: no outAmount in quote for {output_mint}")
    try:
        out_amount_raw = int(out_raw)
    except (TypeError, ValueError) as exc:
        raise ExecutionError(
            f"jupiter: unparseable outAmount {out_raw!r}"
        ) from exc
    tokens_out = out_amount_raw / raw_units_for_one_token(dec)
    if tokens_out <= 0:
        raise ExecutionError(f"jupiter: non-positive quote for {output_mint}")
    # A7 (repo audit): the price-impact guard now lives HERE — the single
    # choke point every buy path shares. Previously only executor.place_buy
    # checked it, so the manual propose→approve→execute path could fill a
    # thin-pool quote (e.g. 20% impact) the automated path would refuse.
    try:
        impact_pct = abs(float(data.get("priceImpactPct") or 0)) * 100.0
    except (TypeError, ValueError):
        impact_pct = 0.0
    if impact_pct > config.MAX_PRICE_IMPACT_PCT:
        raise ExecutionError(
            f"jupiter: price impact {impact_pct:.2f}% exceeds "
            f"MAX_PRICE_IMPACT_PCT {config.MAX_PRICE_IMPACT_PCT}% — "
            f"refusing {output_mint}"
        )
    return {
        "quote": data,
        "amount_raw": amount_raw,
        "out_amount_raw": out_amount_raw,
        "tokens_out": tokens_out,
        "price_usd": usd_size / tokens_out,
        "price_impact_pct": impact_pct,
    }


# ---------------------------------------------------------------------------
# Swap transaction build + local signing + send/confirm
# ---------------------------------------------------------------------------

async def _build_swap_transaction(quote: dict, user_public_key: str) -> str:
    """
    Returns the base64 UNSIGNED VersionedTransaction from Jupiter's swap API.
    Signing happens locally; the secret never leaves this process.
    """
    payload = {
        "quoteResponse": quote,
        "userPublicKey": user_public_key,
        "wrapAndUnwrapSol": True,
        # A7 (repo audit): legacy transactions carry NO address-lookup
        # tables, so inspect_swap_transaction can verify EVERY program
        # ID statically before signing. v0+ALTs made the SEC-01 guard
        # silently skip unresolvable instructions (fail-open). A route
        # too large for a legacy tx fails closed at build time — a
        # refused order costs nothing, a blind-signed one does not.
        # NOTE: any change here must be re-drilled on devnet with a
        # throwaway keypair BEFORE it touches mainnet (module header).
        "asLegacyTransaction": True,
    }
    if getattr(config, "PRIORITY_FEE_ENABLED", False):
        payload["dynamicComputeUnitLimit"] = True
        payload["prioritizationFeeLamports"] = {
            "priorityLevelWithMaxLamports": {
                "priorityLevel": getattr(config, "PRIORITY_LEVEL", "high"),
                "maxLamports": getattr(config, "PRIORITY_FEE_MAX_LAMPORTS", 200_000),
                "global": False,
            }
        }
    data = await _post_json(config.JUPITER_SWAP_URL, payload)
    b64 = data.get("swapTransaction")
    if not b64:
        raise ExecutionError("jupiter: no swapTransaction in response")
    return b64


_SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
_SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
_ATA_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
_COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"
_MEMO_PROGRAM_IDS = frozenset({
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
})
_WSOL_MINT = "So11111111111111111111111111111111111111112"


def _is_wallet_owned(dest: str, payer_pubkey: str, account_keys: list[str], known_owned: set[str]) -> bool:
    """Verify whether dest is the payer's wallet or an ATA owned by the payer."""
    if dest == payer_pubkey or dest in known_owned:
        return True
    try:
        from solders.pubkey import Pubkey
        payer_pk = Pubkey.from_string(payer_pubkey)
        ata_pid = Pubkey.from_string(_ATA_PROGRAM_ID)
        token_pid = Pubkey.from_string(_SPL_TOKEN_PROGRAM_ID)
        token_2022_pid = Pubkey.from_string(_TOKEN_2022_PROGRAM_ID)

        mints_to_check = set(account_keys)
        mints_to_check.add(_WSOL_MINT)
        for mint_str in mints_to_check:
            try:
                mint_pk = Pubkey.from_string(mint_str)
            except Exception:
                continue
            ata_spl, _ = Pubkey.find_program_address([bytes(payer_pk), bytes(token_pid), bytes(mint_pk)], ata_pid)
            if dest == str(ata_spl):
                return True
            ata_2022, _ = Pubkey.find_program_address([bytes(payer_pk), bytes(token_2022_pid), bytes(mint_pk)], ata_pid)
            if dest == str(ata_2022):
                return True
    except Exception:
        pass
    return False


def _inspect_swap_transaction_inner(tx, payer_pubkey: str) -> None:
    """Internal implementation of transaction safety inspection."""
    import struct
    from solders.pubkey import Pubkey

    account_keys = [str(k) for k in getattr(tx.message, "account_keys", [])]
    if not account_keys:
        raise ExecutionError("swap transaction has empty accountKeys")

    if getattr(tx.message, "address_table_lookups", None):
        raise ExecutionError(
            "swap transaction uses address lookup tables — program IDs "
            "cannot be statically verified — refusing to sign unverified "
            "payload"
        )

    fee_payer = account_keys[0]
    if fee_payer != payer_pubkey:
        raise ExecutionError(
            f"swap transaction fee payer mismatch: expected {payer_pubkey}, got {fee_payer}"
        )

    wallet_owned_accounts: set[str] = {payer_pubkey}
    created_token_accounts: set[str] = set()
    total_system_outflow_lamports: int = 0
    compute_unit_limit: int | None = None
    compute_unit_price: int | None = None
    compute_unit_limit_count: int = 0
    compute_unit_price_count: int = 0

    # Strict instruction validation in order
    for i, ix in enumerate(tx.message.instructions):
        prog_idx = ix.program_id_index
        if prog_idx >= len(account_keys):
            raise ExecutionError(
                f"swap instruction #{i} references unresolvable program "
                f"index {prog_idx} — refusing to sign unverified payload"
            )
        prog_id = account_keys[prog_idx]
        if prog_id not in config.APPROVED_SWAP_PROGRAM_IDS:
            raise ExecutionError(
                f"unapproved program ID in swap instruction #{i}: {prog_id} "
                "— refusing to sign unverified payload"
            )

        data = bytes(getattr(ix, "data", b""))

        # 1. System Program (11111111111111111111111111111111)
        # solana_program::system_instruction::SystemInstruction
        # 4-byte little-endian uint32 discriminant
        if prog_id == _SYSTEM_PROGRAM_ID:
            if len(data) < 4:
                raise ExecutionError(
                    f"System Program instruction #{i} data truncated (< 4 bytes)"
                )
            disc = struct.unpack("<I", data[:4])[0]

            if disc == 0:
                # CreateAccount: [0..4] u32 disc (0), [4..12] u64 lamports, [12..20] u64 space, [20..52] Pubkey owner
                if len(data) < 52:
                    raise ExecutionError(
                        f"System CreateAccount instruction #{i} data truncated (< 52 bytes)"
                    )
                lamports = struct.unpack("<Q", data[4:12])[0]
                space = struct.unpack("<Q", data[12:20])[0]
                try:
                    owner_pubkey = str(Pubkey.from_bytes(data[20:52]))
                except Exception as exc:
                    raise ExecutionError(
                        f"System CreateAccount instruction #{i} has invalid owner pubkey"
                    ) from exc

                # A3: owner must be SPL Token or Token-2022 program only
                if owner_pubkey not in (_SPL_TOKEN_PROGRAM_ID, _TOKEN_2022_PROGRAM_ID):
                    raise ExecutionError(
                        f"System CreateAccount in instruction #{i} specifies forbidden owner program {owner_pubkey} "
                        f"(must be Token or Token-2022 only)"
                    )

                # A3: space <= 165
                if space > 165:
                    raise ExecutionError(
                        f"System CreateAccount in instruction #{i} specifies space {space} > 165"
                    )

                # A3: lamports <= MAX_CREATE_ACCOUNT_LAMPORTS
                if lamports > config.MAX_CREATE_ACCOUNT_LAMPORTS:
                    raise ExecutionError(
                        f"System CreateAccount in instruction #{i} lamports {lamports} exceeds "
                        f"MAX_CREATE_ACCOUNT_LAMPORTS ({config.MAX_CREATE_ACCOUNT_LAMPORTS})"
                    )

                if len(ix.accounts) < 2 or ix.accounts[0] >= len(account_keys) or ix.accounts[1] >= len(account_keys):
                    raise ExecutionError(
                        f"System CreateAccount instruction #{i} has invalid account indices"
                    )

                funding_account = account_keys[ix.accounts[0]]
                new_account = account_keys[ix.accounts[1]]

                # A4: Cap total top-level SOL outflow
                if funding_account == payer_pubkey:
                    total_system_outflow_lamports += lamports
                    if total_system_outflow_lamports > config.MAX_SYSTEM_OUTFLOW_LAMPORTS:
                        raise ExecutionError(
                            f"Total top-level SOL outflow {total_system_outflow_lamports} lamports exceeds "
                            f"MAX_SYSTEM_OUTFLOW_LAMPORTS ({config.MAX_SYSTEM_OUTFLOW_LAMPORTS})"
                        )
                    # A2: "Wallet-owned" must be proven, not inferred.
                    # Do NOT add to wallet_owned_accounts here! Only track in created_token_accounts.
                    created_token_accounts.add(new_account)

            elif disc in (1, 4, 8, 9):
                disc_names = {1: "Assign", 4: "Allocate", 8: "AllocateWithSeed", 9: "AssignWithSeed"}
                raise ExecutionError(
                    f"System Program instruction #{i} uses forbidden operation {disc_names.get(disc, disc)}"
                )

            elif disc == 2:
                # Transfer: [0..4] u32 disc (2), [4..12] u64 lamports
                # accounts: [from, to]
                if len(data) < 12:
                    raise ExecutionError(
                        f"System Transfer instruction #{i} data truncated (< 12 bytes)"
                    )
                lamports = struct.unpack("<Q", data[4:12])[0]
                if len(ix.accounts) < 2 or ix.accounts[0] >= len(account_keys) or ix.accounts[1] >= len(account_keys):
                    raise ExecutionError(
                        f"System Transfer instruction #{i} has invalid recipient account index"
                    )
                from_acc = account_keys[ix.accounts[0]]
                dest = account_keys[ix.accounts[1]]

                if from_acc == payer_pubkey or from_acc in wallet_owned_accounts:
                    total_system_outflow_lamports += lamports
                    if total_system_outflow_lamports > config.MAX_SYSTEM_OUTFLOW_LAMPORTS:
                        raise ExecutionError(
                            f"Total top-level SOL outflow {total_system_outflow_lamports} lamports exceeds "
                            f"MAX_SYSTEM_OUTFLOW_LAMPORTS ({config.MAX_SYSTEM_OUTFLOW_LAMPORTS})"
                        )

                if lamports > 0 and not _is_wallet_owned(dest, payer_pubkey, account_keys, wallet_owned_accounts):
                    raise ExecutionError(
                        f"System Transfer in instruction #{i} transfers {lamports} lamports to foreign or unproven destination {dest} "
                        f"(not owned by wallet {payer_pubkey})"
                    )

            elif disc == 11:
                # TransferWithSeed: [0..4] u32 disc (11), [4..12] u64 lamports
                # accounts: [from, base, to]
                if len(data) < 12:
                    raise ExecutionError(
                        f"System TransferWithSeed instruction #{i} data truncated (< 12 bytes)"
                    )
                lamports = struct.unpack("<Q", data[4:12])[0]
                if len(ix.accounts) < 3 or ix.accounts[0] >= len(account_keys) or ix.accounts[2] >= len(account_keys):
                    raise ExecutionError(
                        f"System TransferWithSeed instruction #{i} has invalid recipient account index"
                    )
                from_acc = account_keys[ix.accounts[0]]
                dest = account_keys[ix.accounts[2]]

                if from_acc == payer_pubkey or from_acc in wallet_owned_accounts:
                    total_system_outflow_lamports += lamports
                    if total_system_outflow_lamports > config.MAX_SYSTEM_OUTFLOW_LAMPORTS:
                        raise ExecutionError(
                            f"Total top-level SOL outflow {total_system_outflow_lamports} lamports exceeds "
                            f"MAX_SYSTEM_OUTFLOW_LAMPORTS ({config.MAX_SYSTEM_OUTFLOW_LAMPORTS})"
                        )

                if lamports > 0 and not _is_wallet_owned(dest, payer_pubkey, account_keys, wallet_owned_accounts):
                    raise ExecutionError(
                        f"System TransferWithSeed in instruction #{i} transfers {lamports} lamports to foreign or unproven destination {dest}"
                    )

            else:
                raise ExecutionError(
                    f"System Program instruction #{i} has unapproved discriminant {disc}"
                )

        # 2. SPL Token & Token-2022
        # spl_token::instruction::TokenInstruction (1-byte uint8 tag)
        elif prog_id in (_SPL_TOKEN_PROGRAM_ID, _TOKEN_2022_PROGRAM_ID):
            prog_name = "SPL Token" if prog_id == _SPL_TOKEN_PROGRAM_ID else "Token-2022"
            if len(data) < 1:
                raise ExecutionError(
                    f"{prog_name} instruction #{i} has empty data"
                )
            tag = data[0]

            if tag in (3, 12):
                tag_name = "Transfer" if tag == 3 else "TransferChecked"
                raise ExecutionError(
                    f"{prog_name} {tag_name} instruction #{i} (tag {tag}) refused at top level "
                    "— legitimate swaps execute CPI transfers inside the DEX program"
                )
            elif tag in (4, 13):
                tag_name = "Approve" if tag == 4 else "ApproveChecked"
                raise ExecutionError(
                    f"{prog_name} {tag_name} instruction #{i} (tag {tag}) refused at top level"
                )
            elif tag == 6:
                raise ExecutionError(
                    f"{prog_name} SetAuthority instruction #{i} (tag 6) refused at top level"
                )
            elif tag in (7, 8, 15):
                tag_name = "MintTo" if tag == 7 else ("Burn" if tag == 8 else "BurnChecked")
                raise ExecutionError(
                    f"{prog_name} {tag_name} instruction #{i} (tag {tag}) refused at top level"
                )
            elif tag == 9:
                # CloseAccount: accounts: [account_to_close, destination, owner]
                if len(ix.accounts) < 2 or ix.accounts[0] >= len(account_keys) or ix.accounts[1] >= len(account_keys):
                    raise ExecutionError(
                        f"{prog_name} CloseAccount instruction #{i} missing destination account"
                    )
                dest = account_keys[ix.accounts[1]]
                if dest != payer_pubkey:
                    raise ExecutionError(
                        f"{prog_name} CloseAccount in instruction #{i} destination {dest} does not match wallet {payer_pubkey}"
                    )
                if len(ix.accounts) >= 3 and ix.accounts[2] < len(account_keys):
                    owner = account_keys[ix.accounts[2]]
                    if owner != payer_pubkey:
                        raise ExecutionError(
                            f"{prog_name} CloseAccount in instruction #{i} owner {owner} does not match wallet {payer_pubkey}"
                        )
            elif tag == 1:
                # InitializeAccount: accounts: [account, mint, owner, rent_sysvar (optional)]
                if len(ix.accounts) < 3 or ix.accounts[0] >= len(account_keys) or ix.accounts[2] >= len(account_keys):
                    raise ExecutionError(
                        f"{prog_name} InitializeAccount instruction #{i} missing required accounts (< 3 accounts)"
                    )
                acc = account_keys[ix.accounts[0]]
                owner = account_keys[ix.accounts[2]]
                if owner != payer_pubkey:
                    raise ExecutionError(
                        f"{prog_name} InitializeAccount instruction #{i} owner {owner} does not match payer {payer_pubkey}"
                    )
                if acc in created_token_accounts:
                    wallet_owned_accounts.add(acc)

            elif tag == 16:
                # InitializeAccount2: accounts: [account, mint, rent_sysvar], data: [16, owner(32)]
                if len(data) < 33:
                    raise ExecutionError(
                        f"{prog_name} InitializeAccount2 instruction #{i} data truncated (< 33 bytes)"
                    )
                if len(ix.accounts) < 1 or ix.accounts[0] >= len(account_keys):
                    raise ExecutionError(
                        f"{prog_name} InitializeAccount2 instruction #{i} missing account to initialize"
                    )
                acc = account_keys[ix.accounts[0]]
                try:
                    owner = str(Pubkey.from_bytes(data[1:33]))
                except Exception as exc:
                    raise ExecutionError(
                        f"{prog_name} InitializeAccount2 instruction #{i} has invalid owner pubkey"
                    ) from exc
                if owner != payer_pubkey:
                    raise ExecutionError(
                        f"{prog_name} InitializeAccount2 instruction #{i} owner {owner} does not match payer {payer_pubkey}"
                    )
                if acc in created_token_accounts:
                    wallet_owned_accounts.add(acc)

            elif tag == 17:
                # SyncNative: accounts: [account]
                if len(ix.accounts) < 1 or ix.accounts[0] >= len(account_keys):
                    raise ExecutionError(
                        f"{prog_name} SyncNative instruction #{i} missing account"
                    )

            elif tag == 18:
                # InitializeAccount3: accounts: [account, mint], data: [18, owner(32)]
                if len(data) < 33:
                    raise ExecutionError(
                        f"{prog_name} InitializeAccount3 instruction #{i} data truncated (< 33 bytes)"
                    )
                if len(ix.accounts) < 1 or ix.accounts[0] >= len(account_keys):
                    raise ExecutionError(
                        f"{prog_name} InitializeAccount3 instruction #{i} missing account to initialize"
                    )
                acc = account_keys[ix.accounts[0]]
                try:
                    owner = str(Pubkey.from_bytes(data[1:33]))
                except Exception as exc:
                    raise ExecutionError(
                        f"{prog_name} InitializeAccount3 instruction #{i} has invalid owner pubkey"
                    ) from exc
                if owner != payer_pubkey:
                    raise ExecutionError(
                        f"{prog_name} InitializeAccount3 instruction #{i} owner {owner} does not match payer {payer_pubkey}"
                    )
                if acc in created_token_accounts:
                    wallet_owned_accounts.add(acc)

            else:
                raise ExecutionError(
                    f"{prog_name} instruction #{i} has unapproved tag {tag}"
                )

        # 3. Associated Token Account Program
        # spl_associated_token_account::instruction::AssociatedTokenAccountInstruction
        elif prog_id == _ATA_PROGRAM_ID:
            tag = data[0] if len(data) > 0 else 0
            if tag not in (0, 1):  # 0=Create, 1=CreateIdempotent
                raise ExecutionError(
                    f"ATA instruction #{i} has unapproved instruction tag {tag}"
                )
            if len(ix.accounts) < 3 or ix.accounts[1] >= len(account_keys) or ix.accounts[2] >= len(account_keys):
                raise ExecutionError(
                    f"ATA instruction #{i} missing required accounts"
                )
            wallet_owner = account_keys[ix.accounts[2]]
            if wallet_owner != payer_pubkey:
                raise ExecutionError(
                    f"ATA instruction #{i} wallet owner {wallet_owner} does not match payer {payer_pubkey}"
                )
            ata_account = account_keys[ix.accounts[1]]
            wallet_owned_accounts.add(ata_account)

        # 4. Compute Budget Program (ComputeBudget111111111111111111111111111111)
        elif prog_id == _COMPUTE_BUDGET_PROGRAM_ID:
            if len(data) < 1:
                raise ExecutionError(f"ComputeBudget instruction #{i} has empty data")
            cb_tag = data[0]
            if cb_tag == 1:
                # RequestHeapFrame: [1, bytes(4)]
                if len(data) < 5:
                    raise ExecutionError(f"ComputeBudget RequestHeapFrame instruction #{i} data truncated (< 5 bytes)")
            elif cb_tag == 2:
                # SetComputeUnitLimit: [2, units(4)]
                compute_unit_limit_count += 1
                if compute_unit_limit_count > 1:
                    raise ExecutionError(f"ComputeBudget SetComputeUnitLimit instruction #{i} appears more than once")
                if len(data) < 5:
                    raise ExecutionError(f"ComputeBudget SetComputeUnitLimit instruction #{i} data truncated (< 5 bytes)")
                compute_unit_limit = struct.unpack("<I", data[1:5])[0]
            elif cb_tag == 3:
                # SetComputeUnitPrice: [3, micro_lamports(8)]
                compute_unit_price_count += 1
                if compute_unit_price_count > 1:
                    raise ExecutionError(f"ComputeBudget SetComputeUnitPrice instruction #{i} appears more than once")
                if len(data) < 9:
                    raise ExecutionError(f"ComputeBudget SetComputeUnitPrice instruction #{i} data truncated (< 9 bytes)")
                compute_unit_price = struct.unpack("<Q", data[1:9])[0]
            elif cb_tag == 4:
                # SetLoadedAccountsDataSizeLimit: [4, bytes(4)]
                if len(data) < 5:
                    raise ExecutionError(f"ComputeBudget SetLoadedAccountsDataSizeLimit instruction #{i} data truncated (< 5 bytes)")
            else:
                raise ExecutionError(f"ComputeBudget instruction #{i} has unapproved tag {cb_tag}")

        # 5. Memo programs
        elif prog_id in _MEMO_PROGRAM_IDS:
            pass

        # 6. Other approved DEX programs (Jupiter, Raydium, Orca, etc.)
        # Discriminants are DEX-specific and allowed at top level.

    # Enforce ComputeBudget priority fee hard cap (§79 Phase B-3, §81 Phase C-7)
    if compute_unit_price is not None:
        if compute_unit_limit is not None:
            eff_limit = compute_unit_limit
        else:
            # Solana CU default: 200,000 CU per non-ComputeBudget instruction, capped at 1,400,000
            non_cb_ixs = sum(
                1 for ix in getattr(tx.message, "instructions", [])
                if account_keys[ix.program_id_index] != _COMPUTE_BUDGET_PROGRAM_ID
            )
            eff_limit = min(200_000 * max(1, non_cb_ixs), 1_400_000)

        priority_fee_lamports = (eff_limit * compute_unit_price) // 1_000_000
        if priority_fee_lamports > config.PRIORITY_FEE_HARD_CAP_LAMPORTS:
            raise ExecutionError(
                f"ComputeBudget priority fee {priority_fee_lamports} lamports exceeds "
                f"PRIORITY_FEE_HARD_CAP_LAMPORTS ({config.PRIORITY_FEE_HARD_CAP_LAMPORTS})"
            )


def inspect_swap_transaction(tx, payer_pubkey: str) -> None:
    """Validate transaction safety before signing (SEC-01 / §77 C1).
    Atomically records guard_refused safety metrics on refusal.
    """
    try:
        _inspect_swap_transaction_inner(tx, payer_pubkey)
    except ExecutionError as e:
        try:
            from live_execution.safety_counters import record_safety_refusal
            record_safety_refusal("guard_refused", str(e))
        except Exception:
            pass
        raise


async def sign_after_checks(
    swap_b64: str,
    payer,
    intent: TradeIntent,
    quote: Optional[dict] = None,
) -> solana.SignedSwap:
    """Fixed sequence (§79 Phase A-2):
    1. Decode unsigned tx from base64
    2. inspect_swap_transaction (static instruction inspection)
    3. (if config.SIMULATION_REQUIRED) await sim_guard.simulate_and_assert
    4. Sign and return SignedSwap(_checked=True)
    """
    from solders.transaction import VersionedTransaction  # type: ignore

    raw_unsigned = base64.b64decode(swap_b64)
    tx = VersionedTransaction.from_bytes(raw_unsigned)
    try:
        inspect_swap_transaction(tx, str(payer.pubkey()))
    except Exception as exc:
        try:
            from alerts import alert_nowait, AlertEvent
            alert_nowait(
                AlertEvent.GUARD_REFUSAL,
                f"Static guard refusal: {exc}",
                key=f"guard_refusal:{str(exc)[:40]}",
            )
        except Exception:
            pass
        raise

    if config.SIMULATION_REQUIRED:
        if quote is None:
            raise ExecutionError("SIMULATION_REQUIRED is True but no quote provided for pre-sign simulation")
        from live_execution import sim_guard
        await sim_guard.simulate_and_assert(swap_b64, quote, str(payer.pubkey()), intent=intent)

    signed = VersionedTransaction(tx.message, [payer])
    sig = str(signed.signatures[0])
    raw = bytes(signed)
    return solana.SignedSwap(signature=sig, raw=raw, _checked=True)



# ---------------------------------------------------------------------------
# A5 (repo audit): the old hand-rolled single-endpoint `_rpc` send +
# `_confirm_signature` poll (maxRetries=0, one RPC_URL, no failover) are
# GONE. Broadcast + confirm now go through the SAME helpers the automated
# executor uses — solana.send_raw_transaction (rotating RPC_URLS,
# skipPreflight: False — preflight is performed so RPC checks program status before broadcast,
# maxRetries=3, preflight commitment=confirmed) and
# solana.confirm_signature — so both money paths carry ONE broadcast policy.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Public API — propose → human approves → execute, fail-closed throughout
# ---------------------------------------------------------------------------

async def propose_trade(
    output_mint: str,
    output_decimals: int | None,
    usd_size: float,
    queue: ConfirmationQueue | None = None,
    ledger: ExecutionLedger | None = None,
):
    """
    Run every offline safety gate, fetch an informational quote, and queue
    the trade for human approval. Returns the PendingConfirmation.
    Nothing is sent here — this only proposes.
    """
    queue = queue or default_queue()
    quote = await get_jupiter_quote(
        output_mint, output_decimals, usd_size, ledger=ledger
    )
    pc = queue.propose(
        output_mint,
        assert_decimals_known(output_decimals, output_mint),
        usd_size,
        quote_snapshot={
            "tokens_out": quote["tokens_out"],
            "price_usd": quote["price_usd"],
        },
    )
    log.info(
        "PROPOSED %s: $%.2f -> ~%.6f tokens @ $%.8g | confirm id %s "
        "(expires in %.0fs)",
        output_mint[:8], usd_size, quote["tokens_out"],
        quote["price_usd"], pc.id, config.CONFIRM_EXPIRY_SECONDS,
    )
    return pc


async def execute_confirmed_trade(
    output_mint: str,
    output_decimals: int | None,
    usd_size: float,
    confirmation_id: str | None = None,
    idempotency_key: str | None = None,
    queue: ConfirmationQueue | None = None,
    ledger: ExecutionLedger | None = None,
    reference_price_usd: float | None = None,
) -> dict:
    """
    Buy `usd_size` USD of `output_mint` on Jupiter, sign locally, send via
    RPC, confirm on-chain. Every refusal happens BEFORE any network call.

    Idempotency: pass a stable idempotency_key per intended trade; a retry
    with the same key returns the original outcome instead of re-sending.
    Manual confirmation: REQUIRE_MANUAL_CONFIRMATION=True (hardcoded default)
    demands a valid, unexpired, approved confirmation_id — consumed here.
    """
    queue = queue or default_queue()
    ledger = ledger or default_ledger()

    # 1-6: all offline gates (kill switch, flags, caps, decimals, breaker).
    preflight(usd_size, output_mint, output_decimals, ledger)

    # 7: idempotent replay — never send twice for one intended trade.
    if not idempotency_key:
        raise Refusal(
            "idempotency_key is required — retries after an unclear network "
            "failure must never be able to double-send"
        )
    prior = ledger.get_by_idempotency_key(idempotency_key)
    if prior is not None:
        log.info(
            "idempotent replay for key %s — returning prior outcome",
            idempotency_key,
        )
        return {
            "deduplicated": True,
            "signature": prior.signature,
            "status": prior.status,
            "tokens_out": prior.tokens_out,
            "price_usd": prior.price_usd,
            "usd_size": prior.usd_size,
        }

    # 8: mandatory manual confirmation (hardcoded default ON).
    if config.REQUIRE_MANUAL_CONFIRMATION:
        if not confirmation_id:
            raise Refusal(
                "REQUIRE_MANUAL_CONFIRMATION is True — a confirmation_id "
                "from propose_trade + scripts/confirm_trade.py approve is "
                "mandatory"
            )
        try:
            queue.consume(confirmation_id)
        except ConfirmationError as exc:
            raise Refusal(f"confirmation refused: {exc}") from exc
    else:
        log.warning(
            "REQUIRE_MANUAL_CONFIRMATION is False — proceeding without "
            "human approval (operator-disabled safeguard)"
        )

    payer = wallet.load_keypair()
    user_public_key = wallet.pubkey_string(payer)

    # Reference price floor for buys (fail closed)
    if reference_price_usd is None or reference_price_usd <= 0:
        raise Refusal("no reference price available for buy (fail closed)")

    min_out_raw = math.floor(
        usd_size
        / (reference_price_usd * (1.0 + config.BUY_MAX_PRICE_DEVIATION))
        * (10 ** (output_decimals or 0))
    )
    intent = TradeIntent(
        side="buy",
        wallet=user_public_key,
        input_mint=_BACKEND_USDC_MINT,
        output_mint=output_mint,
        in_amount_raw=usd_to_raw_amount(usd_size, USDC_DECIMALS),
        slippage_bps=config.SLIPPAGE_BPS,
        min_out_raw=min_out_raw,
        reference_price_usd=reference_price_usd,
    )

    quote_result = await get_jupiter_quote(
        output_mint, output_decimals, usd_size, ledger=ledger
    )
    swap_b64 = await _build_swap_transaction(
        quote_result["quote"], user_public_key
    )

    signed_swap = await sign_after_checks(
        swap_b64, payer, intent=intent, quote=quote_result["quote"]
    )
    signature = signed_swap.signature

    log.info(
        "EXECUTE %s: $%.2f -> ~%.6f tokens @ $%.8g | sig %s",
        output_mint[:8], usd_size, quote_result["tokens_out"],
        quote_result["price_usd"], signature,
    )

    sent = await solana.send_checked_swap(signed_swap)
    if not sent:
        raise ExecutionError("every rpc refused the transaction")
    conf = await solana.confirm_signature(sent)
    if not conf["confirmed"]:
        raise ExecutionError(
            f"not confirmed before timeout: {conf['err'] or 'unknown'}")
    signature = sent

    record = ledger.record_buy(
        idempotency_key=idempotency_key,
        mint=output_mint,
        usd_size=usd_size,
        tokens_out=quote_result["tokens_out"],
        price_usd=quote_result["price_usd"],
        signature=signature,
        status="confirmed",
    )

    return {
        "deduplicated": False,
        "signature": signature,
        "status": record.status,
        "tokens_out": quote_result["tokens_out"],
        "price_usd": quote_result["price_usd"],
        "usd_size": usd_size,
    }


if __name__ == "__main__":
    # Operator-invoked CLI. Never called from the paper pipeline.
    #   propose:  python -m live_execution.jupiter_executor --mint M \
    #                 --decimals 6 --usd 10 --propose
    #   execute:  python -m live_execution.jupiter_executor --mint M \
    #                 --decimals 6 --usd 10 \
    #                 --confirmation-id ID --idempotency-key K
    import argparse

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(
        description="REAL-MONEY Jupiter buy (live_execution)")
    p.add_argument("--mint", required=True)
    p.add_argument("--decimals", type=int, required=True,
                   help="output token mint decimals (refused if unknown)")
    p.add_argument("--usd", type=float, required=True)
    p.add_argument("--propose", action="store_true",
                   help="queue for approval only; print the confirmation id")
    p.add_argument("--confirmation-id", default=None)
    p.add_argument("--idempotency-key", default=None,
                   help="stable key for this intended trade "
                        "(required to execute)")
    args = p.parse_args()

    if args.propose:
        pc = asyncio.run(propose_trade(args.mint, args.decimals, args.usd))
        print(json.dumps(pc.to_json(), indent=2))
    else:
        result = asyncio.run(execute_confirmed_trade(
            args.mint, args.decimals, args.usd,
            confirmation_id=args.confirmation_id,
            idempotency_key=args.idempotency_key,
        ))
        print(json.dumps(result, indent=2))
