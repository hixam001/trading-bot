"""
backend/tests/test_audit_77_b.py — Phase B Security Audit Tests (§77 B1–B3).
"""
from __future__ import annotations

import ast
import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import config
from api.main import app
from live_execution import wallet

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = Path(__file__).resolve().parent.parent
API_DIR = BACKEND_DIR / "api"


# ---------------------------------------------------------------------------
# B1: AST and Reference Check — backend/api/ must NEVER reference load_keypair
# ---------------------------------------------------------------------------

def test_api_modules_have_no_load_keypair_references():
    """Verify no file in backend/api/ references or imports load_keypair."""
    violations = []
    for py_file in API_DIR.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "load_keypair":
                violations.append(f"{py_file.relative_to(REPO_ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr == "load_keypair":
                violations.append(f"{py_file.relative_to(REPO_ROOT)}:{node.lineno}")

    assert not violations, f"backend/api/ references load_keypair in: {violations}"


def test_api_routes_do_not_load_keypair(monkeypatch, tmp_path):
    """When load_keypair is poisoned, API routes for holdings/stats/live_book succeed without calling it."""
    # Poison load_keypair
    def _poisoned_load_keypair(*args, **kwargs):
        raise AssertionError("API must not load the key")

    monkeypatch.setattr(wallet, "load_keypair", _poisoned_load_keypair)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "a" * 32)

    from data_providers.mock import MockProvider
    app.state.provider = MockProvider()

    # Provide a public address via public_address() mechanism
    test_pubkey = "11111111111111111111111111111111"
    monkeypatch.setattr(wallet, "public_address", lambda: test_pubkey)

    client = TestClient(app)
    headers = {"X-Admin-Token": "a" * 32}

    # These routes must not trigger load_keypair
    r1 = client.get("/api/holdings", headers=headers)
    assert r1.status_code == 200

    r2 = client.get("/api/stats", headers=headers)
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# B3: Refuse World/Group-Readable Keypair Files
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="POSIX permission checks only")
def test_wallet_refuses_open_file_permissions(tmp_path):
    """wallet._read_file_material refuses keypair files with 0644, accepts 0600 and 0400."""
    from solders.keypair import Keypair
    # Generate throwaway keypair
    kp = Keypair()
    kp_bytes = list(bytes(kp))
    kp_file = tmp_path / "test_keypair.json"
    kp_file.write_text(json.dumps(kp_bytes))

    # 1. 0644 (world and group readable) -> Refused
    kp_file.chmod(0o644)
    with pytest.raises(wallet.WalletError) as exc_info:
        wallet.load_keypair(path=str(kp_file))
    assert "permissions too open" in str(exc_info.value).lower()

    # 2. 0600 (owner rw) -> Accepted
    kp_file.chmod(0o600)
    loaded_600 = wallet.load_keypair(path=str(kp_file))
    assert str(loaded_600.pubkey()) == str(kp.pubkey())

    # 3. 0400 (owner ro) -> Accepted
    kp_file.chmod(0o400)
    loaded_400 = wallet.load_keypair(path=str(kp_file))
    assert str(loaded_400.pubkey()) == str(kp.pubkey())


# ---------------------------------------------------------------------------
# B2: API Operates with Read-Only State Directory (:ro)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="POSIX permission checks only")
def test_api_operates_with_readonly_state_dir(tmp_path, monkeypatch):
    """API endpoints operate successfully when the live_execution/state directory is mounted :ro."""
    from live_execution import config as le_config
    from data_providers.mock import MockProvider
    app.state.provider = MockProvider()

    state_dir = tmp_path / "ro_state"
    state_dir.mkdir()
    # Write pre-existing state files
    (state_dir / "wallet_pubkey.txt").write_text("11111111111111111111111111111111")
    (state_dir / "executions.json").write_text("[]")
    # Make state directory read-only (chmod 0555)
    state_dir.chmod(0o555)

    monkeypatch.setattr(le_config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "a" * 32)
    monkeypatch.setattr(le_config, "LIVE_TRADING_ENABLED", True)

    client = TestClient(app)
    headers = {"X-Admin-Token": "a" * 32}

    try:
        # API endpoints read state without trying to write
        r1 = client.get("/api/live/portfolio", headers=headers)
        assert r1.status_code == 200

        r2 = client.get("/api/holdings", headers=headers)
        assert r2.status_code == 200

        r3 = client.get("/api/stats", headers=headers)
        assert r3.status_code == 200
    finally:
        state_dir.chmod(0o755)

