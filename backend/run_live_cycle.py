"""
run_live_cycle.py - ONE autonomous reference-style decision cycle (backend bridge).

Option-A arming: after a human edits live_execution/config.py to flip
LIVE_TRADING_ENABLED (and, for fully autonomous flow,
REQUIRE_MANUAL_CONFIRMATION=False), this runner drives the full pipeline:

    manage -> read -> think -> gate -> execute

The runner lives INSIDE backend/ (single deployable module) and imports the
paper side READ-ONLY (providers, thinker, pure rule functions are the shared
brain), routing entries/exits through live_execution.executor.place_order
into the REAL book. The isolation contract is preserved in its enforced
form: the PAPER pipeline (main.py, paper_trading_engine.py,
decision_pipeline.py, rule_engine/, data_providers/, llm/) never imports
live_execution — pinned by backend/tests/test_decision_pipeline.py — and
paper trading can never touch real funds. The paper tick loop keeps running
its own simulated book independently - two books, one brain.

Usage (from backend/, or anywhere — the path below is self-bootstrapping):
    .venv/bin/python backend/run_live_cycle.py --once   # one cycle, then exit
    .venv/bin/python backend/run_live_cycle.py          # loop at TICK_INTERVAL_SECONDS

DISARMED by default: while LIVE_TRADING_ENABLED is False every order comes
back "unarmed" and only reads happen. Devnet first. Always.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# This script's own directory IS the backend root (single deployable module).
# Defensive insert so the runner works from any working directory.
BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import config as paper_config                     # noqa: E402
from data_providers import build_provider         # noqa: E402
from data_providers.jupiter import JupiterProvider  # noqa: E402
from llm.thinker import Thinker, template_think     # noqa: E402
from models import Candidate, FeedEvent, PortfolioState, RuleResult, Trade  # noqa: E402
from rule_engine.exits import ExitInput, evaluate_exits, sell_risk_gate  # noqa: E402
from rule_engine.regime import MarketRegime, compute_market_regime  # noqa: E402
from rule_engine.rules import ACTIVE_RULES, cash_available  # noqa: E402
from api import db                                # noqa: E402
from calibration import compute_calibration       # noqa: E402
from paper_trading_engine import (                # noqa: E402
    compute_risk_budget,
    compute_ticket,
    portfolio_equity_and_unrealized,
)

from live_execution import config as live_config  # noqa: E402
from live_execution import solana, wallet         # noqa: E402
from live_execution.commit_log import CommitLog   # noqa: E402
from live_execution.executor import place_order   # noqa: E402
from live_execution.models import ExecutionLedger  # noqa: E402

log = logging.getLogger("run_live_cycle")


# --- LIVE gate rules (micro-bootstrap parity) --------------------------------
# The paper `cash_available` rule checks cash against INTENDED_POSITION_SIZE_USD
# ($100 — sized for the $1,000 paper book). The live book starts from a few
# USDC (REF-R11 micro-bootstrap), so the paper threshold would refuse every
# live entry before sizing even runs. Swap in a live cash rule that checks
# the §45 EQUITY-PROPORTIONAL live floor; every other rule stays verbatim.
# Paper ACTIVE_RULES + INTENDED_POSITION_SIZE_USD are untouched
# (calibration-frozen) — the same "paper frozen, live threads its own floor"
# pattern as compute_ticket(min_ticket_usd=...).
def _live_cash_available(c: Candidate, p: PortfolioState,
                         r: MarketRegime) -> RuleResult:
    # At-cost equity: cash + open position cost. Never price-dependent, never
    # raises — the same definition the sizing path uses, so the gate and the
    # sizing refusal can never disagree about the threshold (the §45 root
    # cause was exactly that disagreement: gate passed at $3.31 cash, then
    # sizing refused the $0.496 ticket against a fixed $0.50 floor).
    equity = float(p.cash_usd) + sum(
        float(t.position_size_usd) for t in p.open_positions)
    floor = live_config.min_live_ticket_usd(equity)
    ok = p.cash_usd >= floor
    return RuleResult(
        "cash_available", ok,
        f"cash ${p.cash_usd:,.2f} vs live floor ${floor:,.2f} "
        f"(10% of at-cost equity ${equity:,.2f})",
        value=p.cash_usd,
    )


LIVE_ACTIVE_RULES = [
    _live_cash_available if rule is cash_available else rule
    for rule in ACTIVE_RULES
]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


async def _journal_live_commit(conn, symbol: str, mint_address: str,
                               verdict: str, entry_allowed: bool,
                               result) -> None:
    """REF-R11: journal a filled live order's seal + on-chain memo into the
    public decision record (decision_commits).

    The nonce/payload/hash are taken VERBATIM from the executor's seal
    (carried on the OrderResult), so /api/verify.json recomputes exactly
    what the live CommitLog sealed and matches it against the memo on chain.
    Never raises: journaling failure must not take down the cycle — the fill
    itself is already journalled in the execution ledger."""
    if not getattr(result, "commit_hash", ""):
        return
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        await db.insert_decision_commit(
            conn,
            created_at=now_iso,
            tick_ts=now_iso,
            symbol=symbol,
            mint_address=mint_address,
            verdict=verdict,
            entry_allowed=entry_allowed,
            nonce=result.commit_nonce,
            payload_json=json.dumps(result.commit_payload, sort_keys=True),
            payload_hash=result.commit_hash,
        )
        commit_id = await db.get_commit_id_by_hash(conn, result.commit_hash)
        if commit_id is None:
            log.warning("live commit %s not found after insert",
                        result.commit_hash[:16])
            return
        if result.memo_signature:
            await db.bind_commit_memo(conn, commit_id, result.memo_signature,
                                      result.memo_slot)
        if result.signature:
            await db.bind_commit_signature(conn, commit_id, result.signature,
                                           "filled", "exact")
            # A3: attribute the executing venue off the confirmed fill tx.
            # Fail-soft — an unknown venue stays null, never blocks journaling.
            try:
                from live_execution.venue import fetch_fill_venue
                venue = await fetch_fill_venue(result.signature)
                if venue.get("label"):
                    await db.bind_commit_venue(conn, commit_id, venue["label"])
            except Exception:
                log.info("venue attribution skipped for %s", result.signature[:16],
                         exc_info=True)
    except Exception:
        log.warning("live commit journaling failed (fill unaffected)",
                    exc_info=True)


async def _reconcile_with_chain(meta: dict) -> dict:
    """A2 (omo audit §28): cross-check the journal against chain truth.

    The chain is the sole authority on HOW MANY tokens the wallet holds; the
    journal stays the sole authority for cost basis. Never mutates the ledger
    — disagreements are flagged on the meta entries (chain_excluded /
    chain_tokens), logged loudly, and reported in the cycle outcome.
    Fail-soft: no wallet configured or RPC unreadable -> unchecked report
    (blocking exits on an RPC outage would bleed the book)."""
    from live_execution.reconcile import reconcile
    try:
        payer = wallet.load_keypair()
    except Exception:
        return {"checked": False, "discrepancies": [],
                "reason": "wallet not configured"}
    balances = await solana.get_token_balances(wallet.pubkey_string(payer))
    if balances is None:
        return {"checked": False, "discrepancies": [],
                "reason": "token balances unreadable"}
    return reconcile(meta, balances,
                     exclude_mints=frozenset({paper_config.USDC_MINT}))


async def _crosscheck_basis(meta: dict) -> list:
    """A4 (omo audit §28): read FOMO's own accounting for the open positions
    back from the thesis feed and cross-check the journal's cost basis.

    OBSERVABILITY ONLY — the journal remains the sole money authority; a
    disagreement is logged and reported, never applied. Disabled unless
    FOMO_OWN_HANDLE is set. Fail-soft: feed unreachable -> empty list."""
    if not getattr(paper_config, "FOMO_OWN_HANDLE", ""):
        return []
    picks = [{"mint": mint, "symbol": mint[:6]}
             for mint, m in meta.items() if not m.get("chain_excluded")]
    if not picks:
        return []
    try:
        from data_providers.crowd import read_own_basis
        rows = await read_own_basis(picks)
    except Exception as exc:
        log.info("basis cross-check skipped: %s", exc)
        return []
    checks = []
    for row in rows:
        m = meta.get(row["mint"]) or {}
        journal_cost = float(m.get("cost") or 0.0)
        delta = row["invested_usd"] - journal_cost
        match = abs(delta) <= max(0.05 * journal_cost, 0.50)
        if not match:
            log.warning("BASIS %s: fomo invested $%.2f vs journal cost "
                        "$%.2f (delta $%.2f) — journal NOT modified, "
                        "operator review", row["mint"][:8],
                        row["invested_usd"], journal_cost, delta)
        checks.append({**row, "journal_cost_usd": journal_cost,
                       "delta_usd": delta, "match": match})
    return checks


async def _live_portfolio(ledger: ExecutionLedger) -> tuple[PortfolioState, dict, dict]:
    """PortfolioState for the gate built from the LIVE ledger (read-only).

    REF-R11 micro-bootstrap: cash is the REAL on-chain USDC balance, not a
    cap-derived phantom — the book starts from $3-5 USDC and must compound
    from the truth. Fail closed: an unreadable balance (or missing wallet
    config) means cash 0.0 — no entries this cycle; exits still run.

    A2: the journal's open positions are cross-checked against on-chain
    token balances; positions the chain says are gone are excluded from the
    book this cycle (see live_execution/reconcile.py).

    Returns (portfolio, {mint: {price_usd, tokens, opened_ts, ...}} metadata,
    chain reconciliation report).
    """
    meta: dict = {}
    for r in ledger._load():   # same package - internal read is acceptable
        if r.get("kind") != "buy" or r.get("status") not in ledger._OPEN:
            continue
        mint = r["mint"]
        price = float(r.get("price_usd") or 0)
        tokens = float(r.get("tokens_out") or 0)
        if mint in meta:
            m = meta[mint]
            m["tokens"] += tokens
            m["cost"] += float(r.get("usd_size") or 0)
            continue
        meta[mint] = {
            "price_usd": price,
            "tokens": tokens,
            "cost": float(r.get("usd_size") or 0),
            "opened_ts": float(r.get("ts") or 0),
        }
    chain_report = await _reconcile_with_chain(meta)
    positions: list[Trade] = []
    for mint, m in meta.items():
        if m.get("chain_excluded"):
            continue   # chain says the tokens are gone — not a position today
        qty = m["tokens"] or 1.0
        positions.append(Trade(
            trade_id=f"live-{mint[:8]}",
            symbol=mint[:6],
            mint_address=mint,
            opened_at=_iso(m["opened_ts"]), 
            entry_price_usd=m["price_usd"],
            position_size_usd=m["cost"],
            quantity=qty,
            candidate_snapshot={}, 
            thesis="live book", 
            is_open=True,
        ))
    cash = 0.0
    try:
        payer = wallet.load_keypair()
        usdc_bal = await solana.get_usdc_balance(wallet.pubkey_string(payer))
        if usdc_bal is not None:
            cash = max(usdc_bal, 0.0)
        else:
            log.warning("live USDC balance unreadable - cash=0 "
                        "(no entries this cycle)")
    except Exception as exc:
        log.warning("live USDC balance unavailable (%s) - cash=0 "
                    "(no entries this cycle)", exc)
    return PortfolioState(cash_usd=cash, open_positions=positions), meta, chain_report


async def _manage(jupiter: JupiterProvider, ledger: ExecutionLedger, hwm: dict, meta: dict) -> None:
    """Re-price every open position, run the the reference exit rule set, route sells."""
    for mint, m in meta.items():
        if m.get("chain_excluded"):
            continue   # A2: chain says the tokens are gone — nothing to exit
        try:
            dec = await solana.get_mint_decimals(mint)
            if dec is None:
                log.warning("manage %s: decimals unknown - skipped this pass", mint[:8])
                continue
            price = await jupiter.get_current_price(mint, decimals=dec)
        except Exception as exc:
            log.warning("manage %s: pricing failed (%s)", mint[:8], exc)
            continue
        if price <= 0:
            continue
        # §32 parity with the paper scanner (handoff §32): a single-cycle
        # price this far ABOVE the established peak is a bad quote, not a
        # market move. Skip this position this cycle and do NOT ratchet
        # high-water or mark the risk budget on suspect data — a poisoned
        # peak can force a premature trail exit. Upward-only on purpose: a
        # genuine collapse must still exit. A live sell can never fabricate
        # money (it is a real swap), but an early exit on a phantom spike is
        # still real harm, so the guard matches the paper side exactly.
        peak = hwm.get(mint, m["price_usd"])
        if peak and peak > 0 and price > peak * paper_config.EXIT_PRICE_JUMP_MAX:
            log.warning("manage %s: price $%.8f is %.0fx the established peak "
                        "$%.8f — treating as a bad quote, skipping this cycle "
                        "(high-water NOT updated)", mint[:8], price,
                        price / peak, peak)
            continue
        prev = hwm.get(mint, m["price_usd"])
        hwm[mint] = max(prev, price)
        m["last_price_usd"] = price   # REF-R8: freshest mark for the risk budget
        trade = Trade(
            trade_id=f"live-{mint[:8]}", symbol=mint[:6], mint_address=mint,
            opened_at=_iso(m["opened_ts"]), entry_price_usd=m["price_usd"],
            position_size_usd=m["cost"], quantity=m["tokens"] or 1.0,
            candidate_snapshot={}, thesis="live book", is_open=True,
            high_water_usd=hwm[mint],
        )
        # §50: feed the engine the tranche counter from the ledger itself —
        # before, tranches_taken was silently 0 on every cycle, so a TP rung
        # would have re-trimmed 33% every 60s until the position was gone.
        tranches = ledger.tranches_taken(mint)
        decision = evaluate_exits(ExitInput(
            trade=trade, price_usd=price, high_water_usd=hwm[mint],
            tranches_taken=tranches,
        ))
        if decision.action == "hold":
            continue
        fraction = decision.fraction
        chain_tokens = m.get("chain_tokens")
        if chain_tokens is not None and m["tokens"] > 0:
            # A2: never sell more than the chain says we hold — clamp the
            # fraction so the sell amount stays within the on-chain balance.
            fraction = min(fraction, chain_tokens / m["tokens"])
            log.info("manage %s: sell fraction clamped to chain balance "
                     "(%.6f/%.6f)", mint[:8], chain_tokens, m["tokens"])
        # §50: the reference sell gate, live-side. Paper's $25 min clip would
        # refuse EVERY trim on this book (33% of a $0.50 ticket is $0.17), so
        # the floor is the §45 equity-proportional live ticket — same formula
        # that gates entries, hardcoded, never env. RISK-OFF rules (stop,
        # liquidity break) bypass the gate inside sell_risk_gate itself, so an
        # emergency exit is never delayed by a cooldown or clip check.
        est_value = (m["tokens"] or 0.0) * price * fraction
        cash_eq = m["cost"]
        min_clip = live_config.min_live_ticket_usd(
            cash_eq + (m["tokens"] or 0.0) * price)
        last_close = ledger.last_close_ts(mint)
        last_close_dt = (
            datetime.fromtimestamp(last_close, tz=timezone.utc)
            if last_close is not None else None
        )
        closes_24h = ledger.closes_since(time.time() - 24 * 3600.0)
        gated, gate_note = sell_risk_gate(
            decision, est_value, last_close_dt, closes_24h,
            datetime.now(timezone.utc), min_clip_usd=min_clip,
        )
        if gated.action == "hold":
            log.info("EXIT GATE held %s [%s]: %s (%s)",
                     mint[:8], decision.rule_id, gate_note, decision.detail)
            continue
        decision = gated
        log.info("EXIT %s %s (%s)", decision.action, mint[:8], decision.detail)
        result = await place_order(
            side="sell", mint=mint, symbol=mint[:6],
            fraction=fraction,
            full_close=(decision.action == "close_full"),
            rule_id=decision.rule_id,
        )
        log.info("sell -> %s %s", result.status, result.reason)
        if result.status == "filled":
            # REF-R11: journal the sell's seal + memo into the public record.
            async with db.get_db() as conn:
                await _journal_live_commit(
                    conn, symbol=mint[:6], mint_address=mint,
                    verdict="sell", entry_allowed=False, result=result)
        if result.status == "filled" and decision.action == "close_full":
            hwm.pop(mint, None)
            async with db.get_db() as conn:
                pnl = result.usd_value - m["cost"]
                await db.retire_thesis(
                    conn,
                    trade_id=f"live-{mint[:8]}",
                    closed_at=datetime.now(timezone.utc).isoformat(),
                    realized_pnl_usd=pnl,
                )
            # §49 (closes the live-side anti-churn GAP): the real-money book
            # now records every full-close outcome into the SAME blocklist
            # sidecar the paper book writes, and the DONT-pattern killer
            # fires identically on both books. Before §49 a coin that
            # stopped out LIVE could be re-bought the very next tick —
            # the live book had NO loss memory at all.
            try:
                from blocklist import (maybe_autoblock as _maybe,
                                       record_close_outcome as _record)
                _record(mint, m.get("symbol") or mint[:6],
                        decision.rule_id, pnl, book="live")
                _maybe(mint, m.get("symbol") or mint[:6])
            except Exception:
                log.warning("§49 close-outcome recording failed for %s "
                            "(non-fatal)", mint[:8], exc_info=True)
            # §49 soft memory on the live book too (reference layer 5): the
            # thinker's next prompt carries the loss lesson for this symbol.
            if pnl < 0.0:
                try:
                    async with db.get_db() as conn:
                        await db.upsert_memory(
                            conn, topic=m.get("symbol") or mint[:6],
                            note=(f"live book closed at a ${abs(pnl):.2f} "
                                  f"loss ({decision.rule_id}) on "
                                  f"{datetime.now(timezone.utc).date().isoformat()}"
                                  f" — we already paid for this lesson"),
                            weight=2.0,
                        )
                        await db.insert_event(
                            conn, "trade",
                            datetime.now(timezone.utc).isoformat(),
                            symbol=m.get("symbol") or mint[:6],
                            mint_address=mint,
                            payload={"outcome": "loss_close",
                                     "rule": decision.rule_id,
                                     "pnl_usd": pnl, "book": "live"},
                        )
                except Exception:
                    log.warning("§49 loss-memory journaling failed for %s "
                                "(non-fatal)", mint[:8], exc_info=True)


async def _journal_cycle_regime(conn, regime, candidate_count: int) -> None:
    """Persist this cycle's market-regime snapshot, shaped exactly like the
    paper tick's, so /api/market-regime + the regime panel show live data."""
    await db.insert_market_regime(
        conn,
        computed_at=regime.computed_at,
        candidate_count=candidate_count,
        pct_green=regime.pct_candidates_green_1h,
        median_vol=regime.median_volume_1h_usd,
        avg_ratio=regime.avg_buy_sell_ratio,
        regime_ok=regime.regime_ok,
        detail=regime.regime_detail,
    )
    await db.insert_event(
        conn, "read", datetime.now(timezone.utc).isoformat(),
        payload={"candidate_count": candidate_count,
                 "regime_ok": regime.regime_ok, "book": "live"},
    )


async def _journal_feed_event(conn, c, think, gate, regime,
                              entry_allowed: bool) -> None:
    """Persist the live cycle's per-candidate decision as a feed_events row,
    shaped exactly like the paper tick's. The dashboard decision feed and the
    WebSocket broadcaster are both DB-driven (they poll feed_events), so this
    single write is what makes live decisions appear on the dashboard.
    Observability only — callers wrap this fail-soft so it can never block
    the trade path."""
    full_thesis = think.thesis + (
        f" | invalidates if: {think.invalidation}" if think.invalidation else ""
    )
    event = FeedEvent(
        symbol=c.symbol,
        mint_address=c.mint_address,
        candidate_snapshot=c.to_dict(),
        verdict="pass" if entry_allowed else "fail",
        thesis=full_thesis,
        rule_breakdown=[
            {"rule_id": r.rule_id, "passed": r.passed,
             "detail": r.detail, "value": r.value,
             "evaluated": r.evaluated}
            for r in gate.rules
        ],
        failed_rule_ids=gate.failed_rule_ids,
        regime_ok=regime.regime_ok,
        grounding_flags=think.grounding_flags,
        narration_source=getattr(think, "source", "unknown"),
    )
    if getattr(think, "llm_usage", None):
        event.model_version = think.llm_usage.model
        event.prompt_version = think.llm_usage.pricing_snapshot_id
    event.id = await db.insert_feed_event(conn, event)
    await db.insert_event(
        conn, "did" if entry_allowed else "refused",
        datetime.now(timezone.utc).isoformat(),
        c.symbol, c.mint_address,
        {"entry_allowed": entry_allowed,
         "failed_rule_ids": list(gate.failed_rule_ids),
         # §43: skipped rules are recorded separately from real rejections.
         "not_evaluated_rule_ids": list(gate.not_evaluated_rule_ids),
         "model_verdict": think.verdict, "book": "live"},
    )


async def run_cycle(once: bool = False) -> dict:
    """One full cycle. Returns a step-by-step outcome record, refusals included."""
    ledger = ExecutionLedger(live_config.STATE_DIR / "executions.json")
    # Item 3: heal any commit whose memo went on-chain but whose fill never
    # followed (historical orphans predate the post-memo fail() wiring). Cheap
    # + idempotent; only touches rows old enough that a fill is long resolved.
    try:
        healed = CommitLog(live_config.STATE_DIR / "commits.json").reconcile_orphaned()
        if healed:
            log.info("reconciled %d orphaned published commit(s) -> failed/no-fill", healed)
    except Exception:
        log.warning("commit orphan reconciliation skipped", exc_info=True)
    portfolio, meta, chain_report = await _live_portfolio(ledger)
    hwm: dict = getattr(run_cycle, "_hwm", {})
    jupiter = JupiterProvider()

    await _manage(jupiter, ledger, hwm, meta)
    run_cycle._hwm = hwm

    # A4: FOMO's own accounting for the open positions, cross-checked against
    # the journal's cost basis (observability only; needs FOMO_OWN_HANDLE).
    basis_checks = await _crosscheck_basis(meta)

    # A11: advance stale open write-ups against the positions' current
    # numbers. Narrative only (thesis text), reuses this cycle's own marks
    # from _manage (no extra network I/O), never raises into the cycle.
    restatements: list = []
    try:
        from thesis_restate import restate_theses
        marks = {mint: m["last_price_usd"] for mint, m in meta.items()
                 if m.get("last_price_usd")}
        async with db.get_db() as conn:
            restatements = await restate_theses(
                conn, portfolio.open_positions, marks)
        if restatements:
            log.info("thesis restatement: %d live write-up(s) advanced",
                     len(restatements))
    except Exception:
        log.warning("thesis restatement pass failed (non-fatal)",
                    exc_info=True)

    # --- READ stage (shared Item #6 core, same code as the paper tick):
    # fetch + blocklist + FAKE-CHART filter (A7 parity — the live cycle
    # previously skipped it) + live-only enrichment. Fail-soft per feed.
    from decision_pipeline import enrich_candidates, read_candidates
    candidates = await read_candidates(build_provider())
    await enrich_candidates(candidates)

    regime = compute_market_regime(candidates)
    # Persist the regime snapshot + a per-candidate decision feed so the
    # dashboard (regime panel + decision feed + WebSocket) shows live data.
    # Fail-soft: observability never blocks the trade path.
    try:
        async with db.get_db() as conn:
            await _journal_cycle_regime(conn, regime, len(candidates))
    except Exception:
        log.warning("regime journal failed (non-fatal)", exc_info=True)
    thinker = Thinker()
    outcome = {"entries": [], "exits": [], "regime_ok": regime.regime_ok,
               "candidates": len(candidates),
               # A2/A4 observability: how the journal compared to chain truth
               # and to FOMO's own accounting this cycle.
               "chain_reconciliation": chain_report,
               "basis_crosscheck": basis_checks,
               # A11: which open write-ups this cycle advanced (narrative only).
               "thesis_restatements": restatements}
    for c in candidates:
        # --- GATE FIRST (§44), STAGED: every cheap rule → (only if they ALL
        # passed) the fomo.fun scrape → the crowd rule(s). A candidate that
        # fails a cheap rule is NEVER scraped; its crowd_heat reads
        # "not evaluated" (fail-closed). Same rule list, same breakdown order.
        from decision_pipeline import gate_candidate_staged
        gate = await gate_candidate_staged(c, portfolio, regime,
                                           LIVE_ACTIVE_RULES)

        # THINK via the shared core (Item #6): template fallback on thinker
        # error instead of a killed cycle, and the break handler with the
        # correct set_break arity (the live copy previously mis-called it).
        # §44: the LLM call is spent only on candidates the rules cleared; a
        # rule-refused candidate gets the deterministic template write-up
        # (verdict forced to "pass", source tagged) so its journal row is
        # still populated without paying for a call that cannot change the
        # outcome. The thinker now also sees the REAL crowd theses, because
        # the scrape above already ran.
        from decision_pipeline import apply_break, think_candidate
        if gate.all_passed:
            think = await think_candidate(c, thinker)
        else:
            think = template_think(c)
            think.verdict = "pass"
            think.source = "template:rules-refused"
        await apply_break(think)

        entry_allowed = gate.all_passed and think.wants_entry
        failed = [r.rule_id for r in gate.rules if not r.passed and r.evaluated]
        # §43: a deliberately un-evaluated rule is reported as skipped, never
        # as a failure (it still blocks entry — evaluate_gate fails closed).
        skipped = list(gate.not_evaluated_rule_ids)
        verdict_txt = "PASS" if gate.all_passed else (
            "FAIL:" + ",".join(failed) if failed
            else "SKIP:" + ",".join(skipped))
        log.info("%s think=%s gate=%s%s", c.symbol, think.verdict,
                 verdict_txt,
                 "" if entry_allowed else " -> refused")
        # Persist the decision to the public feed (dashboard + WebSocket).
        # Fail-soft: a feed write must never block or alter the trade path.
        try:
            async with db.get_db() as conn:
                await _journal_feed_event(conn, c, think, gate, regime,
                                          entry_allowed)
        except Exception:
            log.warning("feed journal failed for %s (non-fatal)",
                        c.symbol, exc_info=True)
        if not entry_allowed:
            continue
        cash = portfolio.cash_usd
        # REF-R8/R9: risk-budget sizing when enabled; otherwise the legacy
        # cash-fraction ticket (unchanged). Equity/unrealized come from the
        # live ledger marked with this cycle freshest prices (at cost when
        # unpriced - never fabricated). Still DISARMED: place_order refuses
        # every order while LIVE_TRADING_ENABLED is False.
        price_map = {mint: m["last_price_usd"] for mint, m in meta.items()
                     if m.get("last_price_usd")}
        eq, unrl = portfolio_equity_and_unrealized(portfolio, price_map)
        # §45: ONE floor formula for gate + sizing + risk_budget threading.
        # Computed from the same at-cost equity the cash rule gates on, so a
        # gate PASS can no longer be followed by a sizing refusal on a
        # threshold disagreement (the "ENTER but no execution" incident).
        live_floor = live_config.min_live_ticket_usd(eq)
        if paper_config.SIZING_MODE == "risk_budget":
            try:
                async with db.get_db() as conn:
                    cf = compute_calibration(
                        await db.get_all_closed_trades(conn)).conviction_factor
            except Exception:
                log.warning("live calibration unreadable - failing closed "
                            "to 1.0", exc_info=True)
                cf = 1.0
            usd = compute_ticket(cash, None, equity_usd=eq,
                                 unrealized_usd=unrl, conviction_factor=cf,
                                 min_ticket_usd=live_floor)
            budget = compute_risk_budget(
                eq, unrl, min_ticket_usd=live_floor)
            deployed_today = ledger.deployed_today_usd()
            if deployed_today + usd > budget.max_daily_usd:
                log.info("%s refused: daily risk budget reached "
                         "($%.0f deployed today, $%.0f ceiling)",
                         c.symbol, deployed_today, budget.max_daily_usd)
                outcome["entries"].append(
                    {"symbol": c.symbol, "status": "refused",
                     "reason": "daily risk budget reached"})
                continue
        else:
            usd = min(cash * paper_config.TICKET_CASH_FRACTION,
                      paper_config.TICKET_MAX_USD)
        if usd < live_floor:
            log.info("%s refused: ticket $%.4f below live floor $%.4f "
                     "(equity $%.2f)", c.symbol, usd, live_floor, eq)
            # §45 visibility fix: a skipped trade must be as visible as an
            # executed one — this refusal previously vanished from the
            # cycle outcome even though the daily-budget refusal records.
            outcome["entries"].append(
                {"symbol": c.symbol, "status": "refused",
                 "reason": f"ticket ${usd:.2f} below live floor "
                           f"${live_floor:.2f}"})
            continue
        result = await place_order(
            side="buy", mint=c.mint_address, symbol=c.symbol, usd=usd,
            output_decimals=c.decimals,
            idempotency_key=f"{c.mint_address}-{datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")}",
        )
        log.info("buy %s $%.2f -> %s %s", c.symbol, usd, result.status, result.reason)
        if result.status == "filled":
            async with db.get_db() as conn:
                await db.upsert_thesis(
                    conn,
                    trade_id=f"live-{c.mint_address[:8]}",
                    mint_address=c.mint_address,
                    symbol=c.symbol,
                    author=f"model:{think.source}",
                    thesis=think.thesis + (f" | invalidates if: {think.invalidation}" if think.invalidation else ""),
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                # REF-R11: journal the EXACT seal + on-chain memo into the
                # public decision record so /api/verify.json can re-verify
                # the commitment from chain data alone.
                await _journal_live_commit(
                    conn, symbol=c.symbol, mint_address=c.mint_address,
                    verdict="buy", entry_allowed=True, result=result)
        outcome["entries"].append({"symbol": c.symbol, "status": result.status,
                                   "reason": result.reason})
        break   # one decision per cycle (the reference cadence parity)

    # --- REF-R7: retro audit-log signature matching (post-cycle) ----------
    # Only runs from the paper-side DB; the live book has its own CommitLog.
    try:
        from retro_matcher import run_retro_match
        async with db.get_db() as retro_conn:
            await run_retro_match(retro_conn)
    except Exception:
        log.debug("retro_match post-cycle failed (non-fatal)", exc_info=True)

    return outcome



def main() -> None:
    parser = argparse.ArgumentParser(description="reference-style live decision cycle")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--drill", action="store_true", help="DEVNET drill: exercise sign/send/confirm without Jupiter or tokens")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    if args.drill:
        from live_execution.drill import run_drill
        steps = asyncio.run(run_drill())
        failed = [s for s in steps if not s["ok"]]
        sys.exit(1 if failed else 0)

    if paper_config.DATA_BACKEND != "live":
        log.error("DATA_BACKEND=%s - the live cycle runs on the live stack only.", paper_config.DATA_BACKEND)
        log.error("Set DATA_BACKEND=live in .env, then re-run.")
        sys.exit(2)

    # the reference disclosure parity: report the signing state unedited.
    if live_config.LIVE_TRADING_ENABLED:
        log.info("ARMED (manual confirmation %s)", "ON" if live_config.REQUIRE_MANUAL_CONFIRMATION else "OFF - fully autonomous")
    else:
        log.warning("UNARMED: LIVE_TRADING_ENABLED=False - reads/think/gate run; every order returns unarmed")

    async def loop() -> None:
        while True:
            try:
                await run_cycle(once=args.once)
            except Exception:
                log.exception("cycle crashed - continuing")
            if args.once:
                return
            await asyncio.sleep(paper_config.TICK_INTERVAL_SECONDS)

    asyncio.run(loop())


if __name__ == "__main__":
    main()