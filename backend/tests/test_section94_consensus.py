"""§94 S3: real account layouts; hot market state and hostile variants."""
import base64
import copy
import inspect
import struct

import httpx
import pytest
from solders.pubkey import Pubkey

from live_execution import solana
from live_execution.jupiter_executor import _SPL_TOKEN_PROGRAM_ID as TOKEN, _TOKEN_2022_PROGRAM_ID as TOKEN2022
from live_execution.trade_intent import TradeIntent
from tests._helpers import encode_token_account_data

WALLET = str(Pubkey.from_bytes(bytes([1]) * 32))
FOREIGN = str(Pubkey.from_bytes(bytes([2]) * 32))
MINT = str(Pubkey.from_bytes(bytes([3]) * 32))
ADDRESSES = [WALLET, 'wallet-token', 'pool-vault', 'hot-pool']


def _account(owner, data=b'', lamports=2_000_000):
    return dict(owner=owner, executable=False, lamports=lamports, data=[base64.b64encode(data).decode(), 'base64'])


def _token(owner, amount=1000):
    return _account(TOKEN, encode_token_account_data(MINT, owner, amount))


def _snapshots():
    first = {'err': None, 'slot': 100, 'post_accounts': [_account('11111111111111111111111111111111', lamports=1_000_000_000), _token(WALLET), _token(FOREIGN, 100_000), _account(FOREIGN, b'pool-slot-100')]}
    second = copy.deepcopy(first)
    second['slot'] = 101
    second['post_accounts'][2] = _token(FOREIGN, 102_000)
    second['post_accounts'][2]['lamports'] += 100_000
    second['post_accounts'][3] = _account(FOREIGN, b'pool-slot-101', lamports=9_000_000)
    return first, second


def _agree(a, b, **kwargs):
    # On the baseline, exercise the original comparator without the new context.
    if 'wallet_address' in inspect.signature(solana.responses_agree).parameters:
        kwargs['wallet_address'] = WALLET
    return solana.responses_agree(a, b, [WALLET], ADDRESSES, **kwargs)


def test_hot_pool_one_slot_apart_agrees_despite_market_data_and_vault_move():
    first, second = _snapshots()
    assert _agree(first, second)
    assert _agree(second, first)


@pytest.mark.parametrize('field', ['mint', 'owner', 'delegate', 'state', 'close_authority', 'delegated_amount', 'is_native', 'extension'])
@pytest.mark.parametrize('index', [1, 2])
def test_every_token_control_field_refuses_even_on_foreign_account(field, index):
    first, _ = _snapshots()
    second = copy.deepcopy(first)
    raw = bytearray(base64.b64decode(second['post_accounts'][index]['data'][0]))
    offsets = {'mint': 0, 'owner': 32, 'delegate': 72, 'state': 108, 'close_authority': 129, 'delegated_amount': 121, 'is_native': 109}
    if field == 'extension':
        first['post_accounts'][index]['owner'] = TOKEN2022
        second['post_accounts'][index]['owner'] = TOKEN2022
        first['post_accounts'][index]['data'][0] = base64.b64encode(bytes(raw) + b'\x02' + struct.pack('<HH', 77, 1) + b'\x00').decode()
        raw.extend(b'\x02' + struct.pack('<HH', 77, 1) + b'\x01')
    else:
        raw[offsets[field]] ^= 1
    second['post_accounts'][index]['data'][0] = base64.b64encode(raw).decode()
    assert not _agree(first, second)


@pytest.mark.parametrize('change', ['wallet_amount', 'wallet_lamports', 'pool_owner', 'pool_executable', 'pool_length', 'missing', 'extra', 'short', 'non_list', 'missing_err', 'bad_data', 'bad_lamports'])
def test_hostile_account_snapshots_refuse(change):
    first, _ = _snapshots()
    second = copy.deepcopy(first)
    if change == 'wallet_amount':
        second['post_accounts'][1] = _token(WALLET, 900)
    elif change == 'wallet_lamports':
        second['post_accounts'][0]['lamports'] -= 100_000
    elif change.startswith('pool_'):
        field, val = {'pool_owner': ('owner', WALLET), 'pool_executable': ('executable', True), 'pool_length': ('data', ['AA==', 'base64'])}[change]
        second['post_accounts'][3][field] = val
    elif change == 'missing':
        second['post_accounts'][3] = None
    elif change == 'extra':
        second['post_accounts'].append(None)
    elif change == 'short':
        second['post_accounts'].pop()
    elif change == 'non_list':
        second['post_accounts'] = {}
    elif change == 'missing_err':
        second.pop('err')
    elif change == 'bad_data':
        second['post_accounts'][3]['data'][0] = '!'
    elif change == 'bad_lamports':
        second['post_accounts'][3]['lamports'] = None
    assert not _agree(first, second)


def test_transfer_fee_withheld_amount_is_the_only_exempt_extension_payload():
    first, _ = _snapshots()
    second = copy.deepcopy(first)
    for snapshot, withheld in ((first, 1), (second, 9)):
        account = snapshot['post_accounts'][2]
        account['owner'] = TOKEN2022
        raw = base64.b64decode(account['data'][0])
        # Token-2022 account discriminator=2; TransferFeeAmount TLV type=2, len=8.
        account['data'][0] = base64.b64encode(raw + b'\x02' + struct.pack('<HHQ', 2, 8, withheld)).decode()
    assert _agree(first, second)
    raw = bytearray(base64.b64decode(second['post_accounts'][2]['data'][0]))
    raw[166] = 3  # Changing extension identity is never market state.
    second['post_accounts'][2]['data'][0] = base64.b64encode(raw).decode()
    assert not _agree(first, second)


def test_foreign_amounts_still_refuse_when_check_f_verdict_differs():
    first, _ = _snapshots()
    second = copy.deepcopy(first)
    pre = dict(zip(ADDRESSES, copy.deepcopy(first['post_accounts'])))
    pre['wallet-token'] = _token(WALLET, 2001)
    first['post_accounts'][2] = _token(FOREIGN, 100_000)
    second['post_accounts'][2] = _token(FOREIGN, 100_001)
    intent = TradeIntent('buy', WALLET, MINT, FOREIGN, 1000, 50, 1, 1)
    assert not _agree(first, second, pre_state=pre, intent=intent)


async def test_consensus_refusals_are_structured_and_bounded(caplog):
    first, _ = _snapshots()
    second = copy.deepcopy(first)
    second['post_accounts'][1] = _token(WALLET, 900)
    before = solana.get_consensus_status()['refused']['buy']['wallet_amount']
    assert not _agree(first, second, side='buy')
    assert 'SIM_CONSENSUS_REFUSED side=buy category=wallet_amount addr=wallet-t' in caplog.text
    assert solana.get_consensus_status()['refused']['buy']['wallet_amount'] == before + 1
    from api.main import health_check
    assert 'simulation_consensus' in await health_check()


async def test_real_quorum_accepts_hot_pool_snapshots(monkeypatch):
    first, second = _snapshots()
    def handler(request):
        snapshot = first if request.url.host == 'first.invalid' else second
        return httpx.Response(200, json={'result': {'value': {'err': None, 'accounts': snapshot['post_accounts']}}})
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(solana, '_get_async_client', lambda **kwargs: httpx.AsyncClient(transport=transport))
    result = await solana.simulate_transaction('dummy', ADDRESSES, endpoints=['https://first.invalid', 'https://second.invalid'], wallet_owned_addresses=[WALLET], wallet_address=WALLET)
    assert result['agreeing_count'] == 2
