"""
live_execution/reconcile.py — A2 chain-vs-journal reconciliation (omo audit §28).

reference: wallet.server.ts readViaRpc/buildHoldings — the reference re-derives
its whole book from on-chain token accounts on every sync, because the chain is
the one source that cannot drift.

Our architecture keeps the ExecutionLedger journal as the sole authority for
COST BASIS (money math, §5.1 atomicity), so this module does not re-derive the
book wholesale — it cross-checks the journal's open positions against chain
truth every cycle and acts on disagreement:

  * chain == journal (within tolerance)  -> nothing to do.
  * chain <  journal                    -> exit sizing is clamped to the chain
                                           amount: the bot can never sell
                                           tokens it does not hold.
  * chain == 0                          -> the position vanished (out-of-band
                                           sell/transfer): it is EXCLUDED from
                                           the book this cycle and flagged for
                                           operator review. The journal row is
                                           NEVER silently mutated — a wrong RPC
                                           read must not be able to corrupt the
                                           money ledger.
  * chain >  journal                    -> keep the journal amount
                                           (conservative: never sell more than
                                           journaled), log the surplus.
  * on-chain mint NOT in the journal    -> unjournaled holding: flagged, never
                                           added (no cost basis — fabricating
                                           one is the decimals-lesson sin).

Every disagreement is logged LOUDLY and reported, so a drifted book is visible
instead of silent. balances=None means "RPC unreadable" and is NOT the same as
"no tokens": reconciliation then skips (fail-soft — blocking exits on an RPC
outage would bleed the book) and says so in the report.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# Relative tolerance for the chain-vs-journal comparison (float dust).
REL_TOL = 1e-6


def _close_enough(a: float, b: float) -> bool:
    return abs(a - b) <= max(REL_TOL * max(a, b), 1e-9)


def reconcile(meta: dict, balances: Optional[dict],
              exclude_mints: frozenset = frozenset()) -> dict:
    """
    Cross-check journal positions against chain balances.

    meta:     {mint: {"tokens": float, "cost": float, ...}} — the journal's
              open positions (as built by run_live_cycle._live_portfolio).
              Entries may be flagged in place:
                chain_excluded=True   position vanished on-chain
                chain_tokens=<float>  exit sizing must clamp to this
    balances: {mint: ui_amount} from chain, or None when unreadable.
    exclude_mints: mints that are never positions (e.g. the USDC mint —
              dry powder, not a trade).

    Returns a report dict:
      {"checked": bool, "discrepancies": [{"mint", "kind", ...}]}
    where kind is one of "vanished" | "chain_below_journal" |
    "chain_above_journal" | "unjournaled".
    """
    report: dict = {"checked": False, "discrepancies": []}
    if balances is None:
        log.warning("reconcile: chain balances unreadable — journal left "
                    "unchecked this cycle")
        return report
    report["checked"] = True

    for mint, m in meta.items():
        journal_tokens = float(m.get("tokens") or 0.0)
        if journal_tokens <= 0:
            continue
        chain_tokens = float(balances.get(mint, 0.0))

        if chain_tokens <= 0:
            log.warning("RECONCILE %s: journal holds %.6f but chain balance "
                        "is 0 — out-of-band sell/transfer? position EXCLUDED "
                        "from the book this cycle; operator review needed",
                        mint[:8], journal_tokens)
            m["chain_excluded"] = True
            report["discrepancies"].append({
                "mint": mint, "kind": "vanished",
                "journal": journal_tokens, "chain": 0.0})
            continue

        if _close_enough(chain_tokens, journal_tokens):
            continue

        if chain_tokens < journal_tokens:
            log.warning("RECONCILE %s: chain %.6f < journal %.6f — exit "
                        "sizing clamped to chain truth",
                        mint[:8], chain_tokens, journal_tokens)
            m["chain_tokens"] = chain_tokens
            report["discrepancies"].append({
                "mint": mint, "kind": "chain_below_journal",
                "journal": journal_tokens, "chain": chain_tokens})
        else:
            log.info("RECONCILE %s: chain %.6f > journal %.6f — keeping "
                     "journal amount (conservative)",
                     mint[:8], chain_tokens, journal_tokens)
            report["discrepancies"].append({
                "mint": mint, "kind": "chain_above_journal",
                "journal": journal_tokens, "chain": chain_tokens})

    # Unjournaled holdings: on-chain but never bought through this bot.
    for mint, amount in balances.items():
        if mint in meta or mint in exclude_mints or amount <= 0:
            continue
        log.warning("RECONCILE: unjournaled on-chain holding %s (%.6f) — "
                    "NOT added to the book (no cost basis; never fabricate)",
                    mint[:8], amount)
        report["discrepancies"].append({
            "mint": mint, "kind": "unjournaled", "chain": amount})

    return report


UNEXPLAINED_OUTFLOW_USD: float = float(os.getenv("UNEXPLAINED_OUTFLOW_USD", "5.0"))


class WalletDropResult(dict):
    """Result of wallet drop check (§81 Phase B)."""
    @property
    def alerted(self) -> bool:
        return bool(self.get("alerted", False))

    @property
    def skipped(self) -> bool:
        return bool(self.get("skipped", False))

    @property
    def reason(self) -> str:
        return str(self.get("reason", ""))

    @property
    def unexplained_drop_usd(self) -> float:
        return float(self.get("unexplained_drop_usd", 0.0))

    @property
    def unexplained_sol_drop(self) -> float:
        return float(self.get("unexplained_sol_drop", 0.0))


def check_wallet_drop(
    current_usdc: Optional[float],
    current_sol: Optional[float],
    sol_price_usd: Optional[float] = None,
    ledger: Any = None,
    state_dir: Optional[Path] = None,
    now_fn: Callable[[], float] = time.time,
) -> WalletDropResult:
    """
    Compare current wallet USDC and SOL balances against the previous snapshot and
    the ExecutionLedger to detect unexplained balance outflows (§81 Phase B).

    Evaluates two independent balance channels:
      1. USDC channel: actual USDC delta vs ledger (opens spend USDC, closes return USDC).
         Tolerance: UNEXPLAINED_OUTFLOW_USD (default $5.00).
      2. SOL channel: actual SOL delta vs gas burn (0.003 SOL/record + 0.02 SOL buffer).

    Fails closed on unreadable balances: if either current_usdc or current_sol is None,
    skips the drop check without alerting and without overwriting the snapshot.
    """
    import json
    try:
        from live_execution import config as live_config
        target_dir = Path(state_dir or live_config.STATE_DIR)
    except Exception:
        target_dir = Path(state_dir or "/tmp/trading_state")

    snap_file = target_dir / "wallet_snapshot.json"
    now = now_fn()

    if current_usdc is None or current_sol is None:
        log.warning(
            "wallet balances unreadable (usdc=%s, sol=%s); skipping drop check",
            current_usdc,
            current_sol,
        )
        return WalletDropResult({
            "alerted": False,
            "skipped": True,
            "reason": "balances_unreadable",
            "unexplained_drop_usd": 0.0,
            "unexplained_sol_drop": 0.0,
        })

    result = WalletDropResult({
        "alerted": False,
        "skipped": False,
        "unexplained_drop_usd": 0.0,
        "unexplained_sol_drop": 0.0,
        "reason": "",
        "channel": "",
    })

    if snap_file.is_file():
        try:
            prev = json.loads(snap_file.read_text())
            prev_ts = float(prev.get("ts", 0.0))
            prev_usdc = float(prev.get("usdc", 0.0))
            prev_sol = float(prev.get("sol", 0.0))

            records = getattr(ledger, "_load", lambda: [])()
            period_buys_usd = 0.0
            period_proceeds_usd = 0.0
            tx_count = 0
            for r in records:
                r_ts = float(r.get("ts", 0.0))
                if prev_ts <= r_ts <= now:
                    tx_count += 1
                    k = r.get("kind")
                    if k in ("buy", "open"):
                        period_buys_usd += float(r.get("usd_size", 0.0))
                    elif k in ("close", "sell"):
                        proceeds = r.get("usd_size") or r.get("proceeds_usd", 0.0)
                        period_proceeds_usd += float(proceeds or 0.0)

            # Channel 1: USDC delta vs ledger flows
            actual_usdc_delta = current_usdc - prev_usdc
            explained_usdc_delta = period_proceeds_usd - period_buys_usd
            unexplained_usdc_delta = actual_usdc_delta - explained_usdc_delta

            outflow_cap = float(os.getenv("UNEXPLAINED_OUTFLOW_USD", str(UNEXPLAINED_OUTFLOW_USD)))
            usdc_dropped = unexplained_usdc_delta < -outflow_cap

            # Channel 2: SOL delta vs gas tolerance
            actual_sol_delta = current_sol - prev_sol
            sol_tolerance = (tx_count * 0.003) + 0.02
            sol_dropped = actual_sol_delta < -sol_tolerance

            if usdc_dropped or sol_dropped:
                result["alerted"] = True
                drop_usdc = abs(unexplained_usdc_delta) if usdc_dropped else 0.0
                drop_sol = abs(actual_sol_delta) if sol_dropped else 0.0
                result["unexplained_drop_usd"] = drop_usdc
                result["unexplained_sol_drop"] = drop_sol

                channels = []
                if usdc_dropped:
                    channels.append(f"USDC drop ${drop_usdc:.2f} (cap ${outflow_cap:.2f})")
                if sol_dropped:
                    channels.append(f"SOL drop {drop_sol:.4f} SOL (tolerance {sol_tolerance:.4f} SOL)")
                channel_str = ", ".join(channels)
                result["channel"] = channel_str
                result["reason"] = f"unexplained outflow: {channel_str}"

                log.critical("WALLET_DROP: %s", result["reason"])
                try:
                    from alerts import alert_nowait, AlertEvent
                    alert_nowait(
                        AlertEvent.WALLET_DROP,
                        f"Unexplained wallet outflow: {channel_str}",
                        key="wallet_drop",
                    )
                except Exception:
                    pass

                # Trip kill switch if configured
                from live_execution import config as live_cfg
                if getattr(live_cfg, "WALLET_DROP_TRIPS_KILL_SWITCH", False):
                    try:
                        from live_execution import kill_switch
                        if not kill_switch.is_tripped(state_dir=target_dir):
                            kill_switch.trip(f"wallet_drop: {channel_str}", state_dir=target_dir)
                            log.critical("KILL SWITCH TRIPPED by wallet drop: %s", channel_str)
                        else:
                            log.info("kill switch already tripped (keeping earlier reason): %s", kill_switch.trip_reason(state_dir=target_dir))
                    except Exception as exc:
                        log.error("Failed tripping kill switch on wallet drop: %s", exc)

        except Exception as exc:
            # Never overwrite the baseline after a failed check: doing so
            # would silently bless whatever outflow we could not evaluate.
            log.warning("reconcile: failed checking wallet drop: %s", exc)
            result["skipped"] = True
            result["reason"] = f"check_failed: {exc}"
            return result

    # Save new snapshot atomically
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        tmp_file = snap_file.with_suffix(".tmp")
        tmp_file.write_text(json.dumps({
            "ts": now,
            "usdc": current_usdc,
            "sol": current_sol,
        }, indent=2))
        tmp_file.replace(snap_file)
    except Exception as exc:
        log.warning("reconcile: failed saving wallet snapshot: %s", exc)

    return result
