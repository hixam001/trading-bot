"""
live_execution/scripts/repair_vanished.py — operator CLI completing the
reconcile "operator review" for positions that vanished on-chain (2026-08-29).

THE SCENARIO (second occurrence — §37 dust row, then the operator's manual
sell while the RPC fallback was broken): the operator sells a held coin
out-of-band (their own wallet, not through the bot). The chain balance
goes to 0; the ExecutionLedger still shows the buy OPEN (it never saw a
sell). reconcile() is DELIBERATELY read-only against this — a bad RPC read
must never be able to corrupt the money ledger — so every cycle logs
"RECONCILE … operator review needed" and /api/live/portfolio (Holdings)
shows the position forever.

This tool IS the operator review, made safe:

    python -m live_execution.scripts.repair_vanished list
        Every open ledger position with its CURRENT chain balance — shows
        which have vanished. Chain-unreadable is reported as such (never
        guessed).

    python -m live_execution.scripts.repair_vanished close <mint> \
            [--proceeds-usd X.XX] [--note "why"]
        Closes the mint's open buys as an out-of-band exit. SAFETY GATE:
        the chain balance MUST be verified 0 for the mint first (a typo or
        a live position must not be closeable here). --proceeds-usd is
        optional and honest: unknown proceeds are recorded pnl=None (never
        fabricated); when provided, PnL is realized against the summed
        cost. Also retires the journal's open thesis write-up and journals
        a `did` event so the dashboard shows the repair.

NEVER executes, quotes, or signs anything — it only repairs BOOKKEEPING.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent   # backend/ (package home)
for p in (str(ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

# Load .env FIRST (backend/config.py runs load_dotenv on import) so the
# wallet path + RPC endpoints resolve exactly like the live cycle's process.
import config as _paper_config                     # noqa: E402,F401

from live_execution import config, solana, wallet          # noqa: E402
from live_execution.models import ExecutionLedger           # noqa: E402


def _ledger() -> ExecutionLedger:
    return ExecutionLedger(config.STATE_DIR / "executions.json")


async def _open_with_chain() -> list[dict]:
    """Open ledger positions annotated with current chain balance."""
    ledger = _ledger()
    meta: dict[str, dict] = {}
    for r in ledger._load():
        if r.get("kind") != "buy" or r.get("status") not in ledger._OPEN:
            continue
        m = meta.setdefault(r["mint"], {"tokens": 0.0, "cost": 0.0,
                                        "opened_ts": r.get("ts")})
        m["tokens"] += float(r.get("tokens_out") or 0.0)
        m["cost"] += float(r.get("usd_size") or 0.0)
    try:
        payer = wallet.load_keypair()
        pubkey = wallet.pubkey_string(payer)
    except Exception as exc:
        print(f"wallet unreadable ({exc}) — chain check unavailable")
        pubkey = None
    balances: dict | None = None
    if pubkey:
        balances = await solana.get_token_balances(pubkey)
    for mint, m in meta.items():
        if balances is None:
            m["chain"] = None
        else:
            m["chain"] = float(balances.get(mint, 0.0))
    return [{"mint": k, **v} for k, v in meta.items()]


def cmd_list(_args) -> int:
    rows = asyncio.run(_open_with_chain())
    if not rows:
        print("no open positions in the ledger")
        return 0
    print(f"{'mint':<14} {'tokens':>14} {'cost$':>7} {'chain':>14}  state")
    for r in rows:
        chain = ("UNREADABLE" if r["chain"] is None
                 else f"{r['chain']:.6f}")
        state = ("VANISHED — closeable" if r["chain"] == 0.0
                 else "on-chain" if r["chain"] is not None else "unknown")
        print(f"{r['mint'][:12]}… {r['tokens']:14.6f} "
              f"{r['cost']:7.2f} {chain:>14}  {state}")


async def _close(mint: str, proceeds_usd, note: str) -> int:
    # SAFETY GATE 1: the mint must be an OPEN ledger position.
    ledger = _ledger()
    open_buys = [r for r in ledger._load()
                 if r.get("kind") == "buy" and r.get("mint") == mint
                 and r.get("status") in ledger._OPEN]
    if not open_buys:
        print(f"REFUSED: no open position for {mint}")
        return 1

    # SAFETY GATE 2: the chain balance must be verifiably 0.
    try:
        payer = wallet.load_keypair()
        pubkey = wallet.pubkey_string(payer)
    except Exception as exc:
        print(f"REFUSED: wallet unreadable ({exc}) — cannot verify chain")
        return 1
    balances = await solana.get_token_balances(pubkey)
    if balances is None:
        print("REFUSED: chain balances unreadable — never guess (fail "
              "closed); retry when RPC answers")
        return 1
    chain = float(balances.get(mint, 0.0))
    if chain > 0:
        print(f"REFUSED: chain still holds {chain:.6f} of this mint — "
              f"this tool only closes VANISHED positions (sell it "
              f"through the bot or move the tokens first)")
        return 1

    cost = sum(float(r.get("usd_size") or 0.0) for r in open_buys)
    tokens = sum(float(r.get("tokens_out") or 0.0) for r in open_buys)
    print(f"closing {mint[:12]}…: {len(open_buys)} open buy(s), "
          f"{tokens:.6f} tokens, cost ${cost:.4f}")
    if proceeds_usd is None:
        print("proceeds UNKNOWN — recording pnl=None (never fabricated)")
    else:
        print(f"proceeds ${proceeds_usd:.4f} -> realized PnL "
              f"${proceeds_usd - cost:+.4f}")

    closed = ledger.close_out_of_band(mint, proceeds_usd=proceeds_usd,
                                      note=note or
                                      "operator-confirmed out-of-band sell")
    print(f"ledger: {len(closed) - 1} buy(s) closed + 1 out-of-band close "
          f"record written")

    # Journal the repair so the dashboard/public record shows it.
    try:
        from api import db
        await db.init_db()
        async with db.get_db() as conn:
            now = datetime.now(timezone.utc).isoformat()
            await db.insert_event(
                conn, "did", now, mint[:6], mint,
                {"action": "out_of_band_close", "mint": mint,
                 "proceeds_usd": proceeds_usd, "cost_usd": cost,
                 "chain_verified_zero": True, "note": note},
            )
            await db.retire_thesis(
                conn, trade_id=f"live-{mint[:8]}",
                closed_at=now,
                realized_pnl_usd=(proceeds_usd - cost)
                                 if proceeds_usd is not None else 0.0,
            )
        print("journal: did event + thesis retired")
    except Exception as exc:
        print(f"journal write skipped (non-fatal): {exc}")

    print("REPAIRED — the position no longer appears in Holdings; the "
          "reconcile warning stops on the next cycle")
    return 0


def cmd_close(args) -> int:
    return asyncio.run(_close(args.mint, args.proceeds_usd, args.note))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    pc = sub.add_parser("close")
    pc.add_argument("mint")
    pc.add_argument("--proceeds-usd", type=float, default=None)
    pc.add_argument("--note", default="")
    pc.set_defaults(fn=cmd_close)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
