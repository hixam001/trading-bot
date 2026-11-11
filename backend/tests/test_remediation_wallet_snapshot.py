"""H19: actual wallet baseline and authenticated restore schema regressions."""
import json

import pytest

from live_execution.reconcile import check_wallet_drop
from live_execution.state_validation import validate_wallet_snapshot


@pytest.mark.parametrize('snapshot', [
    [], None, {}, {'ts': 1, 'usdc': 1},
    {'ts': True, 'usdc': 1, 'sol': 1},
    {'ts': float('inf'), 'usdc': 1, 'sol': 1},
    {'ts': 1, 'usdc': float('nan'), 'sol': 1},
    {'ts': 1, 'usdc': 1, 'sol': float('-inf')},
    {'ts': 1, 'usdc': -1, 'sol': 1},
    {'ts': 1, 'usdc': 1, 'sol': True},
    {'ts': 1, 'usdc': 10**1000, 'sol': 1},
    {'ts': 1, 'usdc': 1, 'sol': 1, 'wallet': ''},
    {'ts': 1, 'usdc': 1, 'sol': 1, 'owner': 42},
])
def test_invalid_baseline_is_not_adopted_or_overwritten(tmp_path, snapshot):
    path = tmp_path / 'wallet_snapshot.json'
    raw = json.dumps(snapshot).encode()
    path.write_bytes(raw)
    result = check_wallet_drop(1.0, 1.0, state_dir=tmp_path, now_fn=lambda: 2.0)
    assert result.skipped and not result.alerted
    assert 'wallet snapshot invalid' in result.reason
    assert path.read_bytes() == raw
    assert not list(tmp_path.glob('*.tmp'))


@pytest.mark.parametrize('current', [True, float('nan'), float('inf'), -1, 10**1000])
def test_invalid_current_balance_cannot_create_new_baseline(tmp_path, current):
    result = check_wallet_drop(current, 1.0, state_dir=tmp_path, now_fn=lambda: 2.0)
    assert result.skipped and 'current_snapshot_invalid' in result.reason
    assert not (tmp_path / 'wallet_snapshot.json').exists()


def test_zero_baseline_and_operator_metadata_remain_valid(tmp_path):
    old = {'ts': 1.0, 'usdc': 0.0, 'sol': 0.0,
           'reason': 'dummy acknowledged deposit', 'acknowledged_by': 'operator'}
    validate_wallet_snapshot(old)
    path = tmp_path / 'wallet_snapshot.json'
    path.write_text(json.dumps(old))
    result = check_wallet_drop(0.0, 0.0, state_dir=tmp_path, now_fn=lambda: 2.0)
    assert not result.skipped and not result.alerted
    assert json.loads(path.read_text()) == {'ts': 2.0, 'usdc': 0.0, 'sol': 0.0}


@pytest.mark.parametrize('snapshot', [
    {'ts': 1.0, 'usdc': float('nan'), 'sol': 1.0},
    {'ts': 1.0, 'usdc': 1.0, 'sol': False},
    {'ts': 1.0, 'usdc': 1.0},
])
def test_authenticated_archive_invalid_snapshot_refused_before_adoption(tmp_path, snapshot):
    from cryptography.fernet import Fernet
    from scripts.backup_state import create_backup
    from scripts.restore_state import restore_backup
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'wallet_snapshot.json').write_text(json.dumps(snapshot))
    key = Fernet.generate_key().decode()
    archive = create_backup(source, None, tmp_path / 'backups', key)
    target = tmp_path / 'not-created'
    with pytest.raises(RuntimeError, match='wallet snapshot invalid'):
        restore_backup(archive, target, None, key)
    assert archive.exists() and not target.exists()


def test_authenticated_valid_snapshot_restores_without_fabricated_identity(tmp_path):
    from cryptography.fernet import Fernet
    from scripts.backup_state import create_backup
    from scripts.restore_state import restore_backup
    source = tmp_path / 'source'
    source.mkdir()
    snapshot = {'ts': 1.0, 'usdc': 0.0, 'sol': 1.0}
    (source / 'wallet_snapshot.json').write_text(json.dumps(snapshot))
    key = Fernet.generate_key().decode()
    archive = create_backup(source, None, tmp_path / 'backups', key)
    target = tmp_path / 'restored'
    restore_backup(archive, target, None, key)
    assert json.loads((target / 'wallet_snapshot.json').read_text()) == snapshot


@pytest.mark.parametrize('usdc,sol', [(float('nan'), 0), (-1, 0), (0, float('inf')), (True, 0)])
def test_acknowledgement_refuses_invalid_snapshot_without_creating_state(tmp_path, monkeypatch, usdc, sol):
    from argparse import Namespace
    from scripts import ack_wallet_change
    target = tmp_path / 'not-created'
    monkeypatch.setattr(ack_wallet_change, 'parse_args', lambda: Namespace(usdc=usdc, sol=sol, live=False, state_dir=target, reason='dummy acknowledgement'))
    assert ack_wallet_change.main() == 1
    assert not target.exists()
