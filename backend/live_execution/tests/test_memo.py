"""
tests for live_execution.memo — REF-R11 on-chain precommit memo.

Covers the OFFLINE-testable subset: memo text format, transaction build
(verified by deserializing with a throwaway keypair — payer is key 0, the
memo program id is present, and the instruction data is exactly the memo
text), and publish_commit_memo success/failure paths with mocked RPC.

Everything here is hermetic: no network. A missing solders install must fail
closed with MemoError, never a half-built transaction.
"""
from __future__ import annotations

import pytest

from live_execution import memo
from live_execution import solana


# --- memo text format ---------------------------------------------------------

def test_memo_text_is_prefix_plus_hash():
    digest = "ab" * 32
    assert memo.memo_text_for_hash(digest) == memo.MEMO_PREFIX + digest
    assert memo.MEMO_PREFIX == "commit:v1:"


def test_memo_program_id_is_the_reference_constant():
    # Reference parity: precommit.server.ts posts to the SPL Memo program.
    assert memo.MEMO_PROGRAM_ID == "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"


# --- transaction build (deserialized + verified, no network) ------------------

def test_build_memo_transaction_round_trips():
    solders = pytest.importorskip("solders")
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction

    payer = Keypair()
    digest = "cd" * 32
    text = memo.memo_text_for_hash(digest)
    raw = memo.build_memo_transaction(
        text, payer, "4uQeVj5tqViQh7yWWGStvkEG1Zmhx6uasJtWCJziofM")

    vtx = VersionedTransaction.from_bytes(raw)
    keys = [str(k) for k in vtx.message.account_keys]
    # The fee payer must be account key 0 (the memo signer).
    assert keys[0] == str(payer.pubkey())
    # The memo program must be one of the account keys.
    assert memo.MEMO_PROGRAM_ID in keys
    # Exactly one instruction, and its data is the memo text bytes.
    assert len(vtx.message.instructions) == 1
    assert bytes(vtx.message.instructions[0].data) == text.encode("utf-8")
    # Locally signed by the single payer.
    assert len(vtx.signatures) == 1


def test_build_memo_transaction_rejects_bad_blockhash():
    pytest.importorskip("solders")
    from solders.keypair import Keypair
    payer = Keypair()
    with pytest.raises(memo.MemoError):
        memo.build_memo_transaction(
            memo.memo_text_for_hash("ef" * 32), payer, "not-a-blockhash")


# --- publish_commit_memo (mocked RPC; fail-closed paths) ----------------------

@pytest.fixture
def payer():
    solders = pytest.importorskip("solders")
    from solders.keypair import Keypair
    return Keypair()


async def test_publish_happy_path_returns_sig_and_slot(payer, monkeypatch):
    async def fake_blockhash(endpoint):
        return "4uQeVj5tqViQh7yWWGStvkEG1Zmhx6uasJtWCJziofM"

    async def fake_send(raw, endpoints=None):
        return "MEMOSIG111"

    async def fake_confirm(sig, timeout_s=None, endpoints=None):
        return {"confirmed": True, "slot": 12345, "err": None}

    monkeypatch.setattr(solana, "latest_blockhash", fake_blockhash)
    monkeypatch.setattr(solana, "send_raw_transaction", fake_send)
    monkeypatch.setattr(solana, "confirm_signature", fake_confirm)

    res = await memo.publish_commit_memo(payer, "ab" * 32)
    assert res == {"signature": "MEMOSIG111", "slot": 12345}


async def test_publish_no_blockhash_fails_closed(payer, monkeypatch):
    async def fake_blockhash(endpoint):
        return None

    async def boom_send(*a, **k):
        raise AssertionError("send attempted despite no blockhash")

    monkeypatch.setattr(solana, "latest_blockhash", fake_blockhash)
    monkeypatch.setattr(solana, "send_raw_transaction", boom_send)
    with pytest.raises(memo.MemoPublishError, match="blockhash"):
        await memo.publish_commit_memo(payer, "ab" * 32)


async def test_publish_send_refused_fails_closed(payer, monkeypatch):
    async def fake_blockhash(endpoint):
        return "4uQeVj5tqViQh7yWWGStvkEG1Zmhx6uasJtWCJziofM"

    async def fake_send(raw, endpoints=None):
        return None   # every rpc refused

    async def boom_confirm(*a, **k):
        raise AssertionError("confirm attempted despite refused send")

    monkeypatch.setattr(solana, "latest_blockhash", fake_blockhash)
    monkeypatch.setattr(solana, "send_raw_transaction", fake_send)
    monkeypatch.setattr(solana, "confirm_signature", boom_confirm)
    with pytest.raises(memo.MemoPublishError, match="refused"):
        await memo.publish_commit_memo(payer, "ab" * 32)


async def test_publish_unconfirmed_fails_closed(payer, monkeypatch):
    async def fake_blockhash(endpoint):
        return "4uQeVj5tqViQh7yWWGStvkEG1Zmhx6uasJtWCJziofM"

    async def fake_send(raw, endpoints=None):
        return "MEMOSIG222"

    async def fake_confirm(sig, timeout_s=None, endpoints=None):
        return {"confirmed": False, "slot": None, "err": "timeout"}

    monkeypatch.setattr(solana, "latest_blockhash", fake_blockhash)
    monkeypatch.setattr(solana, "send_raw_transaction", fake_send)
    monkeypatch.setattr(solana, "confirm_signature", fake_confirm)
    with pytest.raises(memo.MemoPublishError, match="not confirmed"):
        await memo.publish_commit_memo(payer, "ab" * 32)
