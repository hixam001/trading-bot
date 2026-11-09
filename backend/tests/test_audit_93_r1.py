"""§93 R1 — every runtime write has a home on a writable mount.

The §92 S8 containers are ``read_only: true``.  These tests prove the compose
layout gives each default write path a mounted volume, that the backup
denylist still excludes the secret sidecars at their new location, and that
the live/api behaviour tests pass under a simulated read-only root (EROFS)
with the compose-derived environment applied.
"""
from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import config
from scripts import backup_crypto, backup_state
from scripts.audit_runtime_writes import INVENTORY
from tests.rofs_helpers import (
    load_compose,
    service_environment,
    service_by_name,
    writable_mounts,
)

ROOT = Path(__file__).resolve().parents[2]


def _under(path: Path, root: str) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Inventory + compose contract
# ---------------------------------------------------------------------------

def test_runtime_write_inventory_matches_the_ast_scan():
    proc = subprocess.run(
        [sys.executable, "scripts/audit_runtime_writes.py", "--check"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    # Every inventory row is backed by a detected write site (check() enforces
    # this, but make the contract explicit for a reader of the report).
    assert INVENTORY


def test_compose_gives_every_service_write_path_a_writable_mount():
    from scripts import audit_runtime_writes as audit
    compose = load_compose()
    for role in ("api", "live"):
        service = service_by_name(compose, role)
        paths = audit.resolved_runtime_paths(service_environment(service))
        keys = audit.runtime_keys(role)
        assert len(keys) >= 5
        for key in keys:
            assert audit.writable_path(paths[key], service), f"{role}: {key} lacks a writable mount"


def test_new_write_path_without_a_home_fails_the_contract():
    """The contract test logic itself must reject an unmounted path."""
    compose = load_compose()
    service = service_by_name(compose, "api")
    mounts = writable_mounts(service)
    assert not any(_under(Path("/app/backend/blocklist_state.json"), m)
                   for m in mounts)


def test_preflight_runtime_write_paths_check_passes_and_fails():
    from scripts import preflight_deploy as pf

    compose = load_compose()
    checks = pf.check_runtime_write_paths(compose, {})
    check = next(c for c in checks if c.name == "runtime write paths")
    assert check.status == pf.PASS, check.detail

    # A service whose writes resolve to the read-only root must FAIL.
    lax = {"services": {
        "api": {"volumes": ["engine_data:/app/data"],
                "environment": ["SESSION_REVOCATION_FILE=/app/state/rev.json"]},
        "live": {"volumes": ["engine_state:/app/backend/live_execution/state"],
                 "environment": ["BLOCKLIST_STATE_FILE=/app/blocklist.json"]},
    }}
    checks = pf.check_runtime_write_paths(lax, {})
    check = next(c for c in checks if c.name == "runtime write paths")
    assert check.status == pf.FAIL
    assert "read-only root" in check.detail


# ---------------------------------------------------------------------------
# Backup denylist at the new locations
# ---------------------------------------------------------------------------

def test_backup_denylist_excludes_new_secret_paths_and_includes_blocklist(tmp_path):
    assert backup_state.is_denylisted(".fomo_privy.json")
    assert backup_state.is_denylisted(".secret_store.key")
    assert not backup_state.is_denylisted("blocklist_state.json")

    state = tmp_path / "state"
    state.mkdir()
    (state / "blocklist_state.json").write_text(
        json.dumps({"mints": {}}), encoding="utf-8")
    (state / ".fomo_privy.json").write_text('{"refresh_token": "secret"}',
                                            encoding="utf-8")
    (state / ".secret_store.key").write_text("key-material", encoding="utf-8")
    db = tmp_path / "trading_bot.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (id TEXT)")
    conn.commit()
    conn.close()

    key = Fernet.generate_key().decode("utf-8")
    archive = backup_state.create_backup(
        state_dir=state, db_path=db, backup_dir=tmp_path,
        backup_key=key, secret_store_key=Fernet.generate_key().decode("utf-8"),
    )
    tar_bytes = backup_crypto.decrypt_archive(archive.read_bytes(), key)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        names = tar.getnames()
    assert "state/blocklist_state.json" in names
    assert not any(".fomo_privy" in n for n in names)
    assert not any(".secret_store" in n for n in names)


# ---------------------------------------------------------------------------
# live service under simulated EROFS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_writes_succeed_under_simulated_erofs(rofs_service, monkeypatch):
    import blocklist
    from llm import spend_guard
    from api import db_pg

    guard = rofs_service("live")
    assert guard.allowed(config.BLOCKLIST_STATE_FILE)
    assert not guard.allowed("/app/backend/blocklist_state.json")

    # 3 consecutive loss closes -> loss memory persists + auto-block fires.
    mint = "So11111111111111111111111111111111111111112"
    for _ in range(3):
        blocklist.record_close_outcome(mint, "BONK", "exit_stop_loss", -1.0)
    entry = blocklist.mint_entry(mint)
    assert entry is not None and len(entry["closes"]) == 3
    assert blocklist.maybe_autoblock(mint, "BONK") is True
    assert blocklist.is_blocked_mint(mint) is True

    class _C:
        mint_address = mint
        symbol = "BONK"
    kept, blocked = blocklist.filter_candidates([_C()])
    assert kept == [] and blocked and "consecutive loss" in blocked[0][1]

    # A single loss is cooled down even before the auto-block streak exists.
    recent_loss = _C()
    recent_loss.mint_address = "OtherMintForCooldown93"
    recent_loss.symbol = "COOL"
    blocklist.record_close_outcome(recent_loss.mint_address, "COOL", "exit_stop_loss", -1.0)
    assert not blocklist.is_blocked_mint(recent_loss.mint_address)
    kept, refused = blocklist.filter_candidates([recent_loss])
    assert kept == [] and "re-entry cooldown" in refused[0][1]

    # Privy rotated-token sidecar persists and reloads.
    from data_providers import crowd
    monkeypatch.setattr(config, "SECRET_STORE_KEY", Fernet.generate_key().decode("utf-8"))
    token = "rotated-refresh-token-§93-r1"
    crowd._persist_refresh(token)
    assert crowd._load_persisted_refresh() == token

    # TLS TOFU pin writes and re-reads on the writable state volume.
    monkeypatch.setattr(db_pg, "_MEMORY_PIN", None)
    monkeypatch.setattr(db_pg, "_pool", None)

    def _probe(_host, _port):
        return "ab" * 32

    monkeypatch.setattr(db_pg, "_fetch_cert_fingerprint", _probe)
    import ssl

    async def _no_system_trust(*_a, **_kw):
        raise ssl.SSLCertVerificationError("self-signed pooler chain")

    monkeypatch.setattr(db_pg.asyncpg, "connect", _no_system_trust)
    ctx = await db_pg._tls_context()
    assert ctx is not None
    assert db_pg._pin_path().is_file()
    assert db_pg._pin_path().read_text().strip() == "ab" * 32
    # A later connection re-reads the same pin.
    monkeypatch.setattr(db_pg, "_MEMORY_PIN", None)
    assert await db_pg._tls_context() is not None

    # Spend guard reserves on the live state volume.
    spend_guard.reset_for_tests()
    spend_guard.reserve_llm("deepseek")
    assert (Path(config.LIVE_STATE_DIR) / "spend_guard.json").is_file()


# ---------------------------------------------------------------------------
# api service under simulated EROFS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_writes_succeed_under_simulated_erofs(rofs_service, tmp_path,
                                                       monkeypatch):
    from api import auth, db
    from knowledge_base import loader
    from llm import spend_guard

    monkeypatch.setattr(config, "ADMIN_TOKEN",
                        "test-operator-token-§93-r1-32-chars")
    rofs_service("api")

    # Logout revocation persists, then survives a simulated restart.
    token = auth.create_session_token(config.ADMIN_TOKEN)
    auth.revoke_session_token(token)
    path = Path(config.SESSION_REVOCATION_FILE)
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    auth._REVOKED_SESSION_SIGS.clear()
    auth._REVOCATIONS_LOADED = False
    auth._REVOCATIONS_ERROR = False
    assert auth.is_session_revoked(token) is True

    # KB ingest archives the document and writes its DB row.
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "r1_api.db")
    await db.init_db()
    await loader.ingest_file("notes.md", "# §93 R1 knowledge\n")
    assert Path(config.INGESTED_KNOWLEDGE_DIR, "notes.md").is_file()
    async with db.get_db() as conn:
        rows = await db.get_kb_documents(conn)
    assert any(r["filename"] == "notes.md" for r in rows)

    # The API-side spend guard has its own counter file and reserves.
    spend_guard.reset_for_tests()
    spend_guard.reserve_llm("deepseek")
    assert Path(config.SPEND_GUARD_FILE).is_file()


def test_ast_scanner_finds_keyword_modes_flags_aliases_and_path_methods(tmp_path):
    from scripts import audit_runtime_writes as audit
    source = tmp_path / 'backend' / 'new_writer.py'
    source.parent.mkdir()
    source.write_text('''import os
from os import open as raw_open
from sqlite3 import connect
from pathlib import Path

def new_writer():
    open('/unmounted/a', mode='w')
    os.open('/unmounted/b', os.O_WRONLY | os.O_CREAT)
    raw_open('/unmounted/c', flags=os.O_RDWR)
    connect(database='/unmounted/d')
    p = Path('/unmounted/e')
    p.open(mode='a')
    p.replace('/unmounted/f')
    os.open('/unmounted/readonly', flags=os.O_RDONLY)
''')
    sites = audit.scan(root=tmp_path)
    assert len(sites) == 6, [(s.func, s.path_expr) for s in sites]
    assert all(s.file == 'backend/new_writer.py' for s in sites)
    problems, _ = audit.check(root=tmp_path)
    assert any('unclassified' in p and 'backend/new_writer.py' in p
               for p in problems)


def test_new_write_in_existing_file_is_not_implicitly_approved(monkeypatch):
    from scripts import audit_runtime_writes as audit
    new = audit.WriteSite('backend/blocklist.py', 9999, 'write_text',
                          "Path('/unmounted/new-state.json')")
    monkeypatch.setattr(audit, 'scan', lambda **kwargs: [new])
    problems, _ = audit.check()
    assert any('unclassified' in p and '9999' in p for p in problems)


def test_preflight_accepts_expanded_mounts_and_rejects_dotdot_escape():
    from copy import deepcopy
    from scripts import preflight_deploy as pf
    compose = load_compose()
    for service in compose['services'].values():
        expanded = []
        for raw in service.get('volumes', []):
            source, target, *mode = raw.split(':')
            expanded.append({'type': 'volume', 'source': source, 'target': target,
                             'read_only': bool(mode and mode[0] == 'ro')})
        service['volumes'] = expanded
        service['tmpfs'] = [{'target': '/tmp', 'mode': 0o1777}]
    assert pf.check_runtime_write_paths(compose, {})[0].status == pf.PASS
    escaped = deepcopy(compose)
    escaped['services']['live']['environment'].append(
        'BLOCKLIST_STATE_FILE=/app/backend/live_execution/state/../../outside')
    result = pf.check_runtime_write_paths(escaped, {})[0]
    assert result.status == pf.FAIL
    assert 'BLOCKLIST_STATE_FILE' in result.detail


@pytest.mark.parametrize('key,value', [
    ('BREAK_STATE_FILE', '/app/break.json'),
    ('KILL_SWITCH_FILE', '/app/kill.json'),
    ('LIVE_EXECUTION_STATE_DIR', '/app/unmounted'),
    ('LOG_FILE', '/app/escaped.log'),
    ('ERROR_LOG_FILE', '../../escaped.log'),
    ('SECURITY_LOG_FILE', '/app/escaped.log'),
])
def test_runtime_contract_checks_independent_path_overrides(key, value):
    from scripts import preflight_deploy as pf
    compose = load_compose()
    compose['services']['live']['environment'].append(f'{key}={value}')
    result = pf.check_runtime_write_paths(compose, {})[0]
    assert result.status == pf.FAIL
    assert key in result.detail


@pytest.mark.parametrize('role', ['live', 'api'])
def test_smoke_preserves_existing_state_and_resolves_compose_defaults(
        tmp_path, monkeypatch, role):
    from scripts import rofs_smoke as smoke
    monkeypatch.setenv('ROLE', role)
    state = tmp_path / 'state'
    data = tmp_path / 'data'
    logs = tmp_path / 'logs'
    for path in (state, data, logs):
        path.mkdir()
    monkeypatch.setenv('LIVE_STATE_DIR', str(state))
    monkeypatch.setenv('LIVE_EXECUTION_STATE_DIR', str(state))
    monkeypatch.setenv('DB_PATH', str(data / 'book.db'))
    monkeypatch.setenv('LOG_DIR', str(logs))
    sentinels = {}
    keys = ('BLOCKLIST_STATE_FILE', 'FOMO_PRIVY_STATE_FILE',
            'SECRET_STORE_KEY_FILE', 'SUPABASE_PIN_FILE',
            'SESSION_REVOCATION_FILE', 'SPEND_GUARD_FILE')
    for key in keys:
        path = (state if role == 'live' else data) / key.lower()
        path.write_text('operator-state-must-survive')
        sentinels[path] = path.read_bytes()
        monkeypatch.setenv(key, str(path))
    # The live compose service deliberately uses the spend guard's fallback.
    if role == 'live':
        monkeypatch.delenv('SPEND_GUARD_FILE')
        fallback = state / 'spend_guard.json'
        fallback.write_text('real-spend-counter')
        sentinels[fallback] = fallback.read_bytes()
    # Also prove a first-mount API can create its archive directory.
    monkeypatch.setenv('KB_INGESTED_DIR', str(data / 'kb/ingested'))
    assert smoke.main() == 0
    for path, original in sentinels.items():
        assert path.is_file() and path.read_bytes() == original
    assert not list(tmp_path.rglob('.rofs-smoke-*'))


def test_ci_smoke_mounts_and_env_are_derived_from_compose():
    from scripts import rofs_smoke
    compose = load_compose()
    plan = rofs_smoke.container_smoke_plan(compose, 'live',
                                            'trading-bot:test', 'unit-smoke')
    assert '--read-only' in plan.command
    assert '--cap-drop' in plan.command
    assert '--network' in plan.command and 'none' in plan.command
    assert 'BLOCKLIST_STATE_FILE=/app/backend/live_execution/state/blocklist_state.json' in plan.command
    assert any('unit-smoke-engine_state' in arg for arg in plan.command)
    assert not any('777' in arg for arg in plan.command)
    workflow = (ROOT / '.github/workflows/tests.yml').read_text()
    assert 'scripts/rofs_smoke.py --compose-ci' in workflow
    assert 'chmod 777' not in workflow


def test_simulated_erofs_blocks_every_write_primitive_and_readonly_source(
        rofs_service, tmp_path):
    import errno
    import os
    import tempfile
    guard = rofs_service('live')
    forbidden = Path('/unmounted/denied')
    writable = Path(config.BLOCKLIST_STATE_FILE).parent / 'allowed-probe'
    operations = [
        lambda: open(forbidden, mode='w'),
        lambda: os.open(forbidden, flags=os.O_WRONLY | os.O_CREAT, mode=0o600),
        lambda: forbidden.write_text('denied'),
        lambda: forbidden.write_bytes(b'denied'),
        lambda: forbidden.mkdir(),
        lambda: os.replace(forbidden, writable),
        lambda: os.rename(forbidden, writable),
        lambda: tempfile.mkstemp('', 'prefix', str(forbidden)),
        lambda: tempfile.mkdtemp('', 'prefix', str(forbidden)),
    ]
    for operation in operations:
        with pytest.raises(OSError) as failure:
            operation()
        assert failure.value.errno == errno.EROFS
    # Positional tempfile dirs on an allowed mount retain real behavior.
    descriptor, name = tempfile.mkstemp('', 'prefix', str(writable.parent))
    os.close(descriptor)
    Path(name).unlink()
    assert not guard.allowed('/app/backend/live_execution/state/../../escaped')


def test_nested_readonly_mount_overrides_a_writable_parent():
    from scripts import audit_runtime_writes as audit
    service = {'volumes': [
        {'type': 'volume', 'target': '/app/data', 'read_only': False},
        {'type': 'volume', 'target': '/app/data/private', 'read_only': True},
    ]}
    assert audit.writable_path('/app/data/normal', service)
    assert not audit.writable_path('/app/data/private/key', service)
    assert not audit.writable_path('/app/data/../../escaped', service)


def test_container_default_resolution_matches_backend_config_defaults():
    from scripts import audit_runtime_writes as audit
    defaults = audit._default_paths()
    assert defaults, 'backend config defaults could not be resolved'
    resolved = audit.resolved_runtime_paths({})
    for key in set(audit.runtime_keys('api') + audit.runtime_keys('live')):
        if key == 'LIVE_EXECUTION_STATE_DIR':
            # This name belongs to live_execution.config rather than config.
            assert resolved[key] == '/app/backend/live_execution/state'
            continue
        value = defaults[key]
        if key == 'SPEND_GUARD_FILE' and not value:
            value = str(Path(defaults['LIVE_STATE_DIR']) / 'spend_guard.json')
        elif key in {'LOG_FILE', 'ERROR_LOG_FILE', 'SECURITY_LOG_FILE'}:
            value = str(Path(defaults['LOG_DIR']) / value)
        expected = value.replace(str(ROOT), '/app', 1)
        assert resolved[key] == expected, key


def test_relative_runtime_overrides_resolve_like_backend_paths():
    from scripts import audit_runtime_writes as audit
    paths = audit.resolved_runtime_paths({'LOG_DIR': 'logs', 'DB_PATH': 'db.sqlite'})
    assert paths['DB_PATH'] == '/app/backend/db.sqlite'
    assert paths['LOG_FILE'] == '/app/logs/app.log'
