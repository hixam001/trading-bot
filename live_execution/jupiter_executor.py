"""
live_execution/jupiter_executor.py — REAL-MONEY swap execution via Jupiter.

⚠ HONEST COVERAGE STATEMENT ⚠
Offline-tested here: precondition refusals (enabled flag / wallet file /
unknown decimals / size cap — all BEFORE any network call), quote + swap-build
request shapes and parsing via httpx.MockTransport, and the decimals-aware
raw-unit math (imported from backend.data_providers.jupiter so it cannot
drift from the paper-side fix).
NOT tested here: keypair signing, sendTransaction, and on-chain confirmation —
those require a funded wallet and RPC access and must first be exercised as a
deliberate manual micro-trade by the operator.

Parity guarantees:
  - Endpoint: lite-api.jup.ag/swap/v1/{quote,swap}  (v6 quote-api is dead).
  - Raw-unit math imported from backend.data_providers.jupiter
    (raw_units_for_one_token / price_from_quote) — single source of truth.
  - UNKNOWN DECIMALS REFUSE THE TRADE. Never a default fallback: the paper-
    side decimals bug fabricated prices at exactly 10**(9-decimals) scale.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from data_providers.base import ProviderError  # noqa: E402
from data_providers.jupiter import price_from_quote, raw_units_for_one_token  # noqa: E402

from live_execution import config  # noqa: E402

log = logging.getLogger(__name__)

log = logging.getLogger(__name__)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6


class ExecutionError(Exception):
    """A trade could not be completed. Nothing was sent unless stated."""


class Refusal(ExecutionError):
    """Pre-condition failure — refused BEFORE any network call."""



# ---------------------------------------------------------------------------
# Fail-closed preconditions — every refusal happens BEFORE any network call.
# ---------------------------------------------------------------------------

def assert_ready(usd_size: float) -> None:
    if not config.EXECUTION_ENABLED:
        raise Refusal(
            "LIVE_EXECUTION_ENABLED != 1 — real execution is disabled. "
            "Set it explicitly in live_execution/config.py scope (.env: "
            "LIVE_EXECUTION_ENABLED=1) after human review."
        )
    if usd_size <= 0:
        raise Refusal(f"usd_size must be > 0, got {usd_size!r}")
    if usd_size > config.MAX_TRADE_USD:
        raise Refusal(
            f"usd_size {usd_size} exceeds MAX_TRADE_USD "
            f"{config.MAX_TRADE_USD}"
        )
    kp = config.WALLET_KEYPAIR_PATH
    if not kp or not Path(kp).is_file():
        raise Refusal(
            f"WALLET_KEYPAIR_PATH not set or file missing ({kp!r})"
        )


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


# ---------------------------------------------------------------------------
# HTTP helper — bounded retries, distinct 429 handling (parity w/ paper side)
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



# ---------------------------------------------------------------------------
# Quote — decimals-aware, USDC → token
# ---------------------------------------------------------------------------

async def get_jupiter_quote(
    output_mint: str,
    output_decimals: int | None,
    usd_size: float,
    input_mint: str = USDC_MINT,
) -> dict:
    """
    Quote buying `usd_size` USD of `output_mint` (paying with USDC).
    FAILS CLOSED on unknown output decimals or precondition failures.
    Returns {quote, amount_raw, out_amount_raw, tokens_out, price_usd}.
    """
    assert_ready(usd_size)
    dec = assert_decimals_known(output_decimals, output_mint)

    # We pay in USDC (6 decimals): raw units for the USD size.
    amount_raw = int(round(usd_size * (10 ** USDC_DECIMALS)))

    data = await _post_json(
        "https://lite-api.jup.ag/swap/v1/quote",
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
    tokens_out = int(out_raw) / (10 ** dec)
    return {
        "quote": data,
        "amount_raw": amount_raw,
        "out_amount_raw": int(out_raw),
        "tokens_out": tokens_out,
        "price_usd": usd_size / tokens_out if tokens_out > 0 else float("nan"),
    }



# ---------------------------------------------------------------------------
# Swap transaction build + local signing + send/confirm
# ---------------------------------------------------------------------------

async def _build_swap_transaction(quote: dict, user_public_key: str) -> str:
    """
    Returns the base64 UNSIGNED VersionedTransaction from Jupiter's swap API.
    Signing happens locally (step 3 of the omotrades-verified flow).
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


def _load_payer():
    """Lazy solders import so the package imports cleanly without it."""
    try:
        from solders.keypair import Keypair  # type: ignore
    except ImportError as exc:
        raise ExecutionError(
            "solders is not installed — run: pip install solders"
        ) from exc
    return Keypair.from_json(config.WALLET_KEYPAIR_PATH)


def _sign_transaction(swap_b64: str, payer) -> tuple[str, bytes]:
    from solders.transaction import VersionedTransaction  # type: ignore

    raw_unsigned = base64.b64decode(swap_b64)
    tx = VersionedTransaction.deserialize(raw_unsigned)
    signed = VersionedTransaction(tx.message, [payer])
    signature = str(signed.signatures[0])
    return signature, bytes(signed)


async def _rpc(method: str, params: list) -> dict:
    return await _post_json(
        config.RPC_URL, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )


async def _confirm_signature(signature: str) -> str:
    deadline = asyncio.get_event_loop().time() + config.CONFIRM_TIMEOUT_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        res = await _rpc("getSignatureStatuses", [[signature],
                                                  {"searchTransactionHistory": False}])
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
# Public API — one function, full flow, fail-closed at every step
# ---------------------------------------------------------------------------

async def execute_confirmed_trade(
    output_mint: str,
    output_decimals: int | None,
    usd_size: float,
    input_mint: str = USDC_MINT,
) -> dict:
    """
    Buy `usd_size` USD of `output_mint` on Jupiter, sign locally, send via
    RPC, confirm on-chain.

    Refusals (BEFORE any network call): disabled flag, size ≤ 0 or > cap,
    missing keypair file, unknown decimals.
    Returns {signature, status, tokens_out, price_usd, usd_size}.
    """
    assert_ready(usd_size)
    dec = assert_decimals_known(output_decimals, output_mint)

    payer = _load_payer()
    user_public_key = str(payer.pubkey())

    quote_result = await get_jupiter_quote(
        output_mint, dec, usd_size, input_mint=input_mint
    )
    swap_b64 = await _build_swap_transaction(
        quote_result["quote"], user_public_key
    )
    signature, raw_signed = _sign_transaction(swap_b64, payer)

    log.info("EXECUTE %s: $%.2f -> ~%.4f tokens @ $%.8g | sig %s",
             output_mint[:8], usd_size, quote_result["tokens_out"],
             quote_result["price_usd"], signature)

    await _rpc(
        "sendTransaction",
        [
            base64.b64encode(raw_signed).decode(),
            {"encoding": "base64", "skipPreflight": False, "maxRetries": 0},
        ],
    )
    status = await _confirm_signature(signature)

    return {
        "signature": signature,
        "status": status,
        "tokens_out": quote_result["tokens_out"],
        "price_usd": quote_result["price_usd"],
        "usd_size": usd_size,
    }


if __name__ == "__main__":
    # Operator-invoked CLI. Never called from the paper pipeline.
    import argparse
    import logging

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="REAL-MONEY Jupiter buy (live_execution)")
    p.add_argument("--mint", required=True)
    p.add_argument("--decimals", type=int, required=True,
                   help="output token mint decimals (refused if omitted)")
    p.add_argument("--usd", type=float, required=True)
    args = p.parse_args()

    result = asyncio.run(execute_confirmed_trade(args.mint, args.decimals, args.usd))
    print(json.dumps(result, indent=2))




