"""§94 sell operator tooling uses fixtures and typed consent."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from live_execution.commit_log import CommitLog
from live_execution.models import ExecutionLedger
from live_execution import solana
from tests.test_security_sell_recovery import MINT, SUCCESS


@pytest.fixture
def cli_state(tmp_path, monkeypatch):
    log = CommitLog(tmp_path / 'commits.json', now_fn=lambda: 1000)
    row = log.seal('sell', {'mint': MINT, 'symbol': 'DUMMY'})
    log.record_sell_attempt(row['hash'], 'known-signature', 'blockhash', dict(wallet='dummy-wallet', fraction=1, tokens=10, proceeds_usd=12, full_close=True))
    ledger = ExecutionLedger(tmp_path / 'executions.json')
    ledger.record_buy('buy', MINT, 10, 10, 1, 'buy-signature')
    monkeypatch.setattr(solana, 'get_signature_status_all_strict', AsyncMock(return_value=[SUCCESS, SUCCESS]))
    return tmp_path, log, ledger


async def test_list_is_read_only_and_has_operator_evidence(cli_state, capsys):
    from scripts import resolve_pending_sell as cli
    directory, log, _ = cli_state
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    result = await cli.resolve(cli.parse_args(['--state-dir', str(directory), 'list']))
    assert result == 0
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}
    output = capsys.readouterr().out
    assert MINT in output and 'known-signature' in output and 'age' in output
    assert 'https://solscan.io/tx/known-signature' in output
    assert 'confirmed' in output


@pytest.mark.parametrize('action', ['release', 'confirm'])
async def test_operator_actions_are_idempotent_and_require_known_signature(cli_state, action):
    from scripts import resolve_pending_sell as cli
    directory, log, ledger = cli_state
    extra = ['--i-verified-signature-never-landed'] if action == 'release' else ['--proceeds-usd', '12']
    args = cli.parse_args(['--state-dir', str(directory), action, '--mint', MINT, '--signature', 'unknown'] + extra)
    assert await cli.resolve(args, input_fn=lambda _: action, stdin=SimpleNamespace(isatty=lambda: True)) == 1
    args.signature = 'known-signature'
    for _ in range(2):
        assert await cli.resolve(args, input_fn=lambda _: action, stdin=SimpleNamespace(isatty=lambda: True)) == 0
    assert not log.pending_sells(MINT)
    closes = [r for r in ledger._load() if r['kind'] == 'close']
    assert len(closes) == (1 if action == 'confirm' else 0)
    if action == 'confirm':
        assert log.all()[0]['status'] == 'bound'
        assert ledger.total_open_exposure() == 0


@pytest.mark.parametrize('variant', ['no_attestation', 'wrong_typed', 'non_tty', 'negative', 'nan', 'inf', 'unknown_chain'])
async def test_cli_refuses_without_mutation(cli_state, variant):
    from scripts import resolve_pending_sell as cli
    directory, log, ledger = cli_state
    action = 'confirm' if variant in ('negative', 'nan', 'inf', 'unknown_chain') else 'release'
    argv = ['--state-dir', str(directory), action, '--mint', MINT, '--signature', 'known-signature']
    if action == 'release' and variant != 'no_attestation':
        argv += ['--i-verified-signature-never-landed']
    elif action == 'confirm':
        argv += ['--proceeds-usd', {'negative': '-1', 'nan': 'nan', 'inf': 'inf', 'unknown_chain': '12'}[variant]]
    if variant == 'unknown_chain':
        solana.get_signature_status_all_strict.return_value = [solana.ABSENT, solana.UNREACHABLE]
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    result = await cli.resolve(cli.parse_args(argv), input_fn=lambda _: 'bad' if variant == 'wrong_typed' else action, stdin=SimpleNamespace(isatty=lambda: variant != 'non_tty'))
    assert result == 1
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


async def test_cli_confirm_already_closed_settles_without_second_close(cli_state):
    from scripts import resolve_pending_sell as cli
    directory, log, ledger = cli_state
    ledger.close_out_of_band(MINT, proceeds_usd=12, note='operator fixture')
    args = cli.parse_args(['--state-dir', str(directory), 'confirm', '--mint', MINT, '--signature', 'known-signature', '--proceeds-usd', '12'])
    assert await cli.resolve(args, input_fn=lambda _: 'confirm', stdin=SimpleNamespace(isatty=lambda: True)) == 0
    assert not log.pending_sells(MINT)
    assert len([r for r in ledger._load() if r['kind'] == 'close']) == 1
