"""
backend/tests/test_s4_backup_v2.py — S4: backups use a memory-hard KDF.

Old archives were Fernet with the key used directly (sha256 of the password
under the hood). New archives are a versioned envelope:

    MAGIC | u32 header length | JSON header | AES-256-GCM ciphertext

The header carries a random per-archive scrypt salt and GCM nonce. Restore
still accepts legacy Fernet archives so an operator can restore the last
good backup after upgrading.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import struct
import tarfile

import pytest
from cryptography.fernet import Fernet, InvalidToken

from scripts import backup_crypto, backup_state, restore_state


@pytest.fixture
def keys():
    return (Fernet.generate_key().decode("utf-8"),
            Fernet.generate_key().decode("utf-8"))


@pytest.fixture
def sample(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "executions.json").write_text(
        json.dumps([{"kind": "buy", "mint": "MINT1", "usd_size": 25.0}]),
        encoding="utf-8")
    (state_dir / "commits.json").write_text(
        json.dumps([{"hash": "h1"}]), encoding="utf-8")
    (state_dir / "wallet_keypair.json").write_text(
        json.dumps(list(range(64))), encoding="utf-8")

    db_path = tmp_path / "trading_bot.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE feed_events (id INTEGER PRIMARY KEY, ts TEXT);")
    conn.execute("INSERT INTO feed_events VALUES (1, '2026-11-01T00:00:00Z');")
    conn.commit()
    conn.close()

    return state_dir, db_path, tmp_path / "backups"


def _header(blob: bytes) -> dict:
    assert blob.startswith(backup_crypto.MAGIC)
    offset = len(backup_crypto.MAGIC)
    (length,) = struct.unpack(">I", blob[offset:offset + 4])
    return json.loads(blob[offset + 4:offset + 4 + length])


def test_new_archive_is_versioned_scrypt_aesgcm(sample, keys):
    state_dir, db_path, backup_dir = sample
    key, secret = keys

    archive = backup_state.create_backup(
        state_dir=state_dir, db_path=db_path, backup_dir=backup_dir,
        backup_key=key, secret_store_key=secret)
    blob = archive.read_bytes()
    header = _header(blob)

    assert header["version"] == 2
    assert header["kdf"] == "scrypt"
    assert header["n"] >= 2 ** 14
    assert len(base64.b64decode(header["salt"])) >= 16
    assert len(base64.b64decode(header["nonce"])) == 12
    # Two archives of the same state must not share a salt/nonce.
    second = backup_state.create_backup(
        state_dir=state_dir, db_path=db_path,
        backup_dir=backup_dir / "second",
        backup_key=key, secret_store_key=secret)
    other = _header(second.read_bytes())
    assert other["salt"] != header["salt"]
    assert other["nonce"] != header["nonce"]

    with pytest.raises(InvalidToken):
        Fernet(key.encode("utf-8")).decrypt(blob)


def test_new_archive_round_trips_and_excludes_keypairs(sample, keys):
    state_dir, db_path, backup_dir = sample
    key, secret = keys
    archive = backup_state.create_backup(
        state_dir=state_dir, db_path=db_path, backup_dir=backup_dir,
        backup_key=key, secret_store_key=secret)

    target_state = backup_dir / "restored"
    target_db = backup_dir / "restored.db"
    result = restore_state.restore_backup(
        archive, target_state, target_db, key)

    assert json.loads((target_state / "executions.json").read_text()) == [
        {"kind": "buy", "mint": "MINT1", "usd_size": 25.0}]
    assert not (target_state / "wallet_keypair.json").exists()
    assert any("wallet_keypair" not in f["name"]
               for f in result["manifest"]["files"])


def test_wrong_key_and_tampered_ciphertext_fail_closed(sample, keys):
    state_dir, db_path, backup_dir = sample
    key, secret = keys
    archive = backup_state.create_backup(
        state_dir=state_dir, db_path=db_path, backup_dir=backup_dir,
        backup_key=key, secret_store_key=secret)
    blob = archive.read_bytes()

    wrong = Fernet.generate_key().decode("utf-8")
    with pytest.raises(RuntimeError, match="Archive decryption failed"):
        restore_state.restore_backup(
            archive, backup_dir / "wrong", None, wrong)

    tampered = backup_dir / "tampered.enc"
    tampered.write_bytes(blob[:-1] + bytes([blob[-1] ^ 0xFF]))
    with pytest.raises(RuntimeError, match="Archive decryption failed"):
        restore_state.restore_backup(
            tampered, backup_dir / "tampered-target", None, key)

    # Header is authenticated too: swapping the salt must not decrypt.
    header = _header(blob)
    offset = len(backup_crypto.MAGIC)
    (length,) = struct.unpack(">I", blob[offset:offset + 4])
    header_start = offset + 4
    header_end = header_start + length
    header["salt"] = base64.b64encode(b"\x00" * 16).decode()
    forged_header = json.dumps(header, sort_keys=True).encode("utf-8")
    forged = (backup_crypto.MAGIC + struct.pack(">I", len(forged_header))
              + forged_header + blob[header_end:])
    forged_path = backup_dir / "forged.enc"
    forged_path.write_bytes(forged)
    with pytest.raises(RuntimeError, match="Archive decryption failed"):
        restore_state.restore_backup(
            forged_path, backup_dir / "forged-target", None, key)


def test_legacy_fernet_archive_still_restores(tmp_path, keys):
    """An archive written before S4 must remain restorable."""
    key, _secret = keys
    state_dir = tmp_path / "legacy-state"
    state_dir.mkdir()
    payload = json.dumps([{"kind": "sell", "mint": "OLD1"}]).encode()
    manifest = json.dumps({
        "created_at_utc": "2026-10-01T00:00:00Z",
        "files": [{
            "name": "state/executions.json",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }],
    }).encode()

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
        for name, data in (("state/executions.json", payload),
                           ("MANIFEST.json", manifest)):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    legacy = tmp_path / "legacy.tar.gz.enc"
    legacy.write_bytes(Fernet(key.encode()).encrypt(tar_buf.getvalue()))

    target = tmp_path / "legacy-target"
    restore_state.restore_backup(legacy, target, None, key)
    assert json.loads((target / "executions.json").read_text()) == [
        {"kind": "sell", "mint": "OLD1"}]


def test_weak_backup_key_is_refused(sample):
    state_dir, db_path, backup_dir = sample
    with pytest.raises(RuntimeError, match="valid (Fernet|backup) key"):
        backup_state.create_backup(
            state_dir=state_dir, db_path=db_path, backup_dir=backup_dir,
            backup_key="short")
