"""
tests/test_ref_r11_memo_verify.py — REF-R11 on-chain precommit memo verify.

Covers the public verification surface for the commit memo:
  * _memo_text_from_tx parses the memo program's log echo
  * _verify_memo: hash match -> verified, mismatch -> failed, slot ordering,
    RPC unavailable -> unknown (never pass), no memo -> not_published
  * db.bind_commit_memo / get_commit_id_by_hash round-trip on SQLite
  * /api/verify.json end-to-end with a mocked on-chain memo fetch

Hash fixtures are HAND-COMPUTED (defense-first: money/hash math is never
trusted from a black box):
  sha256("nonce123|" + canonical({"mint":"MINTX","side":"buy"}))
    = c19d6c19de90c6a2c5597c7bfc801a0437b98f7c75faec0d1910d0f95afc3169
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

import config
from api import db
from api.main import app
from api.routes.proof import _memo_text_from_tx, _verify_memo

MEMO_PREFIX = "commit:v1:"
NONCE = "nonce123"
PAYLOAD = {"mint": "MINTX", "side": "buy"}
PAYLOAD_JSON = json.dumps(PAYLOAD, sort_keys=True)
HASH_A = "c19d6c19de90c6a2c5597c7bfc801a0437b98f7c75faec0d1910d0f95afc3169"


def _memo_tx(text, slot=100, err=None):
    return {"slot": slot,
            "meta": {"err": err,
                     "logMessages": [f'Memo (len {len(text)}): "{text}"']}}


def _fill_tx(slot=105):
    return {"slot": slot, "meta": {"err": None}}


# --- _memo_text_from_tx -------------------------------------------------------

def test_memo_text_parsed_from_log_echo():
    tx = _memo_tx(MEMO_PREFIX + HASH_A)
    assert _memo_text_from_tx(tx) == MEMO_PREFIX + HASH_A


def test_memo_text_absent_returns_none():
    assert _memo_text_from_tx({"meta": {"logMessages": ["Program log: hi"]}}) is None
    assert _memo_text_from_tx({}) is None


# --- _verify_memo (unit, fake RPC) -------------------------------------------

def _row(memo_signature=None, memo_slot=None, signature=None):
    return {"memo_signature": memo_signature, "memo_slot": memo_slot,
            "signature": signature}


async def test_no_memo_signature_is_not_published():
    res = await _verify_memo(_row(), HASH_A, lambda s: None, MEMO_PREFIX)
    assert res["published"] is False
    assert res["status"] == "not_published"


async def test_rpc_unavailable_is_unknown_never_pass():
    async def no_tx(sig):
        return None
    res = await _verify_memo(_row(memo_signature="MEMOSIG"), HASH_A,
                             no_tx, MEMO_PREFIX)
    assert res["published"] is True
    assert res["status"] == "unknown"


async def test_hash_match_and_ordering_verified():
    async def fake_tx(sig):
        if sig == "MEMOSIG":
            return _memo_tx(MEMO_PREFIX + HASH_A, slot=100)
        if sig == "FILLSIG":
            return _fill_tx(slot=105)
        return None
    row = _row(memo_signature="MEMOSIG", memo_slot=100, signature="FILLSIG")
    res = await _verify_memo(row, HASH_A, fake_tx, MEMO_PREFIX)
    assert res["status"] == "verified"
    names = {c["name"]: c["status"] for c in res["checks"]}
    assert names == {"memo_confirmed": "pass",
                     "memo_hash_matches_chain": "pass",
                     "memo_before_fill": "pass"}


async def test_hash_mismatch_fails():
    async def fake_tx(sig):
        if sig == "MEMOSIG":
            return _memo_tx(MEMO_PREFIX + "f" * 64, slot=100)
        return _fill_tx(slot=105)
    row = _row(memo_signature="MEMOSIG", memo_slot=100, signature="FILLSIG")
    res = await _verify_memo(row, HASH_A, fake_tx, MEMO_PREFIX)
    assert res["status"] == "failed"


async def test_memo_after_fill_fails_ordering():
    async def fake_tx(sig):
        if sig == "MEMOSIG":
            return _memo_tx(MEMO_PREFIX + HASH_A, slot=200)   # AFTER fill
        if sig == "FILLSIG":
            return _fill_tx(slot=105)
        return None
    row = _row(memo_signature="MEMOSIG", memo_slot=200, signature="FILLSIG")
    res = await _verify_memo(row, HASH_A, fake_tx, MEMO_PREFIX)
    assert res["status"] == "failed"
    names = {c["name"]: c["status"] for c in res["checks"]}
    assert names["memo_before_fill"] == "fail"


async def test_no_fill_bound_is_unknown_ordering():
    async def fake_tx(sig):
        return _memo_tx(MEMO_PREFIX + HASH_A, slot=100) if sig == "MEMOSIG" else None
    row = _row(memo_signature="MEMOSIG", memo_slot=100, signature=None)
    res = await _verify_memo(row, HASH_A, fake_tx, MEMO_PREFIX)
    # hash matches but no fill to order against -> unknown, not verified
    assert res["status"] == "unknown"



# --- db round-trip -----------------------------------------------------------

@pytest_asyncio.fixture
async def _db(tmp_path):
    config.DB_PATH = tmp_path / "ref_r11.db"
    await db.init_db()
    async with db.get_db() as conn:
        yield conn


async def test_bind_memo_and_lookup_by_hash(_db):
    await db.insert_decision_commit(
        _db, created_at="2026-08-27T00:00:00+00:00",
        tick_ts="2026-08-27T00:00:00+00:00", symbol="MINTX",
        mint_address="MINTX", verdict="pass", entry_allowed=True,
        nonce=NONCE, payload_json=PAYLOAD_JSON, payload_hash=HASH_A)
    commit_id = await db.get_commit_id_by_hash(_db, HASH_A)
    assert commit_id is not None

    assert await db.bind_commit_memo(_db, commit_id, "MEMOSIG", 100) == 1
    # A second bind is refused (memo already recorded).
    assert await db.bind_commit_memo(_db, commit_id, "OTHER", 1) == 0

    rows = await db.get_verify_commits(_db)
    assert rows[0]["memo_signature"] == "MEMOSIG"
    assert rows[0]["memo_slot"] == 100


async def test_get_commit_id_by_hash_unknown_returns_none(_db):
    assert await db.get_commit_id_by_hash(_db, "deadbeef") is None


# --- /api/verify.json end-to-end --------------------------------------------

@pytest_asyncio.fixture
async def client(_db, monkeypatch):
    import httpx
    # Seed one commit with a bound memo + fill.
    await db.insert_decision_commit(
        _db, created_at="2026-08-27T00:00:00+00:00",
        tick_ts="2026-08-27T00:00:00+00:00", symbol="MINTX",
        mint_address="MINTX", verdict="pass", entry_allowed=True,
        nonce=NONCE, payload_json=PAYLOAD_JSON, payload_hash=HASH_A)
    commit_id = await db.get_commit_id_by_hash(_db, HASH_A)
    await db.bind_commit_memo(_db, commit_id, "MEMOSIG", 100)
    await db.bind_commit_signature(_db, commit_id, "FILLSIG", "filled", "exact")

    async def fake_get_tx(sig):
        if sig == "MEMOSIG":
            return _memo_tx(MEMO_PREFIX + HASH_A, slot=100)
        if sig == "FILLSIG":
            return _fill_tx(slot=105)
        return None

    # The endpoint imports get_transaction at call time -> patch the module.
    import live_execution.solana as live_solana
    monkeypatch.setattr(live_solana, "get_transaction", fake_get_tx)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as c:
        yield c


async def test_verify_endpoint_reports_memo_verified(client):
    r = await client.get("/api/verify.json")
    assert r.status_code == 200
    body = r.json()
    assert body["totals"]["verified"] == 1
    assert body["memo_totals"]["published"] == 1
    assert body["memo_totals"]["verified"] == 1
    row = body["rows"][0]
    assert row["match"] is True
    assert row["memo"]["status"] == "verified"
    assert row["memo"]["memo_signature"] == "MEMOSIG"
