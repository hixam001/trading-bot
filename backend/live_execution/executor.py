"""
live_execution/executor.py - reference-style place_order: ONE entry point for
buys AND sells with the reference bot execute.server.ts result statuses:

  unarmed - LIVE_TRADING_ENABLED is False (the normal state); nothing runs
  blocked - a risk guard refused BEFORE any network call
  failed  - network phase attempted but no confirmed fill
  filled  - confirmed on-chain and journalled (only then)

REF-R11 commit–reveal (operator-approved 2026-08-27, handoff §26): every
armed order seals sha256(nonce|canonical_payload) locally, publishes that
hash on-chain as a Solana memo, and ONLY THEN quotes/builds/broadcasts the
fill. A memo that cannot be published and confirmed BLOCKS the fill (fail
closed — a decision that cannot be committed on-chain is not executed). The
memo precedes the quote so the quote→fill window stays as tight as ever.
Everything else is reference parity at this book scale: local signing,
multi-RPC broadcast, confirmation-before-journal, price-impact floor, SOL
reserve, USDC funding check, daily deploy cap, idempotent buys,
fraction-of-position sells with chain-read decimals.

ZERO live-network test coverage (same honesty rule as jupiter_executor):
exercise the full flow on devnet with a throwaway keypair BEFORE mainnet.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from live_execution import config, kill_switch, memo, solana, wallet
from live_execution.confirmation_queue import ConfirmationError
from live_execution.jupiter_executor import (
    ExecutionError,
    Refusal,
    USDC_DECIMALS,
    _BACKEND_QUOTE_URL,
    _BACKEND_USDC_MINT,
    _build_swap_transaction,
    _get_json,
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
    # The package lives inside backend/: backend/ is the grandparent dir.
    backend = Path(__file__).resolve().parent.parent
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
    # REF-R11: the sealed decision behind this order + its on-chain memo.
    # Empty until the armed flow runs; carried so the bridge can journal the
    # EXACT seal (nonce/payload/hash) into the public decision record.
    commit_hash: str = ""
    commit_nonce: str = ""
    commit_payload: dict = field(default_factory=dict)
    memo_signature: str = ""
    memo_slot: Optional[int] = None

    def to_json(self) -> dict:
        return asdict(self)


def quote_impact_pct(quote: dict) -> float:
    """Jupiter returns priceImpactPct as a decimal fraction."""
    try:
        return abs(float(quote.get("priceImpactPct") or 0)) * 100.0
    except (TypeError, ValueError):
        return 0.0


async def _broadcast_and_confirm(swap_b64: str, payer, logc=None, sealed=None) -> OrderResult:
    """Sign locally, broadcast across RPCs, confirm. Journal is the CALLER job.

    REF-R11: sealing + memo publication already happened in the caller
    BEFORE this runs; on confirmation the fill signature is bound to the
    sealed (and already on-chain) commit."""
    signature, raw_signed = _sign_transaction(swap_b64, payer)
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
    """Automated reference-style BUY: every guard fails closed before any network call."""
    base = OrderResult(side="buy", symbol=symbol, mint=mint)
    ledger = ledger or default_ledger()
    queue = queue or default_queue()
    try:
        preflight(usd, mint, output_decimals, ledger)
    except Refusal as exc:
        status = "unarmed" if not config.LIVE_TRADING_ENABLED else "blocked"
        return OrderResult(**{**base.to_json(), "status": status, "reason": str(exc)})
    except kill_switch.KillSwitchTripped as exc:
        return OrderResult(**{**base.to_json(), "status": "blocked", "reason": str(exc)})
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
    if sol_bal is None or sol_bal < config.MIN_SOL_RESERVE:
        detail = "unreadable" if sol_bal is None else f"{sol_bal:.4f}"
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": f"SOL reserve {detail} below floor {config.MIN_SOL_RESERVE}"})

    # REF-R11 micro-bootstrap: verify REAL funding before committing anything
    # on-chain. Fail closed: an unreadable balance refuses the order, and a
    # balance below the ticket refuses it BEFORE the memo fee can be spent.
    usdc_bal = await solana.get_usdc_balance(addr)
    if usdc_bal is None:
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": "USDC balance unreadable from any rpc - refusing"})
    if usdc_bal < usd:
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": f"insufficient USDC: {usdc_bal:.2f} < {usd:.2f}"})

    # REF-R11 commit-reveal, fail-closed ordering:
    #   seal -> publish memo -> CONFIRM memo -> quote -> build -> broadcast.
    # The memo goes out BEFORE the quote so the quote->fill window stays as
    # tight as ever; a memo that cannot be confirmed blocks the fill entirely
    # (handoff §22 requirement 4).
    intent = dict(kind="buy", mint=mint, symbol=symbol, usd=usd)
    sealed = logc.seal("buy", intent)
    try:
        memo_res = await memo.publish_commit_memo(payer, sealed["hash"])
    except memo.MemoPublishError as exc:
        logc.fail(sealed["hash"], f"memo: {exc}")
        log.warning("commit memo failed for %s: %s (fill NOT broadcast)",
                    mint[:8], exc)
        return OrderResult(**{**base.to_json(), "status": "failed",
                              "reason": f"commit memo failed: {exc}",
                              "commit_hash": sealed["hash"],
                              "commit_nonce": sealed["nonce"],
                              "commit_payload": intent})
    logc.record_memo(sealed["hash"], memo_res["signature"], memo_res["slot"])
    commit_fields = dict(commit_hash=sealed["hash"], commit_nonce=sealed["nonce"],
                         commit_payload=intent, memo_signature=memo_res["signature"],
                         memo_slot=memo_res["slot"])

    try:
        q = await get_jupiter_quote(mint, output_decimals, usd, ledger=ledger)
    except Refusal as exc:
        logc.fail(sealed["hash"], f"quote refused: {exc}")
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": str(exc), **commit_fields})
    except ExecutionError as exc:
        logc.fail(sealed["hash"], f"quote failed: {exc}")
        return OrderResult(**{**base.to_json(), "status": "failed",
                              "reason": str(exc), **commit_fields})
    except Exception as exc:
        # Fail closed on ANY unexpected quote-phase error and journal it —
        # never let it crash the cycle (defense-first).
        logc.fail(sealed["hash"], f"quote crashed: {type(exc).__name__}: {exc}")
        return OrderResult(**{**base.to_json(), "status": "failed",
                              "reason": str(exc), **commit_fields})

    impact = quote_impact_pct(q["quote"])
    if impact > config.MAX_PRICE_IMPACT_PCT:
        reason = f"price impact {impact:.2f}% above floor {config.MAX_PRICE_IMPACT_PCT}%"
        logc.fail(sealed["hash"], reason)
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": reason, **commit_fields})

    try:
        swap_b64 = await _build_swap_transaction(q["quote"], addr)
        outcome = await _broadcast_and_confirm(swap_b64, payer, logc=logc, sealed=sealed)
    except Exception as exc:
        # ANY build/sign/broadcast error (ExecutionError or not) fails closed
        # and lands in the journal. The first real armed order died on an
        # AttributeError here and the commit stayed 'published' with no
        # explanation — that must never happen again.
        logc.fail(sealed["hash"], f"{type(exc).__name__}: {exc}")
        return OrderResult(**{**base.to_json(), "status": "failed", "reason": str(exc),
                              "price_impact_pct": impact, **commit_fields})

    if outcome.status == "filled":
        rec = ledger.record_buy(
            idempotency_key=idempotency_key or f"buy-{mint}-{int(time.time())}",
            mint=mint, usd_size=usd, tokens_out=q["tokens_out"],
            price_usd=q["price_usd"], signature=outcome.signature, status="confirmed")
        outcome.usd_value = usd
        outcome.tokens = q["tokens_out"]
        outcome.price_impact_pct = impact
        log.info("FILLED buy %s $%.2f -> %.6f tokens sig %s", mint[:8], usd, q["tokens_out"], outcome.signature)
    else:
        # Honest journal: a commit whose fill did not confirm is marked failed
        # so the dashboard shows WHY the enter didn't execute (commit_log
        # contract: a skipped trade must be as visible as an executed one).
        logc.fail(sealed["hash"], outcome.reason or "fill not confirmed")
    outcome.commit_hash = sealed["hash"]
    outcome.commit_nonce = sealed["nonce"]
    outcome.commit_payload = intent
    outcome.memo_signature = memo_res["signature"]
    outcome.memo_slot = memo_res["slot"]
    return outcome


async def place_sell(
    mint: str,
    symbol: str,
    fraction: float,
    confirmation_id: Optional[str] = None,
    queue=None,
    ledger: Optional[ExecutionLedger] = None,
    full_close: bool = False,
) -> OrderResult:
    """reference-style SELL: fraction of the open position, decimals read from chain.
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

    # Human approval is consumed BEFORE any irreversible step (the memo is an
    # on-chain commitment; it must not precede the operator's sign-off).
    if config.REQUIRE_MANUAL_CONFIRMATION:
        try:
            queue.consume(confirmation_id)
        except ConfirmationError as exc:
            return OrderResult(**{**base.to_json(), "status": "blocked", "reason": f"confirmation refused: {exc}"})

    addr = wallet.pubkey_string(payer)
    sol_bal = await solana.get_sol_balance(addr)
    if sol_bal is None or sol_bal < config.MIN_SOL_RESERVE:
        detail = "unreadable" if sol_bal is None else f"{sol_bal:.4f}"
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": f"SOL reserve {detail} below floor {config.MIN_SOL_RESERVE}"})

    logc = CommitLog(config.STATE_DIR / "commits.json")
    amount_raw = int(sell_amount * raw_units(decimals))
    if amount_raw <= 0:
        return OrderResult(**{**base.to_json(), "status": "blocked", "reason": "raw sell size rounds to zero"})

    # REF-R11 commit-reveal (fail closed): seal -> memo -> CONFIRM memo ->
    # quote -> build -> broadcast. A sell decision that cannot be committed
    # on-chain is not executed (handoff §22 requirement 4).
    intent = dict(kind="sell", mint=mint, symbol=symbol, fraction=frac)
    sealed = logc.seal("sell", intent)
    try:
        memo_res = await memo.publish_commit_memo(payer, sealed["hash"])
    except memo.MemoPublishError as exc:
        logc.fail(sealed["hash"], f"memo: {exc}")
        log.warning("commit memo failed for sell %s: %s (fill NOT broadcast)",
                    mint[:8], exc)
        return OrderResult(**{**base.to_json(), "status": "failed",
                              "reason": f"commit memo failed: {exc}",
                              "commit_hash": sealed["hash"],
                              "commit_nonce": sealed["nonce"],
                              "commit_payload": intent})
    logc.record_memo(sealed["hash"], memo_res["signature"], memo_res["slot"])
    commit_fields = dict(commit_hash=sealed["hash"], commit_nonce=sealed["nonce"],
                         commit_payload=intent, memo_signature=memo_res["signature"],
                         memo_slot=memo_res["slot"])

    try:
        # Jupiter's /swap/v1/quote is a GET endpoint (query params). POST
        # returns 405 — the same bug the buy path had. The sell path builds
        # its own quote (token -> USDC) so it must use the GET helper too.
        quote = await _get_json(_BACKEND_QUOTE_URL, {
            "inputMint": mint,
            "outputMint": _BACKEND_USDC_MINT,
            "amount": str(amount_raw),
            "slippageBps": str(config.SLIPPAGE_BPS),
        })
    except ExecutionError as exc:
        logc.fail(sealed["hash"], f"quote failed: {exc}")
        return OrderResult(**{**base.to_json(), "status": "failed",
                              "reason": f"quote: {exc}", **commit_fields})
    except Exception as exc:
        logc.fail(sealed["hash"], f"quote crashed: {type(exc).__name__}: {exc}")
        return OrderResult(**{**base.to_json(), "status": "failed",
                              "reason": f"quote: {exc}", **commit_fields})
    out_raw = quote.get("outAmount")
    if out_raw is None:
        logc.fail(sealed["hash"], "no route quoted for this mint")
        return OrderResult(**{**base.to_json(), "status": "failed",
                              "reason": "no route quoted for this mint", **commit_fields})

    impact = quote_impact_pct(quote)
    if impact > config.MAX_PRICE_IMPACT_PCT:
        reason = f"price impact {impact:.2f}% above floor {config.MAX_PRICE_IMPACT_PCT}%"
        logc.fail(sealed["hash"], reason)
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": reason, **commit_fields})
    proceeds_usd = int(out_raw) / raw_units(USDC_DECIMALS)

    try:
        swap_b64 = await _build_swap_transaction(quote, addr)
        outcome = await _broadcast_and_confirm(swap_b64, payer, logc=logc, sealed=sealed)
    except Exception as exc:
        # Same fail-closed contract as the buy path (see comment there).
        logc.fail(sealed["hash"], f"{type(exc).__name__}: {exc}")
        return OrderResult(**{**base.to_json(), "status": "failed", "reason": str(exc),
                              "price_impact_pct": impact, **commit_fields})

    if outcome.status == "filled":
        try:
            ledger.reduce_position(mint, frac, proceeds_usd, full_close=full_close)
        except ValueError as exc:
            log.error("post-fill ledger reduce failed: %s", exc)
        outcome.usd_value = proceeds_usd
        outcome.tokens = sell_amount
        outcome.price_impact_pct = impact
        log.info("FILLED sell %s %.4f tokens -> $%.2f sig %s", mint[:8], sell_amount, proceeds_usd, outcome.signature)
    else:
        # Honest journal (same contract as the buy path).
        logc.fail(sealed["hash"], outcome.reason or "fill not confirmed")
    outcome.commit_hash = sealed["hash"]
    outcome.commit_nonce = sealed["nonce"]
    outcome.commit_payload = intent
    outcome.memo_signature = memo_res["signature"]
    outcome.memo_slot = memo_res["slot"]
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
    full_close: bool = False,
) -> OrderResult:
    """the reference OrderIntent parity: buys sized in USD, sells as a position fraction."""
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
                                confirmation_id=confirmation_id,
                                full_close=full_close)
    return OrderResult(status="blocked", reason=f"unknown side {side!r}", side=side, mint=mint)
