"""
tests/test_wallet_secrets.py — deployment secret resolution for the wallet.

The engine must run on hosts that cannot mount secret files (PaaS) as well
as on a VM with a bind-mounted keypair. Resolution order (fail-closed):
  1. explicit `path` argument
  2. WALLET_KEYPAIR_PATH  — preferred (mounted secret file, safer channel)
  3. WALLET_KEYPAIR_JSON  — keypair JSON direct from env, in-memory only
  4. neither              — WalletError, nothing signs or sends

Everything here is hermetic: throwaway keypairs generated in-test via
solders, tmp state, no network, no real material.
"""
from __future__ import annotations

import json
import logging

import pytest
from solders.keypair import Keypair

import live_execution.config as le_config
from live_execution import wallet


def _kp_material(kp: Keypair) -> str:
    return json.dumps(list(bytes(kp)))


@pytest.fixture
def clean_wallet_env(monkeypatch):
    """Both secret channels empty unless a test sets one explicitly."""
    monkeypatch.setattr(le_config, "WALLET_KEYPAIR_PATH", "")
    monkeypatch.setattr(le_config, "WALLET_KEYPAIR_JSON", "")


# --- WALLET_KEYPAIR_JSON (the new deployment channel) ------------------------

def test_json_env_loads_keypair(clean_wallet_env):
    kp = Keypair()
    le_config.WALLET_KEYPAIR_JSON = _kp_material(kp)
    loaded = wallet.load_keypair()
    assert wallet.pubkey_string(loaded) == str(kp.pubkey())


def test_path_wins_when_both_channels_set(clean_wallet_env, tmp_path):
    file_kp, env_kp = Keypair(), Keypair()
    p = tmp_path / "kp.json"
    p.write_text(_kp_material(file_kp))
    le_config.WALLET_KEYPAIR_PATH = str(p)
    le_config.WALLET_KEYPAIR_JSON = _kp_material(env_kp)
    loaded = wallet.load_keypair()
    # The mounted secret file is the preferred channel — env JSON is only
    # a fallback for hosts that cannot mount files.
    assert wallet.pubkey_string(loaded) == str(file_kp.pubkey())


def test_json_fallback_when_path_empty(clean_wallet_env):
    env_kp = Keypair()
    le_config.WALLET_KEYPAIR_PATH = ""
    le_config.WALLET_KEYPAIR_JSON = _kp_material(env_kp)
    loaded = wallet.load_keypair()
    assert wallet.pubkey_string(loaded) == str(env_kp.pubkey())


def test_identity_pin_enforced_on_json_channel(clean_wallet_env):
    kp = Keypair()
    le_config.WALLET_KEYPAIR_JSON = _kp_material(kp)
    original = le_config.EXPECTED_WALLET_ADDRESS
    le_config.EXPECTED_WALLET_ADDRESS = str(Keypair().pubkey())  # some OTHER account
    try:
        with pytest.raises(wallet.WalletError, match="not the expected wallet"):
            wallet.verify_expected_address(wallet.load_keypair())
    finally:
        le_config.EXPECTED_WALLET_ADDRESS = original


# --- fail-closed refusals -----------------------------------------------------

def test_neither_channel_refuses(clean_wallet_env):
    with pytest.raises(wallet.WalletError, match="no wallet configured"):
        wallet.load_keypair()


def test_json_invalid_refuses(clean_wallet_env):
    le_config.WALLET_KEYPAIR_JSON = "definitely not json"
    with pytest.raises(wallet.WalletError, match="not valid JSON"):
        wallet.load_keypair()


def test_json_wrong_shape_refuses(clean_wallet_env):
    le_config.WALLET_KEYPAIR_JSON = json.dumps([1, 2, 3])
    with pytest.raises(wallet.WalletError, match="64"):
        wallet.load_keypair()


def test_json_material_never_written_to_disk(clean_wallet_env, tmp_path):
    """The env channel is in-memory only: no file may appear anywhere."""
    kp = Keypair()
    le_config.WALLET_KEYPAIR_JSON = _kp_material(kp)
    wallet.load_keypair()
    # tmp_path (and cwd) must contain no keypair artifact from the load.
    assert list(tmp_path.iterdir()) == []


# --- log redaction ------------------------------------------------------------

def test_keypair_redactor_masks_material():
    kp = Keypair()
    material = _kp_material(kp)
    rec = logging.LogRecord(
        "test", logging.INFO, __file__, 1, f"leaked {material}", None, None
    )
    assert wallet._KeypairRedactor().filter(rec) is True
    assert material not in rec.getMessage()
    assert "REDACTED" in rec.getMessage()


def test_redactor_installed_and_active_on_root_logger(caplog):
    kp = Keypair()
    material = _kp_material(kp)
    with caplog.at_level(logging.INFO):
        logging.getLogger("").info("oops %s", material)
    assert material not in caplog.text
    assert "REDACTED" in caplog.text


def test_redactor_ignores_short_arrays():
    rec = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "blocks [1, 2, 3] tripped", None, None
    )
    assert wallet._KeypairRedactor().filter(rec) is True
    assert rec.getMessage() == "blocks [1, 2, 3] tripped"