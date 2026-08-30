"""
live_execution/memo.py — REF-R11 on-chain precommit memo (commit–reveal).

reference: precommit.server.ts. Before a decision can turn into a fill, the
hash of the sealed decision (the SAME sha256(nonce|canonical_payload) the
local CommitLog records) is written into a Solana memo instruction. The hash
is stamped by validators at a time nobody here controls; later anyone can
recompute it from the revealed plaintext and check it matches the memo that
was already on-chain BEFORE the fill landed.

Deviations from the reference (documented in handoff §26):
  * FAIL-CLOSED BLOCKING: a memo that cannot be published and confirmed
    BLOCKS the fill entirely. The reference publishes asynchronously and
    shows un-publishable commits as "unpublished"; handoff §22 requirement 4
    chose the stricter behavior — a decision that cannot be committed
    on-chain is not executed.
  * Immediate reveal: the plaintext payload+nonce are already public in the
    decision record; the ordering proof is the on-chain hash timestamp.
  * Single signer: the memo is signed by the configured trading wallet
    keypair (no separate burner memo key at this book scale).
  * De-branded memo prefix: "commit:v1:" (the reference uses an
    upstream-branded prefix; our commit scheme was never branded).

DISARMED: this module is only reachable through place_buy/place_sell once
LIVE_TRADING_ENABLED is True. While disarmed, every order returns "unarmed"
before any network call and nothing here ever runs.
"""
from __future__ import annotations

import logging

from live_execution import config, solana

log = logging.getLogger(__name__)

# Solana Memo Program (reference parity — the same program id the reference
# precommit.server.ts posts its commitments to).
MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

# Versioned prefix written in front of the hash inside the memo text, so a
# future scheme change is distinguishable on-chain. De-branded by design.
MEMO_PREFIX = "commit:v1:"


class MemoError(Exception):
    """The memo transaction could not be built — nothing was sent."""


class MemoPublishError(Exception):
    """The memo could not be published and confirmed — the fill must NOT run."""


def memo_text_for_hash(digest: str) -> str:
    """The exact on-chain memo text for a sealed decision hash."""
    return MEMO_PREFIX + digest


def build_memo_transaction(memo_text: str, payer, blockhash: str) -> bytes:
    """
    Build + locally sign a memo-only transaction (solders, same primitives
    the devnet drill uses). One instruction: the memo program echoing the
    text, payer as the single signer. Fails closed (MemoError) if solders is
    missing or the build fails — never returns a half-built transaction.
    """
    try:
        from solders.hash import Hash as SolHash  # type: ignore
        from solders.instruction import AccountMeta, Instruction  # type: ignore
        from solders.message import Message  # type: ignore
        from solders.pubkey import Pubkey  # type: ignore
        from solders.transaction import VersionedTransaction  # type: ignore
    except ImportError as exc:
        raise MemoError(f"solders is not installed: {exc}") from exc
    try:
        program_id = Pubkey.from_string(MEMO_PROGRAM_ID)
        ix = Instruction(
            program_id=program_id,
            data=memo_text.encode("utf-8"),
            accounts=[AccountMeta(pubkey=payer.pubkey(), is_signer=True,
                                  is_writable=False)],
        )
        msg = Message.new_with_blockhash(
            [ix], payer.pubkey(), SolHash.from_string(blockhash))
        vtx = VersionedTransaction(msg, [payer])
        return bytes(vtx)
    except MemoError:
        raise
    except Exception as exc:  # solders raises assorted types
        raise MemoError(f"memo transaction build refused: {exc}") from exc


async def publish_commit_memo(payer, seal_hash: str,
                              endpoints: list | None = None) -> dict:
    """
    Publish + confirm the commit memo for a sealed decision hash.

    Order: fresh blockhash (rotating RPCs) → build+sign memo tx → broadcast
    → confirm. Returns {"signature": str, "slot": int|None} ONLY after
    on-chain confirmation. ANY failure raises MemoPublishError — never a
    partial success, never a silent skip (handoff §22 requirement 4).
    """
    text = memo_text_for_hash(seal_hash)
    blockhash = None
    for endpoint in endpoints or config.RPC_URLS:
        blockhash = await solana.latest_blockhash(endpoint)
        if blockhash:
            break
    if not blockhash:
        raise MemoPublishError("no fresh blockhash from any configured rpc")
    try:
        raw = build_memo_transaction(text, payer, blockhash)
    except MemoError as exc:
        raise MemoPublishError(str(exc)) from exc
    sent = await solana.send_raw_transaction(raw, endpoints=endpoints)
    if not sent:
        raise MemoPublishError("every rpc refused the memo transaction")
    conf = await solana.confirm_signature(sent, endpoints=endpoints)
    if not conf["confirmed"]:
        raise MemoPublishError(
            f"memo not confirmed before timeout: {conf['err'] or 'unknown'}")
    log.info("commit memo published sig=%s slot=%s", sent[:16], conf.get("slot"))
    return {"signature": sent, "slot": conf.get("slot")}