"""
tests/test_fill_actuals.py — A4 (repo audit): fill-amount attribution.

The ledger must be able to record the ACTUAL amounts a confirmed fill
moved (net pre/post token-balance deltas read off the confirmed tx)
instead of the pre-trade quote snapshot — real slippage means the fill
rarely equals the quote. The parser is pure and never raises: unknown is
None, never a fabricated number (fail-soft to the quote snapshot).
"""
from __future__ import annotations

import pytest

from live_execution import executor
from live_execution.venue import token_deltas_from_tx

USDC = executor._BACKEND_USDC_MINT   # config.USDC_MINT — imported, never hand-typed
MINT = "So11111111111111111111111111111111111111112"


def _bal(mint: str, ui) -> dict:
    return {"mint": mint,
            "uiTokenAmount": {"uiAmount": ui, "amount": "0", "decimals": 6}}


def _tx(pre_token, post_token, meta=True) -> dict:
    return {
        "transaction": {"message": {"accountKeys": []}},
        "meta": ({"preTokenBalances": pre_token,
                  "postTokenBalances": post_token} if meta else None),
    }


# --- the pure parser ---------------------------------------------------------

def test_buy_deltas_summed_per_mint():
    d = token_deltas_from_tx(_tx(
        [_bal(MINT, 0.0), _bal(USDC, 10.0)],
        [_bal(MINT, 5.0), _bal(USDC, 0.0)],
    ))
    assert d[MINT] == pytest.approx(5.0)
    assert d[USDC] == pytest.approx(-10.0)


def test_multiple_accounts_per_mint_are_summed():
    d = token_deltas_from_tx(_tx(
        [_bal(MINT, 1.0), _bal(MINT, 2.0)],
        [_bal(MINT, 4.0), _bal(MINT, 5.0)],
    ))
    assert d[MINT] == pytest.approx(6.0)


def test_zero_deltas_dropped():
    d = token_deltas_from_tx(_tx(
        [_bal(USDC, 5.0)],
        [_bal(USDC, 5.0), _bal(MINT, 3.0)],
    ))
    assert d == {MINT: pytest.approx(3.0)}


def test_malformed_entries_skipped_not_fatal():
    d = token_deltas_from_tx(_tx(
        [None, "junk", _bal(MINT, 2.0)],
        [{"mint": MINT}, _bal(MINT, 7.0)],
    ))
    assert d[MINT] == pytest.approx(5.0)


@pytest.mark.parametrize("tx", [
    None, {}, {"meta": None}, {"meta": {}},
    _tx("not-a-list", []), _tx([], "not-a-list"),
])
def test_unparseable_tx_yields_none(tx):
    assert token_deltas_from_tx(tx) is None


# --- _fill_actuals: signed interpretation per side (RPC stubbed) -------------

@pytest.mark.asyncio
async def test_fill_actuals_buy_interpretation(monkeypatch):
    async def fake_get_tx(sig):
        return _tx([_bal(MINT, 0.0), _bal(USDC, 10.0)],
                   [_bal(MINT, 4.8), _bal(USDC, 0.0)])

    monkeypatch.setattr(executor.solana, "get_transaction", fake_get_tx)
    out = await executor._fill_actuals("buy", MINT, "SIGX")
    # wallet RECEIVED 4.8 tokens, PAID 10 USDC
    assert out["tokens_delta"] == pytest.approx(4.8)
    assert out["usdc_delta"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_fill_actuals_sell_interpretation(monkeypatch):
    async def fake_get_tx(sig):
        return _tx([_bal(MINT, 4.8), _bal(USDC, 0.0)],
                   [_bal(MINT, 0.0), _bal(USDC, 9.1)])

    monkeypatch.setattr(executor.solana, "get_transaction", fake_get_tx)
    out = await executor._fill_actuals("sell", MINT, "SIGX")
    # wallet GAVE UP 4.8 tokens, RECEIVED 9.1 USDC
    assert out["tokens_delta"] == pytest.approx(4.8)
    assert out["usdc_delta"] == pytest.approx(9.1)


@pytest.mark.asyncio
async def test_fill_actuals_fail_soft(monkeypatch):
    async def boom(sig):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(executor.solana, "get_transaction", boom)
    assert await executor._fill_actuals("buy", MINT, "SIGX") == \
        {"tokens_delta": None, "usdc_delta": None}
    # an empty signature short-circuits to unreadable
    assert await executor._fill_actuals("sell", MINT, "") == \
        {"tokens_delta": None, "usdc_delta": None}