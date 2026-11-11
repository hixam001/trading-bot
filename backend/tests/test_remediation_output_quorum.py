"""Native/wrapped accounting and corrected quorum positive/refusal controls."""
import base64

import httpx
import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from live_execution import sim_guard, solana
from live_execution.jupiter_executor import _WSOL_MINT
from tests.helpers import make_intent_from_quote
from tests.test_audit_78_b import (
    ATA_PROGRAM_ID, SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID, USDC_MINT,
    _make_acc_info, _make_dummy_tx, _make_token_data,
)
from tests.test_security_signing_policy import _quorum_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize('output,native_debit,passes', [
    (100, 2_039_280 + 5100, False),
    (2_039_280 + 6000, 2_039_280 + 5000, True),
    (0, 0, False),  # pool-funded new rent alone is not trade output
    (100, 100, False),  # pool-funded rent cannot hide a wallet-funded wrap
])
async def test_wrapped_output_nets_native_funding_rent_and_fees(monkeypatch, output, native_debit, passes):
    payer = Keypair()
    owner = str(payer.pubkey())
    mint = Pubkey.from_string(_WSOL_MINT)
    tx, _ = _make_dummy_tx(payer)
    ata, _ = Pubkey.find_program_address([bytes(payer.pubkey()), bytes(TOKEN_PROGRAM_ID), bytes(mint)], ATA_PROGRAM_ID)
    rent = 2_039_280
    # For 100 raw output, wallet pays its own 100 principal: no trade output.
    # For rent+6000 output, wallet pays rent + fee: conservative net is 1000.
    async def pre(addresses, **kwargs):
        return [{'owner': str(SYSTEM_PROGRAM_ID), 'lamports': 1_000_000_000, 'data': ['', 'base64']}
                if address == owner else None for address in addresses]
    async def post(_tx, addresses, **kwargs):
        return {'err': None, 'post_accounts': [
            {'owner': str(SYSTEM_PROGRAM_ID), 'lamports': 1_000_000_000 - native_debit, 'data': ['', 'base64']}
            if address == owner else
            _make_acc_info(rent + output, TOKEN_PROGRAM_ID, _make_token_data(mint, payer.pubkey(), output))
            if address == str(ata) else None for address in addresses]}
    monkeypatch.setattr(solana, 'get_multiple_accounts', pre)
    monkeypatch.setattr(solana, 'simulate_transaction', post)
    quote = {'inputMint': str(USDC_MINT), 'outputMint': str(mint), 'inAmount': '1000', 'otherAmountThreshold': '100'}
    call = sim_guard.simulate_and_assert(tx, quote, owner, intent=make_intent_from_quote(quote, owner))
    if passes:
        assert (await call)['output_increase'] == 1000
    else:
        with pytest.raises(sim_guard.SimulationRefused, match='below minimum'):
            await call


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['pre', 'post'])
@pytest.mark.parametrize('malformed', [False, True])
async def test_identical_quorum_and_single_sell_malformed_refusal(monkeypatch, operation, malformed):
    response = _quorum_fixture()
    if malformed:
        response['post_accounts'][1]['lamports'] = True
    def handle(request):
        value = response['post_accounts'] if operation == 'pre' else {'err': None, 'accounts': response['post_accounts']}
        return httpx.Response(200, json={'result': {'value': value}})
    monkeypatch.setattr(solana, '_get_async_client', lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(solana.config, 'SIM_MIN_AGREEING_RPCS', 2)
    monkeypatch.setattr(solana.config, 'SIM_SELL_ALLOW_SINGLE_RPC', True)
    async def run():
        kwargs = {'endpoints': ['https://first.invalid'], 'side': 'sell', 'wallet_owned_addresses': ['payer']}
        if operation == 'pre':
            return await solana.get_multiple_accounts(['payer', 'extra'], **kwargs)
        return await solana.simulate_transaction('dummy', ['payer', 'extra'], **kwargs)
    if malformed:
        with pytest.raises(solana.SimulationError, match='failed across all RPCs'):
            await run()
    else:
        result = await run()
        assert (result if operation == 'pre' else result['post_accounts']) == response['post_accounts']


@pytest.mark.asyncio
async def test_simulation_missing_error_field_is_malformed(monkeypatch):
    def handle(request):
        return httpx.Response(200, json={'result': {'value': {'accounts': [None]}}})
    monkeypatch.setattr(solana, '_get_async_client', lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    with pytest.raises(solana.SimulationError, match='malformed result value'):
        await solana.simulate_transaction('dummy', ['payer'], endpoints=['https://first.invalid'], side='sell')


@pytest.mark.asyncio
@pytest.mark.parametrize('decimals,expected', [(0, 0), (255, 255), (256, None), (-1, None), (True, None), (6.0, None)])
async def test_chain_decimals_native_domain(monkeypatch, decimals, expected):
    async def rpc(*args, **kwargs):
        return {'value': {'decimals': decimals}}
    monkeypatch.setattr(solana, 'rpc', rpc)
    assert await solana.get_mint_decimals('dummy-mint') == expected
