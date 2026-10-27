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

import base64
import logging
from dataclasses import asdict, dataclass, field
from typing import Optional

from solders.transaction import VersionedTransaction

import math
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
    default_ledger,
    default_queue,
    get_jupiter_quote,
    preflight,
    sign_after_checks,
)
from live_execution.models import ExecutionLedger
from live_execution.commit_log import CommitLog
from live_execution.quote_params import build_quote_params
from live_execution.venue import token_deltas_from_tx
from live_execution.trade_intent import TradeIntent, usd_to_raw_amount

log = logging.getLogger(__name__)


def is_retryable_exit_error(exc_or_msg: Exception | str) -> bool:
    """Classify whether an exit error is safe and appropriate to retry on the slippage ladder (§81 Phase A)."""
    from live_execution.exit_errors import classify_exit_failure
    return classify_exit_failure(exc_or_msg).retry



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


async def _fill_actuals(side: str, mint: str, signature: str,
                        owner: Optional[str] = None) -> dict:
    """A4 (repo audit): the ACTUAL amounts a confirmed fill moved.

    Fetches the confirmed tx (jsonParsed) and derives the signed net token
    change of the traded mint and of the USDC mint. Returns
    {"tokens_delta": float|None, "usdc_delta": float|None} where None means
    "unreadable" — the caller then falls back to the quote snapshot (the
    pre-A4 behavior) instead of fabricating a number. Fail-soft: this is
    observability-grade data and must never break a journalled fill.
    """
    out: dict = {"tokens_delta": None, "usdc_delta": None}
    if not signature:
        return out
    try:
        tx = await solana.get_transaction(signature)
        deltas = token_deltas_from_tx(tx, owner=owner)
    except Exception as exc:
        log.info("fill actuals: tx fetch/parse failed for %s: %s",
                 signature[:16], exc)
        return out
    if not deltas:
        return out
    tok = deltas.get(mint)
    usdc = deltas.get(_BACKEND_USDC_MINT)
    if side == "buy":
        # Buy: the wallet RECEIVES the token (positive delta) and PAYS USDC
        # (negative delta). The tx fee is in SOL, never USDC, so the USDC
        # delta is the pure swap input.
        if tok is not None and tok > 0:
            out["tokens_delta"] = tok
        if usdc is not None and usdc < 0:
            out["usdc_delta"] = -usdc
    else:
        # Sell: the wallet gives up the token (negative delta) and receives
        # USDC (positive delta).
        if tok is not None and tok < 0:
            out["tokens_delta"] = -tok
        if usdc is not None and usdc > 0:
            out["usdc_delta"] = usdc
    return out


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
    # §84: the seal's wall-clock time. Journaling must use THIS for
    # decision_commits.created_at — stamping "now" after the fill lands makes
    # every real fill look like it was committed after its own blockTime and
    # fails the independent time-ordering check.
    commit_sealed_at: Optional[float] = None
    memo_signature: str = ""
    memo_slot: Optional[int] = None
    error: Optional[object] = None

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("error", None)
        return d


def quote_impact_pct(quote: dict) -> float:
    """Jupiter returns priceImpactPct as a decimal fraction."""
    try:
        return abs(float(quote.get("priceImpactPct") or 0)) * 100.0
    except (TypeError, ValueError):
        return 0.0


async def _broadcast_and_confirm(
    swap_b64: str,
    payer,
    intent: TradeIntent,
    logc=None,
    sealed=None,
    quote: Optional[dict] = None,
) -> OrderResult:
    """Sign after simulation checks, broadcast via send_checked_swap, confirm. Journal is the CALLER job."""
    from live_execution.exit_errors import SendRejected

    signed_swap = await sign_after_checks(swap_b64, payer, intent=intent, quote=quote)
    try:
        sent = await solana.send_checked_swap(signed_swap)
    except SendRejected as exc:
        return OrderResult(
            status="failed",
            reason=f"send refused: {exc}",
            signature=signed_swap.signature,
            error=exc,
        )
    if not sent:
        # An ambiguous send (transport failure or "already processed") is not
        # a definitive rejection: the signed transaction may have landed.
        # Resolve the LOCAL signature before declaring failure — otherwise a
        # real fill stays unjournaled and a retried buy could double-spend.
        try:
            status = await solana.get_signature_status(signed_swap.signature)
        except Exception as exc:
            log.warning("signature status lookup failed for %s: %s",
                        signed_swap.signature[:16], exc)
            status = None
        if status is None:
            return OrderResult(
                status="failed",
                reason="send outcome unresolved (signature not found on-chain)",
                signature=signed_swap.signature,
            )
        if status.get("err"):
            return OrderResult(
                status="failed",
                reason=f"landed-failed (fee paid): {status['err']}",
                signature=signed_swap.signature,
                error=status.get("err"),
            )
        if status.get("confirmationStatus") in ("confirmed", "finalized"):
            conf = {"confirmed": True, "slot": status.get("slot"), "err": None}
        else:
            conf = await solana.confirm_signature(signed_swap.signature)
            if not conf["confirmed"]:
                return OrderResult(
                    status="failed",
                    reason=conf["err"] or "unconfirmed",
                    signature=signed_swap.signature,
                )
        sent = signed_swap.signature
    else:
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
    reference_price_usd: Optional[float] = None,
) -> OrderResult:
    """Automated reference-style BUY: every guard fails closed before any network call."""
    base = OrderResult(side="buy", symbol=symbol, mint=mint)
    ledger = ledger or default_ledger()
    queue = queue or default_queue()

    if not idempotency_key:
        # A7 (repo audit): the old time-based fallback key was NOT
        # idempotent — a retry after an unclear network failure generated a
        # fresh key, bypassed the replay check, and could double-send.
        # Mirror execute_confirmed_trade: refuse instead of fabricating.
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": "idempotency_key is required for live buys — "
                                        "retries after an unclear failure must never double-send"})
    try:
        preflight(usd, mint, output_decimals, ledger)
    except Refusal as exc:
        status = "unarmed" if not config.LIVE_TRADING_ENABLED else "blocked"
        return OrderResult(**{**base.to_json(), "status": status, "reason": str(exc)})
    except kill_switch.KillSwitchTripped as exc:
        return OrderResult(**{**base.to_json(), "status": "blocked", "reason": str(exc)})
    except RuntimeError as exc:
        # Corrupt kill-switch/ledger state is a refusal, not a crash.
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

    # §81 Phase C-6: Fail closed on missing/invalid reference price BEFORE publishing commit memo
    if reference_price_usd is None or reference_price_usd <= 0:
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": "no reference price available for buy (fail closed)"})

    # REF-R11 commit-reveal, fail-closed ordering:
    #   seal -> publish memo -> CONFIRM memo -> quote -> build -> broadcast.
    # The memo goes out BEFORE the quote so the quote->fill window stays as
    # tight as ever; a memo that cannot be confirmed blocks the fill entirely
    # (handoff §22 requirement 4).
    intent = dict(kind="buy", mint=mint, symbol=symbol, usd=usd)
    try:
        sealed = logc.seal("buy", intent)
    except Exception as exc:
        # A corrupt/unwritable commit log must refuse the order BEFORE any
        # memo, quote, signature, or broadcast — never crash the cycle and
        # never execute a fill without its auditable seal.
        log.error("commit log seal failed for buy %s: %s", mint[:8], exc,
                  exc_info=True)
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": f"commit log unavailable: {exc}"})
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
                         commit_payload=intent,
                         commit_sealed_at=sealed.get("sealed_at"),
                         memo_signature=memo_res["signature"],
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

    min_out_raw = math.floor(
        usd / (reference_price_usd * (1.0 + config.BUY_MAX_PRICE_DEVIATION))
        * (10 ** (output_decimals or 0))
    )
    trade_intent = TradeIntent(
        side="buy",
        wallet=addr,
        input_mint=_BACKEND_USDC_MINT,
        output_mint=mint,
        in_amount_raw=usd_to_raw_amount(usd, USDC_DECIMALS),
        slippage_bps=config.SLIPPAGE_BPS,
        min_out_raw=min_out_raw,
        reference_price_usd=reference_price_usd,
    )

    try:
        swap_b64 = await _build_swap_transaction(q["quote"], addr)
        outcome = await _broadcast_and_confirm(
            swap_b64, payer, intent=trade_intent, logc=logc, sealed=sealed, quote=q["quote"]
        )
    except Exception as exc:
        # ANY build/sign/broadcast error (ExecutionError or not) fails closed
        # and lands in the journal. The first real armed order died on an
        # AttributeError here and the commit stayed 'published' with no
        # explanation — that must never happen again.
        logc.fail(sealed["hash"], f"{type(exc).__name__}: {exc}")
        return OrderResult(**{**base.to_json(), "status": "failed", "reason": str(exc),
                              "price_impact_pct": impact, **commit_fields})

    if outcome.status == "filled":
        # A4 (repo audit): prefer the ACTUAL amounts read off the confirmed
        # tx — real slippage means the fill rarely equals the quote, and the
        # ledger is the money authority. An unreadable tx journals the quote
        # snapshot (and logs loudly), never a guess.
        actuals = await _fill_actuals("buy", mint, outcome.signature, owner=addr)
        usd_actual = actuals["usdc_delta"] or usd
        tokens_actual = actuals["tokens_delta"] or q["tokens_out"]
        if actuals["usdc_delta"] is None and actuals["tokens_delta"] is None:
            log.warning(
                "fill actuals unreadable for %s — journaling the quote "
                "snapshot (usd=%.2f)", outcome.signature[:16], usd)
        rec = ledger.record_buy(
            idempotency_key=idempotency_key,
            mint=mint, usd_size=usd_actual, tokens_out=tokens_actual,
            price_usd=(usd_actual / tokens_actual) if tokens_actual > 0
            else q["price_usd"],
            signature=outcome.signature, status="confirmed")
        outcome.usd_value = usd_actual
        outcome.tokens = tokens_actual
        outcome.price_impact_pct = impact
        log.info("FILLED buy %s $%.2f -> %.6f tokens sig %s",
                 mint[:8], usd_actual, tokens_actual, outcome.signature)
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
    rule_id: Optional[str] = None,
    reference_price_usd: Optional[float] = None,
) -> OrderResult:
    """reference-style SELL: fraction of the open position, decimals read from chain.
    UNKNOWN decimals refuse - never a default (the decimals lesson).
    rule_id (§50): the exit rule driving this sell, journaled on the close
    record for exit-mix forensics + take-profit tranche counting."""
    base = OrderResult(side="sell", symbol=symbol, mint=mint)
    if not config.LIVE_TRADING_ENABLED:
        return OrderResult(**{**base.to_json(), "status": "unarmed", "reason": "disarmed"})
    ledger = ledger or default_ledger()
    queue = queue or default_queue()
    try:
        kill_switch.assert_exit_allowed()
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
    try:
        sealed = logc.seal("sell", intent)
    except Exception as exc:
        log.error("commit log seal failed for sell %s: %s", mint[:8], exc,
                  exc_info=True)
        return OrderResult(**{**base.to_json(), "status": "blocked",
                              "reason": f"commit log unavailable: {exc}"})
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
                          commit_payload=intent,
                          commit_sealed_at=sealed.get("sealed_at"),
                          memo_signature=memo_res["signature"],
                          memo_slot=memo_res["slot"])

    ladder = getattr(config, "EXIT_SLIPPAGE_LADDER_BPS", (50, 150, 300))
    last_sig: Optional[str] = None
    last_blockhash: Optional[str] = None
    last_error_reason: str = ""
    rung_notes: list[str] = []

    for attempt_idx, slippage_rung in enumerate(ladder, start=1):
        if attempt_idx > 1 and last_sig:
            logc.note(sealed["hash"], f"attempt {attempt_idx}: resolving previous signature {last_sig}")
            sig_status = await solana.get_signature_status(last_sig)
            if sig_status and sig_status.get("confirmationStatus") in ("confirmed", "finalized"):
                sig_err = sig_status.get("err")
                if not sig_err:
                    logc.note(sealed["hash"], f"previous signature {last_sig} landed on-chain — stopping ladder")
                    logc.bind(sealed["hash"], last_sig)
                    actuals = await _fill_actuals("sell", mint, last_sig, owner=addr)
                    proceeds_actual = actuals["usdc_delta"] or (proceeds_usd if "proceeds_usd" in locals() else 0.0)
                    tokens_sold_actual = actuals["tokens_delta"] or sell_amount
                    try:
                        ledger.reduce_position(mint, frac, proceeds_actual, full_close=full_close, rule_id=rule_id)
                    except ValueError as exc:
                        log.error("post-fill ledger reduce failed: %s", exc)
                    outcome = OrderResult(
                        side="sell",
                        symbol=symbol,
                        mint=mint,
                        status="filled",
                        signature=last_sig,
                        usd_value=proceeds_actual,
                        tokens=tokens_sold_actual,
                        **commit_fields,
                    )
                    return outcome
                else:
                    # Landed on-chain but failed: conclusive failure, fee was paid, can never double-fill.
                    from live_execution.exit_errors import classify_exit_failure, custom_error_code
                    failure = classify_exit_failure(sig_err)
                    if failure.retry and attempt_idx <= len(ladder):
                        prev_rung = ladder[attempt_idx - 2]
                        code = custom_error_code(sig_err)
                        rung_notes.append(f"{prev_rung}bps=landed-failed({code or 'retryable'})")
                        log.warning(
                            "Previous exit signature %s landed with retryable error: %s. Escalating ladder.",
                            last_sig, sig_err,
                        )
                        last_sig = None
                    else:
                        fail_reason = f"landed-failed (fee paid): {sig_err}"
                        logc.fail(sealed["hash"], fail_reason)
                        return OrderResult(
                            **{
                                **base.to_json(),
                                "status": "failed",
                                "reason": fail_reason,
                                "signature": last_sig,
                                **commit_fields,
                            }
                        )
            else:
                # §84: an unparseable/empty blockhash means we cannot prove
                # the previous signed exit is dead. Treating that as "expired"
                # retried the ladder and risked a second fill. Unknown must
                # stay unresolved (fail closed).
                is_valid = (await solana.is_blockhash_valid(last_blockhash)
                            if last_blockhash else None)
                if is_valid is not False:
                    state = ("unknown" if not last_blockhash
                             else f"may still be valid ({last_blockhash})")
                    reason = (f"exit_unresolved: signature {last_sig} is "
                              f"pending and blockhash is {state}")
                    log.error("DOUBLE FILL SAFETY: %s", reason)
                    try:
                        from alerts import alert_nowait, AlertEvent
                        alert_nowait(AlertEvent.EXIT_UNRESOLVED, f"Exit unresolved for {mint[:8]}: {reason}", key=f"exit_unresolved:{mint}")
                    except Exception:
                        pass
                    logc.note(sealed["hash"], reason)
                    logc.fail(sealed["hash"], reason)
                    return OrderResult(**{**base.to_json(), "status": "failed", "reason": reason, "signature": last_sig, **commit_fields})

        logc.note(sealed["hash"], f"attempt {attempt_idx}/{len(ladder)} with slippage {slippage_rung} bps")
        try:
            params = build_quote_params(
                input_mint=mint,
                output_mint=_BACKEND_USDC_MINT,
                amount_raw=amount_raw,
                slippage_bps=slippage_rung,
                side="sell",
            )
            quote = await _get_json(_BACKEND_QUOTE_URL, params)
        except ExecutionError as exc:
            logc.fail(sealed["hash"], f"quote failed: {exc}")
            return OrderResult(**{**base.to_json(), "status": "failed", "reason": f"quote: {exc}", **commit_fields})
        except Exception as exc:
            logc.fail(sealed["hash"], f"quote crashed: {type(exc).__name__}: {exc}")
            return OrderResult(**{**base.to_json(), "status": "failed", "reason": f"quote: {exc}", **commit_fields})

        out_raw = quote.get("outAmount")
        if out_raw is None:
            logc.fail(sealed["hash"], "no route quoted for this mint")
            return OrderResult(**{**base.to_json(), "status": "failed", "reason": "no route quoted for this mint", **commit_fields})

        impact = quote_impact_pct(quote)
        if impact > config.MAX_PRICE_IMPACT_PCT:
            reason = f"price impact {impact:.2f}% above floor {config.MAX_PRICE_IMPACT_PCT}%"
            logc.fail(sealed["hash"], reason)
            return OrderResult(**{**base.to_json(), "status": "blocked", "reason": reason, **commit_fields})

        proceeds_usd = int(out_raw) / raw_units(USDC_DECIMALS)

        min_out_raw = None
        if reference_price_usd is not None and reference_price_usd > 0:
            min_out_raw = math.floor(
                sell_amount * reference_price_usd * (1.0 - config.SELL_MAX_PRICE_DEVIATION)
                * (10 ** USDC_DECIMALS)
            )
        else:
            log.warning("SELL_NO_REF_PRICE: sell %s has no reference price; relying on self-consistency check", mint[:8])
            try:
                from alerts import alert_nowait, AlertEvent
                alert_nowait(AlertEvent.SELL_NO_REF_PRICE, f"Sell {mint[:8]} has no reference price; relying on quote self-consistency", key=f"sell_no_ref_price:{mint}")
            except Exception:
                pass

        trade_intent = TradeIntent(
            side="sell",
            wallet=addr,
            input_mint=mint,
            output_mint=_BACKEND_USDC_MINT,
            in_amount_raw=amount_raw,
            slippage_bps=slippage_rung,
            min_out_raw=min_out_raw,
            reference_price_usd=reference_price_usd,
        )

        try:
            swap_b64 = await _build_swap_transaction(quote, addr)
        except Exception as exc:
            logc.fail(sealed["hash"], f"build failed: {exc}")
            return OrderResult(**{**base.to_json(), "status": "failed", "reason": str(exc), **commit_fields})

        try:
            raw_tx = base64.b64decode(swap_b64)
            parsed_tx = VersionedTransaction.from_bytes(raw_tx)
            last_blockhash = str(parsed_tx.message.recent_blockhash)
        except Exception:
            last_blockhash = ""

        try:
            outcome = await _broadcast_and_confirm(
                swap_b64, payer, intent=trade_intent, logc=logc, sealed=sealed, quote=quote
            )
            last_sig = outcome.signature
            from live_execution.exit_errors import SendRejected, is_already_processed
            if isinstance(outcome.error, SendRejected):
                attempts = outcome.error.attempts
                if (
                    attempts
                    and all(a.get("kind") == "rpc_error" for a in attempts)
                    and not any(is_already_processed(a) for a in attempts)
                ):
                    # Definitive preflight rejection: transaction never entered the network
                    last_sig = None
                    last_blockhash = None
        except Exception as exc:
            err_str = str(exc)
            if not is_retryable_exit_error(exc) or attempt_idx >= len(ladder):
                logc.fail(sealed["hash"], f"{type(exc).__name__}: {exc}")
                return OrderResult(**{**base.to_json(), "status": "failed", "reason": err_str, "price_impact_pct": impact, **commit_fields})
            log.warning("Exit attempt %d failed with retryable error: %s. Escalating ladder.", attempt_idx, err_str)
            last_error_reason = err_str
            from live_execution.exit_errors import custom_error_code
            code = custom_error_code(getattr(exc, "onchain_err", None) or exc)
            rung_notes.append(f"{slippage_rung}bps=slippage({code or 'retryable'})")
            continue

        if outcome.status == "filled":
            actuals = await _fill_actuals("sell", mint, outcome.signature, owner=addr)
            proceeds_actual = actuals["usdc_delta"] or proceeds_usd
            tokens_sold_actual = actuals["tokens_delta"] or sell_amount
            if actuals["usdc_delta"] is None and actuals["tokens_delta"] is None:
                log.warning(
                    "fill actuals unreadable for %s — journaling the quote "
                    "snapshot (proceeds=%.2f)",
                    outcome.signature[:16], proceeds_usd)
            try:
                ledger.reduce_position(mint, frac, proceeds_actual, full_close=full_close, rule_id=rule_id)
            except ValueError as exc:
                log.error("post-fill ledger reduce failed: %s", exc)
            outcome.usd_value = proceeds_actual
            outcome.tokens = tokens_sold_actual
            outcome.price_impact_pct = impact
            log.info("FILLED sell %s %.4f tokens -> $%.2f sig %s", mint[:8], tokens_sold_actual, proceeds_actual, outcome.signature)
            outcome.commit_hash = sealed["hash"]
            outcome.commit_nonce = sealed["nonce"]
            outcome.commit_payload = intent
            outcome.memo_signature = memo_res["signature"]
            outcome.memo_slot = memo_res["slot"]
            return outcome
        else:
            reason = outcome.reason or "fill not confirmed"
            last_error_reason = reason
            err_target = getattr(outcome, "error", None) or reason
            if not is_retryable_exit_error(err_target) or attempt_idx >= len(ladder):
                logc.fail(sealed["hash"], reason)
                outcome.commit_hash = sealed["hash"]
                outcome.commit_nonce = sealed["nonce"]
                outcome.commit_payload = intent
                outcome.memo_signature = memo_res["signature"]
                outcome.memo_slot = memo_res["slot"]
                return outcome
            log.warning("Exit attempt %d unconfirmed/slippage: %s. Escalating ladder.", attempt_idx, reason)
            from live_execution.exit_errors import custom_error_code
            code = custom_error_code(getattr(outcome, "error", None) or reason)
            rung_notes.append(f"{slippage_rung}bps=preflight-slippage({code or 'retryable'})")
            continue

    final_reason = f"ladder: {' -> '.join(rung_notes)}" if rung_notes else (last_error_reason or "exit ladder exhausted")
    logc.fail(sealed["hash"], final_reason)
    return OrderResult(
        **{
            **base.to_json(),
            "status": "failed",
            "reason": final_reason,
            "signature": last_sig,
            **commit_fields,
        }
    )


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
    rule_id: Optional[str] = None,
    reference_price_usd: Optional[float] = None,
) -> OrderResult:
    """the reference OrderIntent parity: buys sized in USD, sells as a position fraction."""
    if side == "buy":
        if not usd or usd <= 0:
            return OrderResult(status="blocked", reason="buy requires a positive usd size",
                               side="buy", symbol=symbol, mint=mint)
        return await place_buy(mint, symbol, float(usd), output_decimals,
                               confirmation_id=confirmation_id,
                               idempotency_key=idempotency_key,
                               reference_price_usd=reference_price_usd)
    if side == "sell":
        return await place_sell(mint, symbol,
                                1.0 if fraction is None else float(fraction),
                                confirmation_id=confirmation_id,
                                full_close=full_close,
                                rule_id=rule_id,
                                reference_price_usd=reference_price_usd)
    return OrderResult(status="blocked", reason=f"unknown side {side!r}", side=side, mint=mint)
