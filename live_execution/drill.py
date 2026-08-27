"""
live_execution/drill.py - DEVNET drill for the execution plumbing (P0-1).

Exercises every primitive the live path needs WITHOUT Jupiter and WITHOUT
tokens: keypair load, address verification, balance, chain-read decimals,
fresh blockhash, build/sign a real system transfer, broadcast, confirm.
Devnet endpoints only, self-transfer of dust - nothing can go wrong with
funds even in principle. Run BEFORE mainnet is ever considered:

    .venv/bin/python run_live_cycle.py --drill
"""
from __future__ import annotations

import asyncio
import logging
import time

from live_execution import config, wallet
from live_execution.solana import (  # noqa: F401
    LAMPORTS_PER_SOL,
    confirm_signature,
    latest_blockhash,
    rpc as sol_rpc,
    send_raw_transaction,
)

log = logging.getLogger(__name__)


SOL_MINT = "So11111111111111111111111111111111111111112"


async def run_drill() -> list:
    steps = []

    def step(ok, name, detail):
        steps.append({"step": name, "ok": ok, "detail": detail})
        log.info("%s %s: %s", "PASS" if ok else "FAIL", name, detail)
        return ok

    # 1. wallet
    try:
        payer = wallet.load_keypair()
        addr = wallet.verify_expected_address(payer)
        step(True, "wallet", addr)
    except Exception as exc:
        step(False, "wallet", str(exc))
        return steps
    try:
        bal_raw = await sol_rpc("getBalance", [addr], endpoints=[config.DRILL_RPC_URL])
        bal = int((bal_raw or {}).get("value", 0)) / LAMPORTS_PER_SOL
    except Exception as exc:
        step(False, "devnet-rpc", str(exc))
        return steps
    if not step(bal is not None, "devnet-rpc", "balance %.6f SOL" % (bal or 0)):
        return steps

    # 3. chain-read decimals (SOL mint exists on devnet, 9 decimals)
    try:
        res = await sol_rpc("getTokenSupply", [SOL_MINT], endpoints=[config.DRILL_RPC_URL])
        dec = ((res or {}).get("value") or {}).get("decimals")
    except Exception:
        dec = None
    step(dec == 9, "chain-decimals", "SOL mint decimals=%s" % dec)

    # 4. build + sign a real self-transfer of dust
    if bal is None or bal * LAMPORTS_PER_SOL < config.DRILL_TRANSFER_LAMPORTS + 5000:
        step(False, "funds", "airdrop devnet SOL first: solana airdrop 1 %s" % addr)
        return steps
    from solders.pubkey import Pubkey as _PK
    from solders.system_program import TransferParams, transfer
    from solders.message import Message
    from solders.transaction import VersionedTransaction
    from solders.hash import Hash as _Hash
    blockhash = await latest_blockhash(config.DRILL_RPC_URL)
    if not blockhash:
        step(False, "blockhash", "could not fetch blockhash")
        return steps
    try:
        ix = transfer(TransferParams(
            from_pubkey=payer.pubkey(), to_pubkey=payer.pubkey(),
            lamports=config.DRILL_TRANSFER_LAMPORTS))
        msg = Message.new_with_blockhash([ix], payer.pubkey(),
                                         _Hash.from_string(blockhash))
        vtx = VersionedTransaction(msg, [payer])
        raw = bytes(vtx)
    except Exception as exc:
        step(False, "build/sign", str(exc))
        return steps
    sig = await send_raw_transaction(raw, endpoints=[config.DRILL_RPC_URL])
    if not sig:
        step(False, "broadcast", "every rpc refused")
        return steps
    conf = await confirm_signature(sig, endpoints=[config.DRILL_RPC_URL])
    if not step(bool(conf.get("confirmed")), "confirm",
                conf.get("err") or ("slot %s" % conf.get("slot"))):
        return steps

    # 5. REF-R11 commit memo — build/sign/send/confirm a REAL memo tx on
    # devnet (airdrop-funded). This exercises the exact publish_commit_memo
    # path the armed flow uses, BEFORE mainnet is ever considered.
    import hashlib as _hashlib
    from live_execution import memo as _memo
    drill_hash = _hashlib.sha256(b"drill-commit").hexdigest()
    try:
        memo_res = await _memo.publish_commit_memo(
            payer, drill_hash, endpoints=[config.DRILL_RPC_URL])
        step(True, "commit-memo", "sig %s slot %s" % (
            memo_res["signature"][:16], memo_res.get("slot")))
    except _memo.MemoPublishError as exc:
        step(False, "commit-memo", str(exc))
        return steps

    log.info("DRILL COMPLETE: %s of %s steps passed",
             sum(1 for s in steps if s["ok"]), len(steps))
    return steps
