"""
live_execution/executor.py - omo-style place_order: ONE entry point for
buys AND sells with omotrades execute.server.ts result statuses:

  unarmed - LIVE_TRADING_ENABLED is False (the normal state); nothing runs
  blocked - a risk guard refused BEFORE any network call
  failed  - network phase attempted but no confirmed fill
  filled  - confirmed on-chain and journalled (only then)

No memo/commit layer (deliberately omitted per operator decision). Everything
else is omo parity at this book scale: local signing, multi-RPC broadcast,
confirmation-before-journal, price-impact floor, SOL reserve, daily deploy
cap, idempotent buys, fraction-of-position sells with chain-read decimals.

ZERO live-network test coverage (same honesty rule as jupiter_executor):
exercise the full flow on devnet with a throwaway keypair BEFORE mainnet.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Optional

from live_execution import config, kill_switch, solana, wallet
from live_execution.confirmation_queue import ConfirmationError
from live_execution.jupiter_executor import (
    USDC_DECIMALS,
    _BACKEND_QUOTE_URL,
    _BACKEND_USDC_MINT,
    _build_swap_transaction,
    _post_json,
    _sign_transaction,
    default_ledger,
    default_queue,
    get_jupiter_quote,
    preflight,
)
from live_execution.models import ExecutionLedger
from live_execution.commit_log import CommitLog

log = logging.getLogger(__name__)


def raw_units(decimals: int) -> int:
    """10**decimals via the shared backend unit-math identity (no drift)."""
    import sys
    from pathlib import Path
    backend = Path(__file__).resolve().parent.parent / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from data_providers.jupiter import raw_units_for_one_token
    return raw_units_for_one_token(decimals)


@dataclass
class OrderResult:
    status: str = ""        # unarmed | blocked | failed | filled
    reason: str = ""
    side: str = ""
    symbol: str = ""
    mint: str = ""
    usd_value: float = 0.0
    tokens: float = 0.0
    signature: str = ""
    slot: Optional[int] = None
    price_impact_pct: float = 0.0

    def to_json(self) -> dict:
        return asdict(self)


def quote_impact_pct(quote: dict) -> float:
    """Jupiter returns priceImpactPct as a decimal fraction."""
    try:
        return abs(float(quote.get("priceImpactPct") or 0)) * 100.0
    except (TypeError, ValueError):
        return 0.0


async def _broadcast_and_confirm(swap_b64: str, payer, logc=None, intent=None) -> OrderResult:
    """Sign locally, broadcast across RPCs, confirm. Journal is the CALLER job."""
    signature, raw_signed = _sign_transaction(swap_b64, payer)
    sealed = logc.seal(intent.get("kind", "order"), intent) if (logc is not None and intent) else None
    sent = await solana.send_raw_transaction(raw_signed)
    if not sent:
        return OrderResult(status="failed", reason="every rpc refused the transaction", signature=signature)
    conf = await solana.confirm_signature(sent)
    if not conf["confirmed"]:
        return OrderResult(status="failed", reason=conf["err"] or "unconfirmed", signature=sent)
    if sealed is not None and logc is not None:
        logc.bind(sealed["hash"], sent)
    return OrderResult(status="filled", signature=sent, slot=conf.get("slot"))


async def place_buy(
    mint: str,
    symbol: str,
    usd: float,
    output_decimals: Optional[int],
    confirmation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    queue=None,
    ledger: Optional[ExecutionLedger] = None,
) -> OrderResult:
    """Automated omo-style BUY: every guard fails closed before any network call."""
    base = OrderResult(side="buy", symbol=symbol, mint=mint)
    if not config.LIVE_TRADING_ENABLED:
        return OrderResult(**{**base.to_json(), "status": "unarmed",
                              "reason": "LIVE_TRADING_ENABLED is False - real execution is disarmed"})
    ledger = ledger or default_ledger()
    queue = queue or default_queue()
    try:
        kill_switch.assert_not_tripped()
    except Exception as exc:
        return OrderResult(**{**base.to_json(), "status": "blocked", "reason": str(exc)})

    deployed = ledger.deployed_today_usd()
    if deployed + usd > config.MAX_DAILY_DEPLOY_USD:
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": f"daily deploy cap: {deployed:.2f} + {usd:.2f} > {config.MAX_DAILY_DEPLOY_USD:.2f}"})

    if idempotency_key:
        prior = ledger.get_by_idempotency_key(idempotency_key)
        if prior is not None:
            log.info("idempotent replay for key %s", idempotency_key)
            return OrderResult(status=prior.status if prior.status in ("filled",) else "failed" if prior.status == "failed" else prior.status,
                               reason="deduplicated", side="buy", symbol=symbol, mint=mint,
                               usd_value=prior.usd_size, tokens=prior.tokens_out, signature=prior.signature)

    if config.REQUIRE_MANUAL_CONFIRMATION:
        if not confirmation_id:
            return OrderResult(**{**base.to_json(), "status": "blocked",
                                  "reason": "REQUIRE_MANUAL_CONFIRMATION is True and no confirmation_id given"})
        try:
            queue.consume(confirmation_id)
        except ConfirmationError as exc:
            return OrderResult(**{**base.to_json(), "status": "blocked", "reason": f"confirmation refused: {exc}"})

    try:
        payer = wallet.load_keypair()
        wallet.verify_expected_address(payer)
    except Exception as exc:
        return OrderResult(**{**base.to_json(), "status": "blocked", "reason": f"wallet: {exc}"})

    logc = CommitLog(config.STATE_DIR / "commits.json")
    addr = wallet.pubkey_string(payer)
    sol_bal = await solana.get_sol_balance(addr)
    if sol_bal is not None and sol_bal < config.MIN_SOL_RESERVE:
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": f"SOL reserve {sol_bal:.4f} below floor {config.MIN_SOL_RESERVE}"})

    try:
        q = await get_jupiter_quote(mint, output_decimals, usd, ledger=ledger)
    except Refusal as exc:
        return OrderResult(**{**base.to_json(), "status": "blocked", "reason": str(exc)})
    except ExecutionError as exc:
        return OrderResult(**{**base.to_json(), "status": "failed", "reason": str(exc)})

    intent = dict(kind="buy", mint=mint, symbol=symbol, usd=usd)
    impact = quote_impact_pct(q["quote"])
    if impact > config.MAX_PRICE_IMPACT_PCT:
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": f"price impact {impact:.2f}% above floor {config.MAX_PRICE_IMPACT_PCT}%"})

    try:
        swap_b64 = await _build_swap_transaction(q["quote"], addr)
        outcome = await _broadcast_and_confirm(swap_b64, payer, logc=logc, intent=intent)
    except ExecutionError as exc:
        return OrderResult(**{**base.to_json(), "status": "failed", "reason": str(exc), "price_impact_pct": impact})

    if outcome.status == "filled":
        rec = ledger.record_buy(
            idempotency_key=idempotency_key or f"buy-{mint}-{int(time.time())}",
            mint=mint, usd_size=usd, tokens_out=q["tokens_out"],
            price_usd=q["price_usd"], signature=outcome.signature, status="confirmed")
        outcome.usd_value = usd
        outcome.tokens = q["tokens_out"]
        outcome.price_impact_pct = impact
        log.info("FILLED buy %s $%.2f -> %.6f tokens sig %s", mint[:8], usd, q["tokens_out"], outcome.signature)
    return outcome


async def place_sell(
    mint: str,
    symbol: str,
    fraction: float,
    confirmation_id: Optional[str] = None,
    queue=None,
    ledger: Optional[ExecutionLedger] = None,
) -> OrderResult:
    """omo-style SELL: fraction of the open position, decimals read from chain.
    UNKNOWN decimals refuse - never a default (the decimals lesson)."""
    base = OrderResult(side="sell", symbol=symbol, mint=mint)
    if not config.LIVE_TRADING_ENABLED:
        return OrderResult(**{**base.to_json(), "status": "unarmed", "reason": "disarmed"})
    ledger = ledger or default_ledger()
    queue = queue or default_queue()
    try:
        kill_switch.assert_not_tripped()
    except Exception as exc:
        return OrderResult(**{**base.to_json(), "status": "blocked", "reason": str(exc)})

    amounts = ledger.open_token_amounts()
    position_amount = amounts.get(mint, 0.0)
    if position_amount <= 0:
        return OrderResult(**{**base.to_json(), "status": "blocked", "reason": "no position to sell"})
    frac = min(max(fraction, 0.01), 1.0)
    sell_amount = position_amount * frac
    if sell_amount <= 0:
        return OrderResult(**{**base.to_json(), "status": "blocked", "reason": "sell size rounds to zero"})

    decimals = await solana.get_mint_decimals(mint)
    if decimals is None:
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": "could not read mint decimals on-chain - refusing"})

    if config.REQUIRE_MANUAL_CONFIRMATION and not confirmation_id:
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": "REQUIRE_MANUAL_CONFIRMATION is True and no confirmation_id given"})

    try:
        payer = wallet.load_keypair()
        wallet.verify_expected_address(payer)
    except Exception as exc:
        return OrderResult(**{**base.to_json(), "status": "blocked", "reason": f"wallet: {exc}"})

    logc = CommitLog(config.STATE_DIR / "commits.json")
    amount_raw = int(sell_amount * raw_units(decimals))
    if amount_raw <= 0:
        return OrderResult(**{**base.to_json(), "status": "blocked", "reason": "raw sell size rounds to zero"})

    try:
        quote = await _post_json(_BACKEND_QUOTE_URL, {
            "inputMint": mint,
            "outputMint": _BACKEND_USDC_MINT,
            "amount": str(amount_raw),
            "slippageBps": str(config.SLIPPAGE_BPS),
        })
    except ExecutionError as exc:
        return OrderResult(**{**base.to_json(), "status": "failed", "reason": f"quote: {exc}"})
    out_raw = quote.get("outAmount")
    if out_raw is None:
        return OrderResult(**{**base.to_json(), "status": "failed", "reason": "no route quoted for this mint"})

    intent = dict(kind="sell", mint=mint, symbol=symbol, fraction=frac)
    impact = quote_impact_pct(quote)
    if impact > config.MAX_PRICE_IMPACT_PCT:
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": f"price impact {impact:.2f}% above floor {config.MAX_PRICE_IMPACT_PCT}%"})
    proceeds_usd = int(out_raw) / raw_units(USDC_DECIMALS)

    if config.REQUIRE_MANUAL_CONFIRMATION:
        try:
            queue.consume(confirmation_id)
        except ConfirmationError as exc:
            return OrderResult(**{**base.to_json(), "status": "blocked", "reason": f"confirmation refused: {exc}"})

    try:
        swap_b64 = await _build_swap_transaction(quote, wallet.pubkey_string(payer))
        outcome = await _broadcast_and_confirm(swap_b64, payer, logc=logc, intent=intent)
    except ExecutionError as exc:
        return OrderResult(**{**base.to_json(), "status": "failed", "reason": str(exc), "price_impact_pct": impact})

    if outcome.status == "filled":
        try:
            ledger.reduce_position(mint, frac, proceeds_usd)
        except ValueError as exc:
            log.error("post-fill ledger reduce failed: %s", exc)
        outcome.usd_value = proceeds_usd
        outcome.tokens = sell_amount
        outcome.price_impact_pct = impact
        log.info("FILLED sell %s %.4f tokens -> $%.2f sig %s", mint[:8], sell_amount, proceeds_usd, outcome.signature)
    return outcome


async def place_order(
    side: str,
    mint: str,
    symbol: str,
    usd: Optional[float] = None,
    fraction: Optional[float] = None,
    output_decimals: Optional[int] = None,
    confirmation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> OrderResult:
    """omo OrderIntent parity: buys sized in USD, sells as a position fraction."""
    if side == "buy":
        if not usd or usd <= 0:
            return OrderResult(status="blocked", reason="buy requires a positive usd size",
                               side="buy", symbol=symbol, mint=mint)
        return await place_buy(mint, symbol, float(usd), output_decimals,
                               confirmation_id=confirmation_id,
                               idempotency_key=idempotency_key)
    if side == "sell":
        return await place_sell(mint, symbol,
                                1.0 if fraction is None else float(fraction),
                                confirmation_id=confirmation_id)
    return OrderResult(status="blocked", reason=f"unknown side {side!r}", side=side, mint=mint)
