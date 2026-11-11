"""H18/H19: dummy-file state concurrency, crash release and record validation."""
import json
import multiprocessing
import os

import pytest

from live_execution.commit_log import CommitLog
from live_execution.models import ExecutionLedger
from live_execution.state_lock import state_lock


def _writer(root, worker):
    ledger = ExecutionLedger(root / 'executions.json')
    commits = CommitLog(root / 'commits.json')
    for index in range(10):
        ledger.record_buy(f'{worker}-{index}', 'dummy-mint', 1, 1, 1, f'sig-{worker}-{index}')
        commits.seal('buy', {'mint': 'dummy-mint', 'worker': worker, 'index': index})


def _die_with_lock(path):
    with state_lock(path):
        os._exit(0)


def test_overlapping_process_writes_preserve_every_record(tmp_path):
    ctx = multiprocessing.get_context('fork')
    children = [ctx.Process(target=_writer, args=(tmp_path, i)) for i in range(4)]
    for proc in children:
        proc.start()
    for proc in children:
        proc.join(5)
        assert proc.exitcode == 0
    rows = ExecutionLedger(tmp_path / 'executions.json')._load()
    assert len(rows) == 40
    assert len({row['idempotency_key'] for row in rows}) == 40
    assert len(CommitLog(tmp_path / 'commits.json').all()) == 40
    assert ExecutionLedger(tmp_path / 'executions.json').total_open_exposure() == 40


def test_process_crash_releases_lock_and_nested_same_file_is_reentrant(tmp_path):
    path = tmp_path / 'executions.json'
    child = multiprocessing.get_context('fork').Process(target=_die_with_lock, args=(path,))
    child.start()
    child.join(5)
    assert child.exitcode == 0
    with state_lock(path):
        with state_lock(path):
            ExecutionLedger(path).record_buy('new', 'dummy', 1, 1, 1, 'sig')
    assert len(ExecutionLedger(path)._load()) == 1


@pytest.mark.parametrize('patch', [
    {'usd_size': float('nan')}, {'tokens_out': float('inf')},
    {'price_usd': True}, {'usd_size': -1}, {'status': 'invented'},
    {'mint': ''}, {'idempotency_key': None}, {'commit_payload': []},
    {'ts': float('inf')}, {'pnl_usd': float('nan')}, {'tokens_out': 10 ** 1000}, {'signature': ['bad']},
])
def test_invalid_restored_ledger_record_refuses_without_rewriting(tmp_path, patch):
    ledger = ExecutionLedger(tmp_path / 'executions.json')
    ledger.record_buy('buy', 'dummy', 1, 1, 1, 'sig')
    doc = json.loads(ledger.path.read_text())
    doc['records'][0].update(patch)
    raw = json.dumps(doc)
    ledger.path.write_text(raw)
    with pytest.raises(RuntimeError, match='record 0 invalid'):
        ledger.total_open_exposure()
    with pytest.raises(RuntimeError):
        ledger.record_buy('new', 'dummy', 1, 1, 1, 'new')
    assert ledger.path.read_text() == raw


@pytest.mark.parametrize('row', [None, [], {'kind': 'buy'},
    {'kind': 'buy', 'status': 'sealed', 'hash': 'hash', 'nonce': 'nonce', 'payload': [], 'sealed_at': 1},
    {'kind': 'buy', 'status': 'sealed', 'hash': 'hash', 'nonce': 'nonce', 'payload': {}, 'sealed_at': float('nan')},
])
def test_invalid_restored_commit_record_refuses_without_rewriting(tmp_path, row):
    path = tmp_path / 'commits.json'
    raw = json.dumps([row])
    path.write_text(raw)
    with pytest.raises(RuntimeError, match='record 0 invalid'):
        CommitLog(path).seal('buy', {'mint': 'dummy'})
    assert path.read_text() == raw


@pytest.mark.parametrize('filename, contents', [
    ('executions.json', {'records': [{'kind': 'buy', 'mint': 'dummy', 'idempotency_key': 'id', 'status': 'confirmed', 'usd_size': float('nan')}]}),
    ('commits.json', [{'hash': 'h', 'nonce': 'n', 'kind': 'sell', 'status': 'sealed', 'payload': {}, 'sell_attempt': {'status': 'pending', 'signature': 'sig', 'wallet': 'wallet', 'blockhash': '', 'fraction': 1, 'tokens': 1, 'proceeds_usd': 1, 'full_close': True}}]),
])
def test_authenticated_backup_invalid_records_refuse_before_destination_mutations(tmp_path, filename, contents):
    from cryptography.fernet import Fernet
    from scripts.backup_state import create_backup
    from scripts.restore_state import restore_backup
    state = tmp_path / 'dummy-state'
    state.mkdir()
    (state / filename).write_text(json.dumps(contents))
    key = Fernet.generate_key().decode()
    archive = create_backup(state_dir=state, db_path=None, backup_dir=tmp_path / 'backups',
                            backup_key=key, secret_store_key=Fernet.generate_key().decode())
    target = tmp_path / 'not-created'
    with pytest.raises(RuntimeError, match='Invalid JSON'):
        restore_backup(archive, target, None, key)
    assert not target.exists()
    assert archive.is_file()


def test_duplicate_ledger_accounting_identity_is_rejected(tmp_path):
    ledger = ExecutionLedger(tmp_path / 'executions.json')
    ledger.record_buy('same', 'dummy', 1, 1, 1, 'sig')
    doc = json.loads(ledger.path.read_text())
    doc['records'].append(dict(doc['records'][0], usd_size=9))
    raw = json.dumps(doc)
    ledger.path.write_text(raw)
    with pytest.raises(RuntimeError, match='duplicate accounting identity'):
        ledger.open_positions()
    assert ledger.path.read_text() == raw


def _hold_lock(path, ready, release):
    with state_lock(path):
        os.write(ready, b"R")
        if os.read(release, 1) != b"G":
            os._exit(2)


def test_restore_waits_for_authority_writer_without_replacing_lock_inode(tmp_path):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from cryptography.fernet import Fernet
    from scripts.backup_state import create_backup
    from scripts.restore_state import restore_backup
    source = tmp_path / 'source'
    ExecutionLedger(source / 'executions.json').record_buy('buy', 'dummy', 1, 1, 1, 'sig')
    key = Fernet.generate_key().decode()
    archive = create_backup(state_dir=source, db_path=None, backup_dir=tmp_path / 'backups',
                            backup_key=key, secret_store_key=Fernet.generate_key().decode())
    target = tmp_path / 'restore'
    target.mkdir(mode=0o700)
    ctx = multiprocessing.get_context('fork')
    import select
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    child = ctx.Process(target=_hold_lock, args=(target / 'commits.json', ready_write, release_read))
    child.start()
    try:
        assert select.select([ready_read], [], [], 3)[0]
        assert os.read(ready_read, 1) == b"R"
        inode = (target / 'commits.json.lock').stat().st_ino
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(restore_backup, archive, target, None, key)
            try:
                with pytest.raises(TimeoutError):
                    result.result(timeout=0.05)
                assert not (target / 'executions.json').exists()
            finally:
                os.write(release_write, b"G")
            assert result.result(timeout=5)['restored_files']
        assert (target / 'commits.json.lock').stat().st_ino == inode
        assert ExecutionLedger(target / 'executions.json').total_open_exposure() == 1
    finally:
        try:
            os.write(release_write, b"G")
        except BrokenPipeError:
            pass
        child.join(5)
        if child.is_alive():
            child.kill()
            child.join()
        for fd in (ready_read, ready_write, release_read, release_write):
            os.close(fd)
        assert child.exitcode == 0


@pytest.mark.parametrize('reserved', ['executions.json.lock', 'commits.json.lock', '.staged.1.executions.json', '.rollback.1.commits.json', '.pre-restore-dummy/executions.json', './wallet_snapshot.json', 'wallet_snapshot.json/', './executions.json', 'nested//commits.json', 'alias/wallet_snapshot.json'])
def test_authenticated_restore_refuses_reserved_paths_without_replacing_lock(tmp_path, reserved):
    import hashlib
    import io
    import tarfile
    from cryptography.fernet import Fernet
    from scripts import backup_crypto
    from scripts.restore_state import restore_backup
    key = Fernet.generate_key().decode()
    # Valid JSON with an invalid authoritative schema, so alias refusal is
    # required even when ordinary JSON parsing succeeds.
    data = json.dumps({'ts': 1, 'usdc': True, 'sol': 0}).encode()
    if 'executions.json' in reserved:
        data = json.dumps({'records': [{'kind': 'buy', 'status': 'confirmed', 'mint': 'dummy', 'idempotency_key': 'id', 'usd_size': 'invalid'}]}).encode()
    name = 'state/' + reserved
    manifest = json.dumps({'version': 1, 'files': [{'name': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}]}).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for filename, content in ((name, data), ('MANIFEST.json', manifest)):
            entry = tarfile.TarInfo(filename)
            entry.size = len(content)
            tar.addfile(entry, io.BytesIO(content))
    archive = tmp_path / 'dummy.enc'
    archive.write_bytes(backup_crypto.encrypt_archive(buffer.getvalue(), key))
    target = tmp_path / 'target'
    target.mkdir()
    lock = target / 'executions.json.lock'
    lock.write_bytes(b'original lock inode')
    before = lock.stat().st_ino
    if reserved.startswith('alias/'):
        (target / 'alias').symlink_to(target, target_is_directory=True)
        (target / 'wallet_snapshot.json').write_text('{"ts":1,"usdc":2,"sol":0}')
    with pytest.raises(RuntimeError, match='reserved runtime path|canonical path|symlinked child'):
        restore_backup(archive, target, None, key, force=True)
    assert lock.stat().st_ino == before
    assert lock.read_bytes() == b'original lock inode'
    expected = ['executions.json.lock']
    if reserved.startswith('alias/'):
        expected += ['alias', 'wallet_snapshot.json']
        assert (target / 'wallet_snapshot.json').read_text() == '{"ts":1,"usdc":2,"sol":0}'
    assert sorted(p.name for p in target.iterdir()) == sorted(expected)
