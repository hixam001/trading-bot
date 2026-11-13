"""§95 S2: the guard and consensus use the same explicit payer identity."""
import base64
import copy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

from live_execution import sim_guard, solana
from live_execution.trade_intent import TradeIntent
from tests._helpers import intent_from_quote_for_tests
from tests.test_audit_81_c import _make_dummy_quote
from tests.test_section94_consensus import WALLET, FOREIGN, MINT, ADDRESSES, _snapshots, _token


async def test_intent_payer_mismatch_refuses_before_any_rpc(monkeypatch):
    payer = Keypair()
    payer_pubkey = str(payer.pubkey())
    quote = _make_dummy_quote()
    intent = intent_from_quote_for_tests(quote, str(Keypair().pubkey()))
    tx = VersionedTransaction(MessageV0.try_compile(payer.pubkey(), [], [], Hash.default()), [payer])
    calls = AsyncMock(side_effect=AssertionError("RPC must not run"))
    monkeypatch.setattr(solana, "get_multiple_accounts", calls)
    monkeypatch.setattr(solana, "simulate_transaction", calls)
    monkeypatch.setattr(solana, "rpc", calls)
    recorded = []
    monkeypatch.setattr("live_execution.safety_counters.record_safety_refusal", lambda *args: recorded.append(args))
    monkeypatch.setattr("alerts.alert_nowait", lambda *args, **kwargs: None)
    with pytest.raises(sim_guard.SimulationRefused) as caught:
        await sim_guard.simulate_and_assert(base64.b64encode(bytes(tx)).decode(), quote, payer_pubkey, intent)
    calls.assert_not_called()
    assert caught.value.category == "guard_refused"
    assert "intent.wallet" in str(caught.value) and "payer" in str(caught.value)
    assert len(recorded) == 1 and recorded[0][0] == "guard_refused"


def test_consensus_with_intent_and_pre_state_requires_explicit_payer(caplog):
    first, _ = _snapshots()
    pre = dict(zip(ADDRESSES, copy.deepcopy(first["post_accounts"])))
    intent = TradeIntent("buy", WALLET, MINT, FOREIGN, 1000, 50, 1, 1)
    assert not solana.responses_agree(first, first, [WALLET], ADDRESSES, pre_state=pre, intent=intent)
    assert "category=malformed" in caplog.text


def test_check_f_consensus_uses_explicit_payer_even_when_intent_wallet_differs(caplog):
    first, _ = _snapshots()
    second = copy.deepcopy(first)
    pre = dict(zip(ADDRESSES, copy.deepcopy(first["post_accounts"])))
    pre["wallet-token"] = _token(WALLET, 2001)
    second["post_accounts"][2] = _token(FOREIGN, 100_001)
    intent = replace(TradeIntent("buy", WALLET, MINT, FOREIGN, 1000, 50, 1, 1), wallet=FOREIGN)
    assert not solana.responses_agree(first, second, [WALLET], ADDRESSES,
                                      wallet_address=WALLET, pre_state=pre, intent=intent)
    assert "category=vault_amount" in caplog.text
