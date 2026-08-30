"""
tests for live_execution.solana.get_token_balances — A2 chain-derived balance
truth. Hermetic: the rpc() helper is faked, no network.

Contract: {mint: ui_amount} for balances > 0 across BOTH token programs;
{} when the wallet holds nothing (an answered read); None when no RPC
answered at all (unknown is never empty).
"""
from __future__ import annotations

import pytest

from live_execution import solana


def _entry(mint, ui=None, amount=None, decimals=None):
    ta = {}
    if ui is not None:
        ta["uiAmount"] = ui
    if amount is not None:
        ta["amount"] = str(amount)
    if decimals is not None:
        ta["decimals"] = decimals
    return {"account": {"data": {"parsed": {"info": {
        "mint": mint, "tokenAmount": ta}}}}}


def _patch_rpc(monkeypatch, results_by_program):
    """results_by_program: {program_id: rpc result or None}"""
    async def fake_rpc(method, params, timeout=15.0, endpoints=None):
        assert method == "getTokenAccountsByOwner"
        program = params[1]["programId"]
        return results_by_program.get(program)
    monkeypatch.setattr(solana, "rpc", fake_rpc)


async def test_balances_parsed_from_both_programs(monkeypatch):
    _patch_rpc(monkeypatch, {
        solana._TOKEN_PROGRAM_ID: {"value": [_entry("MINTA", ui=12.5)]},
        solana._TOKEN_2022_PROGRAM_ID: {"value": [_entry("MINTB", ui=3.0)]},
    })
    got = await solana.get_token_balances("OWNER")
    assert got == {"MINTA": 12.5, "MINTB": 3.0}


async def test_same_mint_summed_across_accounts(monkeypatch):
    _patch_rpc(monkeypatch, {
        solana._TOKEN_PROGRAM_ID: {"value": [
            _entry("MINTA", ui=10.0), _entry("MINTA", ui=2.5)]},
        solana._TOKEN_2022_PROGRAM_ID: {"value": []},
    })
    got = await solana.get_token_balances("OWNER")
    assert got == {"MINTA": 12.5}


async def test_null_ui_amount_falls_back_to_raw_and_decimals(monkeypatch):
    _patch_rpc(monkeypatch, {
        solana._TOKEN_PROGRAM_ID: {"value": [
            _entry("MINTA", ui=None, amount=2500000, decimals=6)]},
        solana._TOKEN_2022_PROGRAM_ID: {"value": []},
    })
    got = await solana.get_token_balances("OWNER")
    assert got["MINTA"] == pytest.approx(2.5)


async def test_zero_and_dustless_rows_are_dropped(monkeypatch):
    _patch_rpc(monkeypatch, {
        solana._TOKEN_PROGRAM_ID: {"value": [
            _entry("MINTA", ui=0.0), _entry("MINTB", ui=1.0)]},
        solana._TOKEN_2022_PROGRAM_ID: {"value": []},
    })
    got = await solana.get_token_balances("OWNER")
    assert got == {"MINTB": 1.0}


async def test_empty_wallet_is_empty_dict_not_none(monkeypatch):
    _patch_rpc(monkeypatch, {
        solana._TOKEN_PROGRAM_ID: {"value": []},
        solana._TOKEN_2022_PROGRAM_ID: {"value": []},
    })
    got = await solana.get_token_balances("OWNER")
    assert got == {}


async def test_all_rpcs_fail_returns_none(monkeypatch):
    _patch_rpc(monkeypatch, {
        solana._TOKEN_PROGRAM_ID: None,
        solana._TOKEN_2022_PROGRAM_ID: None,
    })
    assert await solana.get_token_balances("OWNER") is None


async def test_malformed_entries_are_skipped_not_fatal(monkeypatch):
    _patch_rpc(monkeypatch, {
        solana._TOKEN_PROGRAM_ID: {"value": [
            {"account": {"data": {"parsed": {"info": {"mint": "MINTA"}}}}},
            {"garbage": True},
            _entry("MINTB", ui=4.0),
        ]},
        solana._TOKEN_2022_PROGRAM_ID: {"value": []},
    })
    got = await solana.get_token_balances("OWNER")
    assert got == {"MINTB": 4.0}
