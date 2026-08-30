"""
live_execution/jupiter_executor.py — REAL-MONEY swap execution via Jupiter.

⚠ HONEST COVERAGE STATEMENT ⚠
This module has ZERO live-network test coverage. Nothing in this package has
ever been executed against Solana mainnet RPC or Jupiter's swap API; the
offline tests cover refusal logic, request shapes, and math via mocks only.
BEFORE THIS EVER RUNS AGAINST MAINNET, the full flow (propose → approve →
execute → on-chain confirm) MUST first be exercised on Solana DEVNET with a
throwaway keypair. That is a hard requirement, not a suggestion — real funds
are at stake.

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

from live_execution import config  # noqa: E402
from live_execution import kill_switch  # noqa: E402
from live_execution import wallet  # noqa: E402
from live_execution.confirmation_queue import (  # noqa: E402
    ConfirmationError,
    ConfirmationQueue,
)
from live_execution.models import ExecutionLedger  # noqa: E402

log = logging.getLogger(__name__)

USDC_DECIMALS = 6


class ExecutionError(Exception):
    """A trade could not be completed. Nothing was sent unless stated."""


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

    # We pay in USDC (6 decimals): raw units for the USD size — via the
    # shared unit-math helper, not a local 10**6.
    amount_raw = int(round(usd_size * raw_units_for_one_token(USDC_DECIMALS)))

    data = await _get_json(
        _BACKEND_QUOTE_URL,
        {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": str(config.SLIPPAGE_BPS),
        },
    )
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
    return {
        "quote": data,
        "amount_raw": amount_raw,
        "out_amount_raw": out_amount_raw,
        "tokens_out": tokens_out,
        "price_usd": usd_size / tokens_out,
    }


# ---------------------------------------------------------------------------
# Swap transaction build + local signing + send/confirm
# ---------------------------------------------------------------------------

async def _build_swap_transaction(quote: dict, user_public_key: str) -> str:
    """
    Returns the base64 UNSIGNED VersionedTransaction from Jupiter's swap API.
    Signing happens locally; the secret never leaves this process.
    """
    data = await _post_json(
        config.JUPITER_SWAP_URL,
        {
            "quoteResponse": quote,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": True,
        },
    )
    b64 = data.get("swapTransaction")
    if not b64:
        raise ExecutionError("jupiter: no swapTransaction in response")
    return b64


def _sign_transaction(swap_b64: str, payer) -> tuple[str, bytes]:
    from solders.transaction import VersionedTransaction  # type: ignore

    raw_unsigned = base64.b64decode(swap_b64)
    # solders 0.29 parse constructor is from_bytes — there is NO .deserialize.
    # That call crashed the first real armed order (AttributeError) after the
    # quote+swap phases had already succeeded. Regression-tested offline.
    tx = VersionedTransaction.from_bytes(raw_unsigned)
    signed = VersionedTransaction(tx.message, [payer])
    signature = str(signed.signatures[0])
    return signature, bytes(signed)


async def _rpc(method: str, params: list) -> dict:
    return await _post_json(
        config.RPC_URL,
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


async def _confirm_signature(signature: str) -> str:
    deadline = asyncio.get_event_loop().time() + config.CONFIRM_TIMEOUT_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        res = await _rpc(
            "getSignatureStatuses",
            [[signature], {"searchTransactionHistory": False}],
        )
        val = (res.get("result") or {}).get("value") or []
        if val and val[0]:
            status = val[0].get("confirmationStatus")
            if status in ("confirmed", "finalized"):
                err = val[0].get("err")
                if err:
                    raise ExecutionError(f"transaction FAILED on-chain: {err}")
                return status
        await asyncio.sleep(2.0)
    raise ExecutionError("confirmation timed out")


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

    # 9+: network phase. Fresh quote at execution time — the approval-time
    # snapshot was informational only; never execute on a stale quote.
    quote_result = await get_jupiter_quote(
        output_mint, output_decimals, usd_size, ledger=ledger
    )
    swap_b64 = await _build_swap_transaction(
        quote_result["quote"], user_public_key
    )
    signature, raw_signed = _sign_transaction(swap_b64, payer)

    log.info(
        "EXECUTE %s: $%.2f -> ~%.6f tokens @ $%.8g | sig %s",
        output_mint[:8], usd_size, quote_result["tokens_out"],
        quote_result["price_usd"], signature,
    )

    await _rpc(
        "sendTransaction",
        [
            base64.b64encode(raw_signed).decode(),
            {"encoding": "base64", "skipPreflight": False, "maxRetries": 0},
        ],
    )
    status = await _confirm_signature(signature)

    record = ledger.record_buy(
        idempotency_key=idempotency_key,
        mint=output_mint,
        usd_size=usd_size,
        tokens_out=quote_result["tokens_out"],
        price_usd=quote_result["price_usd"],
        signature=signature,
        status="confirmed" if status == "finalized" else status,
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
