"""
tests/test_secret_store.py — §54 encrypted-at-rest persistence for
runtime-rotated secrets (the fomo Privy refresh-token sidecar).

Contract under test (all hermetic — throwaway keys in tmp_path, no network,
no real material):
  1. encrypt_to_file writes an envelope {"v":1,"enc":...} at mode 0600;
     the plaintext secret NEVER appears in the raw file bytes.
  2. decrypt_from_file round-trips it under the same key.
  3. Key channels: SECRET_STORE_KEY env (PaaS) and the auto-generated 0600
     key file. A corrupt/invalid key NEVER regenerates (orphaning sidecars)
     — the store disables itself fail-soft instead.
  4. A wrong key cannot decrypt (tamper/rotation) — None, not an exception.
  5. A sidecar that cannot be encrypted is never written in plaintext.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import config
import secret_store


def _fresh_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    """Every test gets its own key-file channel; env channel cleared."""
    monkeypatch.setattr(config, "SECRET_STORE_KEY", "")
    monkeypatch.setattr(config, "SECRET_STORE_KEY_FILE",
                        str(tmp_path / "key.file"))
    yield tmp_path


# --- round-trip + at-rest properties -----------------------------------------

def test_encrypt_never_writes_plaintext_and_is_0600(tmp_path):
    p = str(tmp_path / "sidecar.json")
    assert secret_store.encrypt_to_file(p, {"refresh_token": "super-secret"})
    raw = Path(p).read_text()
    assert "super-secret" not in raw
    envelope = json.loads(raw)
    assert envelope == {"v": 1, "enc": envelope["enc"]}
    assert (stat.S_IMODE(os.stat(p).st_mode) & 0o777) == 0o600


def test_decrypt_round_trip(tmp_path):
    p = str(tmp_path / "sidecar.json")
    assert secret_store.encrypt_to_file(p, {"refresh_token": "tok-1"})
    assert secret_store.decrypt_from_file(p) == {"refresh_token": "tok-1"}


def test_key_file_auto_generated_0600(tmp_path):
    kf = tmp_path / "key.file"
    secret_store.encrypt_to_file(str(tmp_path / "s.json"), {"a": "b"})
    assert kf.is_file()
    assert (stat.S_IMODE(kf.stat().st_mode) & 0o777) == 0o600


# --- env key channel -----------------------------------------------------------

def test_env_key_channel_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SECRET_STORE_KEY", _fresh_key())
    p = str(tmp_path / "sidecar.json")
    assert secret_store.encrypt_to_file(p, {"refresh_token": "env-tok"})
    assert secret_store.decrypt_from_file(p) == {"refresh_token": "env-tok"}
    # PaaS parity: with the env channel active, no key file is created.
    assert not (tmp_path / "key.file").exists()


def test_invalid_env_key_disables_store_never_plaintext(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SECRET_STORE_KEY", "not-a-fernet-key")
    p = str(tmp_path / "sidecar.json")
    assert secret_store.encrypt_to_file(p, {"refresh_token": "x"}) is False
    assert not Path(p).exists()          # nothing written, esp. not plaintext


# --- key handling edge cases ---------------------------------------------------

def test_corrupt_key_file_disables_store_without_regenerating(tmp_path):
    kf = tmp_path / "key.file"
    kf.write_text("garbage-not-a-key")
    p = str(tmp_path / "sidecar.json")
    # Encryption refuses; the corrupt key file is left untouched (a silent
    # regenerate would orphan every existing sidecar under the old key).
    assert secret_store.encrypt_to_file(p, {"refresh_token": "x"}) is False
    assert kf.read_text() == "garbage-not-a-key"
    assert not Path(p).exists()


def test_wrong_key_cannot_decrypt(tmp_path):
    p = str(tmp_path / "sidecar.json")
    assert secret_store.encrypt_to_file(p, {"refresh_token": "tok-A"})
    # Rotate the key file: the old sidecar becomes undecryptable — None,
    # never an exception (fail-soft back to bootstrap channels).
    (tmp_path / "key.file").write_text(_fresh_key())
    assert secret_store.decrypt_from_file(p) is None


def test_tampered_envelope_rejected(tmp_path):
    p = str(tmp_path / "sidecar.json")
    assert secret_store.encrypt_to_file(p, {"refresh_token": "tok-B"})
    envelope = json.loads(Path(p).read_text())
    token = envelope["enc"]
    flipped = token[:-2] + ("A" if token[-2] != "A" else "B")
    Path(p).write_text(json.dumps({"v": 1, "enc": flipped}))
    assert secret_store.decrypt_from_file(p) is None


# --- legacy plaintext migration -------------------------------------------------

def test_load_legacy_plaintext_detects_only_legacy_format(tmp_path):
    legacy = str(tmp_path / "sidecar.json")
    Path(legacy).write_text(json.dumps({"refresh_token": "old-tok"}))
    assert secret_store.load_legacy_plaintext(legacy) == {
        "refresh_token": "old-tok"}
    # Not legacy: encrypted envelope, junk, absent.
    enc = str(tmp_path / "enc.json")
    secret_store.encrypt_to_file(enc, {"refresh_token": "t"})
    assert secret_store.load_legacy_plaintext(enc) is None
    junk = str(tmp_path / "junk.json")
    Path(junk).write_text("corrupt{")
    assert secret_store.load_legacy_plaintext(junk) is None
    assert secret_store.load_legacy_plaintext(str(tmp_path / "nope")) is None


def test_missing_secret_store_dep_degrades_fail_soft(monkeypatch, tmp_path):
    """No cryptography install = store disabled, nothing written plaintext."""
    import sys
    monkeypatch.setitem(sys.modules, "cryptography", None)
    monkeypatch.setitem(sys.modules, "cryptography.fernet", None)
    p = str(tmp_path / "sidecar.json")
    assert secret_store.encrypt_to_file(p, {"refresh_token": "x"}) is False
    assert not Path(p).exists()
    assert secret_store.decrypt_from_file(p) is None
