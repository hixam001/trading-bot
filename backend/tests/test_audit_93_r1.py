"""§93 R1 — every runtime write has a home on a writable mount.

The §92 S8 containers are ``read_only: true``.  These tests prove the compose
layout gives each default write path a mounted volume, that the backup
denylist still excludes the secret sidecars at their new location, and that
the live/api behaviour tests pass under a simulated read-only root (EROFS)
with the compose-derived environment applied.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet

import config
from scripts import backup_crypto, backup_state
from scripts.audit_runtime_writes import INVENTORY, check as inventory_check
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
    compose = load_compose()
    for role in ("api", "live"):
        service = service_by_name(compose, role)
        mounts = writable_mounts(service)
        env = service_environment(service)
        assert mounts, f"{role}: no writable mounts parsed from compose"
        assert "/tmp" in mounts, f"{role}: tmpfs /tmp is missing"

        checked = 0
        for row in INVENTORY:
            if role not in row.services:
                continue
            key = row.config_key
            if key not in env:
                # SPEND_GUARD_FILE falls back to LIVE_STATE_DIR/spend_guard.json
                if key == "SPEND_GUARD_FILE" and "LIVE_STATE_DIR" in env:
                    path = Path(env["LIVE_STATE_DIR"]) / "spend_guard.json"
                else:
                    continue
            else:
                path = Path(env[key])
            checked += 1
            assert any(_under(path, mount) for mount in mounts) or path == Path("/tmp"), (
                f"{role}: {key}={path} is not under a writable mount "
                f"{mounts} or /tmp"
            )
        assert checked >= 5, (role, checked)


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
async def test_live_writes_succeed_under_simulated_erofs(rofs_service):
    import blocklist
    from live_execution import config as live_config
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

    # Privy rotated-token sidecar persists and reloads.
    from data_providers import crowd
    config.SECRET_STORE_KEY = Fernet.generate_key().decode("utf-8")
    token = "rotated-refresh-token-§93-r1"
    crowd._persist_refresh(token)
    assert crowd._load_persisted_refresh() == token

    # TLS TOFU pin writes and re-reads on the writable state volume.
    db_pg._MEMORY_PIN = None
    db_pg._pool = None

    def _probe(_host, _port):
        return "ab" * 32

    db_pg._fetch_cert_fingerprint = _probe
    import ssl

    async def _no_system_trust(*_a, **_kw):
        raise ssl.SSLCertVerificationError("self-signed pooler chain")

    db_pg.asyncpg.connect = _no_system_trust
    ctx = await db_pg._tls_context()
    assert ctx is not None
    assert db_pg._pin_path().is_file()
    assert db_pg._pin_path().read_text().strip() == "ab" * 32
    # A later connection re-reads the same pin.
    db_pg._MEMORY_PIN = None
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
    config.DB_PATH = tmp_path / "r1_api.db"
    await db.init_db()
    result = await loader.ingest_file("notes.md", "# §93 R1 knowledge\n")
    assert Path(config.INGESTED_KNOWLEDGE_DIR, "notes.md").is_file()
    async with db.get_db() as conn:
        rows = await db.get_kb_documents(conn)
    assert any(r["filename"] == "notes.md" for r in rows)

    # The API-side spend guard has its own counter file and reserves.
    spend_guard.reset_for_tests()
    spend_guard.reserve_llm("deepseek")
    assert Path(config.SPEND_GUARD_FILE).is_file()
