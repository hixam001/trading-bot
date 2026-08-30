"""
live_execution/venue.py — A3 fill-venue attribution (omo audit §28).

Labels WHICH program actually executed a fill, read straight off the confirmed
transaction's instructions/account keys, so the label is verifiable against the
same signature anyone can open in a block explorer. A router (jupiter) is named
by the program the order ENTERED, because top-level instructions are read first;
a fill that hops through a router is labelled by the router, not the first pool
it touched.

This is OBSERVABILITY, not a guard: a venue can never block, refuse or alter an
order. Every failure degrades to label=None (fail-soft) — an unknown venue is
honestly "unknown", never a guess.

reference: wallet.server.ts fetchFillVenue + VENUE_PROGRAMS.
"""
from __future__ import annotations

import logging
from typing import Optional

from live_execution import solana

log = logging.getLogger(__name__)


# Which program actually executed a fill. Program ids are network-wide
# constants. Order matters only for dedup; the FIRST known program in
# top-level-instruction order wins the label.
VENUE_PROGRAMS: list[tuple[str, str]] = [
    ("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", "pump.fun bonding curve"),
    ("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA", "pump.fun amm"),
    ("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4", "jupiter router"),
    ("JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB", "jupiter router"),
    ("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", "raydium"),
    ("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK", "raydium clmm"),
    ("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", "orca whirlpool"),
    ("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo", "meteora dlmm"),
]

# Infrastructure programs that are never a venue (system, compute budget,
# token programs, associated-token, memo). Used to name an unknown router.
NON_VENUE_PROGRAMS = frozenset({
    "11111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",       # token program
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",       # token-2022
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
})


def _pubkey_of(key) -> Optional[str]:
    """accountKeys entries are either bare strings or {pubkey: ...} objects
    depending on RPC encoding — normalise both (never raises)."""
    if isinstance(key, str):
        return key
    if isinstance(key, dict):
        pk = key.get("pubkey")
        return str(pk) if pk else None
    return None


def _program_ids(instructions) -> list[str]:
    out: list[str] = []
    for ix in instructions or []:
        if not isinstance(ix, dict):
            continue
        pid = ix.get("programId")
        if pid:
            out.append(str(pid))
    return out


def fill_venue_from_tx(tx: Optional[dict]) -> dict:
    """
    Pure parser over a fetched (jsonParsed) transaction.

    Returns {"label": str|None, "programs": [str]}:
      * label is the first KNOWN venue in top-instruction order, else a raw
        "program XXXX…YYYY" for an unrecognised executing router, else
        "token transfer · no swap program" when nothing swapped.
      * programs is the ordered list of matched venue labels (may be empty).
    Never raises; a missing/None tx yields {"label": None, "programs": []}.
    """
    if not isinstance(tx, dict):
        return {"label": None, "programs": []}

    message = (tx.get("transaction") or {}).get("message")
    if not isinstance(message, dict):
        # Not a parseable transaction (fetch returned a stub) -> unknown, never
        # a guessed label.
        return {"label": None, "programs": []}

    top = _program_ids(message.get("instructions"))
    inner: list[str] = []
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        if isinstance(group, dict):
            inner.extend(_program_ids(group.get("instructions")))
    keys = [_pubkey_of(k) for k in message.get("accountKeys") or []]
    keys = [k for k in keys if k]

    ordered: list[str] = []
    for p in top + inner + keys:
        if p not in ordered:
            ordered.append(p)

    known = dict(VENUE_PROGRAMS)
    labels: list[str] = []
    for program in ordered:
        label = known.get(program)
        if label and label not in labels:
            labels.append(label)
    if labels:
        return {"label": labels[0], "programs": labels}

    # No known AMM/router: name the unrecognised executing program outright so
    # nothing is invented.
    for p in top:
        if p not in NON_VENUE_PROGRAMS:
            short = f"{p[:4]}\u2026{p[-4:]}"
            return {"label": f"program {short}", "programs": [p]}

    return {"label": "token transfer \u00b7 no swap program", "programs": []}


async def fetch_fill_venue(signature: str) -> dict:
    """
    Fetch the confirmed fill tx and attribute its venue. Fail-soft: any RPC or
    parse failure returns {"label": None, "programs": []} — venue is
    observability and must never block or raise.
    """
    if not signature:
        return {"label": None, "programs": []}
    try:
        tx = await solana.get_transaction(signature)
    except Exception as exc:  # get_transaction is None-on-error, but be safe
        log.info("venue: fetch failed for %s: %s", signature[:16], exc)
        return {"label": None, "programs": []}
    venue = fill_venue_from_tx(tx)
    if venue["label"]:
        log.info("venue: %s -> %s", signature[:16], venue["label"])
    return venue
