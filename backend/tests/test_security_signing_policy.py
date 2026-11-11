"""Offline policy regressions using real solders serialization and token decoder.

Snapshots are mocked RPC observations, not evidence of approved-DEX native
program execution. No transaction reaches a signing/broadcast boundary here.
"""
import base64
import copy

import httpx
import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from live_execution import config, sim_guard, solana
from live_execution.jupiter_executor import _SPL_TOKEN_PROGRAM_ID, _WSOL_MINT
from tests.helpers import make_intent_from_quote
from tests.test_audit_78_b import (
    ATA_PROGRAM_ID, SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID, USDC_MINT,
    _make_acc_info, _make_dummy_tx, _make_token_data,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("old_before,old_after,credit,passes", [
    (1000, 0, 100, False),  # original gross-credit arithmetic lead
    (100, 0, 200, True),
    (100, 0, 199, False),
    (100, 100, 100, True),
])
async def test_output_minimum_is_net_wallet_gain(monkeypatch, old_before, old_after, credit, passes):
    payer, extra = Keypair(), Keypair().pubkey()
    mint = Keypair().pubkey()
    tx_b64, _ = _make_dummy_tx(payer, extra)
    output_ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN_PROGRAM_ID), bytes(mint)], ATA_PROGRAM_ID,
    )
    quote = {"inputMint": str(USDC_MINT), "outputMint": str(mint),
             "inAmount": "1000", "otherAmountThreshold": "100"}

    def snapshot(addresses, post):
        result = []
        for addr in addresses:
            if addr == str(payer.pubkey()):
                result.append({"lamports": 1_000_000_000 - (5000 if post else 0),
                               "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]})
            elif addr == str(extra):
                result.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID,
                    _make_token_data(mint, payer.pubkey(), old_after if post else old_before)))
            elif addr == str(output_ata) and post:
                result.append(_make_acc_info(2_000_000, TOKEN_PROGRAM_ID,
                    _make_token_data(mint, payer.pubkey(), credit)))
            else:
                result.append(None)
        return result

    async def pre(addresses, **kwargs):
        assert kwargs["wallet_owned_addresses"] == addresses
        return snapshot(addresses, False)

    async def simulate(_tx, addresses, **kwargs):
        assert kwargs["wallet_owned_addresses"] == addresses
        return {"err": None, "post_accounts": snapshot(addresses, True)}

    monkeypatch.setattr(solana, "get_multiple_accounts", pre)
    monkeypatch.setattr(solana, "simulate_transaction", simulate)
    call = sim_guard.simulate_and_assert(tx_b64, quote, str(payer.pubkey()),
        intent=make_intent_from_quote(quote, str(payer.pubkey())))
    if passes:
        assert (await call)["output_increase"] == old_after + credit - old_before
    else:
        with pytest.raises(sim_guard.SimulationRefused, match="output token increase"):
            await call


@pytest.mark.asyncio
@pytest.mark.parametrize("old_wrapped,native_trade,passes", [(1000, 100, True), (1000, 0, False), (0, 0, False)])
async def test_native_output_excludes_existing_wrapped_balance_and_rent(monkeypatch, old_wrapped, native_trade, passes):
    payer, extra = Keypair(), Keypair().pubkey()
    mint = Pubkey.from_string(_WSOL_MINT)
    tx_b64, _ = _make_dummy_tx(payer, extra)
    rent = 2_039_280
    quote = {"inputMint": str(USDC_MINT), "outputMint": str(mint),
             "inAmount": "1000", "otherAmountThreshold": "100"}

    async def pre(addresses, **kwargs):
        return [
            {"lamports": 1_000_000_000, "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]}
            if addr == str(payer.pubkey()) else
            _make_acc_info(rent + old_wrapped, TOKEN_PROGRAM_ID,
                           _make_token_data(mint, payer.pubkey(), old_wrapped))
            if addr == str(extra) else None for addr in addresses
        ]

    async def simulate(_tx, addresses, **kwargs):
        return {"err": None, "post_accounts": [
            {"lamports": 1_000_000_000 + rent + old_wrapped + native_trade,
             "owner": str(SYSTEM_PROGRAM_ID), "data": ["", "base64"]}
            if addr == str(payer.pubkey()) else None for addr in addresses
        ]}

    monkeypatch.setattr(solana, "get_multiple_accounts", pre)
    monkeypatch.setattr(solana, "simulate_transaction", simulate)
    call = sim_guard.simulate_and_assert(tx_b64, quote, str(payer.pubkey()),
        intent=make_intent_from_quote(quote, str(payer.pubkey())))
    if passes:
        assert (await call)["output_increase"] == native_trade
    else:
        with pytest.raises(sim_guard.SimulationRefused, match="output token increase"):
            await call


def _quorum_fixture():
    payer, mint = Keypair().pubkey(), Keypair().pubkey()
    token = _make_acc_info(2_000_000, TOKEN_PROGRAM_ID, _make_token_data(mint, payer, 1000))
    return {"err": None, "post_accounts": [
        {"owner": str(SYSTEM_PROGRAM_ID), "lamports": 1_000_000_000, "data": ["", "base64"]}, token,
    ]}


@pytest.mark.parametrize("change", ["amount", "mint", "owner", "missing", "malformed", "control"])
def test_consensus_covers_additional_account_even_when_not_in_ata_set(change):
    original = _quorum_fixture()
    other = copy.deepcopy(original)
    token = other["post_accounts"][1]
    if change == "missing":
        other["post_accounts"][1] = None
    elif change == "malformed":
        token["data"] = ["!!", "base64"]
    elif change == "owner":
        token["owner"] = str(SYSTEM_PROGRAM_ID)
    else:
        raw = bytearray(base64.b64decode(token["data"][0]))
        if change == "amount":
            raw[64:72] = (0).to_bytes(8, "little")
        elif change == "mint":
            raw[:32] = bytes(Keypair().pubkey())
        else:
            raw[108] = 2
        token["data"][0] = base64.b64encode(raw).decode()
    for left, right in ((original, other), (other, original)):
        assert not solana.responses_agree(left, right, ["payer"], ["payer", "extra"])
    assert solana.responses_agree(original, original, ["payer"], ["payer", "extra"])


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["pre", "post"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_real_quorum_helpers_refuse_extra_account_disagreement(monkeypatch, operation, reverse):
    first = _quorum_fixture()
    second = copy.deepcopy(first)
    second["post_accounts"][1] = None
    responses = [first, second] if not reverse else [second, first]

    def handler(request):
        response = responses[0 if request.url.host == "first.invalid" else 1]
        if operation == "pre":
            value = response["post_accounts"]
        else:
            value = {"err": None, "accounts": response["post_accounts"]}
        return httpx.Response(200, json={"result": {"value": value}})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(solana, "_get_async_client", lambda **kwargs: httpx.AsyncClient(transport=transport))
    monkeypatch.setattr(config, "SIM_MIN_AGREEING_RPCS", 2)
    endpoints = ["https://first.invalid", "https://second.invalid"]
    with pytest.raises(solana.SimulationError, match="quorum"):
        if operation == "pre":
            await solana.get_multiple_accounts(["payer", "extra"], endpoints=endpoints, wallet_owned_addresses=["payer"])
        else:
            await solana.simulate_transaction("dummy", ["payer", "extra"], endpoints=endpoints, wallet_owned_addresses=["payer"])


@pytest.mark.parametrize("bad", [True, -1, "100", None])
def test_malformed_account_lamports_are_not_agreement(bad):
    response = _quorum_fixture()
    response["post_accounts"][1]["lamports"] = bad
    assert not solana.responses_agree(response, response, ["payer"], ["payer", "extra"])
