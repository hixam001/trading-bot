"""H17 immediate settlement uses strict outcomes at mocked send boundaries."""
from unittest.mock import AsyncMock

import pytest

from live_execution import executor as ex, solana
from live_execution.trade_intent import TradeIntent

SUCCESS = {"slot": 10, "err": None, "confirmationStatus": "confirmed"}
FAILURE = {"slot": 10, "err": {"InstructionError": [1, "InvalidAccountData"]}, "confirmationStatus": "finalized"}


@pytest.fixture
def boundary(monkeypatch):
    signed = solana.SignedSwap('local-dummy-signature', b'dummy-raw', _checked=True, blockhash='dummy-blockhash')
    monkeypatch.setattr(ex, 'sign_after_checks', AsyncMock(return_value=signed))
    monkeypatch.setattr(solana, 'send_checked_swap', AsyncMock(return_value=signed.signature))
    monkeypatch.setattr(solana, 'confirm_signature', AsyncMock(return_value={'confirmed': False, 'err': 'InvalidAccountData', 'slot': 10}))
    monkeypatch.setattr(solana, 'get_signature_status_all_strict', AsyncMock(return_value=[]))
    intent = TradeIntent('buy', 'dummy-wallet', 'dummy-input', 'dummy-output', 100, 50, 1, 1.)
    return intent


@pytest.mark.asyncio
@pytest.mark.parametrize('observations', [
    [], [solana.ABSENT, solana.UNREACHABLE], [FAILURE, solana.UNREACHABLE],
    [SUCCESS, FAILURE], [{'err': 'InvalidAccountData', 'confirmationStatus': 'processed', 'slot': 10}] * 2,
    [{'err': 'InvalidAccountData', 'confirmationStatus': 'confirmed'}] * 2,
    [{'err': {'UnknownError': True}, 'confirmationStatus': 'confirmed', 'slot': 10}] * 2,
])
@pytest.mark.parametrize('ambiguous', [False, True])
async def test_unresolved_negative_or_ambiguous_send_never_releases_intent(boundary, monkeypatch, observations, ambiguous):
    if ambiguous:
        monkeypatch.setattr(solana, 'send_checked_swap', AsyncMock(return_value=None))
    monkeypatch.setattr(solana, 'get_signature_status_all_strict', AsyncMock(return_value=observations))
    result = await ex._broadcast_and_confirm('dummy', object(), boundary)
    assert result.unresolved and result.status == 'failed'
    assert result.signature == 'local-dummy-signature'
    solana.send_checked_swap.assert_awaited_once()
    if ambiguous:
        solana.confirm_signature.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('observations,status,unresolved', [([solana.ABSENT,SUCCESS],'filled',False), ([FAILURE,FAILURE],'failed',False)])
async def test_strict_success_or_unanimous_failure_can_settle(boundary, monkeypatch, observations, status, unresolved):
    monkeypatch.setattr(solana, 'get_signature_status_all_strict', AsyncMock(return_value=observations))
    result = await ex._broadcast_and_confirm('dummy', object(), boundary)
    assert result.status == status and result.unresolved is unresolved
    if status == 'filled': assert result.slot == 10
    else: assert result.error == FAILURE['err']


@pytest.mark.asyncio
@pytest.mark.parametrize('confirmation',[None, {}, {'confirmed':True,'err':None,'slot':True}, {'confirmed':True,'err':None}, {'confirmed':1,'err':None,'slot':10}])
async def test_malformed_confirmation_cannot_adopt_or_release(boundary, monkeypatch, confirmation):
    monkeypatch.setattr(solana,'confirm_signature',AsyncMock(return_value=confirmation))
    result=await ex._broadcast_and_confirm('dummy',object(),boundary)
    assert result.unresolved
    solana.get_signature_status_all_strict.assert_awaited_once_with('local-dummy-signature')


@pytest.mark.asyncio
async def test_receipt_signature_mismatch_resolves_only_locally_signed_identity(boundary,monkeypatch):
    monkeypatch.setattr(solana,'send_checked_swap',AsyncMock(return_value='other-dummy-signature'))
    result=await ex._broadcast_and_confirm('dummy',object(),boundary)
    assert result.unresolved and result.signature=='local-dummy-signature'
    solana.confirm_signature.assert_not_awaited()
    solana.get_signature_status_all_strict.assert_awaited_once_with('local-dummy-signature')


@pytest.mark.asyncio
async def test_valid_immediate_confirmed_success_retains_existing_provider_policy(boundary,monkeypatch):
    monkeypatch.setattr(solana,'confirm_signature',AsyncMock(return_value={'confirmed':True,'err':None,'slot':10}))
    result=await ex._broadcast_and_confirm('dummy',object(),boundary)
    assert result.status=='filled' and result.slot==10
    solana.get_signature_status_all_strict.assert_not_awaited()


@pytest.mark.asyncio
async def test_negative_confirmation_keeps_durable_buy_exposure_across_retry(tmp_path, monkeypatch):
    from live_execution.models import ExecutionLedger
    from live_execution.tests.test_executor_memo_flow import _arm_balances, _arm_fill
    monkeypatch.setattr(ex.config, 'LIVE_TRADING_ENABLED', True)
    monkeypatch.setattr(ex.config, 'REQUIRE_MANUAL_CONFIRMATION', False)
    monkeypatch.setattr(ex.config, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(ex.wallet, 'load_keypair', lambda: object())
    monkeypatch.setattr(ex.wallet, 'verify_expected_address', lambda payer: None)
    monkeypatch.setattr(ex.wallet, 'pubkey_string', lambda payer: 'dummy-wallet')
    monkeypatch.setattr(solana, 'get_mint_decimals', AsyncMock(return_value=6))
    _arm_balances(monkeypatch)
    _arm_fill(monkeypatch, [])
    monkeypatch.setattr(solana, 'send_checked_swap', AsyncMock(return_value='FILLSIG'))
    monkeypatch.setattr(solana, 'confirm_signature', AsyncMock(return_value={'confirmed': False, 'slot': 10, 'err': 'InvalidAccountData'}))
    monkeypatch.setattr(solana, 'get_signature_status_all_strict', AsyncMock(return_value=[FAILURE,solana.UNREACHABLE]))
    path = tmp_path/'executions.json'
    ledger = ExecutionLedger(path)
    result = await ex.place_buy('MINT','DUMMY',1.5,output_decimals=6,idempotency_key='dummy-intent',ledger=ledger,reference_price_usd=1.)
    assert result.unresolved
    assert ledger.get_by_idempotency_key('dummy-intent').status == 'pending'
    assert ledger.open_positions() == {'MINT':1.5}
    restored = ExecutionLedger(path)
    await ex.place_buy('MINT','DUMMY',1.5,output_decimals=6,idempotency_key='dummy-intent',ledger=restored,reference_price_usd=1.)
    solana.send_checked_swap.assert_awaited_once()
    assert restored.get_by_idempotency_key('dummy-intent').status == 'pending'
