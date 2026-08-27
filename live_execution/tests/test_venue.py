"""
tests/test_venue.py — A3 fill-venue attribution (omo audit §28).

Offline/hermetic: every case feeds a hand-built jsonParsed transaction dict to
the pure parser fill_venue_from_tx, plus a mocked-RPC fetch_fill_venue. No
network. Program ids are the real mainnet constants from VENUE_PROGRAMS.
"""
from __future__ import annotations

import pytest

from live_execution import venue as v


JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
RAYDIUM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
ORCA = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
SYSTEM = "11111111111111111111111111111111"
TOKEN_PROG = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _tx(top=(), inner=(), keys=()):
    """Build a minimal jsonParsed transaction dict."""
    return {
        "transaction": {"message": {
            "accountKeys": list(keys),
            "instructions": [{"programId": p} for p in top],
        }},
        "meta": {"innerInstructions": [
            {"instructions": [{"programId": p} for p in inner]}] if inner else []},
    }


# --- known venues --------------------------------------------------------------

def test_jupiter_router_top_level():
    r = v.fill_venue_from_tx(_tx(top=[JUPITER], keys=[SYSTEM, TOKEN_PROG]))
    assert r["label"] == "jupiter router"
    assert r["programs"] == ["jupiter router"]


def test_raydium_via_inner_instructions():
    r = v.fill_venue_from_tx(_tx(top=[JUPITER], inner=[RAYDIUM],
                                 keys=[SYSTEM]))
    # jupiter entered first (top-level) so it wins the label; raydium is listed
    assert r["label"] == "jupiter router"
    assert r["programs"] == ["jupiter router", "raydium"]


def test_pump_amm_alone():
    r = v.fill_venue_from_tx(_tx(top=[PUMP_AMM], keys=[SYSTEM]))
    assert r["label"] == "pump.fun amm"


def test_orca_from_account_keys_only():
    # Some encodings surface the venue only in accountKeys.
    r = v.fill_venue_from_tx(_tx(top=[SYSTEM], keys=[ORCA, TOKEN_PROG]))
    assert r["label"] == "orca whirlpool"


def test_account_keys_as_dict_objects():
    tx = _tx(top=[RAYDIUM])
    tx["transaction"]["message"]["accountKeys"] = [
        {"pubkey": SYSTEM}, {"pubkey": RAYDIUM}]
    r = v.fill_venue_from_tx(tx)
    assert r["label"] == "raydium"


# --- unknown / empty -----------------------------------------------------------

def test_unknown_router_named_outright_not_invented():
    mystery = "MysteryProgram1111111111111111111111111111"
    r = v.fill_venue_from_tx(_tx(top=[mystery], keys=[SYSTEM]))
    assert r["label"] == f"program {mystery[:4]}\u2026{mystery[-4:]}"
    assert r["programs"] == [mystery]


def test_no_swap_program_labelled_plain_transfer():
    r = v.fill_venue_from_tx(_tx(top=[SYSTEM, TOKEN_PROG], keys=[SYSTEM]))
    assert r["label"] == "token transfer \u00b7 no swap program"
    assert r["programs"] == []


def test_none_and_garbage_tx_yield_unknown():
    assert v.fill_venue_from_tx(None) == {"label": None, "programs": []}
    assert v.fill_venue_from_tx({}) == {"label": None, "programs": []}
    assert v.fill_venue_from_tx({"transaction": None}) == {
        "label": None, "programs": []}


# --- fetch wrapper (mocked RPC) ------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_fill_venue_uses_get_transaction(monkeypatch):
    seen = {}

    async def fake_get_tx(sig):
        seen["sig"] = sig
        return _tx(top=[RAYDIUM], keys=[SYSTEM])

    monkeypatch.setattr(v.solana, "get_transaction", fake_get_tx)
    r = await v.fetch_fill_venue("SIGabc")
    assert seen["sig"] == "SIGabc"
    assert r["label"] == "raydium"


@pytest.mark.asyncio
async def test_fetch_fill_venue_fail_soft_on_rpc_none(monkeypatch):
    async def fake_get_tx(sig):
        return None

    monkeypatch.setattr(v.solana, "get_transaction", fake_get_tx)
    assert await v.fetch_fill_venue("SIGabc") == {"label": None, "programs": []}


@pytest.mark.asyncio
async def test_fetch_fill_venue_fail_soft_on_raise(monkeypatch):
    async def boom(sig):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(v.solana, "get_transaction", boom)
    assert await v.fetch_fill_venue("SIGabc") == {"label": None, "programs": []}


@pytest.mark.asyncio
async def test_fetch_fill_venue_empty_signature_short_circuits(monkeypatch):
    async def should_not_be_called(sig):
        raise AssertionError("get_transaction must not be called")

    monkeypatch.setattr(v.solana, "get_transaction", should_not_be_called)
    assert await v.fetch_fill_venue("") == {"label": None, "programs": []}
