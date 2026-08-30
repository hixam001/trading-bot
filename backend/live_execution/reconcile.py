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
from typing import Optional

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
