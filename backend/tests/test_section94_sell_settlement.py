"""§94 S2: confirmed exits settle even after independent ledger closure."""
from unittest.mock import AsyncMock
import logging

import pytest

from live_execution import executor as ex, solana
from live_execution.commit_log import CommitLog
from live_execution.models import ExecutionLedger
from tests.test_security_sell_recovery import MINT, SUCCESS, sell_env


def _pending(commits):
    rec = commits.seal('sell', {'mint': MINT, 'symbol': 'DUMMY'})
    commits.record_sell_attempt(rec['hash'], 'confirmed-signature', 'blockhash', dict(wallet='dummy-wallet', fraction=1, tokens=90, proceeds_usd=100, full_close=True))
    return rec


async def test_confirmed_sell_after_oob_close_settles_once_without_pnl_duplication(sell_env, monkeypatch, caplog):
    ledger, commits = sell_env
    rec = _pending(commits)
    with caplog.at_level(logging.WARNING):
        ledger.close_out_of_band(MINT, proceeds_usd=100, note='operator fixture')
    monkeypatch.setattr(solana, 'get_signature_status_all_strict', AsyncMock(return_value=[SUCCESS, SUCCESS]))
    send = AsyncMock()
    monkeypatch.setattr(ex, '_broadcast_and_confirm', send)
    first = await ex.place_sell(MINT, 'DUMMY', 1, ledger=ExecutionLedger(ledger.path))
    assert first.status == 'blocked'
    assert first.reason == 'sell confirmed on-chain; position already closed in ledger'
    assert not first.unresolved
    assert not commits.pending_sells(MINT)
    for _ in range(2):
        result = await ex.place_sell(MINT, 'DUMMY', 1, ledger=ExecutionLedger(ledger.path))
        assert result.status == 'blocked' and result.reason == 'no position to sell'
    rows = ledger._load()
    closes = [r for r in rows if r['kind'] == 'close']
    assert len(closes) == 1
    assert closes[0]['pnl_usd'] == 10
    assert ledger.realized_pnl_today() == 10
    assert 'pending sell' in caplog.text and 'confirmed-signature' in caplog.text
    assert 'confirmed-signature' in closes[0]['note']
    row = commits.all()[0]
    assert row['status'] == 'bound'
    assert row['sell_attempt']['status'] == 'confirmed'
    assert any(n['text'] == 'ledger_already_closed: confirmed-signature' for n in row['notes'])
    send.assert_not_awaited()


async def test_immediate_confirmed_sell_after_oob_close_does_not_raise_or_report_filled(sell_env, monkeypatch):
    ledger, commits = sell_env
    async def filled(*args, **kwargs):
        ledger.close_out_of_band(MINT, proceeds_usd=100, note='operator fixture')
        return ex.OrderResult(status='filled', signature='immediate-signature')
    monkeypatch.setattr(ex, '_broadcast_and_confirm', filled)
    result = await ex.place_sell(MINT, 'DUMMY', 1, ledger=ledger)
    assert result.status == 'blocked'
    assert result.reason == 'sell confirmed on-chain; position already closed in ledger'
    assert not commits.pending_sells(MINT)
    assert len([r for r in ledger._load() if r['kind'] == 'close']) == 1
    assert ledger.realized_pnl_today() == 10


@pytest.mark.parametrize('immediate', [False, True])
@pytest.mark.parametrize('error', [ValueError('fixture accounting failure'), OSError('fixture write failure')])
async def test_accounting_failure_is_unresolved_then_converges(sell_env, monkeypatch, immediate, error):
    ledger, commits = sell_env
    if not immediate:
        _pending(commits)
    monkeypatch.setattr(solana, 'get_signature_status_all_strict', AsyncMock(return_value=[SUCCESS, SUCCESS]))
    send = AsyncMock(return_value=ex.OrderResult(status='filled', signature='confirmed-signature'))
    monkeypatch.setattr(ex, '_broadcast_and_confirm', send)
    reduce = ledger.reduce_position
    def fail_once(*args, **kwargs):
        monkeypatch.setattr(ledger, 'reduce_position', reduce)
        raise error
    monkeypatch.setattr(ledger, 'reduce_position', fail_once)
    first = await ex.place_sell(MINT, 'DUMMY', 1, ledger=ledger)
    assert first.status == 'failed' and first.unresolved
    assert commits.pending_sells(MINT)
    assert not [r for r in ledger._load() if r['kind'] == 'close']
    result = await ex.place_sell(MINT, 'DUMMY', 1, ledger=ExecutionLedger(ledger.path))
    assert result.status == 'filled'
    assert not commits.pending_sells(MINT)
    assert len([r for r in ledger._load() if r['kind'] == 'close']) == 1
    assert send.await_count == int(immediate)


@pytest.mark.parametrize('method', ['mark_close', 'reduce_position', 'close_out_of_band'])
def test_no_open_position_has_specific_value_error_subclass(tmp_path, method):
    from live_execution.models import NoOpenPosition
    ledger = ExecutionLedger(tmp_path / 'empty.json')
    args = (MINT, 1, 1) if method == 'reduce_position' else (MINT, 1)
    with pytest.raises(NoOpenPosition) as caught:
        getattr(ledger, method)(*args)
    assert isinstance(caught.value, ValueError)
